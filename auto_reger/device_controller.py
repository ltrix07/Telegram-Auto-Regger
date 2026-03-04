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
        "connect proxy",
        "connect",
        "\u0432\u043a\u043b\u044e\u0447\u0438\u0442\u044c \u043f\u0440\u043e\u043a\u0441\u0438",
        "\u0432\u043a\u043b\u044e\u0447\u0438\u0442\u044c",
        "\u0432\u043a\u043b",
        "\u0438\u0441\u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u044c",
        "\u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0438\u0442\u044c",
    )
    PROXY_ENABLE_RESOURCE_ID_SUFFIXES = (
        "button1",
        "button_positive",
        "positive_button",
        "next_button",
        "done_button",
        "ok_button",
        "login_btn",
    )
    START_MESSAGING_TEXT_CANDIDATES = (
        "start messaging",
        "\u043d\u0430\u0447\u0430\u0442\u044c \u043e\u0431\u0449\u0435\u043d\u0438\u0435",
        "start",
    )
    PHONE_COUNTRY_CODE_RESOURCE_ID_SUFFIXES = (
        "login_phone_code_text",
        "phone_code",
        "country_code",
    )
    PHONE_NUMBER_RESOURCE_ID_SUFFIXES = (
        "login_phone_number_text",
        "phone_input",
        "phone_number",
        "phone_field",
    )
    NEXT_DONE_TEXT_CANDIDATES = (
        "done",
        "next",
        "\u043f\u0440\u043e\u0434\u043e\u043b\u0436\u0438\u0442\u044c",
        "\u0434\u0430\u043b\u0435\u0435",
    )
    CODE_RESOURCE_ID_SUFFIXES = (
        "login_code_text",
        "code_field",
        "login_code_field",
    )
    CODE_TEXT_CANDIDATES = (
        "code",
        "\u043a\u043e\u0434",
        "verification code",
        "sms code",
    )
    FIRST_NAME_RESOURCE_ID_SUFFIXES = (
        "first_name_field",
        "first_name",
        "firstname",
    )
    LAST_NAME_RESOURCE_ID_SUFFIXES = (
        "last_name_field",
        "last_name",
        "lastname",
        "surname_field",
    )
    PROFILE_FINISH_TEXT_CANDIDATES = (
        "done",
        "finish",
        "\u0433\u043e\u0442\u043e\u0432",
    )
    CONTINUE_TEXT_CANDIDATES = (
        "continue",
        "continue in english",
        "\u043f\u0440\u043e\u0434\u043e\u043b\u0436\u0438\u0442\u044c",
        "\u043f\u0440\u043e\u0434\u043e\u043b\u0436\u0438\u0442\u044c \u043d\u0430 \u0430\u043d\u0433\u043b\u0438\u0439\u0441\u043a\u043e\u043c",
        "\u0434\u0430\u043b\u0435\u0435",
    )
    YES_TEXT_CANDIDATES = (
        "yes",
        "\u0434\u0430",
        "\u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u0442\u044c",
    )
    OK_TEXT_CANDIDATES = (
        "ok",
        "okay",
        "\u043e\u043a",
        "\u043f\u043e\u043d\u044f\u0442\u043d\u043e",
    )
    ACCEPT_TEXT_CANDIDATES = (
        "accept",
        "agree",
        "\u043f\u0440\u0438\u043d\u044f\u0442\u044c",
        "\u0441\u043e\u0433\u043b\u0430\u0441\u0435\u043d",
    )
    ALLOW_TEXT_CANDIDATES = (
        "allow",
        "allow only while using the app",
        "while using the app",
        "allow all the time",
        "only this time",
        "\u0440\u0430\u0437\u0440\u0435\u0448\u0438\u0442\u044c",
        "\u0442\u043e\u043b\u044c\u043a\u043e \u043f\u0440\u0438 \u0438\u0441\u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u043d\u0438\u0438",
        "\u0440\u0430\u0437\u0440\u0435\u0448\u0438\u0442\u044c \u0432\u0441\u0435\u0433\u0434\u0430",
        "\u0442\u043e\u043b\u044c\u043a\u043e \u0441\u0435\u0439\u0447\u0430\u0441",
    )
    ANDROID_ALLOW_RESOURCE_IDS = (
        "android:id/button1",
        "com.android.packageinstaller:id/permission_allow_button",
        "com.android.permissioncontroller:id/permission_allow_button",
        "com.android.permissioncontroller:id/permission_allow_foreground_only_button",
        "com.android.permissioncontroller:id/permission_allow_always_button",
        "com.android.permissioncontroller:id/permission_allow_one_time_button",
    )
    DIDNT_GET_CODE_TEXT_CANDIDATES = (
        "didn't get the code",
        "didnt get the code",
        "did not get the code",
        "\u043d\u0435 \u043f\u043e\u043b\u0443\u0447\u0438\u043b\u0438 \u043a\u043e\u0434",
        "\u043d\u0435 \u043f\u0440\u0438\u0448\u0435\u043b \u043a\u043e\u0434",
    )
    EDIT_NUMBER_TEXT_CANDIDATES = (
        "edit number",
        "edit",
        "\u0438\u0437\u043c\u0435\u043d\u0438\u0442\u044c \u043d\u043e\u043c\u0435\u0440",
        "\u0438\u0437\u043c\u0435\u043d\u0438\u0442\u044c",
    )
    BACK_TEXT_CANDIDATES = (
        "back",
        "go back",
        "\u043d\u0430\u0437\u0430\u0434",
    )
    NUMBER_BANNED_TEXT_CANDIDATES = (
        "this phone number is banned",
        "phone number is banned",
        "\u044d\u0442\u043e\u0442 \u043d\u043e\u043c\u0435\u0440 \u0437\u0430\u0431\u043b\u043e\u043a\u0438\u0440\u043e\u0432\u0430\u043d",
        "\u043d\u043e\u043c\u0435\u0440 \u0437\u0430\u0431\u043b\u043e\u043a\u0438\u0440\u043e\u0432\u0430\u043d",
    )
    ALREADY_REGISTERED_TEXT_CANDIDATES = (
        "check your telegram messages",
        "\u043f\u0440\u043e\u0432\u0435\u0440\u044c\u0442\u0435 \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u044f telegram",
    )
    EMAIL_BANNED_TEXT_CANDIDATES = (
        "email address is banned",
        "email is banned",
        "invalid email",
        "\u0430\u0434\u0440\u0435\u0441 \u044d\u043b\u0435\u043a\u0442\u0440\u043e\u043d\u043d\u043e\u0439 \u043f\u043e\u0447\u0442\u044b \u0437\u0430\u0431\u043b\u043e\u043a\u0438\u0440\u043e\u0432\u0430\u043d",
        "\u044d\u043b\u0435\u043a\u0442\u0440\u043e\u043d\u043d\u0430\u044f \u043f\u043e\u0447\u0442\u0430 \u0437\u0430\u0431\u043b\u043e\u043a\u0438\u0440\u043e\u0432\u0430\u043d\u0430",
        "\u043f\u043e\u0447\u0442\u0430 \u0437\u0430\u0431\u043b\u043e\u043a\u0438\u0440\u043e\u0432\u0430\u043d\u0430",
        "\u043d\u0435\u0432\u0435\u0440\u043d\u044b\u0439 \u0430\u0434\u0440\u0435\u0441",
    )
    TOO_MANY_ATTEMPTS_TEXT_CANDIDATES = (
        "too many attempts",
        "try again later",
        "\u0441\u043b\u0438\u0448\u043a\u043e\u043c \u043c\u043d\u043e\u0433\u043e \u043f\u043e\u043f\u044b\u0442\u043e\u043a",
        "\u043f\u043e\u043f\u0440\u043e\u0431\u0443\u0439\u0442\u0435 \u043f\u043e\u0437\u0436\u0435",
    )
    EXISTING_ACCOUNT_TEXT_CANDIDATES = (
        "check your telegram messages",
        "check your email",
        "\u043f\u0440\u043e\u0432\u0435\u0440\u044c\u0442\u0435 \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u044f telegram",
        "\u043f\u0440\u043e\u0432\u0435\u0440\u044c\u0442\u0435 \u043f\u043e\u0447\u0442\u0443",
    )
    TWO_FA_REQUIRED_TEXT_CANDIDATES = (
        "two-step verification enabled",
        "additional password",
        "\u0434\u0432\u0443\u0445\u044d\u0442\u0430\u043f\u043d\u0430\u044f \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0430",
        "\u0434\u043e\u043f\u043e\u043b\u043d\u0438\u0442\u0435\u043b\u044c\u043d\u044b\u0439 \u043f\u0430\u0440\u043e\u043b\u044c",
    )
    FORGOT_PASSWORD_TEXT_CANDIDATES = (
        "forgot password",
        "\u0437\u0430\u0431\u044b\u043b\u0438 \u043f\u0430\u0440\u043e\u043b\u044c",
    )
    RESET_ACCOUNT_TEXT_CANDIDATES = (
        "reset account",
        "\u0441\u0431\u0440\u043e\u0441\u0438\u0442\u044c \u0430\u043a\u043a\u0430\u0443\u043d\u0442",
        "\u0441\u0431\u0440\u043e\u0441\u0438\u0442\u044c \u0443\u0447\u0435\u0442\u043d\u0443\u044e \u0437\u0430\u043f\u0438\u0441\u044c",
    )
    GET_CODE_VIA_SMS_TEXT_CANDIDATES = (
        "get the code via sms",
        "via sms",
        "\u043f\u043e\u043b\u0443\u0447\u0438\u0442\u044c \u043a\u043e\u0434 \u043f\u043e sms",
        "\u043f\u043e\u043b\u0443\u0447\u0438\u0442\u044c \u043a\u043e\u0434 \u043f\u043e \u0441\u043c\u0441",
    )
    SMS_FEE_TEXT_CANDIDATES = (
        "sms fee",
        "\u043f\u043b\u0430\u0442\u0430 \u0437\u0430 sms",
        "\u0441\u0442\u043e\u0438\u043c\u043e\u0441\u0442\u044c sms",
    )
    EMAIL_FIELD_TEXT_CANDIDATES = (
        "email",
        "mail",
        "\u043f\u043e\u0447\u0442",
        "@",
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
        self._ui_xml_cache: Optional[str] = None
        self._ui_root_cache: Optional[ET.Element] = None

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

        offline_error = f"{result.stdout}\n{result.stderr}".lower()
        if result.returncode != 0 and "device offline" in offline_error:
            LOGGER.warning("ADB device %s is offline. Reconnecting and retrying command: %s", self.device_id, " ".join(cmd))
            if ":" in self.device_id:
                self._connect_network_device(check=False, timeout=20, log_attempt=False)
            time.sleep(1.0)
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

    def _connect_network_device(
        self,
        *,
        check: bool = True,
        timeout: int = 20,
        log_attempt: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """
        Connect ``host:port`` devices via ``adb connect``.
        """
        if ":" not in self.device_id:
            raise RuntimeError(f"`adb connect` is only applicable for network devices: {self.device_id}")

        if log_attempt:
            LOGGER.info("Connecting to ADB device %s", self.device_id)

        result = subprocess.run(
            [self.adb_path, "connect", self.device_id],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if check and result.returncode != 0:
            raise RuntimeError(
                "Failed to connect ADB device {}: {}".format(
                    self.device_id,
                    (result.stderr or result.stdout or "").strip(),
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
        max_attempts = 4
        last_adbd_probe: Optional[subprocess.CompletedProcess[str]] = None
        last_su_probe: Optional[subprocess.CompletedProcess[str]] = None

        for attempt in range(1, max_attempts + 1):
            LOGGER.info("Ensuring root access on %s (attempt %d/%d)", self.device_id, attempt, max_attempts)

            self._run_adb("root", check=False, timeout=20)
            time.sleep(3)

            if ":" in self.device_id:
                subprocess.run(
                    [self.adb_path, "disconnect", self.device_id],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self._connect_network_device(check=False, timeout=15, log_attempt=False)

            try:
                wait_result = self._run_adb("wait-for-device", check=False, timeout=20)
            except subprocess.TimeoutExpired:
                LOGGER.warning(
                    "wait-for-device timed out on %s (attempt %d/%d)",
                    self.device_id,
                    attempt,
                    max_attempts,
                )
                continue
            if wait_result.returncode != 0:
                LOGGER.warning(
                    "wait-for-device failed on %s (attempt %d/%d): %s",
                    self.device_id,
                    attempt,
                    max_attempts,
                    (wait_result.stderr or wait_result.stdout or "").strip(),
                )
                time.sleep(1.0)
                continue

            last_adbd_probe = self._run_adb("shell", "id", check=False, timeout=10)
            adbd_stdout = (last_adbd_probe.stdout or "").strip()
            if last_adbd_probe.returncode == 0 and "uid=0" in adbd_stdout:
                self._root_mode = "adbd"
                LOGGER.info("Root mode for %s: adbd", self.device_id)
                return

            last_su_probe = self._run_adb("shell", "su", "-c", "id", check=False, timeout=10)
            su_stdout = (last_su_probe.stdout or "").strip()
            if last_su_probe.returncode == 0 and "uid=0" in su_stdout:
                self._root_mode = "su"
                LOGGER.info("Root mode for %s: su", self.device_id)
                return

            LOGGER.warning(
                "Root probe failed on %s (attempt %d/%d): adbd=%r su=%r",
                self.device_id,
                attempt,
                max_attempts,
                adbd_stdout,
                su_stdout,
            )
            time.sleep(1.0)

        adbd_stdout = (last_adbd_probe.stdout or "").strip() if last_adbd_probe else ""
        su_stdout = (last_su_probe.stdout or "").strip() if last_su_probe else ""
        raise RuntimeError(
            "Root access is required but unavailable on {} (adbd stdout={!r}, su stdout={!r})".format(
                self.device_id,
                adbd_stdout,
                su_stdout,
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
            initial_state_result = self._run_adb("get-state", check=False, timeout=10)
            initial_state = initial_state_result.stdout.strip().lower()
            if not (initial_state_result.returncode == 0 and initial_state == "device"):
                self._connect_network_device(check=True, timeout=20, log_attempt=True)

            stable_deadline = time.monotonic() + 10.0
            while time.monotonic() < stable_deadline:
                state_result = self._run_adb("get-state", check=False, timeout=5)
                state = state_result.stdout.strip().lower()
                if state_result.returncode == 0 and state == "device":
                    break
                self._connect_network_device(check=False, timeout=10, log_attempt=False)
                time.sleep(1.0)
            else:
                raise RuntimeError(f"ADB device {self.device_id} failed to reach stable `device` state")
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
        Save a PNG screenshot to host filesystem.

        Primary command:
          adb -s <device_udid> exec-out screencap -p > <save_path>

        Fallback command sequence:
          adb -s <device_udid> shell screencap -p /sdcard/screen.png
          adb -s <device_udid> pull /sdcard/screen.png <save_path>
        """
        raw_path = str(save_path or "").strip()
        if not raw_path:
            LOGGER.error("Screenshot save path is empty for device %s", self.device_id)
            return False
        target_path = Path(raw_path)
        remote_tmp_path = "/sdcard/screen.png"

        def _device_is_available() -> bool:
            state_result = self._run_adb("get-state", check=False, timeout=10)
            state = (state_result.stdout or "").strip().lower()
            return state_result.returncode == 0 and state == "device"

        def _reconnect_device() -> None:
            if ":" not in self.device_id:
                return
            self._connect_network_device(check=False, timeout=20, log_attempt=False)
            time.sleep(1.0)

        def _capture_via_exec_out() -> bool:
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

        def _capture_via_pull_fallback() -> bool:
            shell_result = subprocess.run(
                [self.adb_path, "-s", self.device_id, "shell", "screencap", "-p", remote_tmp_path],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if shell_result.returncode != 0:
                LOGGER.error(
                    "Fallback screenshot shell command failed on %s: %s",
                    self.device_id,
                    (shell_result.stderr or shell_result.stdout or "").strip(),
                )
                return False

            pull_result = subprocess.run(
                [self.adb_path, "-s", self.device_id, "pull", remote_tmp_path, str(target_path)],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            subprocess.run(
                [self.adb_path, "-s", self.device_id, "shell", "rm", "-f", remote_tmp_path],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )

            if pull_result.returncode != 0:
                LOGGER.error(
                    "Fallback screenshot pull command failed on %s: %s",
                    self.device_id,
                    (pull_result.stderr or pull_result.stdout or "").strip(),
                )
                target_path.unlink(missing_ok=True)
                return False

            if not target_path.exists() or target_path.stat().st_size == 0:
                LOGGER.error("Fallback captured screenshot is empty for %s", self.device_id)
                target_path.unlink(missing_ok=True)
                return False

            return True

        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            if not _device_is_available():
                LOGGER.warning(
                    "Device %s is not available before screenshot capture. Attempting reconnect.",
                    self.device_id,
                )
                _reconnect_device()

            if _capture_via_exec_out():
                return True

            LOGGER.warning(
                "Retrying screenshot capture via exec-out after reconnect on %s",
                self.device_id,
            )
            _reconnect_device()
            if _capture_via_exec_out():
                return True

            LOGGER.warning(
                "exec-out screenshot failed on %s. Falling back to adb shell/pull method.",
                self.device_id,
            )
            if _capture_via_pull_fallback():
                return True

            target_path.unlink(missing_ok=True)
            return False
        except Exception:
            LOGGER.exception("Failed to capture screenshot for %s", self.device_id)
            try:
                target_path.unlink(missing_ok=True)
            except Exception:
                LOGGER.exception("Failed to cleanup invalid screenshot file: %s", target_path)
            return False

    def start_recording(self, remote_path: str = "/sdcard/debug_reg.mp4") -> subprocess.Popen:
        cmd = [
            self.adb_path,
            "-s",
            self.device_id,
            "shell",
            "screenrecord",
            "--bit-rate",
            "1000000",
            remote_path,
        ]
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop_recording_and_pull(self, proc: subprocess.Popen, remote_path: str, local_path: str) -> bool:
        self._adb("shell", "kill -2 $(pidof screenrecord)", check=False, timeout=5)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

        time.sleep(1.5)
        self._adb("pull", remote_path, local_path, check=False, timeout=30)
        self._adb("shell", "rm", "-f", remote_path, check=False, timeout=5)

        target = Path(local_path)
        return target.exists() and target.stat().st_size > 0

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

        result = self._run_adb(
            "shell",
            "am",
            "start",
            "-W",
            "-a",
            "android.intent.action.VIEW",
            "-d",
            safe_deep_link,
            self.telegram_package,
        )
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

    def enable_telegram_proxy_popup(self, timeout: float = 15.0, poll_interval: float = 0.4) -> str:
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
        detected_texts: set[str] = set()
        deadline = time.time() + max(timeout, 0.5)
        while time.time() < deadline:
            xml_text = self._dump_ui_xml()
            try:
                root = ET.fromstring(xml_text)
            except ET.ParseError:
                LOGGER.debug("Proxy popup XML parse failed on %s", self.device_id, exc_info=True)
                time.sleep(max(poll_interval, 0.1))
                continue

            for text_candidate in self._extract_text_candidates(xml_text):
                normalized_text = str(text_candidate).strip()
                if normalized_text:
                    detected_texts.add(normalized_text)

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

            # Some Telegram builds expose button label only via content-desc.
            for node in root.iter("node"):
                node_desc = str(node.attrib.get("content-desc", "")).strip()
                if node_desc.lower() != "connect proxy":
                    continue
                center = self._parse_bounds(str(node.attrib.get("bounds", "")))
                if not center:
                    continue
                self._tap(*center)
                selector = "content-desc='Connect Proxy'"
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

        if detected_texts:
            LOGGER.warning(
                "Telegram proxy popup timeout on %s. Screen texts: %s",
                self.device_id,
                sorted(detected_texts),
            )
        else:
            LOGGER.warning(
                "Telegram proxy popup timeout on %s. Screen texts were not extracted.",
                self.device_id,
            )

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
            "am",
            "start",
            "-a",
            "android.intent.action.MAIN",
            "-c",
            "android.intent.category.LAUNCHER",
            "-n",
            f"{self.telegram_package}/org.telegram.messenger.ui.LaunchActivity",
            check=False,
        )
        time.sleep(2.0)
        if self._tap_by_text_candidates(self.START_MESSAGING_TEXT_CANDIDATES):
            time.sleep(0.5)

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
        self._safe_tap_by_text_candidates(self.START_MESSAGING_TEXT_CANDIDATES, reason="start messaging")
        self._safe_tap_by_text_candidates(self.CONTINUE_TEXT_CANDIDATES, reason="continue")
        self._tap_system_allow_button()

        if country_code:
            cc_digits = re.sub(r"\D", "", country_code)
            if cc_digits and self._safe_tap_telegram_resources(
                self.PHONE_COUNTRY_CODE_RESOURCE_ID_SUFFIXES,
                reason="country code field",
            ):
                self._input_text(cc_digits)

        phone_digits = re.sub(r"\D", "", phone_number)
        if self._safe_tap_telegram_resources(self.PHONE_NUMBER_RESOURCE_ID_SUFFIXES, reason="phone number field"):
            self._input_text(phone_digits)
        else:
            # TODO: calibrate coordinates for your Telegram build if no resource-id found.
            self._tap_percent(0.5, 0.42)
            self._input_text(phone_digits)

        if not self._safe_tap_telegram_resources(
            ("login_btn", "next_button", "done_button", "ok_button"),
            reason="submit phone number",
        ):
            self._safe_tap_by_text_candidates(self.NEXT_DONE_TEXT_CANDIDATES, reason="submit phone number")
            self._adb("shell", "input", "keyevent", "66", check=False)
            self.invalidate_ui_dump_cache()

        self._safe_tap_by_text_candidates(["yes", "да"], reason="confirm phone number")
        time.sleep(0.5)
        self.invalidate_ui_dump_cache()
        self._raise_for_auth_blockers(step_name="phone submission", include_existing_account=True)
        self._handle_post_action_popups(rounds=2, include_accept=False)

    def input_code(self, code: str) -> None:
        """
        Input verification SMS code in Telegram.
        """
        LOGGER.info("Inputting SMS code on Telegram UI")
        self._safe_tap_by_text_candidates(self.GET_CODE_VIA_SMS_TEXT_CANDIDATES, reason="request code via SMS")
        code_field_tapped = self._safe_tap_telegram_resources(self.CODE_RESOURCE_ID_SUFFIXES, reason="code field")
        if not code_field_tapped:
            code_field_tapped = self._safe_tap_by_text_candidates(self.CODE_TEXT_CANDIDATES, reason="code field")
        if not code_field_tapped:
            self._tap_percent(0.5, 0.36)
        self._input_text(str(code))
        self._adb("shell", "input", "keyevent", "66", check=False)
        self.invalidate_ui_dump_cache()

        self._raise_for_auth_blockers(step_name="code confirmation", include_existing_account=False)
        self._handle_post_action_popups(rounds=2, include_accept=False)
        self._handle_two_factor_reset_flow()
        self._handle_post_action_popups(rounds=3, include_accept=False)

    def input_email(self, email: str) -> None:
        """
        Input email on Telegram's email verification step (if requested).

        Field identifiers vary across Telegram versions; this method uses
        best-effort matching with a fallback tap.
        """
        LOGGER.info("Inputting email on Telegram UI")
        known_resource_names = ("email", "login_email_field", "email_field")
        tapped = self._safe_tap_telegram_resources(known_resource_names, reason="email field")
        if not tapped:
            if not self._safe_tap_by_text_candidates(self.EMAIL_FIELD_TEXT_CANDIDATES, reason="email field"):
                # TODO: calibrate tap coordinates for email field if needed.
                self._tap_percent(0.5, 0.42)
        self._input_text(email)
        if not self._safe_tap_telegram_resources(
            ("login_btn", "next_button", "done_button", "ok_button"),
            reason="submit email",
        ):
            self._safe_tap_by_text_candidates(self.NEXT_DONE_TEXT_CANDIDATES, reason="submit email")
            self._adb("shell", "input", "keyevent", "66", check=False)

        self._raise_for_auth_blockers(step_name="email submission", include_existing_account=False)
        self._handle_post_action_popups(rounds=2, include_accept=False)

    def fill_profile(self, first_name: str, last_name: str) -> None:
        """
        Fill first/last name step in Telegram profile setup.
        """
        LOGGER.info("Filling Telegram profile name fields")
        if self._safe_tap_telegram_resources(self.FIRST_NAME_RESOURCE_ID_SUFFIXES, reason="first name field"):
            self._input_text(first_name)
        else:
            # TODO: calibrate first-name field coordinates for your UI build.
            self._tap_percent(0.5, 0.32)
            self._input_text(first_name)

        if self._safe_tap_telegram_resources(self.LAST_NAME_RESOURCE_ID_SUFFIXES, reason="last name field"):
            self._input_text(last_name)
        else:
            # TODO: calibrate last-name field coordinates for your UI build.
            self._tap_percent(0.5, 0.40)
            self._input_text(last_name)

        if not self._safe_tap_telegram_resources(
            ("login_btn", "done_button", "next_button", "ok_button"),
            reason="submit profile name",
        ):
            self._safe_tap_by_text_candidates(self.PROFILE_FINISH_TEXT_CANDIDATES, reason="submit profile name")

        self._handle_post_action_popups(rounds=2, include_accept=True)
        self._safe_tap_by_text_candidates(self.ACCEPT_TEXT_CANDIDATES, reason="accept terms popup")
        self._safe_tap_by_text_candidates(self.CONTINUE_TEXT_CANDIDATES, reason="continue after profile")
        self._tap_system_allow_button()
        self._tap_system_allow_button()
        self._handle_post_action_popups(rounds=2, include_accept=False)
        self._raise_for_auth_blockers(step_name="profile setup", include_existing_account=False)

    def _safe_tap_by_text_candidates(self, candidates: Iterable[str], reason: str = "") -> bool:
        self.invalidate_ui_dump_cache()
        tapped = self._tap_by_text_candidates(candidates)
        if tapped:
            if reason:
                LOGGER.debug("Tapped `%s` by text candidates", reason)
        return tapped

    def _safe_tap_telegram_resources(self, resource_names: Iterable[str], reason: str = "") -> bool:
        self.invalidate_ui_dump_cache()
        tapped = self._tap_telegram_resource_candidates(resource_names)
        if tapped:
            if reason:
                LOGGER.debug("Tapped `%s` by Telegram resource-id candidates", reason)
        return tapped

    def _tap_system_allow_button(self) -> bool:
        for resource_id in self.ANDROID_ALLOW_RESOURCE_IDS:
            self.invalidate_ui_dump_cache()
            if self._tap_by_resource_id(resource_id):
                LOGGER.debug("Tapped Android allow button by resource-id `%s`", resource_id)
                time.sleep(0.2)
                return True
        return self._safe_tap_by_text_candidates(self.ALLOW_TEXT_CANDIDATES, reason="android allow button")

    def _handle_post_action_popups(self, rounds: int = 3, include_accept: bool = False) -> None:
        max_rounds = max(int(rounds), 0)
        for _ in range(max_rounds):
            # 1. Сбрасываем кеш ровно ОДИН раз в начале раунда
            self.invalidate_ui_dump_cache()
            
            tapped_any = False
            
            # 2. Используем базовые методы _tap_*, которые не сбрасывают кеш
            if include_accept and self._tap_by_text_candidates(self.ACCEPT_TEXT_CANDIDATES):
                LOGGER.debug("Tapped `accept` popup")
                tapped_any = True
            elif self._tap_by_text_candidates(self.YES_TEXT_CANDIDATES):
                LOGGER.debug("Tapped `yes` popup")
                tapped_any = True
            elif self._tap_by_text_candidates(self.OK_TEXT_CANDIDATES):
                LOGGER.debug("Tapped `ok` popup")
                tapped_any = True
            elif self._tap_by_text_candidates(self.CONTINUE_TEXT_CANDIDATES):
                LOGGER.debug("Tapped `continue` popup")
                tapped_any = True
            else:
                # Проверяем системные ID кнопок (используем базовый метод)
                for resource_id in self.ANDROID_ALLOW_RESOURCE_IDS:
                    if self._tap_by_resource_id(resource_id):
                        LOGGER.debug("Tapped Android allow button by resource-id `%s`", resource_id)
                        tapped_any = True
                        break
                
                # Если системные ID не сработали, проверяем по тексту
                if not tapped_any and self._tap_by_text_candidates(self.ALLOW_TEXT_CANDIDATES):
                    LOGGER.debug("Tapped `allow` popup")
                    tapped_any = True

            # 3. Если мы ничего не нажали в этом раунде, значит попапов больше нет — выходим
            if not tapped_any:
                break
                
            # Если что-то нажали — ждем анимацию перед следующим дампом
            time.sleep(0.05)

    def _screen_contains_candidates(self, candidates: Iterable[str]) -> bool:
        self.invalidate_ui_dump_cache()
        return self.screen_contains_any(candidates)

    def _handle_existing_account_fallback(self) -> None:
        LOGGER.info("Handling existing-account fallback: trying `Didn't get the code?` and edit-number flow")
        tapped_didnt_get_code = self._safe_tap_by_text_candidates(
            self.DIDNT_GET_CODE_TEXT_CANDIDATES,
            reason="didn't get code",
        )
        if tapped_didnt_get_code:
            self._safe_tap_by_text_candidates(self.EDIT_NUMBER_TEXT_CANDIDATES, reason="edit number")

        if self._safe_tap_by_text_candidates(self.BACK_TEXT_CANDIDATES, reason="back"):
            self._safe_tap_by_text_candidates(self.EDIT_NUMBER_TEXT_CANDIDATES, reason="edit number")

    def _raise_for_auth_blockers(self, step_name: str, *, include_existing_account: bool) -> None:
        if self._screen_contains_candidates(self.NUMBER_BANNED_TEXT_CANDIDATES):
            self._safe_tap_by_text_candidates(self.OK_TEXT_CANDIDATES, reason="banned number dialog")
            raise RuntimeError(f"Telegram rejected number on `{step_name}`: phone number is banned.")

        if self._screen_contains_candidates(self.ALREADY_REGISTERED_TEXT_CANDIDATES):
            raise RuntimeError(f"Telegram sent code to another app on `{step_name}`: number is already registered.")

        if self._screen_contains_candidates(self.EMAIL_BANNED_TEXT_CANDIDATES):
            self._safe_tap_by_text_candidates(self.OK_TEXT_CANDIDATES, reason="banned email dialog")
            raise RuntimeError(f"Telegram rejected email on `{step_name}`: email is banned.")

        if self._screen_contains_candidates(self.TOO_MANY_ATTEMPTS_TEXT_CANDIDATES):
            raise RuntimeError(f"Telegram blocked retries on `{step_name}`: too many attempts.")

        if self._screen_contains_candidates(self.SMS_FEE_TEXT_CANDIDATES):
            raise RuntimeError(f"Telegram requested paid SMS flow on `{step_name}`.")

        if include_existing_account and self._screen_contains_candidates(self.EXISTING_ACCOUNT_TEXT_CANDIDATES):
            self._handle_existing_account_fallback()
            raise RuntimeError(
                f"Phone number is already linked to Telegram on `{step_name}` (check Telegram messages/email screen)."
            )

    def _handle_two_factor_reset_flow(self) -> None:
        if not self._screen_contains_candidates(self.TWO_FA_REQUIRED_TEXT_CANDIDATES):
            return

        LOGGER.warning("2FA password prompt detected, trying forgot-password/reset-account fallback flow")
        forgot_tapped = self._safe_tap_by_text_candidates(
            self.FORGOT_PASSWORD_TEXT_CANDIDATES,
            reason="forgot password",
        )
        if not forgot_tapped:
            LOGGER.warning("2FA prompt is present, but `Forgot password` button was not found")
            return

        self._safe_tap_by_text_candidates(self.RESET_ACCOUNT_TEXT_CANDIDATES, reason="reset account")
        time.sleep(0.3)
        self._safe_tap_by_text_candidates(self.RESET_ACCOUNT_TEXT_CANDIDATES, reason="reset account confirm")
        self._handle_post_action_popups(rounds=3, include_accept=False)

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

        if self._tap_by_text_candidates(("Telegram", "\u0422\u0435\u043b\u0435\u0433\u0440\u0430\u043c")):
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
            re.compile(r"(?i)(?:code|\u043a\u043e\u0434)[^\d]{0,20}(\d{5,6})"),
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

    def _tap_telegram_resource_candidates(self, resource_names: Iterable[str]) -> bool:
        for resource_name in resource_names:
            normalized = str(resource_name or "").strip()
            if not normalized:
                continue
            if self._tap_telegram_resource(normalized):
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

    def invalidate_ui_dump_cache(self) -> None:
        """
        Drop cached UI dump/root. Call before steps that are expected to change UI.
        """
        self._ui_xml_cache = None
        self._ui_root_cache = None

    def _dump_ui_xml(self) -> str:
        self._adb("shell", "uiautomator", "dump", self.UI_DUMP_PATH, check=False)
        xml_text = self._adb("shell", "cat", self.UI_DUMP_PATH).stdout
        if not xml_text.strip():
            raise RuntimeError("uiautomator dump returned empty XML.")
        return xml_text

    def _get_cached_ui_xml(self) -> str:
        if self._ui_xml_cache is None:
            self._ui_xml_cache = self._dump_ui_xml()
            self._ui_root_cache = None
        return self._ui_xml_cache

    def _get_cached_ui_root(self) -> Optional[ET.Element]:
        if self._ui_root_cache is not None:
            return self._ui_root_cache
        xml_text = self._get_cached_ui_xml()
        try:
            self._ui_root_cache = ET.fromstring(xml_text)
        except ET.ParseError:
            LOGGER.exception("Unable to parse cached UI dump XML")
            self.invalidate_ui_dump_cache()
            return None
        return self._ui_root_cache

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
        root = self._get_cached_ui_root()
        if root is None:
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
        root = self._get_cached_ui_root()
        if root is None:
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
        self.invalidate_ui_dump_cache()

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
        chunk_size = 3
        for i in range(0, len(text), chunk_size):
            chunk = text[i : i + chunk_size]
            safe_chunk = self._escape_adb_text(chunk)
            self._adb("shell", "input", "text", safe_chunk)
        self.invalidate_ui_dump_cache()

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
