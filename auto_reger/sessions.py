from __future__ import annotations

import base64
import json
import logging
import random
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
import xml.etree.ElementTree as ET

from telethon.crypto import AuthKey
from telethon.errors import FloodError, PasswordHashInvalidError, SessionPasswordNeededError
from telethon.sessions import MemorySession, SQLiteSession
from telethon.sync import TelegramClient

try:
    from TGConvertor.manager import SessionManager  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    SessionManager = None

try:
    from AndroidTelePorter import AndroidSession  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    AndroidSession = None

from .device_controller import DeviceController
from .tdesktop import get_auth_key_and_dc_id
from .utils import CONFIG, PROJECT_ROOT, read_txt_list

logger = logging.getLogger(__name__)


DC_IPS: Dict[int, str] = {
    1: "149.154.175.50",
    2: "149.154.167.51",
    3: "149.154.175.100",
    4: "149.154.167.91",
    5: "91.108.56.130",
}


TELETHON_CONFIG: Dict[str, Any] = CONFIG.get("telethon", {})
API_ID: int = TELETHON_CONFIG.get("api_id")
API_HASH: str = TELETHON_CONFIG.get("api_hash")

TELEGRAM_CLIENT_META: Dict[str, Any] = CONFIG.get("telethon", {})
DEVICE_MODEL: str = TELEGRAM_CLIENT_META.get("telegram_device_model", "Python client")
SYSTEM_VERSION: str = TELEGRAM_CLIENT_META.get("telegram_system_version", "Unknown OS")
APP_VERSION: str = TELEGRAM_CLIENT_META.get("telegram_app_version", "1.0")


@dataclass(slots=True)
class AndroidAuthBundle:
    auth_key: bytes
    dc_id: int
    user_id: Optional[int] = None
    source: str = "unknown"


@dataclass(slots=True)
class UserConfigSnapshot:
    dc_id: Optional[int]
    user_id: Optional[int]
    auth_key: Optional[bytes]
    values: Dict[str, Any]


def _validate_dc_id(dc_id: int) -> int:
    if not isinstance(dc_id, int):
        raise TypeError(f"dc_id must be int, got {type(dc_id).__name__}")
    if dc_id not in DC_IPS:
        raise ValueError(f"Unsupported dc_id={dc_id}. Supported values: {sorted(DC_IPS)}")
    return dc_id


def _validate_auth_key_bytes(raw: bytes | bytearray) -> bytes:
    auth_key = bytes(raw)
    if len(auth_key) != 256:
        raise ValueError(f"Invalid auth_key length: expected 256 bytes, got {len(auth_key)}")
    return auth_key


def _decode_auth_key_string(value: str) -> Optional[bytes]:
    raw = str(value or "").strip()
    if not raw:
        return None

    if re.fullmatch(r"[0-9a-fA-F]{512}", raw):
        return _validate_auth_key_bytes(bytes.fromhex(raw))

    try:
        decoded = base64.b64decode(raw, validate=True)
    except Exception:
        return None

    if len(decoded) == 256:
        return decoded
    return None


def _first_valid_int(values: Dict[str, int], keys: Iterable[str]) -> Optional[int]:
    for key in keys:
        if key in values and values[key] > 0:
            return values[key]
    return None


def parse_userconfig_xml(userconfig_path: str | Path) -> UserConfigSnapshot:
    path = Path(userconfig_path)
    if not path.exists():
        raise FileNotFoundError(f"userconfig XML file not found: {path}")

    root = ET.fromstring(path.read_text(encoding="utf-8"))
    int_values: Dict[str, int] = {}
    str_values: Dict[str, str] = {}

    for node in root:
        key = str(node.attrib.get("name", "")).strip()
        if not key:
            continue
        tag = node.tag.lower()
        text = str(node.text or "").strip()

        if tag in {"int", "long"}:
            try:
                int_values[key] = int(text)
            except ValueError:
                continue
        elif tag == "string":
            str_values[key] = text

    dc_id = _first_valid_int(
        int_values,
        (
            "currentDatacenterId",
            "currentDcId",
            "dc_id",
            "datacenterSetId",
            "lastDcId",
        ),
    )
    user_id = _first_valid_int(
        int_values,
        (
            "clientUserId",
            "userId",
            "user_id",
            "lastUserId",
        ),
    )

    auth_candidates: list[tuple[str, bytes]] = []
    for key, value in str_values.items():
        low = key.lower()
        if "auth" not in low or "key" not in low:
            continue
        decoded = _decode_auth_key_string(value)
        if decoded:
            auth_candidates.append((key, decoded))

    auth_key: Optional[bytes] = None
    if auth_candidates:
        unique_values = {item[1] for item in auth_candidates}
        if len(unique_values) > 1:
            names = ", ".join(item[0] for item in auth_candidates)
            raise RuntimeError(f"Conflicting auth_key candidates found in XML keys: {names}")
        auth_key = auth_candidates[0][1]

    return UserConfigSnapshot(
        dc_id=dc_id,
        user_id=user_id,
        auth_key=auth_key,
        values={
            "ints": int_values,
            "strings": str_values,
        },
    )


def _normalize_auth_candidate(value: Any) -> Optional[bytes]:
    if isinstance(value, (bytes, bytearray)):
        try:
            return _validate_auth_key_bytes(value)
        except ValueError:
            return None
    if isinstance(value, str):
        return _decode_auth_key_string(value)
    return None


def _extract_auth_key_from_object(root_obj: Any, max_depth: int = 4) -> Optional[bytes]:
    queue: list[tuple[Any, int]] = [(root_obj, 0)]
    visited: set[int] = set()

    while queue:
        current, depth = queue.pop(0)
        if id(current) in visited or depth > max_depth:
            continue
        visited.add(id(current))

        if isinstance(current, dict):
            for key, value in current.items():
                key_name = str(key).lower()
                if "auth" in key_name and "key" in key_name:
                    decoded = _normalize_auth_candidate(value)
                    if decoded:
                        return decoded
                queue.append((value, depth + 1))
            continue

        if isinstance(current, (list, tuple, set)):
            for item in current:
                queue.append((item, depth + 1))
            continue

        if hasattr(current, "__dict__"):
            for attr_name, attr_value in vars(current).items():
                low = attr_name.lower()
                if "auth" in low and "key" in low:
                    decoded = _normalize_auth_candidate(attr_value)
                    if decoded:
                        return decoded

                if any(token in low for token in ("auth", "key", "dc", "user", "config", "session", "state")):
                    queue.append((attr_value, depth + 1))

    return None


def _extract_dc_id_from_object(root_obj: Any, max_depth: int = 4) -> Optional[int]:
    queue: list[tuple[Any, int]] = [(root_obj, 0)]
    visited: set[int] = set()
    dc_keys = {"dc", "dc_id", "dcid", "current_dc", "currentdcid", "datacenterid"}

    while queue:
        current, depth = queue.pop(0)
        if id(current) in visited or depth > max_depth:
            continue
        visited.add(id(current))

        if isinstance(current, dict):
            for key, value in current.items():
                key_name = re.sub(r"[^a-z0-9_]+", "", str(key).lower())
                if key_name in dc_keys and isinstance(value, int) and value > 0:
                    return value
                queue.append((value, depth + 1))
            continue

        if isinstance(current, (list, tuple, set)):
            for item in current:
                queue.append((item, depth + 1))
            continue

        if hasattr(current, "__dict__"):
            for attr_name, attr_value in vars(current).items():
                key_name = re.sub(r"[^a-z0-9_]+", "", attr_name.lower())
                if key_name in dc_keys and isinstance(attr_value, int) and attr_value > 0:
                    return attr_value
                if any(token in key_name for token in ("dc", "data", "config", "session", "state")):
                    queue.append((attr_value, depth + 1))

    return None


def extract_android_auth_bundle(
    tgnet_path: str | Path,
    userconfig_path: str | Path,
) -> AndroidAuthBundle:
    xml_snapshot = parse_userconfig_xml(userconfig_path)
    parser_errors: list[str] = []

    if AndroidSession is not None:
        try:
            session_obj = AndroidSession.from_tgnet(
                tgnet_path=str(tgnet_path),
                userconfig_path=str(userconfig_path),
            )
            auth_key = _extract_auth_key_from_object(session_obj)
            dc_id = _extract_dc_id_from_object(session_obj) or xml_snapshot.dc_id
            if auth_key is not None and dc_id is not None:
                return AndroidAuthBundle(
                    auth_key=_validate_auth_key_bytes(auth_key),
                    dc_id=_validate_dc_id(int(dc_id)),
                    user_id=xml_snapshot.user_id,
                    source="AndroidSession.from_tgnet",
                )
            parser_errors.append("AndroidSession parser returned incomplete auth data")
        except Exception as exc:
            parser_errors.append(f"AndroidSession parser failed: {exc}")

    if xml_snapshot.auth_key is not None and xml_snapshot.dc_id is not None:
        return AndroidAuthBundle(
            auth_key=_validate_auth_key_bytes(xml_snapshot.auth_key),
            dc_id=_validate_dc_id(int(xml_snapshot.dc_id)),
            user_id=xml_snapshot.user_id,
            source="userconfig.xml",
        )

    details = "; ".join(parser_errors) if parser_errors else "no parser available"
    raise RuntimeError(
        "Unable to extract auth_key/dc_id from Android session sources safely. "
        f"Details: {details}"
    )


def _validate_telethon_session_file(
    session_path: Path,
    expected_dc_id: int,
    expected_auth_key: bytes,
) -> None:
    with sqlite3.connect(str(session_path)) as conn:
        row = conn.execute("SELECT dc_id, auth_key FROM sessions LIMIT 1").fetchone()

    if row is None:
        raise RuntimeError(f"Telethon session DB {session_path} has no `sessions` row")

    dc_id_value = int(row[0])
    auth_key_blob = row[1]
    if not isinstance(auth_key_blob, (bytes, bytearray)):
        raise RuntimeError("Auth key in Telethon DB is not stored as BLOB bytes")

    auth_key_bytes = bytes(auth_key_blob)
    if len(auth_key_bytes) != 256:
        raise RuntimeError(f"Stored auth_key has invalid length {len(auth_key_bytes)} (expected 256)")
    if dc_id_value != expected_dc_id:
        raise RuntimeError(f"Stored dc_id={dc_id_value} does not match expected dc_id={expected_dc_id}")
    if auth_key_bytes != expected_auth_key:
        raise RuntimeError("Stored auth_key bytes differ from extracted source bytes")


class Converter:
    """
    Helper for Telegram sessions and TData conversion.
    """

    DC_IPS = DC_IPS

    def __init__(self, sessions_root: Optional[Path] = None) -> None:
        self.sessions_root = (
            Path(sessions_root) if sessions_root is not None else PROJECT_ROOT / "sessions"
        )

    async def tdata_to_session(self, tdata_path: str | Path, session_folder: str | Path) -> bool:
        session_folder = Path(session_folder)
        session_folder.mkdir(parents=True, exist_ok=True)

        if SessionManager is None:
            logger.error("TGConvertor is not installed, cannot convert TData -> Telethon session.")
            return False

        try:
            account_name = Path(tdata_path).resolve().parent.name
            session_path = session_folder / f"{account_name}.session"

            acc_data = get_auth_key_and_dc_id(tdata_path)
            if not acc_data:
                raise RuntimeError(f"Unable to extract account data from TData: {tdata_path}")

            auth_key_bytes = _validate_auth_key_bytes(bytes.fromhex(acc_data["auth_key"]))
            dc_id = _validate_dc_id(int(acc_data["dc_id"]))
            user_id = int(acc_data["user_id"])

            session_mgr = SessionManager(auth_key=auth_key_bytes, user_id=user_id, dc_id=dc_id)
            await session_mgr.to_telethon_file(str(session_path))
            _validate_telethon_session_file(session_path, dc_id, auth_key_bytes)

            logger.info('Converted TData "%s" -> %s', account_name, session_path)
            return True
        except Exception:
            logger.exception("Failed to convert TData to session from %s", tdata_path)
            return False

    def define_client_from_auth_key(
        self,
        auth_key_hex: str,
        dc_id: int,
        api_id: int,
        api_hash: str,
        save_session: bool = False,
        **kwargs: Any,
    ) -> TelegramClient:
        auth_key_bytes = _validate_auth_key_bytes(bytes.fromhex(str(auth_key_hex)))
        dc_id = _validate_dc_id(int(dc_id))

        if save_session:
            session_path: Path = kwargs.pop("session_path")
            session_path.parent.mkdir(parents=True, exist_ok=True)
            session = SQLiteSession(str(session_path.resolve()))
        else:
            session = MemorySession()

        session.set_dc(dc_id, self.DC_IPS[dc_id], 443)
        session.auth_key = AuthKey(auth_key_bytes)
        return TelegramClient(session=session, api_id=api_id, api_hash=api_hash, **kwargs)

    def create_session_from_auth_key_bytes(
        self,
        session_path: str | Path,
        auth_key: bytes,
        dc_id: int,
    ) -> Path:
        session_path = Path(session_path)
        session_path.parent.mkdir(parents=True, exist_ok=True)

        auth_key_bytes = _validate_auth_key_bytes(auth_key)
        dc_id = _validate_dc_id(int(dc_id))

        session = SQLiteSession(str(session_path))
        try:
            session.set_dc(dc_id, self.DC_IPS[dc_id], 443)
            session.auth_key = AuthKey(auth_key_bytes)
            session.save()
        finally:
            close_method = getattr(session, "close", None)
            if callable(close_method):
                close_method()

        _validate_telethon_session_file(session_path, dc_id, auth_key_bytes)
        logger.info("Session file created at %s from raw auth key (dc_id=%s)", session_path, dc_id)
        return session_path

    def create_session_from_auth_key(
        self,
        session_path: str | Path,
        auth_key_hex: str,
        dc_id: int,
    ) -> Path:
        return self.create_session_from_auth_key_bytes(
            session_path=session_path,
            auth_key=_validate_auth_key_bytes(bytes.fromhex(str(auth_key_hex))),
            dc_id=int(dc_id),
        )


def _resolve_device_id(udid: Optional[str]) -> str:
    if udid and str(udid).strip():
        return str(udid).strip()

    fallback = str(CONFIG.get("adb", {}).get("device_udid", "")).strip()
    if fallback:
        return fallback

    raise ValueError("ADB device id is required. Pass `udid` or set `adb.device_udid` in config.")


def transfer_dat_session(
    udid: Optional[str] = None,
    destination_dir: str | Path | None = None,
    package_name: str = DeviceController.TELEGRAM_PACKAGE,
) -> Dict[str, str]:
    """
    Export Telegram Android raw auth files (`tgnet.dat` + shared_prefs XML) via ADB root.
    """
    device_id = _resolve_device_id(udid)
    adb_path = str(CONFIG.get("adb", {}).get("adb_path", "adb")).strip() or "adb"

    target_dir = Path(destination_dir) if destination_dir is not None else PROJECT_ROOT / "sessions" / "dat"
    target_dir.mkdir(parents=True, exist_ok=True)

    controller = DeviceController(device_id=device_id, adb_path=adb_path)
    controller.connect()
    exported = controller.export_telegram_session_files(output_dir=target_dir, package_name=package_name)

    result: Dict[str, str] = {}
    for name, source_path in exported.items():
        final_path = target_dir / name
        shutil.copy2(source_path, final_path)
        result[name] = str(final_path)

    logger.info("Transferred Telegram session files from %s to %s", device_id, target_dir)
    return result


def _resolve_dat_paths(dat_dir: Path) -> tuple[Path, Path]:
    tgnet_path = dat_dir / "tgnet.dat"
    if not tgnet_path.exists():
        raise FileNotFoundError(f"tgnet.dat not found: {tgnet_path}")

    xml_candidates = [
        dat_dir / "userconfing.xml",
        dat_dir / "userconfig.xml",
    ]
    for xml_path in xml_candidates:
        if xml_path.exists():
            return tgnet_path, xml_path

    raise FileNotFoundError(
        f"userconfing.xml/userconfig.xml not found in {dat_dir}"
    )


def convert_dat_to_session(
    phone_number: str,
    *,
    dat_dir: str | Path | None = None,
    output_root: str | Path | None = None,
) -> bool:
    """
    Convert Android `tgnet.dat` + `userconfing.xml` to a Telethon `.session`.

    Strict rules:
      - auth_key is accepted only if it is exactly 256 bytes
      - auth_key bytes are written as-is (no byte-order changes, no trimming/padding)
      - dc_id must be explicit and supported
    """
    try:
        source_dir = Path(dat_dir) if dat_dir is not None else PROJECT_ROOT / "sessions" / "dat"
        tgnet_path, userconfig_path = _resolve_dat_paths(source_dir)
        auth_bundle = extract_android_auth_bundle(tgnet_path=tgnet_path, userconfig_path=userconfig_path)

        today = datetime.now().strftime("%Y-%m-%d")
        destination_root = (
            Path(output_root) if output_root is not None else PROJECT_ROOT / "sessions" / "converted"
        )
        destination_dir = destination_root / today
        destination_dir.mkdir(parents=True, exist_ok=True)

        session_path = destination_dir / f"acc_{phone_number}.session"
        converter = Converter()
        converter.create_session_from_auth_key_bytes(
            session_path=session_path,
            auth_key=auth_bundle.auth_key,
            dc_id=auth_bundle.dc_id,
        )

        metadata = {
            "source": auth_bundle.source,
            "dc_id": auth_bundle.dc_id,
            "user_id": auth_bundle.user_id,
            "auth_key_length": len(auth_bundle.auth_key),
            "tgnet_path": str(tgnet_path),
            "userconfig_path": str(userconfig_path),
            "session_path": str(session_path),
            "created_at": datetime.now().isoformat(),
        }
        metadata_path = session_path.with_suffix(".meta.json")
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        logger.info("Session for acc_%s created in %s", phone_number, session_path)
        return True
    except Exception:
        logger.exception("Error creating session for acc_%s", phone_number)
        return False


def convert_dat_to_tdata(phone_number: str) -> bool:
    """
    Convert Android `tgnet.dat` + `userconfing.xml` into Telegram Desktop TData.
    """
    if AndroidSession is None:
        logger.error("AndroidTelePorter is not installed, cannot convert to TData.")
        return False

    sessions_dir = PROJECT_ROOT / "sessions"
    dat_dir = sessions_dir / "dat"

    try:
        tgnet_path, config_path = _resolve_dat_paths(dat_dir)
        today = datetime.now().strftime("%Y-%m-%d")
        account_dir = sessions_dir / "converted" / today / f"acc_{phone_number}"
        account_dir.mkdir(parents=True, exist_ok=True)

        session = AndroidSession.from_tgnet(
            tgnet_path=str(tgnet_path),
            userconfig_path=str(config_path),
        )
        session.to_tdata(str(account_dir))
        logger.info("TData for account %s created in %s", phone_number, account_dir)
        return True
    except Exception:
        logger.exception("Error creating TData for account %s", phone_number)
        return False


def set_2fa_safe(
    auth_key: str,
    dc_id: int,
    country: str,
    password: Optional[str],
    cur_password: Optional[str] = None,
    hint: str = "my password",
) -> None:
    """
    Safely set or reset Telegram 2FA using only auth key + DC.
    """
    proxy_list = read_txt_list(f"{country}_proxies.txt")
    if not proxy_list:
        raise RuntimeError(f"Proxy list is empty: {country}_proxies.txt")

    proxy_str_splat = random.choice(proxy_list).strip().split(":")
    if len(proxy_str_splat) < 4:
        raise RuntimeError("Invalid proxy format in proxy file; expected host:port:user:pass")

    proxy = (
        "socks5",
        proxy_str_splat[0],
        int(proxy_str_splat[1]),
        True,
        proxy_str_splat[2],
        proxy_str_splat[3],
    )

    converter = Converter()
    client = converter.define_client_from_auth_key(
        auth_key_hex=auth_key,
        dc_id=dc_id,
        api_id=API_ID,
        api_hash=API_HASH,
        system_version=SYSTEM_VERSION,
        device_model=DEVICE_MODEL,
        app_version=APP_VERSION,
        proxy=proxy,
    )

    try:
        if not client.is_connected():
            client.connect()

        result = client.edit_2fa(
            current_password=cur_password,
            new_password=password,
            hint=hint,
        )

        if password:
            print("Cloud password set successfully:", result)
        else:
            print("Cloud password reset successfully:", result)
    except FloodError as e:
        if getattr(e, "seconds", None):
            hours = round(e.seconds / 3600, 2)
            print(f"FloodError: password change is temporarily locked. Wait {e.seconds} seconds (~{hours} h).")
        else:
            print("FloodError: method is frozen for this account.")
    except PasswordHashInvalidError:
        print("Invalid current password (cur_password).")
    except SessionPasswordNeededError:
        print("This account already has a password set. You must pass cur_password.")
    except Exception as e:
        print("Unexpected error while changing 2FA password:", repr(e))
    finally:
        try:
            client.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    print("sessions.py module loaded. Use Converter and set_2fa_safe() from other scripts.")
