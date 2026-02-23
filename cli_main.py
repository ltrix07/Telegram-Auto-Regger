from __future__ import annotations

import argparse
import logging
import queue
import random
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

from auto_reger.device_controller import DeviceController
from auto_reger.emulator import DockerAndroidController
from auto_reger.email_api import EmailApi
from auto_reger.notifier import TelegramNotifier
from auto_reger.proxy_api import ProxyApi
from auto_reger.session_generator import SessionGenerator
from auto_reger.sms_api import (
    SmsApi,
    can_set_status_8,
    remove_activation_from_json,
    save_activation_to_json,
)
from auto_reger.utils import CONFIG, load_names, resolve_project_path


LOGGER = logging.getLogger("cli_regger")
STOP_EVENT = threading.Event()
ACTIVE_ACTIVATIONS_LOCK = threading.Lock()
ACTIVE_ACTIVATIONS: dict[str, dict[str, str]] = {}
ALERT_RATE_LIMIT_LOCK = threading.Lock()
LAST_CYCLE_ALERT_AT: dict[str, float] = {}
ALERT_SEND_LOCK = threading.Lock()

ROUTINE_ALERT_SKIP_PATTERNS: tuple[str, ...] = (
    "sms code not received",
    "sms status polling failed",
    "wait sms code",
    "rent sms number failed",
    "no free numbers",
    "no_numbers",
    "not enough balance",
    "invalid proxy payload",
    "failed to set proxy",
    "proxy apply verification failed",
    "proxy api returned an empty list",
    "telegram proxy intent failed",
    "telegram proxy popup was not confirmed",
    "proxy must be socks5",
    "internet connectivity check failed",
)


class ShutdownRequested(RuntimeError):
    """Raised when graceful shutdown was requested."""


@dataclass(slots=True)
class CycleResult:
    index: int
    success: bool
    device_id: str
    phone_number: str = ""


class DevicePool:
    """Thread-safe pool where each ADB device can be leased by one worker at a time."""

    def __init__(self, device_ids: Iterable[str]) -> None:
        devices = [str(item).strip() for item in device_ids if str(item).strip()]
        if not devices:
            raise ValueError("DevicePool requires at least one device.")

        self._queue: queue.Queue[str] = queue.Queue()
        for device_id in devices:
            self._queue.put(device_id)

    def acquire(self, stop_event: threading.Event, timeout: float = 1.0) -> str:
        while not stop_event.is_set():
            try:
                return self._queue.get(timeout=timeout)
            except queue.Empty:
                continue
        raise ShutdownRequested("Shutdown requested before a device was acquired.")

    def release(self, device_id: str) -> None:
        if device_id:
            self._queue.put(device_id)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(threadName)s | %(name)s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Headless Telegram Auto Regger with multi-threaded ADB device pool."
    )
    parser.add_argument("--count", type=int, required=True, help="Total accounts to register.")
    parser.add_argument(
        "--threads",
        type=int,
        default=None,
        help="Worker threads. Defaults to config.concurrency.max_workers.",
    )
    parser.add_argument(
        "--devices",
        type=str,
        default="",
        help="Path to text file with ADB serials (one per line, e.g. 127.0.0.1:5555).",
    )
    parser.add_argument(
        "--device",
        action="append",
        default=[],
        help="Single ADB serial override. Can be passed multiple times.",
    )
    return parser.parse_args()


def _check_shutdown(stop_event: threading.Event) -> None:
    if stop_event.is_set():
        raise ShutdownRequested("Stop signal received.")


def retry_call(
    operation: str,
    func,
    attempts: int = 3,
    initial_delay: float = 1.0,
    backoff: float = 1.5,
    retry_exceptions: tuple[type[BaseException], ...] = (Exception,),
):
    delay = initial_delay
    last_error: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except retry_exceptions as exc:
            last_error = exc
            if attempt == attempts:
                break
            LOGGER.warning(
                "%s failed on attempt %s/%s: %s. Retrying in %.1fs",
                operation,
                attempt,
                attempts,
                exc,
                delay,
            )
            time.sleep(delay)
            delay *= backoff

    raise RuntimeError(f"{operation} failed after {attempts} attempts") from last_error


def build_sms_api() -> SmsApi:
    sms_cfg = CONFIG.get("sms_api", {})
    service_name = str(sms_cfg.get("service_name", "sms-activate")).strip()
    api_key_path = str(sms_cfg.get("api_key_path", "")).strip()
    api_url = str(sms_cfg.get("api_url", "")).strip() or None
    if not api_key_path:
        raise ValueError("Missing config key: sms_api.api_key_path")

    resolved_key_path = resolve_project_path(api_key_path)
    return SmsApi(service=service_name, api_key_path=str(resolved_key_path), api_url=api_url)


def build_email_api() -> Optional[EmailApi]:
    email_cfg = CONFIG.get("email_api", {})
    token = (
        str(email_cfg.get("kopeechka_api_token", "")).strip()
        or str(email_cfg.get("api_token", "")).strip()
    )
    if not token:
        return None
    return EmailApi(api_token=token)


def build_session_generator() -> SessionGenerator:
    telethon_cfg = CONFIG.get("telethon", {})
    api_id = telethon_cfg.get("api_id")
    api_hash = telethon_cfg.get("api_hash")
    if not api_id or not api_hash:
        raise ValueError("Missing telethon.api_id or telethon.api_hash in config.yaml")

    return SessionGenerator(
        api_id=int(api_id),
        api_hash=str(api_hash),
        sessions_dir=Path("sessions"),
        app_version=str(telethon_cfg.get("app_version", "11.8.3")),
        system_lang_code=str(telethon_cfg.get("system_lang_code", "en")),
        lang_code=str(telethon_cfg.get("lang_code", "en")),
    )


def extract_activation_and_phone(payload: Dict[str, Any]) -> Tuple[str, str]:
    activation_id = str(payload.get("activationId") or payload.get("id") or "").strip()
    phone_number = str(
        payload.get("phoneNumber")
        or payload.get("phone")
        or payload.get("number")
        or ""
    ).strip()

    if not activation_id or not phone_number:
        raise RuntimeError(f"Unexpected number payload from SMS provider: {payload!r}")
    return activation_id, phone_number


def load_profile_names() -> list[str]:
    paths_cfg = CONFIG.get("paths", {})
    names_file = str(paths_cfg.get("last_names_file", "data/last_names.txt"))
    resolved = resolve_project_path(names_file)
    if not resolved.exists():
        LOGGER.warning("Last names file not found: %s. Using fallback values.", resolved)
        return ["Smith", "Johnson", "Brown", "Taylor"]
    return load_names(str(resolved))


def build_telegram_notifier() -> Optional[TelegramNotifier]:
    notifications_cfg = CONFIG.get("notifications", {})
    if not isinstance(notifications_cfg, dict):
        return None
    if not bool(notifications_cfg.get("enabled", False)):
        return None

    bot_token = str(notifications_cfg.get("bot_token", "")).strip()
    admin_chat_id = str(notifications_cfg.get("admin_chat_id", "")).strip()
    if not bot_token or not admin_chat_id:
        LOGGER.warning("Notifications enabled, but bot_token/admin_chat_id are not configured.")
        return None

    try:
        return TelegramNotifier(bot_token=bot_token, chat_id=admin_chat_id)
    except Exception:
        LOGGER.exception("Failed to initialize Telegram notifier.")
        return None


def _is_routine_cycle_error(exc: BaseException) -> bool:
    chunks = [str(exc)]
    if exc.__cause__ is not None:
        chunks.append(str(exc.__cause__))
    if exc.__context__ is not None:
        chunks.append(str(exc.__context__))
    payload = " | ".join(part for part in chunks if part).lower()
    if not payload:
        return False
    return any(pattern in payload for pattern in ROUTINE_ALERT_SKIP_PATTERNS)


def _build_alert_signature(exc: BaseException) -> str:
    chunks = [str(exc)]
    if exc.__cause__ is not None:
        chunks.append(str(exc.__cause__))
    payload = " | ".join(part for part in chunks if part).lower()
    payload = re.sub(r"0x[0-9a-f]+", "<hex>", payload)
    payload = re.sub(r"\d+", "<num>", payload)
    payload = re.sub(r"\s+", " ", payload).strip()
    return f"{type(exc).__name__}:{payload[:220]}"


def _cycle_alert_cooldown_seconds() -> int:
    notifications_cfg = CONFIG.get("notifications", {})
    if not isinstance(notifications_cfg, dict):
        return 300
    raw_value = notifications_cfg.get("cycle_alert_cooldown_seconds", 300)
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return 300
    return max(value, 0)


def _acquire_cycle_alert_slot(signature: str, cooldown_seconds: int) -> tuple[bool, float]:
    now = time.time()
    with ALERT_RATE_LIMIT_LOCK:
        last_ts = LAST_CYCLE_ALERT_AT.get(signature)
        if last_ts is not None:
            elapsed = now - last_ts
            if elapsed < cooldown_seconds:
                return False, cooldown_seconds - elapsed
        LAST_CYCLE_ALERT_AT[signature] = now
    return True, 0.0


def _build_debug_screenshot_path(device_id: str, reason: str) -> Path:
    debug_dir = Path("debug")
    debug_dir.mkdir(parents=True, exist_ok=True)
    safe_reason = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(reason or "error")).strip("_") or "error"
    safe_device = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(device_id or "unknown")).strip("_") or "unknown"
    return debug_dir / f"{safe_reason}_{safe_device}_{int(time.time())}.png"


def maybe_send_cycle_error_alert(
    notifier: Optional[TelegramNotifier],
    *,
    cycle_index: int,
    total_cycles: int,
    device_id: str,
    device: Optional[DeviceController],
    exc: BaseException,
    error_trace: str,
) -> None:
    if not notifier:
        return
    if _is_routine_cycle_error(exc):
        LOGGER.info(
            "Cycle %s/%s routine failure, Telegram alert skipped: %s",
            cycle_index,
            total_cycles,
            exc,
        )
        return

    signature = _build_alert_signature(exc)
    cooldown_seconds = _cycle_alert_cooldown_seconds()
    allowed, retry_after = _acquire_cycle_alert_slot(signature, cooldown_seconds)
    if not allowed:
        LOGGER.info(
            "Cycle alert suppressed by cooldown (retry in %.0fs): %s",
            retry_after,
            signature,
        )
        return

    screenshot_path: Optional[str] = None
    try:
        if device:
            path = _build_debug_screenshot_path(device.device_id, reason=f"cycle_{cycle_index}_alert")
            if device.take_screenshot(str(path)):
                screenshot_path = str(path)
        elif device_id:
            screenshot_path = take_alert_screenshot(device_id=device_id, reason=f"cycle_{cycle_index}_alert")
    except Exception:
        LOGGER.exception("Failed to capture cycle alert screenshot on %s", device_id or "unknown")

    alert_text = (
        f"Cycle {cycle_index}/{total_cycles} unexpected failure on {device_id or 'unknown'}\n"
        f"Error type: {type(exc).__name__}\n"
        f"Message: {exc}\n\n"
        f"Traceback:\n{error_trace}"
    )
    with ALERT_SEND_LOCK:
        sent = notifier.send_error_alert(error_message=alert_text, screenshot_path=screenshot_path)
    if sent:
        LOGGER.info("Cycle alert sent to Telegram for cycle %s on %s", cycle_index, device_id or "unknown")
    else:
        LOGGER.warning("Cycle alert delivery failed for cycle %s on %s", cycle_index, device_id or "unknown")


def resolve_alert_device_id() -> str:
    devices = CONFIG.get("concurrency", {}).get("device_list", [])
    if isinstance(devices, list):
        for candidate in devices:
            normalized = str(candidate).strip()
            if normalized:
                return normalized
    return str(CONFIG.get("adb", {}).get("device_udid", "")).strip()


def take_alert_screenshot(device_id: str, reason: str = "fatal_error") -> Optional[str]:
    normalized_id = str(device_id or "").strip()
    if not normalized_id:
        return None

    adb_path = str(CONFIG.get("adb", {}).get("adb_path", "adb")).strip() or "adb"
    if ":" in normalized_id:
        subprocess.run(
            [adb_path, "connect", normalized_id],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    screenshot_path = _build_debug_screenshot_path(normalized_id, reason=reason)

    device = DeviceController(device_id=normalized_id, adb_path=adb_path)
    if device.take_screenshot(str(screenshot_path)):
        return str(screenshot_path)
    return None


def maybe_handle_email_step(device: DeviceController, email_api: Optional[EmailApi]) -> None:
    if not email_api:
        return
    if not device.screen_contains_any(("email", "mail", "почт")):
        return

    LOGGER.info("Email challenge detected. Requesting temporary mailbox.")
    task_id, email_address = email_api.get_email(site="telegram.org", mail_type="OUTLOOK")
    device.input_email(email_address)
    email_code = email_api.wait_for_email_code(task_id=task_id, timeout=120)
    device.input_code(email_code)


def maybe_fill_profile_step(device: DeviceController, last_names: list[str]) -> None:
    if not device.screen_contains_any(("first name", "last name", "имя", "фамилия", "profile")):
        return

    first_name = f"user{random.randint(1000, 9999)}"
    last_name = random.choice(last_names) if last_names else "Smith"
    device.fill_profile(first_name=first_name, last_name=last_name)


def register_active_activation(activation_id: str, phone_number: str, device_id: str) -> None:
    with ACTIVE_ACTIVATIONS_LOCK:
        ACTIVE_ACTIVATIONS[activation_id] = {
            "phone_number": phone_number,
            "device_id": device_id,
        }


def unregister_active_activation(activation_id: str) -> None:
    with ACTIVE_ACTIVATIONS_LOCK:
        ACTIVE_ACTIVATIONS.pop(activation_id, None)


def cancel_activation_safe(
    sms_api: SmsApi,
    activation_id: str,
    *,
    force: bool = False,
) -> None:
    if not activation_id:
        return

    try:
        if force or can_set_status_8(activation_id):
            retry_call(
                operation=f"cancel activation {activation_id}",
                func=lambda: sms_api.setStatus(activation_id, status=8),
                attempts=3,
                initial_delay=1.0,
            )
            LOGGER.info("Activation %s cancelled with status=8", activation_id)
        else:
            LOGGER.info("Activation %s is too new for status=8 cancellation", activation_id)
    except Exception:
        LOGGER.exception("Failed to cancel activation %s", activation_id)
    finally:
        remove_activation_from_json(activation_id)
        unregister_active_activation(activation_id)


def safe_set_activation_done(sms_api: SmsApi, activation_id: str) -> None:
    retry_call(
        operation=f"complete activation {activation_id}",
        func=lambda: sms_api.setStatus(activation_id, status=6),
        attempts=3,
        initial_delay=1.0,
    )


def rent_number_with_retry(sms_api: SmsApi) -> Tuple[str, str]:
    sms_cfg = CONFIG.get("sms_api", {})
    registration_cfg = CONFIG.get("registration", {})
    telegram_service_code = str(sms_cfg.get("telegram_service_code", "tg")).strip()
    country = str(registration_cfg.get("default_country", "USA")).strip()
    max_price = registration_cfg.get("default_max_price")

    def _rent_number() -> Tuple[str, str]:
        payload = sms_api.verification_number(
            service=telegram_service_code,
            country=country,
            max_price=max_price,
        )
        return extract_activation_and_phone(payload)

    activation_id, phone_number = retry_call(
        operation="rent SMS number",
        func=_rent_number,
        attempts=4,
        initial_delay=2.0,
    )

    save_activation_to_json(activation_id=activation_id, phone_number=phone_number)
    return activation_id, phone_number


def wait_sms_code(sms_api: SmsApi, activation_id: str) -> str:
    try:
        return retry_call(
            operation=f"wait SMS code for activation {activation_id}",
            func=lambda: sms_api.check_verif_status(
                activation_id=activation_id,
                timeout=300,
                poll_interval=5,
            ),
            attempts=2,
            initial_delay=3.0,
            retry_exceptions=(Exception,),
        )
    except Exception:
        LOGGER.exception("SMS status polling failed for activation %s", activation_id)
        return ""


def hard_reset_device(device: DeviceController, reason: str) -> None:
    try:
        screenshot_path = _build_debug_screenshot_path(device.device_id, reason=reason)
        if device.take_screenshot(str(screenshot_path)):
            LOGGER.info("Saved debug screenshot: %s", screenshot_path)
        else:
            LOGGER.warning("Failed to capture screenshot on %s", device.device_id)
    except Exception:
        LOGGER.exception("Unexpected screenshot error on %s", device.device_id)

    try:
        device.set_proxy("")
    except Exception:
        LOGGER.exception("Failed to clear proxy during hard reset for %s", device.device_id)

    try:
        device.prepare_device()
    except Exception:
        LOGGER.exception("Failed to clear Telegram state during hard reset for %s", device.device_id)


def _load_devices_from_file(path_value: str) -> list[str]:
    file_path = resolve_project_path(path_value)
    if not file_path.exists():
        raise FileNotFoundError(f"Devices file not found: {file_path}")

    result: list[str] = []
    for line in file_path.read_text(encoding="utf-8").splitlines():
        normalized = line.strip()
        if not normalized or normalized.startswith("#"):
            continue
        result.append(normalized)
    return result


def resolve_devices(args: argparse.Namespace) -> list[str]:
    configured: list[str] = []

    if args.devices:
        configured.extend(_load_devices_from_file(args.devices))
    else:
        concurrency_cfg = CONFIG.get("concurrency", {})
        cfg_devices = concurrency_cfg.get("device_list", [])
        if isinstance(cfg_devices, list):
            configured.extend(str(item).strip() for item in cfg_devices if str(item).strip())

    if args.device:
        configured.extend(str(item).strip() for item in args.device if str(item).strip())

    if not configured:
        fallback = str(CONFIG.get("adb", {}).get("device_udid", "")).strip()
        if fallback:
            configured.append(fallback)

    unique: list[str] = []
    seen: set[str] = set()
    for item in configured:
        if item not in seen:
            seen.add(item)
            unique.append(item)

    if not unique:
        raise ValueError("No devices configured. Use --devices or config.concurrency.device_list")
    return unique


def resolve_workers(args: argparse.Namespace, devices_count: int) -> int:
    if args.threads is not None:
        workers = int(args.threads)
    else:
        workers = int(CONFIG.get("concurrency", {}).get("max_workers", 1))

    if workers < 1:
        raise ValueError("--threads must be >= 1")

    if workers > devices_count:
        LOGGER.warning(
            "Requested %s workers for %s devices. Extra workers will wait for a free device.",
            workers,
            devices_count,
        )
    return workers


def build_docker_controller_from_config() -> tuple[DockerAndroidController, int]:
    docker_cfg = CONFIG.get("docker", {})
    if not isinstance(docker_cfg, dict):
        docker_cfg = {}

    compose_file = str(docker_cfg.get("compose_file", "docker-compose.yml")).strip()
    project_name = str(docker_cfg.get("project_name", "auto_reger")).strip()
    boot_timeout_raw = docker_cfg.get("boot_timeout_seconds", 60)

    try:
        boot_timeout_seconds = int(boot_timeout_raw)
    except (TypeError, ValueError):
        LOGGER.warning(
            "Invalid docker.boot_timeout_seconds=%r; using default 60",
            boot_timeout_raw,
        )
        boot_timeout_seconds = 60

    boot_timeout_seconds = max(boot_timeout_seconds, 5)
    controller = DockerAndroidController(
        compose_file=compose_file,
        compose_project=project_name,
    )

    LOGGER.info(
        "Docker controller configured: project_name=%s, compose_file=%s, boot_timeout_seconds=%s",
        project_name,
        compose_file,
        boot_timeout_seconds,
    )
    return controller, boot_timeout_seconds


def run_single_cycle(
    cycle_index: int,
    total_cycles: int,
    *,
    stop_event: threading.Event,
    device_pool: DevicePool,
    proxy_api: ProxyApi,
    sms_api: SmsApi,
    last_names: list[str],
    notifier: Optional[TelegramNotifier],
) -> CycleResult:
    device_id = ""
    device: Optional[DeviceController] = None
    docker_controller: Optional[DockerAndroidController] = None
    email_api: Optional[EmailApi] = None
    session_generator: Optional[SessionGenerator] = None
    activation_id = ""
    phone_number = ""
    cycle_success = False

    try:
        device_id = device_pool.acquire(stop_event=stop_event)
        LOGGER.info("Cycle %s/%s started on device %s", cycle_index, total_cycles, device_id)
        _check_shutdown(stop_event)

        docker_controller, boot_timeout = build_docker_controller_from_config()
        LOGGER.info(
            "Cycle %s/%s pre-flight cleanup: stopping old Docker Android container",
            cycle_index,
            total_cycles,
        )
        docker_controller.stop_container()
        _check_shutdown(stop_event)

        LOGGER.info(
            "Cycle %s/%s starting fresh Docker Android container",
            cycle_index,
            total_cycles,
        )
        docker_controller.start_container()
        docker_controller.wait_for_boot(device_udid=device_id, timeout=boot_timeout)
        LOGGER.info(
            "Cycle %s/%s Android container is fully booted on %s",
            cycle_index,
            total_cycles,
            device_id,
        )

        device = DeviceController(device_id=device_id)
        email_api = build_email_api()
        session_generator = build_session_generator()

        device.connect()
        if not device.is_ready():
            raise RuntimeError(f"Device {device_id} is not ready for registration.")

        country = str(CONFIG.get("registration", {}).get("default_country", "US")).strip() or "US"
        proxy_data = proxy_api.get_proxy(country_code=country)
        if not proxy_data:
            LOGGER.error(
                "Cycle %s/%s proxy acquisition failed: proxy_data is empty for country=%s.",
                cycle_index,
                total_cycles,
                country,
            )
            raise RuntimeError(f"Proxy is required for registration but none returned for country={country}.")

        proxy_host = str(proxy_data.get("ip", "")).strip()
        proxy_port = str(proxy_data.get("port", "")).strip()
        proxy_user = str(proxy_data.get("user", "")).strip()
        proxy_password = str(proxy_data.get("pass", "")).strip()
        proxy_type = str(proxy_data.get("type", "")).strip().lower()
        if not proxy_host or not proxy_port.isdigit():
            LOGGER.error(
                "Cycle %s/%s invalid proxy payload from ProxyApi: %r",
                cycle_index,
                total_cycles,
                proxy_data,
            )
            raise RuntimeError(f"Invalid proxy payload from ProxyApi: {proxy_data!r}")
        if proxy_type != "socks5":
            raise RuntimeError(
                f"Proxy must be SOCKS5 for Telegram intent flow. Got type={proxy_type!r}, payload={proxy_data!r}"
            )

        proxy_dict: Dict[str, Any] = {
            "type": "socks5",
            "host": proxy_host,
            "port": int(proxy_port),
            "username": proxy_user,
            "password": proxy_password,
        }

        if not device.set_telegram_proxy_via_intent(
            proxy_host,
            proxy_port,
            proxy_user,
            proxy_password,
        ):
            raise RuntimeError(
                f"Telegram proxy intent failed for {proxy_host}:{proxy_port} on {device_id}"
            )

        try:
            device.enable_telegram_proxy_popup(timeout=7.0)
        except TimeoutError as exc:
            raise RuntimeError(
                "Telegram proxy popup was not confirmed within timeout."
            ) from exc

        _check_shutdown(stop_event)

        activation_id, phone_number = rent_number_with_retry(sms_api)
        register_active_activation(activation_id, phone_number, device_id)
        LOGGER.info(
            "Rented number %s (activation_id=%s) for cycle %s on %s",
            phone_number,
            activation_id,
            cycle_index,
            device_id,
        )

        phone_digits = re.sub(r"\D", "", phone_number)
        country_guess = f"+{phone_digits[:-10]}" if len(phone_digits) > 10 else None

        device.launch_telegram()
        device.input_phone(phone_number=phone_number, country_code=country_guess)

        sms_code = wait_sms_code(sms_api, activation_id=activation_id)
        if not sms_code:
            raise RuntimeError("SMS code not received from provider.")

        _check_shutdown(stop_event)

        device.input_code(sms_code)
        maybe_handle_email_step(device=device, email_api=email_api)
        maybe_fill_profile_step(device=device, last_names=last_names)

        session_path = session_generator.generate_session(
            phone_number=phone_number,
            device_controller=device,
            proxy_dict=proxy_dict,
        )
        LOGGER.info("Session generated for %s: %s", phone_number, session_path)

        safe_set_activation_done(sms_api=sms_api, activation_id=activation_id)
        remove_activation_from_json(activation_id)
        unregister_active_activation(activation_id)
        activation_id = ""

        LOGGER.info("Cycle %s/%s completed on device %s", cycle_index, total_cycles, device_id)
        cycle_success = True
    except ShutdownRequested:
        LOGGER.info("Cycle %s interrupted by shutdown", cycle_index)
    except Exception as exc:
        if _is_routine_cycle_error(exc):
            LOGGER.exception(
                "Cycle %s/%s failed with routine error on device %s",
                cycle_index,
                total_cycles,
                device_id or "unknown",
            )
        else:
            LOGGER.exception(
                "Cycle %s/%s failed with unexpected/UI error on device %s",
                cycle_index,
                total_cycles,
                device_id or "unknown",
            )
        maybe_send_cycle_error_alert(
            notifier=notifier,
            cycle_index=cycle_index,
            total_cycles=total_cycles,
            device_id=device_id,
            device=device,
            exc=exc,
            error_trace=traceback.format_exc(),
        )
    finally:
        if activation_id:
            try:
                cancel_activation_safe(sms_api, activation_id, force=stop_event.is_set())
            except Exception:
                LOGGER.exception(
                    "Failed to cancel activation %s for failed cycle %s",
                    activation_id,
                    cycle_index,
                )

        try:
            controller = docker_controller
            if controller is None:
                controller, _ = build_docker_controller_from_config()
            LOGGER.info(
                "Cycle %s/%s final cleanup: stopping Docker Android container with volume wipe",
                cycle_index,
                total_cycles,
            )
            controller.stop_container()
        except Exception:
            LOGGER.exception(
                "Cycle %s/%s failed to stop Docker container in finally",
                cycle_index,
                total_cycles,
            )

        if device_id:
            device_pool.release(device_id)

    return CycleResult(
        index=cycle_index,
        success=cycle_success,
        device_id=device_id or "unknown",
        phone_number=phone_number,
    )


def install_signal_handlers(stop_event: threading.Event) -> None:
    def _handler(signum, _frame) -> None:
        if stop_event.is_set():
            return
        LOGGER.warning("Received signal %s. Starting graceful shutdown.", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handler)


def cancel_remaining_activations(sms_api: SmsApi) -> None:
    with ACTIVE_ACTIVATIONS_LOCK:
        pending = list(ACTIVE_ACTIVATIONS.items())

    if not pending:
        return

    LOGGER.warning("Cancelling %s active activations before exit", len(pending))

    for activation_id, metadata in pending:
        LOGGER.info(
            "Cancelling activation %s for %s on %s",
            activation_id,
            metadata.get("phone_number", "unknown"),
            metadata.get("device_id", "unknown"),
        )
        cancel_activation_safe(sms_api=sms_api, activation_id=activation_id, force=True)


def run(notifier: Optional[TelegramNotifier] = None) -> int:
    args = parse_args()
    setup_logging()
    runtime_notifier = notifier or build_telegram_notifier()

    if args.count < 1:
        raise ValueError("--count must be >= 1")

    install_signal_handlers(STOP_EVENT)

    devices = resolve_devices(args)
    workers = resolve_workers(args, devices_count=len(devices))
    if workers > 1:
        LOGGER.warning(
            "Docker hard-reset mode is enabled with %s workers. "
            "Shared docker-compose lifecycle can conflict in parallel runs.",
            workers,
        )
    shared_sms_api = build_sms_api()
    shared_proxy_api = ProxyApi()

    LOGGER.info(
        "Initialized shared API clients for workers: SmsApi(service=%s), ProxyApi(stateful-rotation).",
        getattr(shared_sms_api, "service_name", "unknown"),
    )
    LOGGER.info("Loaded %s devices", len(devices))

    device_pool = DevicePool(devices)
    last_names = load_profile_names()

    success_count = 0
    futures: list[Future[CycleResult]] = []

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="reg-worker") as executor:
        for cycle_index in range(1, args.count + 1):
            futures.append(
                executor.submit(
                    run_single_cycle,
                    cycle_index,
                    args.count,
                    stop_event=STOP_EVENT,
                    device_pool=device_pool,
                    proxy_api=shared_proxy_api,
                    sms_api=shared_sms_api,
                    last_names=last_names,
                    notifier=runtime_notifier,
                )
            )

        pending: set[Future[CycleResult]] = set(futures)
        while pending:
            done, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
            for future in done:
                if future.cancelled():
                    continue
                try:
                    result = future.result()
                except ShutdownRequested:
                    continue
                except Exception:
                    LOGGER.exception("Worker future failed unexpectedly")
                    continue
                if result.success:
                    success_count += 1

            if STOP_EVENT.is_set() and pending:
                for future in list(pending):
                    if future.cancel():
                        pending.remove(future)

    cancel_remaining_activations(shared_sms_api)

    LOGGER.info("Flow finished. Success: %s/%s", success_count, args.count)
    return 0 if success_count == args.count else 1


if __name__ == "__main__":
    notifier = build_telegram_notifier()
    try:
        sys.exit(run(notifier=notifier))
    except Exception:
        error_trace = traceback.format_exc()
        LOGGER.error("Fatal unhandled error in cli_main:\n%s", error_trace)

        if notifier:
            screenshot_path = None
            try:
                alert_device_id = resolve_alert_device_id()
                screenshot_path = take_alert_screenshot(alert_device_id, reason="fatal_error")
            except Exception:
                LOGGER.exception("Failed to capture screenshot for fatal alert.")

            with ALERT_SEND_LOCK:
                notifier.send_error_alert(
                    error_message=f"Auto-regger fatal error:\n{error_trace}",
                    screenshot_path=screenshot_path,
                )

        sys.exit(1)
