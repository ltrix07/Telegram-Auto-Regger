from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from telethon import TelegramClient
from telethon.errors import FloodWaitError, SessionPasswordNeededError

from .utils import PROJECT_ROOT

try:
    import socks  # type: ignore
except Exception:  # pragma: no cover - runtime fallback for environments without PySocks
    socks = None


LOGGER = logging.getLogger(__name__)

OFFICIAL_ANDROID_API_ID = 6
OFFICIAL_ANDROID_API_HASH = "eb06d4abfb49dc3eeb1aeb98ae0f581e"
DEFAULT_ANDROID_APP_VERSION = "11.8.3"


class SessionGenerator:
    """
    Build Telethon `.session` files using code delivery to an already logged-in
    Telegram Android app.

    Workflow:
      1. Telethon sends a login code request.
      2. Telegram app receives code in the system chat.
      3. DeviceController opens that chat and parses code from UI XML dump.
      4. Telethon completes sign-in and writes a persistent session file.
    """

    def __init__(
        self,
        api_id: Optional[int] = None,
        api_hash: Optional[str] = None,
        sessions_dir: str | Path = "sessions",
        device_model: Optional[str] = None,
        system_version: Optional[str] = None,
        app_version: Optional[str] = None,
        system_lang_code: str = "en",
        lang_code: str = "en",
        code_delivery_wait_seconds: float = 8.0,
    ) -> None:
        """
        :param api_id: Telegram API ID.
        :param api_hash: Telegram API hash.
        :param sessions_dir: Directory where `.session` files are stored.
        :param device_model: Android device model for Telethon metadata.
        :param system_version: Android system version for Telethon metadata.
        :param app_version: Telegram Android app version for Telethon metadata.
        :param system_lang_code: System language code sent to Telegram.
        :param lang_code: App language code sent to Telegram.
        :param code_delivery_wait_seconds: Pause before reading code from device.
        """
        normalized_api_id = int(api_id) if api_id else None
        normalized_api_hash = str(api_hash).strip() if api_hash else ""
        if (
            (normalized_api_id and normalized_api_id != OFFICIAL_ANDROID_API_ID)
            or (normalized_api_hash and normalized_api_hash != OFFICIAL_ANDROID_API_HASH)
        ):
            LOGGER.warning(
                "Overriding custom Telethon api_id/api_hash with official Telegram Android keys."
            )

        self.api_id = OFFICIAL_ANDROID_API_ID
        self.api_hash = OFFICIAL_ANDROID_API_HASH
        self.sessions_dir = Path(sessions_dir)
        if not self.sessions_dir.is_absolute():
            self.sessions_dir = PROJECT_ROOT / self.sessions_dir

        self.device_model = str(device_model or "").strip()
        self.system_version = str(system_version or "").strip()
        self.device_app_version = str(app_version or "").strip()
        self.default_app_version = DEFAULT_ANDROID_APP_VERSION
        self.system_lang_code = system_lang_code
        self.lang_code = lang_code
        self.code_delivery_wait_seconds = float(code_delivery_wait_seconds)

        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def generate_session(
        self,
        phone_number: str,
        device_controller: Any,
        proxy_dict: Optional[Dict[str, Any]],
    ) -> Path:
        """
        Generate and persist a Telethon session for a phone number.

        :param phone_number: E.164-like phone string (e.g. `+15551234567`).
        :param device_controller: DeviceController with ADB helpers.
        :param proxy_dict: Proxy dict with keys `type`, `host`, `port`,
                           optionally `username`, `password`.
        :return: Path to generated `.session` file.
        """
        return asyncio.run(
            self._generate_session_async(
                phone_number=phone_number,
                device_controller=device_controller,
                proxy_dict=proxy_dict,
            )
        )

    async def _generate_session_async(
        self,
        phone_number: str,
        device_controller: Any,
        proxy_dict: Optional[Dict[str, Any]],
    ) -> Path:
        phone_digits = re.sub(r"\D", "", str(phone_number))
        if not phone_digits:
            raise ValueError(f"Invalid phone number: {phone_number!r}")

        session_file = self.sessions_dir / f"{phone_digits}.session"
        fingerprint_file = self._fingerprint_path(session_file)
        stored_fingerprint = self._load_fingerprint(fingerprint_file)
        live_fingerprint = self._safe_collect_fingerprint(device_controller)
        explicit_fingerprint = {
            "device_model": self.device_model,
            "system_version": self.system_version,
            "app_version": self.device_app_version,
        }
        device_info = self._resolve_device_metadata(
            explicit_fingerprint=explicit_fingerprint,
            stored_fingerprint=stored_fingerprint,
            live_fingerprint=live_fingerprint,
        )
        proxy_tuple = self._build_proxy(proxy_dict)

        LOGGER.info(
            "Generating Telethon session for %s using model=%s system=%s app=%s",
            phone_number,
            device_info.get("device_model"),
            device_info.get("system_version"),
            device_info.get("app_version"),
        )

        client = TelegramClient(
            session=str(session_file),
            api_id=self.api_id,
            api_hash=self.api_hash,
            device_model=str(device_info.get("device_model", "Android")),
            system_version=str(device_info.get("system_version", "Android")),
            app_version=str(device_info.get("app_version", self.default_app_version)),
            system_lang_code=self.system_lang_code,
            lang_code=self.lang_code,
            proxy=proxy_tuple,
        )

        try:
            await client.connect()
            if await client.is_user_authorized():
                LOGGER.info("Session already authorized for %s", phone_number)
                if not stored_fingerprint and live_fingerprint:
                    self._save_fingerprint(
                        fingerprint_file=fingerprint_file,
                        phone_number=phone_number,
                        session_file=session_file,
                        fingerprint=live_fingerprint,
                    )
                return session_file

            sent = await client.send_code_request(phone_number)
            LOGGER.info("Telethon login code requested for %s", phone_number)

            await asyncio.sleep(self.code_delivery_wait_seconds)
            device_controller.open_telegram_system_chat()
            raw_code = device_controller.read_telegram_code_from_screen()
            code_digits = re.sub(r"\D", "", str(raw_code or ""))
            if len(code_digits) != 5:
                raise RuntimeError(
                    f"Telegram login code was not detected or invalid: {raw_code!r}"
                )

            await client.sign_in(
                phone=phone_number,
                code=code_digits,
                phone_code_hash=sent.phone_code_hash,
            )
            final_fingerprint = self._safe_collect_fingerprint(device_controller) or live_fingerprint or device_info
            self._save_fingerprint(
                fingerprint_file=fingerprint_file,
                phone_number=phone_number,
                session_file=session_file,
                fingerprint=final_fingerprint,
            )
            LOGGER.info("Telethon sign-in completed for %s", phone_number)
            return session_file
        except SessionPasswordNeededError as exc:
            LOGGER.error("2FA password required for %s; cannot auto-complete session", phone_number)
            raise RuntimeError("Session generation failed: account requires 2FA password.") from exc
        except FloodWaitError as exc:
            LOGGER.error("Telethon FloodWait for %s: wait %ss", phone_number, exc.seconds)
            raise RuntimeError(
                f"Session generation failed: FloodWaitError ({exc.seconds}s)."
            ) from exc
        finally:
            await client.disconnect()

    @staticmethod
    def _build_proxy(proxy_dict: Optional[Dict[str, Any]]) -> Optional[Tuple[Any, ...]]:
        """
        Convert proxy dictionary into Telethon/PySocks tuple.

        Expected mapping:
          - type: `http` | `socks4` | `socks5`
          - host: proxy host/IP
          - port: proxy port
          - username: optional
          - password: optional
        """
        if not proxy_dict:
            return None

        proxy_type = str(proxy_dict.get("type", "")).lower()
        host = str(proxy_dict.get("host", "")).strip()
        port = int(proxy_dict.get("port", 0) or 0)
        username = str(proxy_dict.get("username", "")).strip() or None
        password = str(proxy_dict.get("password", "")).strip() or None

        proxy_map = {
            "http": socks.HTTP if socks else 3,
            "socks4": socks.SOCKS4 if socks else 1,
            "socks5": socks.SOCKS5 if socks else 2,
        }
        if proxy_type not in proxy_map:
            raise ValueError(f"Unsupported proxy type for Telethon: {proxy_type}")
        if not host or not port:
            raise ValueError(f"Invalid proxy payload: {proxy_dict!r}")

        return proxy_map[proxy_type], host, port, True, username, password

    def _safe_collect_fingerprint(self, device_controller: Any) -> Dict[str, str]:
        try:
            if hasattr(device_controller, "get_device_fingerprint"):
                payload = device_controller.get_device_fingerprint()
            else:
                payload = device_controller.get_device_info()
        except Exception:
            LOGGER.exception("Failed to collect device fingerprint from ADB")
            return {}

        return {
            "device_model": str(payload.get("device_model", "")).strip(),
            "system_version": str(payload.get("system_version", "")).strip(),
            "app_version": str(payload.get("app_version", "")).strip(),
        }

    def _resolve_device_metadata(
        self,
        explicit_fingerprint: Dict[str, str],
        stored_fingerprint: Dict[str, str],
        live_fingerprint: Dict[str, str],
    ) -> Dict[str, str]:
        merged = {
            "device_model": "",
            "system_version": "",
            "app_version": "",
        }
        for source in (explicit_fingerprint, live_fingerprint, stored_fingerprint):
            for key in merged:
                value = str(source.get(key, "")).strip()
                if value:
                    merged[key] = value

        if not merged["device_model"]:
            merged["device_model"] = "Android"
        if not merged["system_version"]:
            merged["system_version"] = "Android"
        if not merged["app_version"]:
            merged["app_version"] = self.default_app_version
        return merged

    @staticmethod
    def _fingerprint_path(session_file: Path) -> Path:
        return session_file.with_suffix(session_file.suffix + ".fingerprint.json")

    def _load_fingerprint(self, fingerprint_file: Path) -> Dict[str, str]:
        if not fingerprint_file.exists():
            return {}

        try:
            data = json.loads(fingerprint_file.read_text(encoding="utf-8"))
            fingerprint = data.get("device_fingerprint", {})
            if not isinstance(fingerprint, dict):
                return {}
            return {
                "device_model": str(fingerprint.get("device_model", "")).strip(),
                "system_version": str(fingerprint.get("system_version", "")).strip(),
                "app_version": str(fingerprint.get("app_version", "")).strip(),
            }
        except Exception:
            LOGGER.exception("Failed to read fingerprint file %s", fingerprint_file)
            return {}

    def _save_fingerprint(
        self,
        *,
        fingerprint_file: Path,
        phone_number: str,
        session_file: Path,
        fingerprint: Dict[str, str],
    ) -> None:
        payload = {
            "phone_number": phone_number,
            "session_file": str(session_file),
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "device_fingerprint": {
                "device_model": str(fingerprint.get("device_model", "")).strip(),
                "system_version": str(fingerprint.get("system_version", "")).strip(),
                "app_version": str(fingerprint.get("app_version", "")).strip(),
            },
        }
        fingerprint_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
