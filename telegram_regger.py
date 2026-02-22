import asyncio
import logging
import os
import random
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from selenium.webdriver.common.by import By
from telethon import TelegramClient
from telethon.errors import PhoneCodeEmptyError, PhoneCodeExpiredError, PhoneCodeInvalidError
from telethon.tl.functions.messages import GetDialogsRequest

from auto_reger.adb import get_device_info
from auto_reger.emulator import Telegram
from auto_reger.sms_api import (
    SmsApi,
    can_set_status_8,
    remove_activation_from_json,
    save_activation_to_json,
)
from auto_reger.utils import (
    CONFIG,
    get_config_value,
    kill_emulator,
    load_names,
    resolve_project_path,
    read_json,
    write_json,
)
from auto_reger.windows_automation import Onion, TelegramDesktop, VPN, VPNConnectionError


logging.basicConfig(
    filename=str(get_config_value(["logging", "file"], "telegram_regger.log")),
    level=getattr(logging, str(get_config_value(["logging", "level"], "INFO")).upper(), logging.INFO),
    format="%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(funcName)s() - %(message)s",
    encoding="utf-8",
)
LOGGER = logging.getLogger(__name__)


PROJECT_ROOT = Path(__file__).resolve().parent
CECH_PATH = resolve_project_path(str(get_config_value(["paths", "cech_file"], "cech.json")))
SESSIONS_DIR = resolve_project_path(str(get_config_value(["paths", "sessions_converted_dir"], "sessions/converted")))
TELETHON_SESSIONS_DIR = resolve_project_path(
    str(get_config_value(["paths", "telethon_sessions_dir"], "sessions/telethon"))
)
LAST_NAMES_FILE = resolve_project_path(str(get_config_value(["paths", "last_names_file"], "last_names.txt")))
TELEGRAM_DESKTOP_ACCOUNTS_DIR = resolve_project_path(
    str(get_config_value(["paths", "telegram_desktop_accounts_dir"], "sessions/tg_desk"))
)
TELEGRAM_DESKTOP_EXE = resolve_project_path(
    str(get_config_value(["paths", "telegram_desktop_executable"], "Telegram.exe"))
)
TELEGRAM_DESKTOP_DATA_DIR = Path(
    os.path.expandvars(str(get_config_value(["paths", "telegram_desktop_data_dir"], r"%LOCALAPPDATA%\Telegram Desktop")))
)

NEED_CHANGE_LOCATION = False


class RegistrationError(RuntimeError):
    """Base registration exception."""


class CriticalRegistrationError(RegistrationError):
    """Critical failure that should stop current registration attempt."""


class SessionCreationError(CriticalRegistrationError):
    """Telethon session creation failed."""


def _prompt_country() -> str:
    default_country = str(get_config_value(["registration", "default_country"], "USA"))
    if not bool(get_config_value(["registration", "prompt_for_country"], True)):
        return default_country
    user_country = input(f"Enter country for registration [{default_country}]: ").strip()
    return user_country or default_country


def _prompt_max_price() -> float:
    default_price = float(get_config_value(["registration", "default_max_price"], 10.0))
    if not bool(get_config_value(["registration", "prompt_for_max_price"], True)):
        return default_price

    user_value = input(f"Enter maximum price [{default_price}]: ").strip()
    if not user_value:
        return default_price
    try:
        return float(user_value)
    except ValueError:
        LOGGER.warning("Invalid max price input `%s`. Using default `%s`", user_value, default_price)
        return default_price


def setup_cech() -> Dict[str, Any]:
    """Load or initialize local registration metadata."""
    if CECH_PATH.is_file():
        try:
            return read_json(str(CECH_PATH))
        except Exception:
            LOGGER.exception("Failed to read `%s`, recreating file", CECH_PATH)
    cech_data: Dict[str, Any] = {}
    write_json(cech_data, str(CECH_PATH))
    return cech_data


async def perform_neutral_actions(client: TelegramClient) -> None:
    try:
        await client(
            GetDialogsRequest(
                offset_date=None,
                offset_id=0,
                offset_peer=None,
                limit=10,
                hash=0,
            )
        )
        anti_bot_min = float(get_config_value(["registration", "anti_bot_min_delay_seconds"], 2.0))
        anti_bot_max = float(get_config_value(["registration", "anti_bot_max_delay_seconds"], 5.0))
        await asyncio.sleep(random.uniform(anti_bot_min, anti_bot_max))
        LOGGER.info("Neutral dialogs action completed")
    except Exception:
        LOGGER.exception("Failed during neutral actions")


def save_session(
    phone_number: str,
    device_info: Dict[str, Any],
    number_price: float,
    session_path: str,
) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    session_dir = SESSIONS_DIR / today
    session_dir.mkdir(parents=True, exist_ok=True)

    session_data = {
        "phone_number": phone_number,
        "device_info": device_info,
        "number_price": number_price,
        "session_path": session_path,
        "registration_time": datetime.now().isoformat(),
    }

    session_file = session_dir / f"{phone_number}.json"
    write_json(session_data, str(session_file))
    LOGGER.info("Session data saved for %s", phone_number)


def activation_admin(sms_obj: SmsApi, last_names: list[str], phone_number: str, activation_id: str) -> Path:
    global NEED_CHANGE_LOCATION
    NEED_CHANGE_LOCATION = random.randint(0, 100) < 30
    random_last_name = random.choice(last_names) if last_names else "Unknown"

    cech_data = setup_cech()
    cech_data["last_name"] = random_last_name
    cech_data["phone_number"] = phone_number

    try:
        if can_set_status_8(activation_id):
            sms_obj.setStatus(activation_id, 8)
            remove_activation_from_json(activation_id)
            cech_data["activation_status"] = "canceled"
            cech_data["reason"] = "No code received or registration failed"
            LOGGER.info("Activation %s canceled", activation_id)
        else:
            cech_data["activation_status"] = "not_canceled"
            cech_data["reason"] = "Too early to cancel status=8"
            LOGGER.info("Activation %s cannot be canceled yet", activation_id)
    except Exception:
        cech_data["activation_status"] = "error"
        cech_data["reason"] = "activation_admin_exception"
        LOGGER.exception("Activation admin failed for %s", activation_id)

    write_json(cech_data, str(CECH_PATH))
    return CECH_PATH


def wait_for_sms_code(
    telegram: Telegram,
    timeout_seconds: int,
    poll_interval_seconds: float,
) -> Optional[str]:
    per_attempt_timeout = int(get_config_value(["timeouts", "sms_read_attempt_seconds"], 20))
    deadline = time.monotonic() + timeout_seconds

    while time.monotonic() < deadline:
        remaining = max(1, int(deadline - time.monotonic()))
        try:
            code = telegram.read_sms_with_code(timeout=min(remaining, per_attempt_timeout))
            if code:
                return code
        except Exception:
            LOGGER.exception("Error reading SMS code from Telegram app")
        time.sleep(poll_interval_seconds)

    return None


def create_tdata_with_telegram_desktop(phone_number: str, telegram: Telegram) -> Path:
    tg_desk_acc_dir = TELEGRAM_DESKTOP_ACCOUNTS_DIR / phone_number
    tg_desk_acc_dir.mkdir(parents=True, exist_ok=True)
    tg_app_path = tg_desk_acc_dir / "Telegram.exe"

    if TELEGRAM_DESKTOP_DATA_DIR.exists():
        shutil.rmtree(TELEGRAM_DESKTOP_DATA_DIR, ignore_errors=True)

    if not TELEGRAM_DESKTOP_EXE.exists():
        raise FileNotFoundError(f"Telegram Desktop executable not found: {TELEGRAM_DESKTOP_EXE}")

    shutil.copy(str(TELEGRAM_DESKTOP_EXE), str(tg_app_path))

    tg_desk = TelegramDesktop(str(tg_app_path))
    try:
        tg_desk.start_and_enter_number(phone_number)
        code_timeout = int(get_config_value(["timeouts", "sms_code_wait_seconds"], 120))
        poll_interval = float(get_config_value(["timeouts", "sms_code_poll_seconds"], 5.0))
        code = wait_for_sms_code(telegram, timeout_seconds=code_timeout, poll_interval_seconds=poll_interval)
        if not code:
            raise SessionCreationError("Unable to read SMS code for Telegram Desktop")
        tg_desk.enter_code(code)
    finally:
        tg_desk.close()
        subprocess.run(["taskkill", "/F", "/IM", "Telegram.exe"], capture_output=True, text=True, check=False)
        if tg_app_path.exists():
            tg_app_path.unlink()

    return tg_desk_acc_dir


async def create_session_with_telethon(phone_number: str, telegram: Telegram) -> Optional[str]:
    telethon_cfg = CONFIG.get("telethon", {})
    api_id = telethon_cfg.get("api_id")
    api_hash = telethon_cfg.get("api_hash")

    if not api_id or not api_hash:
        raise SessionCreationError("Missing `telethon.api_id` or `telethon.api_hash` in config.yaml")

    session_dir = TELETHON_SESSIONS_DIR / phone_number
    session_dir.mkdir(parents=True, exist_ok=True)
    session_path = session_dir / f"{phone_number}.session"

    if session_path.exists():
        LOGGER.info("Telethon session already exists for %s", phone_number)
        return str(session_path)

    client: Optional[TelegramClient] = None
    try:
        client = TelegramClient(
            session=str(session_path),
            api_id=int(api_id),
            api_hash=str(api_hash),
            device_model=str(telethon_cfg.get("device_model", "Desktop")),
            system_version=str(telethon_cfg.get("system_version", "Windows 10")),
            app_version=str(telethon_cfg.get("app_version", "4.0.4 x64")),
            system_lang_code=str(telethon_cfg.get("system_lang_code", "en")),
            lang_code=str(telethon_cfg.get("lang_code", "en")),
        )

        await client.connect()
        await client.send_code_request(phone_number)
        LOGGER.info("Telethon code requested for %s", phone_number)

        code_timeout = int(get_config_value(["timeouts", "telethon_code_wait_seconds"], 120))
        poll_interval = float(get_config_value(["timeouts", "sms_code_poll_seconds"], 5.0))
        code = wait_for_sms_code(telegram, timeout_seconds=code_timeout, poll_interval_seconds=poll_interval)
        if not code:
            LOGGER.error("No SMS code received for Telethon session creation (%s)", phone_number)
            return None

        await client.sign_in(phone_number, code)
        await perform_neutral_actions(client)
        LOGGER.info("Telethon session created successfully for %s", phone_number)
        return str(session_path)

    except (PhoneCodeInvalidError, PhoneCodeExpiredError, PhoneCodeEmptyError):
        LOGGER.exception("Invalid or expired Telethon code for %s", phone_number)
        return None
    except Exception:
        LOGGER.exception("Failed to create Telethon session for %s", phone_number)
        return None
    finally:
        if client:
            await client.disconnect()


def _enter_code_in_mobile_app(telegram: Telegram, code: str) -> None:
    code_input_xpath = str(
        get_config_value(["registration", "telegram_code_input_xpath"], "//android.widget.EditText")
    )
    code_field_timeout = int(get_config_value(["timeouts", "element_search_seconds"], 10))
    telegram.send_keys(By.XPATH, code_input_xpath, code, timeout=code_field_timeout)


def phone_number_send(
    sms_obj: SmsApi,
    phone_number: str,
    activation_id: str,
    telegram: Telegram,
    last_names: list[str],
    max_price: float,
) -> Optional[str]:
    try:
        telegram.input_phone_number(phone_number)

        if telegram.check_banned():
            LOGGER.warning("Number %s is banned", phone_number)
            activation_admin(sms_obj, last_names, phone_number, activation_id)
            return None

        if telegram.check_too_many_attempts():
            LOGGER.warning("Too many attempts for %s", phone_number)
            activation_admin(sms_obj, last_names, phone_number, activation_id)
            return None

        code_timeout = int(get_config_value(["timeouts", "sms_code_wait_seconds"], 120))
        poll_interval = float(get_config_value(["timeouts", "sms_code_poll_seconds"], 5.0))
        code = wait_for_sms_code(telegram, timeout_seconds=code_timeout, poll_interval_seconds=poll_interval)

        if not code:
            activation_admin(sms_obj, last_names, phone_number, activation_id)
            return None

        _enter_code_in_mobile_app(telegram, code)

        try:
            if last_names:
                first_name = f"user_{random.randint(1000, 9999)}"
                last_name = random.choice(last_names)
                telegram.registration_name_and_surname(first_name, last_name)
        except Exception:
            LOGGER.info("Name/surname step skipped for %s", phone_number)

        session_path = asyncio.run(create_session_with_telethon(phone_number, telegram))
        if not session_path:
            activation_admin(sms_obj, last_names, phone_number, activation_id)
            return None

        udid = str(get_config_value(["adb", "device_udid"], "")).strip()
        device_info = get_device_info(udid) if udid else {}
        save_session(
            phone_number=phone_number,
            device_info=device_info,
            number_price=max_price,
            session_path=session_path,
        )

        sms_obj.setStatus(activation_id, 6)
        remove_activation_from_json(activation_id)
        LOGGER.info("Number %s successfully activated (activation %s)", phone_number, activation_id)
        return session_path

    except Exception:
        LOGGER.exception("Error in phone_number_send for %s", phone_number)
        activation_admin(sms_obj, last_names, phone_number, activation_id)
        return None


def register_telegram_account(country: str, max_price: float) -> Optional[str]:
    vpn: Optional[VPN] = None
    onion: Optional[Onion] = None
    telegram: Optional[Telegram] = None

    try:
        vpn = VPN()
        vpn.connect()

        onion = Onion()
        kill_emulator()

        sms_cfg = CONFIG.get("sms_api", {})
        service_name = str(sms_cfg.get("service_name", "")).strip()
        api_key_path = str(sms_cfg.get("api_key_path", "")).strip()
        api_url = str(sms_cfg.get("api_url", "")).strip() or None
        telegram_service_code = str(sms_cfg.get("telegram_service_code", "tg"))

        if not service_name or not api_key_path:
            raise CriticalRegistrationError("`sms_api.service_name` and `sms_api.api_key_path` are required")

        sms_api = SmsApi(service_name, str(resolve_project_path(api_key_path)), api_url=api_url)
        number_data = sms_api.getNumber(service=telegram_service_code, country=country)
        if not number_data:
            raise CriticalRegistrationError("Failed to rent phone number from SMS provider")

        activation_id = str(number_data.get("id") or number_data.get("activation_id") or "")
        phone = str(number_data.get("phone") or number_data.get("number") or "")
        if not activation_id or not phone:
            raise CriticalRegistrationError(f"Unexpected number payload from SMS provider: {number_data}")

        save_activation_to_json(activation_id, phone)

        adb_cfg = CONFIG.get("adb", {})
        telegram = Telegram(
            udid=adb_cfg.get("device_udid"),
            appium_port=int(adb_cfg.get("appium_port", 4723)),
            emulator_path=get_config_value(["emulator", "executable_path"]),
            emulator_name=get_config_value(["emulator", "process_name"]),
            physical_device=bool(adb_cfg.get("physical_device", False)),
        )

        if LAST_NAMES_FILE.exists():
            last_names = load_names(str(LAST_NAMES_FILE))
        else:
            LOGGER.warning("Last names file not found: %s. Continuing with empty list.", LAST_NAMES_FILE)
            last_names = []
        return phone_number_send(sms_api, phone, activation_id, telegram, last_names, max_price)

    except VPNConnectionError as exc:
        LOGGER.exception("VPN connection failed")
        raise CriticalRegistrationError("VPN did not connect") from exc
    except Exception:
        LOGGER.exception("Global registration error")
        raise
    finally:
        if telegram:
            try:
                telegram.close()
            except Exception:
                LOGGER.exception("Failed to close Telegram emulator resources")

        if onion:
            try:
                onion.close()
            except Exception:
                LOGGER.exception("Failed to close Onion browser")

        if vpn:
            try:
                vpn.disconnect()
            except Exception:
                LOGGER.exception("Failed to disconnect VPN")
            try:
                vpn.close()
            except Exception:
                LOGGER.exception("Failed to close VPN process")


def main() -> None:
    country = _prompt_country()
    max_price = _prompt_max_price()

    while True:
        try:
            register_telegram_account(country=country, max_price=max_price)
        except CriticalRegistrationError:
            LOGGER.exception("Critical error during registration attempt")
        except Exception:
            LOGGER.exception("Unhandled registration exception")

        if not bool(get_config_value(["registration", "continue_prompt"], True)):
            break

        user_input = input("Register another? (y/n): ").strip().lower()
        if user_input != "y":
            break


if __name__ == "__main__":
    main()
