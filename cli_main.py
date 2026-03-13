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
from auto_reger.email_api import EmailApi, EmailAuthorizationError, NoEmailsLeftError
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
    "sms status polling failed",
    "sms code not received",
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
    "number is already registered",
    "already linked to telegram",
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
    parser.add_argument(
        "--root",
        action="store_true",
        help="Enable root access requirement for ADB commands.",
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


def build_email_api() -> EmailApi:
    email_cfg = CONFIG.get("email_api", {})
    emails_file_raw = str(email_cfg.get("emails_file", "emails.txt")).strip() or "emails.txt"
    resolved_emails_file = resolve_project_path(emails_file_raw)

    poll_interval_raw = email_cfg.get("poll_interval_seconds", 7.0)
    try:
        poll_interval_seconds = float(poll_interval_raw or 7.0)
    except (TypeError, ValueError):
        LOGGER.error(
            "Invalid email_api.poll_interval_seconds=%r, fallback to 7.0",
            poll_interval_raw,
        )
        poll_interval_seconds = 7.0

    imap_timeout_raw = email_cfg.get("imap_timeout_seconds", 20)
    try:
        imap_timeout_seconds = int(imap_timeout_raw or 20)
    except (TypeError, ValueError):
        LOGGER.error(
            "Invalid email_api.imap_timeout_seconds=%r, fallback to 20",
            imap_timeout_raw,
        )
        imap_timeout_seconds = 20

    max_messages_raw = email_cfg.get("max_messages_to_scan", 30)
    try:
        max_messages_to_scan = int(max_messages_raw or 30)
    except (TypeError, ValueError):
        LOGGER.error(
            "Invalid email_api.max_messages_to_scan=%r, fallback to 30",
            max_messages_raw,
        )
        max_messages_to_scan = 30

    LOGGER.info("Initializing local EmailApi with emails file: %s", resolved_emails_file)
    return EmailApi(
        emails_file=str(resolved_emails_file),
        poll_interval_seconds=poll_interval_seconds,
        imap_timeout_seconds=imap_timeout_seconds,
        max_messages_to_scan=max_messages_to_scan,
    )


def build_session_generator(
    device_fingerprint: Optional[Dict[str, str]] = None,
) -> SessionGenerator:
    telethon_cfg = CONFIG.get("telethon", {})
    system_lang_code = str(telethon_cfg.get("system_lang_code", "en")).strip() or "en"
    lang_code = str(telethon_cfg.get("lang_code", "en")).strip() or "en"
    device_model: Optional[str] = None
    system_version: Optional[str] = None
    app_version: Optional[str] = None
    if device_fingerprint:
        device_model = str(device_fingerprint.get("device_model", "")).strip() or None
        system_version = str(device_fingerprint.get("system_version", "")).strip() or None
        app_version = str(device_fingerprint.get("app_version", "")).strip() or None

    return SessionGenerator(
        api_id=6,
        api_hash="eb06d4abfb49dc3eeb1aeb98ae0f581e",
        sessions_dir=Path("sessions"),
        device_model=device_model,
        system_version=system_version,
        app_version=app_version,
        system_lang_code=system_lang_code,
        lang_code=lang_code,
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
    video_path: Optional[str] = None,
    screenshot_path: Optional[str] = None,
    screenshot_failed: bool = False,
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

    resolved_screenshot_path = screenshot_path
    screenshot_capture_failed = screenshot_failed
    if resolved_screenshot_path is None and not screenshot_capture_failed:
        try:
            if device:
                path = _build_debug_screenshot_path(device.device_id, reason=f"cycle_{cycle_index}_alert")
                if device.take_screenshot(str(path)):
                    resolved_screenshot_path = str(path)
                else:
                    screenshot_capture_failed = True
            elif device_id:
                resolved_screenshot_path = take_alert_screenshot(
                    device_id=device_id,
                    reason=f"cycle_{cycle_index}_alert",
                )
                screenshot_capture_failed = resolved_screenshot_path is None
            else:
                screenshot_capture_failed = True
        except Exception:
            screenshot_capture_failed = True
            LOGGER.exception("Failed to capture cycle alert screenshot on %s", device_id or "unknown")

    alert_text = (
        f"Cycle {cycle_index}/{total_cycles} unexpected failure on {device_id or 'unknown'}\n"
        f"Error type: {type(exc).__name__}\n"
        f"Message: {exc}\n\n"
        f"Traceback:\n{error_trace}"
    )
    if screenshot_capture_failed and not resolved_screenshot_path:
        alert_text += "\n[System]: Screenshot failed (Device Offline)"

    if video_path:
        with ALERT_SEND_LOCK:
            sent_video = notifier.send_video_alert(
                error_message=alert_text,
                video_path=video_path,
            )
        if sent_video:
            LOGGER.info(
                "Cycle video alert sent to Telegram for cycle %s on %s",
                cycle_index,
                device_id or "unknown",
            )
            alert_text = "Screenshot for the error above:"
            if screenshot_capture_failed and not resolved_screenshot_path:
                alert_text += " [System]: Screenshot failed (Device Offline)"
        else:
            LOGGER.warning(
                "Cycle video alert delivery failed for cycle %s on %s. Falling back to screenshot alert.",
                cycle_index,
                device_id or "unknown",
            )

    with ALERT_SEND_LOCK:
        sent = notifier.send_error_alert(
            error_message=alert_text,
            screenshot_path=resolved_screenshot_path,
        )
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

    email_screen_patterns = (
        "check your email",
        "email",
        "e-mail",
        "mail",
        "почт",
        "address" # Добавил на всякий случай
    )
    email_resource_markers = (
        "email_field",
        "login_email_field",
        ":id/email",
    )

    LOGGER.info("Checking if email step is required (waiting for screen to render)...")
    email_step_detected = False
    
    # Делаем 5 попыток с паузой в 3 секунды, чтобы дождаться загрузки экрана Telegram
    for attempt in range(5):
        email_step_detected = device._screen_contains_candidates(email_screen_patterns)
        
        if not email_step_detected:
            try:
                ui_xml = device._dump_ui_xml().lower()
                email_step_detected = any(marker in ui_xml for marker in email_resource_markers)
            except Exception:
                LOGGER.debug("Email step pre-check XML dump failed", exc_info=True)
                
        if email_step_detected:
            break # Экран найден, выходим из цикла ожидания
            
        time.sleep(3) # Ждем 3 секунды перед следующей попыткой

    if not email_step_detected:
        LOGGER.info("Email step is not required by Telegram, skipping...")
        
        # --- ДОБАВЬ ЭТОТ БЛОК ДЛЯ ДЕБАГА ---
        try:
            stamp = int(time.time())
            device.take_screenshot(f"/app/debug_email_skip_{stamp}.png")
            with open(f"/app/debug_email_skip_{stamp}.xml", "w", encoding="utf-8") as f:
                f.write(device._dump_ui_xml())
            LOGGER.info(f"Saved debug screenshot and XML to /app/debug_email_skip_{stamp}")
        except Exception as e:
            LOGGER.error(f"Failed to save debug info: {e}")
        # -----------------------------------
        
        return

    LOGGER.info("Email challenge detected. Taking local email credentials from file.")
    email_address: Optional[str] = None
    try:
        email_address, email_password = email_api.get_email()
        LOGGER.info("Inputting email to Telegram: %s", email_address)
        device.input_email(email_address)
        email_code = email_api.wait_for_email_code(email_address=email_address, password=email_password, timeout=120)
        LOGGER.info("Submitting email verification code for %s", email_address)
        device.input_code(email_code)
    except NoEmailsLeftError as exc:
        LOGGER.error("No emails left: %s", exc)
        return
    except EmailAuthorizationError as exc:
        if email_address:
            LOGGER.error("IMAP authorization failed for %s: %s", email_address, exc)
        else:
            LOGGER.error("IMAP authorization failed: %s", exc)
        raise RuntimeError(f"Email step failed: {exc}") from exc
    except Exception:
        LOGGER.exception("Failed to complete Telegram email verification step.")
        raise


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
    max_price_raw = sms_cfg.get("max_price", registration_cfg.get("default_max_price"))
    try:
        max_price = float(max_price_raw) if max_price_raw is not None else None
    except (TypeError, ValueError):
        LOGGER.warning(
            "Invalid max price in config (sms_api.max_price=%r), falling back to registration.default_max_price",
            max_price_raw,
        )
        fallback_price = registration_cfg.get("default_max_price")
        try:
            max_price = float(fallback_price) if fallback_price is not None else None
        except (TypeError, ValueError):
            LOGGER.warning(
                "Invalid registration.default_max_price=%r. Sending request without max price limit.",
                fallback_price,
            )
            max_price = None

    country_id_raw = sms_cfg.get("country_id")
    country_id: Optional[int] = None
    if country_id_raw not in (None, ""):
        try:
            country_id = int(country_id_raw)
        except (TypeError, ValueError):
            LOGGER.warning(
                "Invalid sms_api.country_id=%r. Falling back to country name resolution for %s.",
                country_id_raw,
                country,
            )

    LOGGER.info(
        "Requesting number with ID %s and limit $%s.",
        country_id if country_id is not None else "auto",
        f"{max_price:.2f}" if max_price is not None else "none",
    )

    def _rent_number() -> Tuple[str, str]:
        payload = sms_api.verification_number(
            service=telegram_service_code,
            country=country,
            max_price=max_price,
            country_id=country_id,
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
    boot_timeout_raw = docker_cfg.get("boot_timeout_seconds", 90)

    try:
        boot_timeout_seconds = int(boot_timeout_raw)
    except (TypeError, ValueError):
        LOGGER.warning(
            "Invalid docker.boot_timeout_seconds=%r; using default 90",
            boot_timeout_raw,
        )
        boot_timeout_seconds = 90

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
    require_root: bool,
) -> CycleResult:
    device_id = ""
    device: Optional[DeviceController] = None
    docker_controller: Optional[DockerAndroidController] = None
    email_api: Optional[EmailApi] = None
    session_generator: Optional[SessionGenerator] = None
    record_proc = None
    remote_video_path = "/sdcard/cycle_record.mp4"
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
        try:
            docker_controller.install_apk(device_id)
        except Exception:
            LOGGER.critical(
                "Cycle %s/%s failed: Telegram APK installation failed on %s. Aborting cycle.",
                cycle_index,
                total_cycles,
                device_id,
                exc_info=True,
            )
            return CycleResult(
                index=cycle_index,
                success=False,
                device_id=device_id or "unknown",
                phone_number=phone_number,
            )
        LOGGER.info(
            "Cycle %s/%s Android container is fully booted on %s",
            cycle_index,
            total_cycles,
            device_id,
        )

        device = DeviceController(device_id=device_id, require_root=require_root)
        email_api = build_email_api()

        device.connect()
        if not device.is_ready():
            raise RuntimeError(f"Device {device_id} is not ready for registration.")
        device.hide_root()
        record_proc = device.start_recording(remote_video_path)

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

        device.launch_telegram()

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
            device.enable_telegram_proxy_popup(timeout=15.0)
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

        device.input_phone(phone_number=phone_number, country_code=country_guess)
        maybe_handle_email_step(device=device, email_api=email_api)

        sms_code = wait_sms_code(sms_api, activation_id=activation_id)
        if not sms_code:
            raise RuntimeError("SMS code not received from provider.")

        _check_shutdown(stop_event)

        device.input_code(sms_code)
        maybe_fill_profile_step(device=device, last_names=last_names)

        device.restore_root()

        LOGGER.info("Starting Telethon post-registration login for %s", phone_number)
        device_fingerprint: Dict[str, str] = {}
        try:
            if hasattr(device, "get_device_fingerprint"):
                device_fingerprint = device.get_device_fingerprint()
            else:
                device_fingerprint = device.get_device_info()
        except Exception:
            LOGGER.exception(
                "Failed to collect device fingerprint for Telethon login on %s",
                device_id,
            )
            device_fingerprint = {}

        session_generator = build_session_generator(device_fingerprint=device_fingerprint)
        try:
            session_path = session_generator.generate_session(
                phone_number=phone_number,
                device_controller=device,
                proxy_dict=proxy_dict,
            )
            LOGGER.info("Telethon session saved for %s at %s", phone_number, session_path)
        except Exception:
            LOGGER.exception("Telethon login failed for %s", phone_number)
            raise

        safe_set_activation_done(sms_api=sms_api, activation_id=activation_id)
        remove_activation_from_json(activation_id)
        unregister_active_activation(activation_id)
        activation_id = ""

        LOGGER.info("Cycle %s/%s completed on device %s", cycle_index, total_cycles, device_id)
        cycle_success = True
    except ShutdownRequested:
        LOGGER.info("Cycle %s interrupted by shutdown", cycle_index)
    except Exception as exc:
        video_path: Optional[str] = None
        if record_proc and device:
            local_video = _build_debug_screenshot_path(
                device.device_id,
                reason=f"cycle_{cycle_index}_video",
            ).with_suffix(".mp4")
            if device.stop_recording_and_pull(record_proc, remote_video_path, str(local_video)):
                video_path = str(local_video)

        screenshot_path: Optional[str] = None
        screenshot_failed = False
        try:
            if device:
                path = _build_debug_screenshot_path(device.device_id, reason=f"cycle_{cycle_index}_alert")
                if device.take_screenshot(str(path)):
                    screenshot_path = str(path)
                else:
                    screenshot_failed = True
            elif device_id:
                screenshot_path = take_alert_screenshot(
                    device_id=device_id,
                    reason=f"cycle_{cycle_index}_alert",
                )
                screenshot_failed = screenshot_path is None
            else:
                screenshot_failed = True
        except Exception:
            screenshot_failed = True
            LOGGER.exception("Failed to capture cycle error screenshot on %s", device_id or "unknown")

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
            video_path=video_path,
            screenshot_path=screenshot_path,
            screenshot_failed=screenshot_failed,
        )
    finally:
        if cycle_success and record_proc:
            try:
                if record_proc.poll() is None:
                    record_proc.kill()
            except Exception:
                LOGGER.exception(
                    "Failed to stop cycle screenrecord process for cycle %s on %s",
                    cycle_index,
                    device_id or "unknown",
                )
            if device:
                try:
                    device._adb("shell", "rm", "-f", remote_video_path, check=False, timeout=5)
                except Exception:
                    LOGGER.exception(
                        "Failed to cleanup remote cycle video on %s",
                        device.device_id,
                    )

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
    require_root = args.root

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
    attempt_count = 0
    max_attempts = args.count * 15

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="reg-worker") as executor:
        pending: set[Future[CycleResult]] = set()

        def _spawn_needed() -> None:
            nonlocal attempt_count
            if STOP_EVENT.is_set():
                return

            needed = args.count - success_count
            active = len(pending)

            while (
                needed > active
                and active < workers
                and attempt_count < max_attempts
                and not STOP_EVENT.is_set()
            ):
                attempt_count += 1
                pending.add(
                    executor.submit(
                        run_single_cycle,
                        attempt_count,
                        max_attempts,
                        stop_event=STOP_EVENT,
                        device_pool=device_pool,
                        proxy_api=shared_proxy_api,
                        sms_api=shared_sms_api,
                        last_names=last_names,
                        notifier=runtime_notifier,
                        require_root=require_root,
                    )
                )
                active += 1

        _spawn_needed()
        while pending:
            done, _ = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
            for future in done:
                pending.discard(future)
                if future.cancelled():
                    continue
                try:
                    result = future.result()
                except ShutdownRequested:
                    continue
                except Exception:
                    LOGGER.exception("Worker future failed unexpectedly")
                    if success_count < args.count:
                        _spawn_needed()
                    continue
                if result.success is True:
                    success_count += 1
                elif success_count < args.count:
                    _spawn_needed()

            if STOP_EVENT.is_set() and pending:
                for future in list(pending):
                    if future.cancel():
                        pending.remove(future)

            if not STOP_EVENT.is_set() and success_count < args.count:
                _spawn_needed()

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
