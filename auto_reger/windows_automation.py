import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

import psutil
from DrissionPage import ChromiumOptions, ChromiumPage
from pywinauto import Application, findwindows

from .utils import LOG_FILE, get_config_value, resolve_project_path

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(funcName)s() - %(message)s",
    encoding="utf-8",
)


WINDOW_WAIT_SECONDS = float(get_config_value(["timeouts", "window_wait_seconds"], 20))
POLL_INTERVAL_SECONDS = float(get_config_value(["timeouts", "poll_interval_seconds"], 1.0))
VPN_SETTLE_SECONDS = float(get_config_value(["timeouts", "vpn_settle_seconds"], 5.0))
KEY_DELAY_SECONDS = float(get_config_value(["timeouts", "desktop_key_delay_seconds"], 0.2))


class WindowsAutomationError(RuntimeError):
    """Base exception for Windows automation failures."""


class AppLaunchError(WindowsAutomationError):
    """Raised when a desktop application cannot be started or attached."""


class VPNConnectionError(WindowsAutomationError):
    """Raised when VPN connection action fails."""


class App:
    def __init__(self) -> None:
        self.handle_title: Optional[str] = None
        self.app_name: Optional[str] = None
        self.app: Optional[Application] = None

    def _wait_for_handle(self, timeout_seconds: float = WINDOW_WAIT_SECONDS) -> Optional[int]:
        if not self.handle_title:
            return None

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            handles = findwindows.find_windows(title_re=self.handle_title)
            if handles:
                return handles[0]
            time.sleep(POLL_INTERVAL_SECONDS)
        return None

    def start_app(
        self,
        app_name: str,
        app_path: Optional[str] = None,
        backend: str = "uia",
    ) -> Application:
        app = Application(backend=backend)

        if app_name == "vpn":
            self.handle_title = str(
                get_config_value(["windows_automation", "vpn_window_title_regex"], "ExpressVPN.*")
            )
            if app_path is None:
                raw_path = str(get_config_value(["paths", "express_vpn_executable"], "")).strip()
                if not raw_path:
                    raise AppLaunchError("`paths.express_vpn_executable` is not set in config.yaml")
                app_path = str(resolve_project_path(raw_path))
        elif app_name == "telegram":
            self.handle_title = str(get_config_value(["windows_automation", "telegram_window_title"], "Telegram"))
            if not app_path:
                raise AppLaunchError("Telegram Desktop executable path is required")
        else:
            raise AppLaunchError(f"Unknown app type: {app_name}")

        app_path = os.path.expandvars(os.path.expanduser(app_path))
        self.app_name = Path(app_path).name

        handle = self._wait_for_handle(timeout_seconds=POLL_INTERVAL_SECONDS)
        if handle:
            logging.info("Attaching to existing window `%s`", self.handle_title)
            app.connect(handle=handle)
            self.app = app
            return app

        if not os.path.exists(app_path):
            raise AppLaunchError(f"Executable not found: {app_path}")

        logging.info("Starting application: %s", app_path)
        app.start(app_path)

        handle = self._wait_for_handle(timeout_seconds=WINDOW_WAIT_SECONDS)
        if not handle:
            raise AppLaunchError(f"Window not found after launch: {self.handle_title}")

        app.connect(handle=handle)
        self.app = app
        return app

    def close(self) -> None:
        if not self.app_name:
            return

        for proc in psutil.process_iter(["name", "pid"]):
            try:
                proc_name = (proc.info.get("name") or "").lower()
                if proc_name == self.app_name.lower():
                    proc.kill()
                    logging.info("Killed process `%s` (pid=%s)", self.app_name, proc.info.get("pid"))
            except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
                logging.warning("Failed to kill `%s`: %s", self.app_name, exc)


class Onion:
    def __init__(self) -> None:
        co = ChromiumOptions()
        chrome_path_cfg = str(get_config_value(["paths", "chrome_executable"], "")).strip()
        if chrome_path_cfg:
            chrome_path = str(resolve_project_path(chrome_path_cfg))
            if os.path.exists(chrome_path):
                co.set_browser_path(chrome_path)
            else:
                logging.warning("Configured Chrome path does not exist: %s", chrome_path)

        co.set_argument("--no-first-run")
        co.set_argument("--force-color-profile=srgb")
        co.set_argument("--password-store=basic")
        co.set_argument("--no-default-browser-check")

        self.page = ChromiumPage(addr_or_opts=co)

    def reg_and_login(self, username: str, password: str, domain: Optional[str] = None) -> Optional[str]:
        try:
            logging.info("Navigating to Onion Mail registration")
            self.page.get("https://onionmail.org/create")

            field_timeout = int(get_config_value(["timeouts", "element_search_seconds"], 10))
            username_el = self.page.ele("@name=username", timeout=field_timeout)
            password_el = self.page.ele("@name=password", timeout=field_timeout)
            confirm_el = self.page.ele("@name=confirm", timeout=field_timeout)

            if not (username_el and password_el and confirm_el):
                logging.error("Onion Mail registration form did not load in time")
                return None

            username_el.input(username)
            password_el.input(password)
            confirm_el.input(password)

            create_btn = self.page.ele("text:Create", timeout=field_timeout)
            if create_btn:
                create_btn.click()

            continue_btn = self.page.ele("text:Continue", timeout=5)
            if continue_btn:
                continue_btn.click()

            inbox_timeout = int(get_config_value(["timeouts", "page_load_seconds"], 30))
            if self.page.wait.ele("text:Logout", timeout=inbox_timeout):
                logging.info("Successfully registered and logged in to Onion Mail")
                mail_domain = domain or "onionmail.org"
                return f"{username}@{mail_domain}"

            logging.error("Onion Mail registration failed: inbox not detected")
            return None
        except Exception:
            logging.exception("Error during Onion Mail registration")
            return None

    def extract_code(
        self,
        service: str = "telegram",
        timeout_minutes: int = 5,
        second_req: bool = False,
    ) -> Optional[str]:
        del second_req
        logging.info("Checking inbox for `%s` code", service)

        if not self.page.url.endswith("/inbox"):
            self.page.get("https://onionmail.org/inbox")

        deadline = time.monotonic() + timeout_minutes * 60
        while time.monotonic() < deadline:
            refresh_btn = self.page.ele("text:Check mail", timeout=3)
            if refresh_btn:
                refresh_btn.click()
            else:
                self.page.refresh()

            page_text = self.page.html
            if service == "telegram":
                match = re.search(r"Login code:\s*(\d{5})", page_text)
                if match:
                    code = match.group(1)
                    logging.info("Found `%s` code", service)
                    return code

            time.sleep(POLL_INTERVAL_SECONDS)

        logging.warning("Code not found in inbox for `%s` within %s minutes", service, timeout_minutes)
        return None

    def close(self) -> None:
        try:
            self.page.quit()
        except Exception:
            logging.exception("Failed to close browser page")


class VPN(App):
    def __init__(self) -> None:
        super().__init__()
        self.start_app("vpn")

    def _vpn_window(self):
        if self.app is None:
            raise VPNConnectionError("VPN app is not initialised")

        window = self.app.window(title_re=self.handle_title or "ExpressVPN.*")
        window.wait("exists enabled visible ready", timeout=WINDOW_WAIT_SECONDS)
        return window

    def connect(self) -> None:
        try:
            logging.info("Connecting VPN")
            win = self._vpn_window()
            win.set_focus()
            win.type_keys("{ENTER}")
            time.sleep(VPN_SETTLE_SECONDS)
        except Exception as exc:
            logging.exception("Failed to connect VPN")
            raise VPNConnectionError("Failed to connect VPN") from exc

    def disconnect(self) -> None:
        try:
            logging.info("Disconnecting VPN")
            win = self._vpn_window()
            win.set_focus()
            win.type_keys("{ENTER}")
            time.sleep(POLL_INTERVAL_SECONDS)
        except Exception:
            logging.exception("Failed to disconnect VPN")


class TelegramDesktop(App):
    def __init__(self, app_path: str) -> None:
        super().__init__()
        self.start_app("telegram", app_path=app_path)

    def start_and_enter_number(self, phone: str) -> None:
        if self.app is None:
            raise AppLaunchError("Telegram Desktop app is not initialised")

        title = str(get_config_value(["windows_automation", "telegram_window_title"], "Telegram"))
        win = self.app.window(title_re=title)
        win.wait("exists enabled visible ready", timeout=WINDOW_WAIT_SECONDS)
        win.set_focus()

        win.type_keys("{ENTER}")
        time.sleep(KEY_DELAY_SECONDS)
        win.type_keys(phone, with_spaces=True, pause=0.05)
        win.type_keys("{ENTER}")

    def enter_code(self, code: str) -> None:
        if self.app is None:
            raise AppLaunchError("Telegram Desktop app is not initialised")

        title = str(get_config_value(["windows_automation", "telegram_window_title"], "Telegram"))
        win = self.app.window(title_re=title)
        win.wait("exists enabled visible ready", timeout=WINDOW_WAIT_SECONDS)
        win.set_focus()
        win.type_keys(code, with_spaces=True, pause=0.05)
