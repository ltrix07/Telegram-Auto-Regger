from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from telethon import TelegramClient

from auto_reger.adb import connect_adb, ensure_adb_root, get_device_fingerprint
from auto_reger.sessions import Converter, extract_android_auth_bundle, transfer_dat_session
from auto_reger.utils import CONFIG, PROJECT_ROOT


LOGGER = logging.getLogger("smoke_test")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Smoke test for Android raw Telegram session export -> Telethon session conversion."
    )
    parser.add_argument(
        "--device",
        type=str,
        default="",
        help="ADB device id (host:port). Defaults to config adb.device_udid.",
    )
    parser.add_argument(
        "--out",
        type=str,
        default="sessions/smoke",
        help="Output directory for smoke artifacts (relative to project root by default).",
    )
    return parser.parse_args()


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def _resolve_device_id(args: argparse.Namespace) -> str:
    if str(args.device).strip():
        return str(args.device).strip()

    value = str(CONFIG.get("adb", {}).get("device_udid", "")).strip()
    if not value:
        raise RuntimeError("Missing ADB device id. Pass --device or set adb.device_udid in config.yaml.")
    return value


def _resolve_output_dir(args: argparse.Namespace) -> Path:
    target = Path(str(args.out).strip() or "sessions/smoke")
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    target.mkdir(parents=True, exist_ok=True)
    return target


def _pick_required_files(exported: Dict[str, str]) -> tuple[Path, Path]:
    files_by_name = {name: Path(path) for name, path in exported.items()}
    tgnet_path = files_by_name.get("tgnet.dat")
    if tgnet_path is None or not tgnet_path.exists():
        raise RuntimeError("Export did not provide tgnet.dat")

    userconfig_path = None
    for name in ("userconfing.xml", "userconfig.xml"):
        if name in files_by_name and files_by_name[name].exists():
            userconfig_path = files_by_name[name]
            break
    if userconfig_path is None:
        raise RuntimeError("Export did not provide userconfing.xml/userconfig.xml")

    return tgnet_path, userconfig_path


def _write_sidecar_json(
    session_path: Path,
    device_id: str,
    fingerprint: Dict[str, str],
    auth_bundle: Any,
) -> Path:
    sidecar_path = session_path.with_suffix(session_path.suffix + ".fingerprint.json")
    payload = {
        "device_id": device_id,
        "session_file": str(session_path),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "device_fingerprint": {
            "device_model": str(fingerprint.get("device_model", "")).strip(),
            "system_version": str(fingerprint.get("system_version", "")).strip(),
            "app_version": str(fingerprint.get("app_version", "")).strip(),
        },
        "auth_bundle": {
            "source": getattr(auth_bundle, "source", "unknown"),
            "dc_id": int(getattr(auth_bundle, "dc_id", 0) or 0),
            "user_id": getattr(auth_bundle, "user_id", None),
            "auth_key_length": len(getattr(auth_bundle, "auth_key", b"") or b""),
        },
    }
    sidecar_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return sidecar_path


def _load_meta_from_sidecar(sidecar_path: Path) -> Dict[str, str]:
    raw = json.loads(sidecar_path.read_text(encoding="utf-8"))
    fp = raw.get("device_fingerprint", {}) or {}
    return {
        "device_model": str(fp.get("device_model", "")).strip() or "Android",
        "system_version": str(fp.get("system_version", "")).strip() or "Android",
        "app_version": str(fp.get("app_version", "")).strip() or "1.0",
    }


async def _telethon_probe(session_path: Path, metadata: Dict[str, str]) -> Dict[str, Any]:
    telethon_cfg = CONFIG.get("telethon", {})
    api_id = int(telethon_cfg.get("api_id") or 0)
    api_hash = str(telethon_cfg.get("api_hash") or "").strip()
    if not api_id or not api_hash:
        raise RuntimeError("Missing telethon.api_id / telethon.api_hash in config.yaml")

    client = TelegramClient(
        session=str(session_path),
        api_id=api_id,
        api_hash=api_hash,
        device_model=metadata["device_model"],
        system_version=metadata["system_version"],
        app_version=metadata["app_version"],
        system_lang_code=str(telethon_cfg.get("system_lang_code", "en")),
        lang_code=str(telethon_cfg.get("lang_code", "en")),
    )

    try:
        await client.connect()
        authorized = await client.is_user_authorized()
        if not authorized:
            raise RuntimeError("Session is not authorized after conversion.")

        me = await client.get_me()
        if me is None:
            raise RuntimeError("Telethon get_me() returned None for an authorized session.")

        return {
            "id": int(getattr(me, "id", 0) or 0),
            "username": str(getattr(me, "username", "") or ""),
            "phone": str(getattr(me, "phone", "") or ""),
            "first_name": str(getattr(me, "first_name", "") or ""),
            "last_name": str(getattr(me, "last_name", "") or ""),
        }
    finally:
        await client.disconnect()


def main() -> int:
    args = parse_args()
    setup_logging()

    device_id = _resolve_device_id(args)
    output_root = _resolve_output_dir(args)
    adb_path = str(CONFIG.get("adb", {}).get("adb_path", "adb")).strip() or "adb"

    run_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_device = re.sub(r"[^A-Za-z0-9_.-]+", "_", device_id)
    run_dir = output_root / f"{run_tag}_{safe_device}"
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"[0/6] Smoke test started | device={device_id} | run_dir={run_dir}")

    print("[1/6] Connecting to ADB and checking root...")
    if not connect_adb(device_id, adb_path=adb_path):
        raise RuntimeError(f"ADB connect/root check failed for device {device_id}")
    root_mode = ensure_adb_root(device_id, adb_path=adb_path)
    print(f"[1/6] OK | root_mode={root_mode}")

    print("[2/6] Collecting device fingerprint...")
    fingerprint = get_device_fingerprint(device_id, adb_path=adb_path)
    fingerprint_path = run_dir / "device_fingerprint.json"
    fingerprint_path.write_text(json.dumps(fingerprint, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[2/6] OK | fingerprint={fingerprint}")
    print(f"[2/6] Saved fingerprint -> {fingerprint_path}")

    print("[3/6] Exporting raw Telegram auth files from Android container...")
    exported = transfer_dat_session(udid=device_id, destination_dir=run_dir)
    print(f"[3/6] OK | exported_files={sorted(exported)}")

    tgnet_path, userconfig_path = _pick_required_files(exported)
    print(f"[3/6] tgnet.dat -> {tgnet_path}")
    print(f"[3/6] userconfig -> {userconfig_path}")

    print("[4/6] Converting raw Android auth into Telethon SQLite session...")
    auth_bundle = extract_android_auth_bundle(tgnet_path=tgnet_path, userconfig_path=userconfig_path)
    converter = Converter()
    session_path = run_dir / "smoke.session"
    converter.create_session_from_auth_key_bytes(
        session_path=session_path,
        auth_key=auth_bundle.auth_key,
        dc_id=auth_bundle.dc_id,
    )
    sidecar_path = _write_sidecar_json(
        session_path=session_path,
        device_id=device_id,
        fingerprint=fingerprint,
        auth_bundle=auth_bundle,
    )
    print(
        "[4/6] OK | session={} | dc_id={} | auth_key_len={} | sidecar={}".format(
            session_path,
            auth_bundle.dc_id,
            len(auth_bundle.auth_key),
            sidecar_path,
        )
    )

    print("[5/6] Initializing Telethon from sidecar fingerprint...")
    metadata = _load_meta_from_sidecar(sidecar_path)
    print(f"[5/6] Telethon metadata -> {metadata}")

    print("[6/6] Running Telegram API probe: client.get_me()...")
    me_data = asyncio.run(_telethon_probe(session_path=session_path, metadata=metadata))
    print(f"[6/6] OK | get_me -> {me_data}")

    print("[DONE] Smoke test passed successfully.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        LOGGER.exception("Smoke test failed: %s", exc)
        print(f"[FAILED] {exc}")
        raise
