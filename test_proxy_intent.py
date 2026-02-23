from __future__ import annotations

import argparse
import logging
import sys

from auto_reger.device_controller import DeviceController
from auto_reger.utils import CONFIG


LOGGER = logging.getLogger("test_proxy_intent")


def parse_args() -> argparse.Namespace:
    adb_cfg = CONFIG.get("adb", {})
    default_device = str(adb_cfg.get("device_udid", "")).strip()
    default_adb_path = str(adb_cfg.get("adb_path", "adb")).strip() or "adb"

    parser = argparse.ArgumentParser(
        description="Dry-run test for Telegram SOCKS deep-link intent and proxy popup confirmation."
    )
    parser.add_argument(
        "--device",
        default=default_device,
        help="ADB serial / host:port. Defaults to adb.device_udid from config.yaml.",
    )
    parser.add_argument(
        "--adb-path",
        default=default_adb_path,
        help="ADB binary path. Defaults to adb.adb_path from config.yaml.",
    )
    parser.add_argument(
        "--ip",
        default="8.8.8.8",
        help="Proxy server IP for deep-link test. Default: 8.8.8.8",
    )
    parser.add_argument(
        "--port",
        default="1080",
        help="Proxy server port for deep-link test. Default: 1080",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Popup wait timeout in seconds. Default: 10.0",
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    args = parse_args()
    device_id = str(args.device or "").strip()
    if not device_id:
        LOGGER.error("ADB device is empty. Pass --device or configure adb.device_udid in config.yaml.")
        return 2

    LOGGER.info("Starting proxy intent dry-run on device: %s", device_id)
    LOGGER.info("Intent payload: tg://socks?server=%s&port=%s", args.ip, args.port)
    LOGGER.info("ADB path: %s", args.adb_path)

    controller = DeviceController(device_id=device_id, adb_path=str(args.adb_path))

    try:
        controller.connect()
        LOGGER.info("ADB connection initialized successfully.")
    except Exception:
        LOGGER.exception("Failed to initialize ADB connection.")
        return 3

    try:
        intent_ok = controller.set_telegram_proxy_via_intent(ip=str(args.ip), port=str(args.port))
    except Exception:
        LOGGER.exception("Unexpected error while running Telegram proxy intent.")
        return 4

    if not intent_ok:
        LOGGER.error("Telegram proxy intent returned failure status.")
        return 5

    LOGGER.info("Telegram proxy intent started successfully. Waiting for proxy popup...")

    try:
        selector_used = controller.enable_telegram_proxy_popup(timeout=float(args.timeout))
    except TimeoutError:
        LOGGER.exception("Proxy popup did not appear or confirm button was not found.")
        return 6
    except Exception:
        LOGGER.exception("Unexpected error while waiting/clicking proxy popup.")
        return 7

    LOGGER.info("Proxy popup confirmed successfully.")
    LOGGER.info("Selector used: %s", selector_used)
    print("RESULT: OK")
    print(f"DEVICE: {device_id}")
    print(f"SELECTOR: {selector_used}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
