from __future__ import annotations

import logging
import os
import secrets
import shlex
import shutil
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
        worker_index: Optional[int] = None,
        poll_interval_seconds: float = 1.5,
        country_code: Optional[str] = None,
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

        base_project = (
            compose_project
            or str(docker_cfg.get("project_name", "auto_reger")).strip()
            or "auto_reger"
        )
        # worker_index is a stable ID for the lifetime of one worker thread.
        # It never changes between cycles, so stop→start always targets the same project.
        self._worker_index: int = int(worker_index) if worker_index is not None else 0
        self.compose_project = f"{base_project}_{self._worker_index}" if worker_index is not None else base_project

        workdir_raw = workdir or str(docker_cfg.get("workdir", "")).strip()
        if workdir_raw:
            self.workdir = resolve_project_path(workdir_raw)
        else:
            self.workdir = Path.cwd()

        self.adb_path = str(adb_path or adb_cfg.get("adb_path", "adb")).strip() or "adb"
        self.poll_interval_seconds = max(float(poll_interval_seconds), 0.2)
        self.country_code = str(country_code).strip().upper() if country_code else None

    def _compose_base_command(self) -> list[str]:
        command = list(self.compose_command)
        if self.compose_project:
            command.extend(["-p", self.compose_project])
        if self.compose_file:
            command.extend(["-f", self.compose_file])
        return command

    def _run_compose(
        self, args: list[str], *, action: str, env: Optional[dict[str, str]] = None
    ) -> subprocess.CompletedProcess[str]:
        command = [*self._compose_base_command(), *args]
        LOGGER.info("Docker action `%s`: %s", action, " ".join(command))

        process_env = os.environ.copy()
        if env:
            process_env.update(env)

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            cwd=str(self.workdir),
            env=process_env,
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

    @staticmethod
    def _generate_device_serial() -> str:
        """Generates a unique 14-character hex device serial (e.g. 'a3f1c9e72b4d8f')."""
        return secrets.token_hex(7)  # 7 bytes → 14 hex chars

    @staticmethod
    def _generate_build_id() -> str:
        """Generates a build ID in Android format: letter + digits (e.g. 'RQ3A.210705.001')."""
        prefix = secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        mid = secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        digits = secrets.randbelow(900000) + 100000  # 6-digit number
        patch = secrets.randbelow(900) + 100         # 3-digit patch
        return f"{prefix}{mid}{secrets.randbelow(9) + 1}A.{digits}.{patch:03d}"

    def start_container(self, extra_env: Optional[dict[str, str]] = None) -> None:
        """Starts Android container stack using docker-compose up -d.

        Automatically generates unique DEVICE_SERIAL and DEVICE_BUILD_ID so each
        container instance gets distinct hardware identifiers without relying on
        external bash scripts.
        """
        env: dict[str, str] = {}

        # Ensure ./adb_keys directory exists so Docker bind mount never fails.
        # Path is hardcoded in docker-compose.yml as ./adb_keys — no env var needed.
        dest = self.workdir / "adb_keys"
        if dest.exists() and dest.is_file():
            os.remove(dest)
        dest.mkdir(parents=True, exist_ok=True)

        system_key = Path.home() / ".android" / "adbkey.pub"
        if system_key.exists():
            shutil.copy2(system_key, dest / "adbkey.pub")
            LOGGER.info("Copied system ADB key %s → %s", system_key, dest)
        else:
            LOGGER.info(
                "ADB key not found at %s — running `adb devices` to trigger key generation",
                system_key,
            )
            system_key.parent.mkdir(parents=True, exist_ok=True)
            try:
                subprocess.run(
                    [self.adb_path, "devices"],
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
            except Exception:
                LOGGER.debug("adb devices failed; key generation skipped", exc_info=True)
            if system_key.exists():
                shutil.copy2(system_key, dest / "adbkey.pub")
                LOGGER.info("Copied newly generated ADB key %s → %s", system_key, dest)
            else:
                LOGGER.warning(
                    "ADB public key still not found at %s. "
                    "Container will reject ADB connections (ro.secure=1).",
                    system_key,
                )
        LOGGER.info("ADB key dir: %s (exists=%s)", dest, dest.exists())

        if self.country_code:
            from .sim_spoofing import get_sim_env_for_country
            LOGGER.info("Applying SIM spoofing for country: %s", self.country_code)
            env.update(get_sim_env_for_country(self.country_code))

        # Generate unique HW identifiers — overrides any defaults in docker-compose.yml
        device_serial = self._generate_device_serial()
        device_build_id = self._generate_build_id()
        env["DEVICE_SERIAL"] = device_serial
        env["DEVICE_BUILD_ID"] = device_build_id
        LOGGER.info(
            "Generated device identifiers: DEVICE_SERIAL=%s DEVICE_BUILD_ID=%s",
            device_serial,
            device_build_id,
        )

        # Each worker gets a unique host-side ADB port so parallel workers don't clash.
        # Worker 0 → 5555, worker 1 → 5565, worker 2 → 5575, …
        adb_port = 5555 + self._worker_index * 10
        env["ADB_PORT"] = str(adb_port)
        LOGGER.info("Worker %s: ADB host port set to %s", self._worker_index, adb_port)

        if extra_env:
            env.update(extra_env)

        self._run_compose(["up", "-d"], action="start_container", env=env)

    def stop_container(self) -> None:
        """Stops and destroys Android container stack, including volumes."""
        self._run_compose(["down", "-v", "--remove-orphans"], action="stop_container")

    def wait_for_boot(self, device_udid: str, timeout: int = 90) -> None:
        """Waits until adb reports Android boot completion flag as 1."""
        normalized_udid = str(device_udid or "").strip()
        if not normalized_udid:
            raise ValueError("device_udid is required for boot wait.")

        LOGGER.info(
            "Waiting for Android boot completion on `%s` (timeout=%ss)",
            normalized_udid,
            timeout,
        )

        # Сбрасываем залипший статус "offline" перед началом опроса
        try:
            subprocess.run(
                [self.adb_path, "disconnect", normalized_udid],
                capture_output=True,
                timeout=5,
                check=False,
            )
        except Exception:
            LOGGER.debug("adb disconnect failed before boot wait", exc_info=True)
        try:
            subprocess.run(
                [self.adb_path, "connect", normalized_udid],
                capture_output=True,
                timeout=5,
                check=False,
            )
        except Exception:
            LOGGER.debug("adb connect failed before boot wait", exc_info=True)

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
            print(
                f"[boot_check] attempt={attempt} udid={normalized_udid} "
                f"rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr.strip()!r}"
            )
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

