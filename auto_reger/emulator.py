from __future__ import annotations

import logging
import shlex
import subprocess
import time
from pathlib import Path
from typing import Optional

from auto_reger.utils import CONFIG, resolve_project_path


LOGGER = logging.getLogger(__name__)


class DockerAndroidController:
    """Manages Docker-based Android lifecycle for one registration cycle."""

    def __init__(
        self,
        *,
        adb_path: Optional[str] = None,
        workdir: Optional[str] = None,
        compose_file: Optional[str] = None,
        compose_project: Optional[str] = None,
        poll_interval_seconds: float = 1.5,
    ) -> None:
        docker_cfg = CONFIG.get("docker", {})
        adb_cfg = CONFIG.get("adb", {})

        compose_command_raw = docker_cfg.get("compose_command", "docker-compose")
        if isinstance(compose_command_raw, list):
            compose_command = [str(item).strip() for item in compose_command_raw if str(item).strip()]
        else:
            compose_command = shlex.split(str(compose_command_raw).strip())
        self.compose_command = compose_command or ["docker-compose"]

        compose_file_raw = compose_file or str(docker_cfg.get("compose_file", "docker-compose.yml")).strip()
        self.compose_file = str(resolve_project_path(compose_file_raw)) if compose_file_raw else ""
        self.compose_project = (
            compose_project
            or str(docker_cfg.get("project_name", "auto_reger")).strip()
            or "auto_reger"
        )

        workdir_raw = workdir or str(docker_cfg.get("workdir", "")).strip()
        if workdir_raw:
            self.workdir = resolve_project_path(workdir_raw)
        else:
            self.workdir = Path.cwd()

        self.adb_path = str(adb_path or adb_cfg.get("adb_path", "adb")).strip() or "adb"
        self.poll_interval_seconds = max(float(poll_interval_seconds), 0.2)

    def _compose_base_command(self) -> list[str]:
        command = list(self.compose_command)
        if self.compose_project:
            command.extend(["-p", self.compose_project])
        if self.compose_file:
            command.extend(["-f", self.compose_file])
        return command

    def _run_compose(self, args: list[str], *, action: str) -> subprocess.CompletedProcess[str]:
        command = [*self._compose_base_command(), *args]
        LOGGER.info("Docker action `%s`: %s", action, " ".join(command))
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            cwd=str(self.workdir),
        )
        if result.stdout.strip():
            LOGGER.debug("Docker `%s` stdout: %s", action, result.stdout.strip())
        if result.stderr.strip():
            LOGGER.debug("Docker `%s` stderr: %s", action, result.stderr.strip())
        if result.returncode != 0:
            raise RuntimeError(
                f"Docker action `{action}` failed with code {result.returncode}: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        LOGGER.info("Docker action `%s` completed successfully", action)
        return result

    def start_container(self) -> None:
        """Starts Android container stack using docker-compose up -d."""
        self._run_compose(["up", "-d"], action="start_container")

    def stop_container(self) -> None:
        """Stops and destroys Android container stack, including volumes."""
        self._run_compose(["down", "-v"], action="stop_container")

    def wait_for_boot(self, device_udid: str, timeout: int = 60) -> None:
        """Waits until adb reports Android boot completion flag as 1."""
        normalized_udid = str(device_udid or "").strip()
        if not normalized_udid:
            raise ValueError("device_udid is required for boot wait.")

        LOGGER.info(
            "Waiting for Android boot completion on `%s` (timeout=%ss)",
            normalized_udid,
            timeout,
        )

        deadline = time.monotonic() + int(timeout)
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            try:
                subprocess.run(
                    [self.adb_path, "connect", normalized_udid],
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                LOGGER.debug(
                    "ADB connect timed out on boot check attempt %s for %s",
                    attempt,
                    normalized_udid,
                )
                continue

            cmd = [
                self.adb_path,
                "-s",
                normalized_udid,
                "shell",
                "getprop",
                "sys.boot_completed",
            ]
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
            except subprocess.TimeoutExpired:
                LOGGER.debug(
                    "ADB getprop timed out on boot check attempt %s for %s",
                    attempt,
                    normalized_udid,
                )
                continue
            status = result.stdout.strip()
            LOGGER.debug(
                "Boot check attempt %s on %s: return_code=%s, status=%r, stderr=%r",
                attempt,
                normalized_udid,
                result.returncode,
                status,
                result.stderr.strip(),
            )
            if status == "1":
                LOGGER.info("Android boot completed on `%s` after %s checks", normalized_udid, attempt)
                return
            time.sleep(self.poll_interval_seconds)

        raise TimeoutError(
            f"Timed out waiting for Android boot on `{normalized_udid}` after {timeout}s."
        )

    def install_apk(self, device_udid: str, apk_path: str = "/app/telegram.apk") -> None:
        normalized_udid = str(device_udid or "").strip()
        if not normalized_udid:
            raise ValueError("device_udid is required for APK installation.")

        normalized_apk_path = str(apk_path or "").strip()
        if not normalized_apk_path:
            raise ValueError("apk_path is required for APK installation.")

        LOGGER.info(
            "Installing Telegram APK on `%s` from `%s` with auto-granted permissions",
            normalized_udid,
            normalized_apk_path,
        )
        result = subprocess.run(
            [self.adb_path, "-s", normalized_udid, "install", "-g", normalized_apk_path],
            check=True,
            capture_output=True,
            text=True,
        )
        if result.stdout.strip():
            LOGGER.info("ADB install output on `%s`: %s", normalized_udid, result.stdout.strip())
        if result.stderr.strip():
            LOGGER.debug("ADB install stderr on `%s`: %s", normalized_udid, result.stderr.strip())


class Emulator:
    """Backward-compatible placeholder for deprecated Appium flow."""

    def __init__(self, *args, **kwargs):
        raise RuntimeError(
            "auto_reger.emulator.Emulator is deprecated. "
            "Use DockerAndroidController + DeviceController."
        )


class Telegram(Emulator):
    pass


class Instagram(Emulator):
    pass

