from __future__ import annotations

import logging
import re
import shlex
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple
from urllib.parse import quote

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
    TELEGRAM_PACKAGE_CANDIDATES = (
        "org.telegram.messenger.web",
        "org.telegram.messenger",
        "org.telegram.messenger.beta",
    )
    UI_DUMP_PATH = "/sdcard/window_dump.xml"
    PROXY_ENABLE_TEXT_CANDIDATES = (
        "enable proxy",
        "enable",
        "turn on proxy",
        "\u0432\u043a\u043b\u044e\u0447\u0438\u0442\u044c \u043f\u0440\u043e\u043a\u0441\u0438",
        "\u0432\u043a\u043b\u044e\u0447\u0438\u0442\u044c",
        "\u0432\u043a\u043b",
    )
    PROXY_ENABLE_RESOURCE_ID_SUFFIXES = (
        "button1",
        "button_positive",
        "positive_button",
        "login_btn",
    )

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
        self.telegram_package = self.TELEGRAM_PACKAGE
        self.debug_dir = PROJECT_ROOT / "debug"
        self._root_mode: Optional[str] = None
        self._root_checked = False

    def _run_adb(self, *args: str, check: bool = True, timeout: int = 30) -> subprocess.CompletedProcess[str]:
        """
        Execute raw ADB command for target device without shell/root rewriting.

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

    def _ensure_root_access(self) -> None:
        """
        Ensure root shell is available and cache execution mode.

        Preferred mode is ``adb root`` (adbd as root). If unavailable, falls
        back to ``su -c`` shell commands.
        """
        if self._root_checked:
            if not self._root_mode:
                raise RuntimeError(f"Root access is unavailable on {self.device_id}")
            return

        self._root_checked = True
        self._root_mode = None

        self._run_adb("root", check=False, timeout=20)
        time.sleep(0.8)

        adbd_probe = self._run_adb("shell", "id", "-u", check=False, timeout=10)
        if adbd_probe.returncode == 0 and adbd_probe.stdout.strip() == "0":
            self._root_mode = "adbd"
            LOGGER.info("Root mode for %s: adbd", self.device_id)
            return

        su_probe = self._run_adb("shell", "su", "-c", "id -u", check=False, timeout=10)
        if su_probe.returncode == 0 and su_probe.stdout.strip() == "0":
            self._root_mode = "su"
            LOGGER.info("Root mode for %s: su", self.device_id)
            return

        raise RuntimeError(
            "Root access is required but unavailable on {} (adbd stdout={!r}, su stdout={!r})".format(
                self.device_id,
                (adbd_probe.stdout or "").strip(),
                (su_probe.stdout or "").strip(),
            )
        )

    def _adb(self, *args: str, check: bool = True, timeout: int = 30) -> subprocess.CompletedProcess[str]:
        """
        Execute ADB command with automatic root wrapping for ``shell`` actions.
        """
        if args and args[0] == "shell":
            self._ensure_root_access()
            shell_args = [str(part) for part in args[1:]]
            if not shell_args:
                return self._run_adb(*args, check=check, timeout=timeout)

            if self._root_mode == "adbd":
                return self._run_adb("shell", *shell_args, check=check, timeout=timeout)

            root_command = shlex.join(shell_args)
            return self._run_adb("shell", "su", "-c", root_command, check=check, timeout=timeout)

        return self._run_adb(*args, check=check, timeout=timeout)

    def connect(self) -> None:
        """
        Connect to network ADB device if ``device_id`` is in ``host:port`` format.

        USB serials are left untouched.
        """
        if ":" in self.device_id:
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
        self._ensure_root_access()

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

    def take_screenshot(self, save_path: str) -> bool:
        """
        Save a PNG screenshot to host filesystem using fast ``exec-out`` stream.

        Command pattern:
          adb -s <device_udid> exec-out screencap -p > <save_path>
        """
        raw_path = str(save_path or "").strip()
        if not raw_path:
            LOGGER.error("Screenshot save path is empty for device %s", self.device_id)
            return False
        target_path = Path(raw_path)

        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            cmd = [self.adb_path, "-s", self.device_id, "exec-out", "screencap", "-p"]
            with target_path.open("wb") as output:
                result = subprocess.run(
                    cmd,
                    stdout=output,
                    stderr=subprocess.PIPE,
                    timeout=20,
                    check=False,
                )

            if result.returncode != 0:
                LOGGER.error(
                    "Failed to capture screenshot via exec-out on %s: %s",
                    self.device_id,
                    (result.stderr or b"").decode("utf-8", errors="ignore").strip(),
                )
                target_path.unlink(missing_ok=True)
                return False

            if not target_path.exists() or target_path.stat().st_size == 0:
                LOGGER.error("Captured screenshot is empty for %s", self.device_id)
                target_path.unlink(missing_ok=True)
                return False

            return True
        except Exception:
            LOGGER.exception("Failed to capture screenshot for %s", self.device_id)
            try:
                target_path.unlink(missing_ok=True)
            except Exception:
                LOGGER.exception("Failed to cleanup invalid screenshot file: %s", target_path)
            return False

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

    def set_telegram_proxy_via_intent(
        self,
        ip: str,
        port: str,
        user: str = "",
        password: str = "",
    ) -> bool:
        """
        Open Telegram SOCKS proxy deep-link and trigger system proxy-enable popup.

        Command pattern:
          adb -s <device_udid> shell am start -W -a android.intent.action.VIEW \
            -d "tg://socks?server=<ip>&port=<port>" <detected_telegram_package>
        """
        host = str(ip or "").strip()
        port_raw = str(port or "").strip()
        if not host:
            LOGGER.error("Telegram proxy intent skipped: empty host for %s", self.device_id)
            return False
        if not port_raw.isdigit():
            LOGGER.error("Telegram proxy intent skipped: invalid port %r for %s", port_raw, self.device_id)
            return False

        port_value = int(port_raw)
        if not 1 <= port_value <= 65535:
            LOGGER.error("Telegram proxy intent skipped: out-of-range port %s for %s", port_value, self.device_id)
            return False

        deep_link = f"tg://socks?server={host}&port={port_value}"
        username = str(user or "").strip()
        user_password = str(password or "").strip()

        if username and user_password:
            deep_link += f"&user={quote(username, safe='')}&pass={quote(user_password, safe='')}"

        safe_deep_link = f"'{deep_link}'" 

        result = self._run_adb([
            "shell", "am", "start", "-W", 
            "-a", "android.intent.action.VIEW", 
            "-d", safe_deep_link, 
            self.telegram_package 
        ])
        output = f"{result.stdout}\n{result.stderr}".lower()
        if result.returncode != 0 or "error:" in output or "exception" in output:
            LOGGER.error(
                "Failed to open Telegram proxy intent on %s for %s:%s | stdout=%r stderr=%r",
                self.device_id,
                host,
                port_value,
                (result.stdout or "").strip(),
                (result.stderr or "").strip(),
            )
            return False

        LOGGER.info(
            "Telegram proxy intent opened on %s for %s:%s (auth=%s)",
            self.device_id,
            host,
            port_value,
            "yes" if username and user_password else "no",
        )
        return True

    def enable_telegram_proxy_popup(self, timeout: float = 7.0, poll_interval: float = 0.4) -> str:
        """
        Wait for Telegram proxy confirmation popup and tap its positive action.

        :return: Selector description used to tap the button.
        :raises TimeoutError: If confirmation button was not found in time.
        """
        normalized_candidates = [
            candidate.strip().lower()
            for candidate in self.PROXY_ENABLE_TEXT_CANDIDATES
            if str(candidate).strip()
        ]
        proxy_enable_resource_ids = self._proxy_enable_resource_ids()
        deadline = time.time() + max(timeout, 0.5)
        while time.time() < deadline:
            xml_text = self._dump_ui_xml()
            try:
                root = ET.fromstring(xml_text)
            except ET.ParseError:
                LOGGER.debug("Proxy popup XML parse failed on %s", self.device_id, exc_info=True)
                time.sleep(max(poll_interval, 0.1))
                continue

            # Prefer stable resource-id selectors when available.
            for resource_id in proxy_enable_resource_ids:
                for node in root.iter("node"):
                    if node.attrib.get("resource-id") != resource_id:
                        continue
                    center = self._parse_bounds(str(node.attrib.get("bounds", "")))
                    if not center:
                        continue
                    self._tap(*center)
                    selector = f"resourceId={resource_id}"
                    LOGGER.info("Tapped Telegram proxy enable button by %s", selector)
                    return selector

            # Fallback to locale-aware text matching.
            for node in root.iter("node"):
                node_text = str(node.attrib.get("text", "")).strip()
                node_desc = str(node.attrib.get("content-desc", "")).strip()
                haystack = f"{node_text} {node_desc}".lower()
                if not haystack:
                    continue

                matched_candidate = next(
                    (cand for cand in normalized_candidates if cand and cand in haystack),
                    "",
                )
                if not matched_candidate:
                    continue

                center = self._parse_bounds(str(node.attrib.get("bounds", "")))
                if not center:
                    continue
                self._tap(*center)
                selector = (
                    f"text~{matched_candidate!r} "
                    f"(node_text={node_text!r}, content_desc={node_desc!r})"
                )
                LOGGER.info("Tapped Telegram proxy enable button by %s", selector)
                return selector

            # Compatibility fallback to existing generic helpers.
            for resource_id in proxy_enable_resource_ids:
                try:
                    if self._tap_by_resource_id(resource_id):
                        selector = f"resourceId={resource_id}"
                        LOGGER.info("Tapped Telegram proxy enable button by %s", selector)
                        return selector
                except Exception:
                    LOGGER.debug(
                        "Proxy popup check by id failed on %s: %s",
                        self.device_id,
                        resource_id,
                        exc_info=True,
                    )

            try:
                if self._tap_by_text_candidates(self.PROXY_ENABLE_TEXT_CANDIDATES):
                    selector = "text-candidates:fallback"
                    LOGGER.info("Tapped Telegram proxy enable button by %s", selector)
                    return selector
            except Exception:
                LOGGER.debug("Proxy popup text scan failed on %s", self.device_id, exc_info=True)

            time.sleep(max(poll_interval, 0.1))

        raise TimeoutError(
            f"Telegram proxy enable popup did not appear on {self.device_id} within {timeout:.1f}s"
        )

    def prepare_device(self) -> None:
        """
        Stop Telegram and clear its app data.
        """
        LOGGER.info("Preparing device %s: clearing Telegram app data", self.device_id)
        self._adb("shell", "am", "force-stop", self.telegram_package, check=False)
        self._adb("shell", "pm", "clear", self.telegram_package)

    def cleanup_telegram(self, package_name: Optional[str] = None) -> None:
        """
        Backward-compatible alias for app cleanup.
        """
        normalized = str(package_name or "").strip()
        if normalized:
            self.telegram_package = normalized
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
            self.telegram_package,
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
        )
        time.sleep(2.0)

    def open_telegram(self, package_name: Optional[str] = None) -> None:
        """
        Backward-compatible alias for app launch.
        """
        normalized = str(package_name or "").strip()
        if normalized:
            self.telegram_package = normalized
        self.launch_telegram()

    def get_device_info(self) -> Dict[str, str]:
        """
        Read Android model/version for Telethon device spoofing.

        :return: Dict with keys: ``device_model`` and ``system_version``.
        """
        fingerprint = self.get_device_fingerprint()
        info = {
            "device_model": fingerprint["device_model"],
            "system_version": fingerprint["system_version"],
            "app_version": fingerprint["app_version"],
        }
        LOGGER.info("Device info detected: %s", info)
        return info

    def get_device_fingerprint(self) -> Dict[str, str]:
        """
        Collect real Telegram Android fingerprint from the container.

        Required fields:
          - ro.product.model
          - ro.build.version.release
          - Telegram APK versionName from dumpsys package
        """
        model = self._adb("shell", "getprop", "ro.product.model").stdout.strip() or "Unknown Android"
        android_release = (
            self._adb("shell", "getprop", "ro.build.version.release").stdout.strip() or "Unknown"
        )
        dumpsys_out = self._adb("shell", "dumpsys", "package", self.telegram_package).stdout
        telegram_version = self._extract_telegram_version_from_dumpsys(dumpsys_out)

        fingerprint = {
            "device_model": model,
            "system_version": f"Android {android_release}",
            "app_version": telegram_version or "Unknown",
        }
        LOGGER.info("Device fingerprint collected for %s: %s", self.device_id, fingerprint)
        return fingerprint

    def export_telegram_session_files(
        self,
        output_dir: str | Path,
        package_name: Optional[str] = None,
    ) -> Dict[str, Path]:
        """
        Export Telegram auth artifacts from private app storage to local directory.

        Files are copied under root to ``/sdcard/.tg_session_export_*`` and then
        pulled to host. Expected key artifacts include:
          - files/tgnet.dat
          - shared_prefs/userconfing.xml (and userconfig.xml fallback)
          - additional shared_prefs XML files
        """
        destination = Path(output_dir)
        destination.mkdir(parents=True, exist_ok=True)

        export_tag = f".tg_session_export_{int(time.time() * 1000)}"
        remote_export_dir = f"/sdcard/{export_tag}"
        remote_files_dir = f"{remote_export_dir}/files"
        remote_prefs_dir = f"{remote_export_dir}/shared_prefs"
        local_export_dir = destination / export_tag

        resolved_package_name = str(package_name or "").strip() or self.telegram_package
        base_data_path = ""
        for candidate in (
            f"/data/data/{resolved_package_name}",
            f"/data/user/0/{resolved_package_name}",
        ):
            check_cmd = f"test -d {shlex.quote(candidate)} && echo ok"
            probe = self._adb("shell", "sh", "-c", check_cmd, check=False)
            if "ok" in (probe.stdout or ""):
                base_data_path = candidate
                break

        if not base_data_path:
            raise RuntimeError(
                f"Telegram data dir not found for package {resolved_package_name!r} on {self.device_id}"
            )

        setup_script = (
            f"rm -rf {shlex.quote(remote_export_dir)} && "
            f"mkdir -p {shlex.quote(remote_files_dir)} {shlex.quote(remote_prefs_dir)} && "
            f"cp {shlex.quote(base_data_path + '/files/tgnet.dat')} {shlex.quote(remote_files_dir + '/tgnet.dat')} && "
            f"chmod 0644 {shlex.quote(remote_files_dir + '/tgnet.dat')} && "
            f"cp {shlex.quote(base_data_path + '/shared_prefs')}/userconfing.xml {shlex.quote(remote_prefs_dir)}/ 2>/dev/null || true && "
            f"cp {shlex.quote(base_data_path + '/shared_prefs')}/userconfig.xml {shlex.quote(remote_prefs_dir)}/ 2>/dev/null || true && "
            f"cp {shlex.quote(base_data_path + '/shared_prefs')}/mainconfig.xml {shlex.quote(remote_prefs_dir)}/ 2>/dev/null || true && "
            f"cp {shlex.quote(base_data_path + '/shared_prefs')}/*.xml {shlex.quote(remote_prefs_dir)}/ 2>/dev/null || true && "
            f"chmod 0644 {shlex.quote(remote_prefs_dir)}/*.xml 2>/dev/null || true && "
            f"test -f {shlex.quote(remote_files_dir + '/tgnet.dat')}"
        )
        self._adb("shell", "sh", "-c", setup_script, timeout=45)

        try:
            self._adb("pull", remote_export_dir, str(destination), timeout=60)
        finally:
            self._adb("shell", "rm", "-rf", remote_export_dir, check=False, timeout=20)

        if not local_export_dir.exists():
            # Fallback for host-specific adb pull behavior.
            fallback_match = next(
                (item for item in destination.glob(f"**/{export_tag}") if item.is_dir()),
                None,
            )
            if fallback_match is not None:
                local_export_dir = fallback_match

        pulled_files: Dict[str, Path] = {}
        for local_file in local_export_dir.rglob("*"):
            if local_file.is_file():
                pulled_files[local_file.name] = local_file

        if "tgnet.dat" not in pulled_files:
            raise RuntimeError(f"tgnet.dat was not exported from {self.device_id}")
        if "userconfing.xml" not in pulled_files and "userconfig.xml" not in pulled_files:
            raise RuntimeError("Neither userconfing.xml nor userconfig.xml was exported.")

        LOGGER.info(
            "Exported %s Telegram session files from %s to %s",
            len(pulled_files),
            self.device_id,
            local_export_dir,
        )
        return pulled_files

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
                if self._tap_telegram_resource("login_phone_code_text"):
                    self._input_text(cc_digits)
                    time.sleep(0.3)

        phone_digits = re.sub(r"\D", "", phone_number)
        if self._tap_telegram_resource("login_phone_number_text"):
            self._input_text(phone_digits)
        else:
            # TODO: calibrate coordinates for your Telegram build if no resource-id found.
            self._tap_percent(0.5, 0.42)
            self._input_text(phone_digits)

        if not self._tap_telegram_resource("login_btn"):
            self._tap_by_text_candidates(("Done", "Next", "Продолжить", "Далее"))
            self._adb("shell", "input", "keyevent", "66", check=False)
        time.sleep(1.5)

    def input_code(self, code: str) -> None:
        """
        Input verification SMS code in Telegram.
        """
        LOGGER.info("Inputting SMS code on Telegram UI")
        if not self._tap_telegram_resource("login_code_text"):
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
        known_resource_names = (
            "email",
            "login_email_field",
            "code_field",
        )

        tapped = any(self._tap_telegram_resource(resource_name) for resource_name in known_resource_names)
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
        if self._tap_telegram_resource("first_name_field"):
            self._input_text(first_name)
        else:
            # TODO: calibrate first-name field coordinates for your UI build.
            self._tap_percent(0.5, 0.32)
            self._input_text(first_name)

        if self._tap_telegram_resource("last_name_field"):
            self._input_text(last_name)
        else:
            # TODO: calibrate last-name field coordinates for your UI build.
            self._tap_percent(0.5, 0.40)
            self._input_text(last_name)

        if not self._tap_telegram_resource("login_btn"):
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

    def _telegram_packages_priority(self) -> tuple[str, ...]:
        ordered = [self.telegram_package, *self.TELEGRAM_PACKAGE_CANDIDATES]
        unique: list[str] = []
        seen: set[str] = set()
        for package_name in ordered:
            normalized = str(package_name or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            unique.append(normalized)
        return tuple(unique)

    def _telegram_resource_id(self, resource_name: str, package_name: Optional[str] = None) -> str:
        normalized_name = str(resource_name or "").strip()
        if normalized_name.startswith("id/"):
            resource_suffix = normalized_name
        else:
            resource_suffix = f"id/{normalized_name}"
        target_package = str(package_name or self.telegram_package).strip() or self.TELEGRAM_PACKAGE
        return f"{target_package}:{resource_suffix}"

    def _telegram_resource_id_candidates(self, resource_name: str) -> tuple[str, ...]:
        return tuple(
            self._telegram_resource_id(resource_name=resource_name, package_name=package_name)
            for package_name in self._telegram_packages_priority()
        )

    def _tap_telegram_resource(self, resource_name: str) -> bool:
        for resource_id in self._telegram_resource_id_candidates(resource_name):
            if self._tap_by_resource_id(resource_id):
                return True
        return False

    def _proxy_enable_resource_ids(self) -> tuple[str, ...]:
        resource_ids = ["android:id/button1"]
        for package_name in self._telegram_packages_priority():
            for resource_suffix in self.PROXY_ENABLE_RESOURCE_ID_SUFFIXES:
                resource_ids.append(self._telegram_resource_id(resource_suffix, package_name=package_name))
        return tuple(dict.fromkeys(resource_ids))

    def _is_telegram_installed(self) -> bool:
        result = subprocess.run(
            [self.adb_path, "-s", self.device_id, "shell", "pm", "list", "packages"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        packages_out = result.stdout or ""
        candidates = [
            "org.telegram.messenger.web",
            "org.telegram.messenger",
            "org.telegram.messenger.beta",
        ]

        for package_name in candidates:
            if f"package:{package_name}" in packages_out:
                self.telegram_package = package_name
                LOGGER.info("Successfully detected Telegram package: %s", package_name)
                return True

        telegram_lines = [line for line in packages_out.splitlines() if "telegram" in line.lower()]
        LOGGER.error(
            "Could not find exact Telegram package. Lines containing 'telegram': %s",
            telegram_lines,
        )
        return False

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

    @staticmethod
    def _extract_telegram_version_from_dumpsys(dumpsys_output: str) -> str:
        for line in dumpsys_output.splitlines():
            if "versionName=" not in line:
                continue
            _, _, version = line.partition("versionName=")
            cleaned = version.strip()
            if cleaned:
                return cleaned
        return ""

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

