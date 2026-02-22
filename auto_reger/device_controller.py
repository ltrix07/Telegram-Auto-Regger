from __future__ import annotations

import logging
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

from .utils import PROJECT_ROOT

LOGGER = logging.getLogger(__name__)


class DeviceController:
    """
    Headless Android controller built around plain ADB commands.

    The class intentionally avoids Windows GUI automation and can run on servers.
    It provides a minimal Telegram registration toolkit:
      * preparing and launching the app,
      * global proxy setup,
      * best-effort text input/taps,
      * UI XML dump parsing to extract Telegram login codes.
    """

    TELEGRAM_PACKAGE = "org.telegram.messenger"
    UI_DUMP_PATH = "/sdcard/window_dump.xml"

    def __init__(self, device_id: str, adb_path: str = "adb") -> None:
        """
        :param device_id: ADB serial, e.g. ``emulator-5554`` or ``127.0.0.1:5555``.
        :param adb_path: ADB binary path. Defaults to ``adb`` from PATH.
        """
        normalized = str(device_id).strip()
        if not normalized:
            raise ValueError("Device serial is required.")

        self.device_id = normalized
        self.adb_path = adb_path
        self.debug_dir = PROJECT_ROOT / "debug"

    def _adb(self, *args: str, check: bool = True, timeout: int = 30) -> subprocess.CompletedProcess[str]:
        """
        Execute an ADB command for the target device.

        :param args: ADB command arguments after ``adb -s <device_id>``.
        :param check: Raise RuntimeError on non-zero exit code if True.
        :param timeout: Command timeout in seconds.
        :return: CompletedProcess with stdout/stderr.
        """
        cmd = [self.adb_path, "-s", self.device_id, *args]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

        if check and result.returncode != 0:
            raise RuntimeError(
                "ADB command failed: {} | stdout={!r} stderr={!r}".format(
                    " ".join(cmd),
                    (result.stdout or "").strip(),
                    (result.stderr or "").strip(),
                )
            )
        return result

    def connect(self) -> None:
        """
        Connect to network ADB device if ``device_id`` is in ``host:port`` format.

        USB serials are left untouched.
        """
        if ":" not in self.device_id:
            return

        LOGGER.info("Connecting to ADB device %s", self.device_id)
        result = subprocess.run(
            [self.adb_path, "connect", self.device_id],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "Failed to connect ADB device {}: {}".format(
                    self.device_id,
                    (result.stderr or result.stdout or "").strip(),
                )
            )

    def is_ready(self) -> bool:
        """
        Verify core readiness for headless execution:
          - ADB device state is `device`
          - screen responds to `wm size`
          - internet is available
          - Telegram package exists
        """
        try:
            state = self._adb("get-state", timeout=10).stdout.strip().lower()
            if state != "device":
                LOGGER.error("Device %s is not in `device` state: %s", self.device_id, state)
                return False

            wm_size = self._adb("shell", "wm", "size", timeout=10).stdout
            if not re.search(r"\d+x\d+", wm_size):
                LOGGER.error("Device %s screen is not responsive (wm size=%r)", self.device_id, wm_size)
                return False

            if not self._is_telegram_installed():
                LOGGER.error("Telegram package is not installed on %s", self.device_id)
                return False

            if not self._has_internet():
                LOGGER.error("Internet connectivity check failed on %s", self.device_id)
                return False

            return True
        except Exception:
            LOGGER.exception("Readiness check failed for device %s", self.device_id)
            return False

    def take_screenshot(self, name: str) -> Path:
        """
        Save current screen and UI XML snapshot into `debug/`.
        """
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name or "error")).strip("_") or "error"
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        device_key = self.device_id.replace(":", "_")
        stem = f"{safe_name}_{device_key}_{timestamp}"

        self.debug_dir.mkdir(parents=True, exist_ok=True)
        local_png = self.debug_dir / f"{stem}.png"
        local_xml = self.debug_dir / f"{stem}.xml"
        remote_png = f"/sdcard/{stem}.png"

        self._adb("shell", "screencap", "-p", remote_png, check=False, timeout=20)
        pull_result = self._adb("pull", remote_png, str(local_png), check=False, timeout=30)
        self._adb("shell", "rm", "-f", remote_png, check=False, timeout=10)

        try:
            local_xml.write_text(self._dump_ui_xml(), encoding="utf-8")
        except Exception:
            LOGGER.exception("Failed to dump UI XML during screenshot capture for %s", self.device_id)

        if pull_result.returncode != 0 or not local_png.exists():
            raise RuntimeError(
                "Failed to pull screenshot from device {}: {}".format(
                    self.device_id,
                    (pull_result.stderr or pull_result.stdout or "").strip(),
                )
            )

        return local_png

    def set_proxy(self, proxy_string: str) -> None:
        """
        Configure Android global proxy from ``type:ip:port:user:pass`` string.

        Android global proxy does not support authenticated SOCKS/HTTP credentials
        in a universal way. This method applies host:port and logs a warning when
        credentials are provided.
        """
        raw = str(proxy_string or "").strip()
        target_proxy = ":0"

        if raw:
            parts = raw.split(":")
            if len(parts) < 3:
                raise ValueError(
                    "Invalid proxy format. Expected type:ip:port:user:pass or type:ip:port"
                )

            proxy_type = parts[0].lower().strip()
            host = parts[1].strip()
            port = parts[2].strip()
            user = parts[3].strip() if len(parts) > 3 else ""
            password = parts[4].strip() if len(parts) > 4 else ""

            if proxy_type not in {"http", "https", "socks4", "socks5"}:
                raise ValueError(f"Unsupported proxy type: {proxy_type}")
            if not host or not port.isdigit():
                raise ValueError("Proxy host/port are invalid")

            target_proxy = f"{host}:{port}"
            if user or password:
                LOGGER.warning(
                    "Proxy credentials provided, but Android global proxy auth is not universally supported. "
                    "Configure per-app proxying/VPN if required."
                )
            LOGGER.info(
                "Setting Android global proxy on %s: type=%s host=%s port=%s",
                self.device_id,
                proxy_type,
                host,
                port,
            )
        else:
            LOGGER.info("Clearing Android global proxy on %s", self.device_id)

        for attempt in range(1, 4):
            self._adb("shell", "settings", "put", "global", "http_proxy", target_proxy, check=False)
            current_value = (
                self._adb("shell", "settings", "get", "global", "http_proxy", check=False)
                .stdout.strip()
            )
            if current_value == target_proxy or (target_proxy != ":0" and target_proxy in current_value):
                return

            LOGGER.warning(
                "Proxy apply verification failed on %s (attempt %s/3): expected=%s got=%s",
                self.device_id,
                attempt,
                target_proxy,
                current_value,
            )
            time.sleep(0.5)

        raise RuntimeError(
            f"Failed to set proxy on {self.device_id}. Expected={target_proxy!r}, got={current_value!r}"
        )

    def prepare_device(self) -> None:
        """
        Stop Telegram and clear its app data.
        """
        LOGGER.info("Preparing device %s: clearing Telegram app data", self.device_id)
        self._adb("shell", "am", "force-stop", self.TELEGRAM_PACKAGE, check=False)
        self._adb("shell", "pm", "clear", self.TELEGRAM_PACKAGE)

    def cleanup_telegram(self, package_name: str = TELEGRAM_PACKAGE) -> None:
        """
        Backward-compatible alias for app cleanup.
        """
        _ = package_name
        self.prepare_device()

    def launch_telegram(self) -> None:
        """
        Launch official Telegram app via launcher intent.
        """
        LOGGER.info("Launching Telegram on %s", self.device_id)
        self._adb(
            "shell",
            "monkey",
            "-p",
            self.TELEGRAM_PACKAGE,
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
        )
        time.sleep(2.0)

    def open_telegram(self, package_name: str = TELEGRAM_PACKAGE) -> None:
        """
        Backward-compatible alias for app launch.
        """
        _ = package_name
        self.launch_telegram()

    def get_device_info(self) -> Dict[str, str]:
        """
        Read Android model/version for Telethon device spoofing.

        :return: Dict with keys: ``device_model`` and ``system_version``.
        """
        model = self._adb("shell", "getprop", "ro.product.model").stdout.strip() or "Unknown Android"
        version = (
            self._adb("shell", "getprop", "ro.build.version.release").stdout.strip()
            or "Unknown"
        )
        info = {
            "device_model": model,
            "system_version": f"Android {version}",
        }
        LOGGER.info("Device info detected: %s", info)
        return info

    def input_phone(self, phone_number: str, country_code: Optional[str] = None) -> None:
        """
        Best-effort fill Telegram phone form and continue.

        :param phone_number: Phone number in any format (digits/+ accepted).
        :param country_code: Optional country code like ``+1``.
        """
        LOGGER.info("Inputting phone number on Telegram UI")
        self._tap_by_text_candidates(("Start Messaging", "НАЧАТЬ ОБЩЕНИЕ", "Start"))

        if country_code:
            cc_digits = re.sub(r"\D", "", country_code)
            if cc_digits:
                if self._tap_by_resource_id("org.telegram.messenger:id/login_phone_code_text"):
                    self._input_text(cc_digits)
                    time.sleep(0.3)

        phone_digits = re.sub(r"\D", "", phone_number)
        if self._tap_by_resource_id("org.telegram.messenger:id/login_phone_number_text"):
            self._input_text(phone_digits)
        else:
            # TODO: calibrate coordinates for your Telegram build if no resource-id found.
            self._tap_percent(0.5, 0.42)
            self._input_text(phone_digits)

        if not self._tap_by_resource_id("org.telegram.messenger:id/login_btn"):
            self._tap_by_text_candidates(("Done", "Next", "Продолжить", "Далее"))
            self._adb("shell", "input", "keyevent", "66", check=False)
        time.sleep(1.5)

    def input_code(self, code: str) -> None:
        """
        Input verification SMS code in Telegram.
        """
        LOGGER.info("Inputting SMS code on Telegram UI")
        if not self._tap_by_resource_id("org.telegram.messenger:id/login_code_text"):
            self._tap_percent(0.5, 0.36)
        self._input_text(str(code))
        self._adb("shell", "input", "keyevent", "66", check=False)
        time.sleep(1.0)

    def input_email(self, email: str) -> None:
        """
        Input email on Telegram's email verification step (if requested).

        Field identifiers vary across Telegram versions; this method uses
        best-effort matching with a fallback tap.
        """
        LOGGER.info("Inputting email on Telegram UI")
        known_ids = (
            "org.telegram.messenger:id/email",
            "org.telegram.messenger:id/login_email_field",
            "org.telegram.messenger:id/code_field",
        )

        tapped = any(self._tap_by_resource_id(rid) for rid in known_ids)
        if not tapped:
            if not self._tap_by_text_candidates(("Email", "Почта", "@")):
                # TODO: calibrate tap coordinates for email field if needed.
                self._tap_percent(0.5, 0.42)
        self._input_text(email)
        self._adb("shell", "input", "keyevent", "66", check=False)
        time.sleep(1.0)

    def fill_profile(self, first_name: str, last_name: str) -> None:
        """
        Fill first/last name step in Telegram profile setup.
        """
        LOGGER.info("Filling Telegram profile name fields")
        if self._tap_by_resource_id("org.telegram.messenger:id/first_name_field"):
            self._input_text(first_name)
        else:
            # TODO: calibrate first-name field coordinates for your UI build.
            self._tap_percent(0.5, 0.32)
            self._input_text(first_name)

        if self._tap_by_resource_id("org.telegram.messenger:id/last_name_field"):
            self._input_text(last_name)
        else:
            # TODO: calibrate last-name field coordinates for your UI build.
            self._tap_percent(0.5, 0.40)
            self._input_text(last_name)

        if not self._tap_by_resource_id("org.telegram.messenger:id/login_btn"):
            self._tap_by_text_candidates(("Done", "Next", "Continue", "Готово"))
        time.sleep(1.0)

    def open_telegram_system_chat(self) -> None:
        """
        Open Telegram's official system chat ("Telegram") using ADB input actions.

        This is used after Telethon triggers a login code message.
        """
        LOGGER.info("Opening Telegram system chat to read Telethon login code")
        self.launch_telegram()

        # Return to chats list.
        for _ in range(3):
            self._adb("shell", "input", "keyevent", "4", check=False)
            time.sleep(0.3)

        if self._tap_by_text_candidates(("Telegram", "Телеграм")):
            time.sleep(0.8)
            return

        # TODO: calibrate coordinates for the topmost system chat on your devices.
        self._tap_percent(0.5, 0.18)
        time.sleep(0.8)

    def read_telegram_code_from_screen(self, timeout: int = 45, poll_interval: float = 2.0) -> str:
        """
        Parse current Telegram UI hierarchy and extract a 5-digit login code.

        :param timeout: Max time to wait for code on screen.
        :param poll_interval: Delay between XML dump polls.
        :return: Extracted 5-digit code.
        :raises TimeoutError: If code was not found in time.
        """
        LOGGER.info("Reading Telegram login code from UI dump")
        deadline = time.time() + timeout
        patterns = (
            re.compile(r"\b(\d{5})\b"),
            re.compile(r"(?i)(?:code|код)[^\d]{0,20}(\d{5,6})"),
        )

        while time.time() < deadline:
            xml_text = self._dump_ui_xml()
            texts = self._extract_text_candidates(xml_text)
            for text in texts:
                for pattern in patterns:
                    match = pattern.search(text)
                    if match:
                        code = match.group(1)
                        LOGGER.info("Telegram system code detected: %s", code)
                        return code
            time.sleep(poll_interval)

        raise TimeoutError("Failed to extract Telegram system code from screen within timeout.")

    def screen_contains_any(self, patterns: Iterable[str]) -> bool:
        """
        Check whether any substring appears in current UI dump text nodes.
        """
        xml_text = self._dump_ui_xml()
        text_candidates = " | ".join(self._extract_text_candidates(xml_text)).lower()
        return any(str(item).lower() in text_candidates for item in patterns)

    def _is_telegram_installed(self) -> bool:
        result = self._adb("shell", "pm", "path", self.TELEGRAM_PACKAGE, check=False, timeout=15)
        output = (result.stdout or "").strip()
        return result.returncode == 0 and "package:" in output

    def _has_internet(self) -> bool:
        ping_result = self._adb(
            "shell",
            "ping",
            "-c",
            "1",
            "-W",
            "2",
            "1.1.1.1",
            check=False,
            timeout=15,
        )
        if ping_result.returncode == 0:
            return True

        connectivity_dump = self._adb(
            "shell",
            "dumpsys",
            "connectivity",
            check=False,
            timeout=20,
        ).stdout.lower()
        return "validated=true" in connectivity_dump or "connected" in connectivity_dump

    def _dump_ui_xml(self) -> str:
        self._adb("shell", "uiautomator", "dump", self.UI_DUMP_PATH, check=False)
        xml_text = self._adb("shell", "cat", self.UI_DUMP_PATH).stdout
        if not xml_text.strip():
            raise RuntimeError("uiautomator dump returned empty XML.")
        return xml_text

    @staticmethod
    def _parse_bounds(bounds: str) -> Optional[Tuple[int, int]]:
        match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds or "")
        if not match:
            return None
        left, top, right, bottom = map(int, match.groups())
        center_x = (left + right) // 2
        center_y = (top + bottom) // 2
        return center_x, center_y

    def _tap_by_text_candidates(self, candidates: Iterable[str]) -> bool:
        xml_text = self._dump_ui_xml()
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            LOGGER.exception("Unable to parse UI dump XML while tapping by text")
            return False

        normalized = [c.strip().lower() for c in candidates if str(c).strip()]
        for node in root.iter("node"):
            node_text = str(node.attrib.get("text", "")).strip()
            desc_text = str(node.attrib.get("content-desc", "")).strip()
            haystack = f"{node_text} {desc_text}".lower()
            if not haystack:
                continue
            if not any(cand in haystack for cand in normalized):
                continue

            center = self._parse_bounds(str(node.attrib.get("bounds", "")))
            if center:
                self._tap(*center)
                return True
        return False

    def _tap_by_resource_id(self, resource_id: str) -> bool:
        xml_text = self._dump_ui_xml()
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            LOGGER.exception("Unable to parse UI dump XML while tapping by resource-id")
            return False

        for node in root.iter("node"):
            if node.attrib.get("resource-id") != resource_id:
                continue
            center = self._parse_bounds(str(node.attrib.get("bounds", "")))
            if center:
                self._tap(*center)
                return True
        return False

    def _extract_text_candidates(self, xml_text: str) -> list[str]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            LOGGER.exception("Unable to parse UI dump XML while extracting text candidates")
            return []

        values: list[str] = []
        for node in root.iter("node"):
            text = str(node.attrib.get("text", "")).strip()
            content = str(node.attrib.get("content-desc", "")).strip()
            if text:
                values.append(text)
            if content:
                values.append(content)
        return values

    def _tap(self, x: int, y: int) -> None:
        self._adb("shell", "input", "tap", str(x), str(y))

    def _tap_percent(self, x_percent: float, y_percent: float) -> None:
        wm_size = self._adb("shell", "wm", "size").stdout
        match = re.search(r"(\d+)x(\d+)", wm_size)
        if not match:
            raise RuntimeError(f"Unable to parse screen size from `wm size`: {wm_size!r}")
        width, height = map(int, match.groups())
        x = int(width * x_percent)
        y = int(height * y_percent)
        self._tap(x, y)

    def _input_text(self, value: str) -> None:
        text = str(value)
        if not text:
            return
        safe = self._escape_adb_text(text)
        self._adb("shell", "input", "text", safe)

    @staticmethod
    def _escape_adb_text(text: str) -> str:
        escaped = text.replace("\\", "\\\\").replace(" ", "%s")
        specials = '()<>|;&*\'"!?$#[]{}'
        for char in specials:
            escaped = escaped.replace(char, f"\\{char}")
        return escaped

    def dump_screen(self) -> str:
        """
        Backward-compatible helper that returns current UI XML dump.
        """
        return self._dump_ui_xml()

