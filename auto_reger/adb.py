import random
import logging
import subprocess
import secrets
import time
import string
import base64
import os
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import NameOID
from datetime import datetime, timedelta

from .utils import CONFIG

ADB_PATH_DEFAULT = "adb"


def resolve_adb_path() -> str:
    env_value = os.environ.get("ADB_PATH", "").strip()
    if env_value:
        return env_value

    try:
        config_value = str(CONFIG.get("adb", {}).get("adb_path", ADB_PATH_DEFAULT)).strip()
    except Exception:
        config_value = ADB_PATH_DEFAULT
    return config_value or ADB_PATH_DEFAULT


ADB_PATH = resolve_adb_path()
_ROOT_MODE_CACHE: dict[str, str] = {}


USER_AGENTS = [
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36... (KHTML, like Gecko) Chrome/83.0.4103.106 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/84.0.4147.89 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36... (KHTML, like Gecko) Chrome/85.0.4183.127 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/86.0.4240.75 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36... (KHTML, like Gecko) Chrome/87.0.4280.141 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/88.0.4324.93 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/89.0.4389.90 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36... (KHTML, like Gecko) Chrome/90.0.4430.210 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36... (KHTML, like Gecko) Chrome/91.0.4472.164 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36 (KHTML, like Gecko) Firefox/72.0 Mobile",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36 (KHTML, like Gecko) Firefox/73.0 Mobile",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36 (KHTML, like Gecko) Firefox/74.0 Mobile",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/12.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/12.1 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/13.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36 (KHTML, like Gecko) Edge/44.0.2403.119 Mobile",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36 (KHTML, like Gecko) Edge/45.0.2454.94 Mobile",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36 (KHTML, like Gecko) Edge/46.0.2486.0 Mobile",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36 (KHTML, like Gecko) Opera/58.0.3135.107 Mobile",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36 (KHTML, like Gecko) Opera/59.0.3206.125 Mobile",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36 (KHTML, like Gecko) Opera/60.0.3255.109 Mobile",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36 (KHTML, like Gecko) UCBrowser/13.2.0.1298 Mobile",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36 (KHTML, like Gecko) UCBrowser/13.3.0.1305 Mobile",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/83.0.4103.61 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36... (KHTML, like Gecko) Chrome/84.0.4147.125 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36... (KHTML, like Gecko) Chrome/85.0.4183.127 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36... (KHTML, like Gecko) Chrome/86.0.4240.198 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/87.0.4280.66 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36... (KHTML, like Gecko) Chrome/88.0.4324.181 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/89.0.4389.90 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36... (KHTML, like Gecko) Chrome/90.0.4430.210 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36... (KHTML, like Gecko) Chrome/91.0.4472.164 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36 (KHTML, like Gecko) Firefox/73.0 Mobile",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36 (KHTML, like Gecko) Firefox/74.0 Mobile",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36 (KHTML, like Gecko) Firefox/75.0 Mobile",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/13.2 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/14.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36 (KHTML, like Gecko) Edge/44.0.2403.140 Mobile",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36 (KHTML, like Gecko) Edge/45.0.2454.62 Mobile",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36 (KHTML, like Gecko) Opera/61.0.3290.111 Mobile",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36 (KHTML, like Gecko) Opera/62.0.3331.99 Mobile",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36 (KHTML, like Gecko) UCBrowser/13.4.0.1306 Mobile",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36... (KHTML, like Gecko) Chrome/92.0.4515.131 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/93.0.4577.62 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/94.0.4606.71 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36 (KHTML, like Gecko) Firefox/76.0 Mobile",
    "Mozilla/5.0 (Linux; Android 9; SM-G781B) AppleWebKit/537.36 (KHTML, like Gecko) Firefox/77.0 Mobile"
]


REAL_DEVICES = [
    {"model": "SM-G960F", "board": "universal9810", "name": "starltexx", "cpu_abi": "arm64-v8a", "hardware": "exynos9810", "full_name": "Samsung Galaxy S9"},
    {"model": "SM-G965F", "board": "universal9810", "name": "star2ltexx", "cpu_abi": "arm64-v8a", "hardware": "exynos9810", "full_name": "Samsung Galaxy S9+"},
    {"model": "SM-N960F", "board": "crownlte", "name": "crownltexx", "cpu_abi": "arm64-v8a", "hardware": "exynos9810", "full_name": "Samsung Galaxy Note9"},
    {"model": "SM-G970F", "board": "beyond0", "name": "beyond0ltexx", "cpu_abi": "arm64-v8a", "hardware": "exynos9820", "full_name": "Samsung Galaxy S10e"},
    {"model": "SM-G973F", "board": "beyond1", "name": "beyond1ltexx", "cpu_abi": "arm64-v8a", "hardware": "exynos9820", "full_name": "Samsung Galaxy S10"},
    {"model": "SM-G975F", "board": "beyond2", "name": "beyond2ltexx", "cpu_abi": "arm64-v8a", "hardware": "exynos9820", "full_name": "Samsung Galaxy S10+"},
    {"model": "SM-A505F", "board": "a50", "name": "a50dd", "cpu_abi": "arm64-v8a", "hardware": "exynos9610", "full_name": "Samsung Galaxy A50"},
    {"model": "SM-A705F", "board": "a70q", "name": "a70q", "cpu_abi": "arm64-v8a", "hardware": "exynos7904", "full_name": "Samsung Galaxy A70"},
    {"model": "SM-G781B", "board": "r8q", "name": "r8qxxx", "cpu_abi": "arm64-v8a", "hardware": "qcom", "full_name": "Samsung Galaxy S20 FE 5G"},
]


def _adb_prefix(udid: str | None = None, adb_path: str | None = None) -> list[str]:
    resolved_adb_path = adb_path or ADB_PATH
    prefix = [resolved_adb_path]
    if udid:
        prefix.extend(["-s", udid])
    return prefix


def _adb_run(
    args: list[str],
    udid: str | None = None,
    adb_path: str | None = None,
    timeout: int = 20,
) -> subprocess.CompletedProcess[str]:
    cmd = _adb_prefix(udid=udid, adb_path=adb_path) + args
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def ensure_adb_root(udid: str | None = None, adb_path: str | None = None) -> str:
    """
    Ensure root command execution is available for the current device.

    :return: Root mode: ``adbd`` or ``su``.
    :raises RuntimeError: If no root mode is available.
    """
    cache_key = udid or "__default__"
    if cache_key in _ROOT_MODE_CACHE:
        return _ROOT_MODE_CACHE[cache_key]

    _adb_run(["root"], udid=udid, adb_path=adb_path, timeout=20)
    time.sleep(0.8)

    adbd_probe = _adb_run(["shell", "id", "-u"], udid=udid, adb_path=adb_path, timeout=10)
    if adbd_probe.returncode == 0 and adbd_probe.stdout.strip() == "0":
        _ROOT_MODE_CACHE[cache_key] = "adbd"
        return "adbd"

    su_probe = _adb_run(["shell", "su", "-c", "id -u"], udid=udid, adb_path=adb_path, timeout=10)
    if su_probe.returncode == 0 and su_probe.stdout.strip() == "0":
        _ROOT_MODE_CACHE[cache_key] = "su"
        return "su"

    raise RuntimeError(
        "Root access is unavailable for device {} (adbd stdout={!r}, su stdout={!r})".format(
            udid or "<default>",
            (adbd_probe.stdout or "").strip(),
            (su_probe.stdout or "").strip(),
        )
    )


def run_adb_shell_command(
    command: str,
    udid: str | None = None,
    adb_path: str | None = None,
    timeout: int = 20,
) -> str:
    """
    Execute a shell command on Android device with forced root rights.
    """
    root_mode = ensure_adb_root(udid=udid, adb_path=adb_path)
    if root_mode == "adbd":
        result = _adb_run(["shell", "sh", "-c", command], udid=udid, adb_path=adb_path, timeout=timeout)
    else:
        result = _adb_run(
            ["shell", "su", "-c", command],
            udid=udid,
            adb_path=adb_path,
            timeout=timeout,
        )

    if result.returncode != 0:
        raise RuntimeError(
            "ADB shell command failed (device={}, command={!r}, stderr={!r})".format(
                udid or "<default>",
                command,
                (result.stderr or result.stdout or "").strip(),
            )
        )
    return (result.stdout or "").strip()


def _parse_telegram_version_from_dumpsys(dumpsys_output: str) -> str:
    for line in dumpsys_output.splitlines():
        if "versionName=" not in line:
            continue
        _, _, tail = line.partition("versionName=")
        value = tail.strip()
        if value:
            return value
    return "Unknown"


def get_device_fingerprint(udid: str, adb_path: str | None = None) -> dict:
    """
    Collect real Android fingerprint fields for Telethon metadata.

    Returns keys:
      - device_model
      - system_version
      - app_version
    """
    device_model = run_adb_shell_command(
        "getprop ro.product.model",
        udid=udid,
        adb_path=adb_path,
    ) or "Unknown Android"
    android_release = run_adb_shell_command(
        "getprop ro.build.version.release",
        udid=udid,
        adb_path=adb_path,
    ) or "Unknown"
    dumpsys_output = run_adb_shell_command(
        "dumpsys package org.telegram.messenger",
        udid=udid,
        adb_path=adb_path,
        timeout=30,
    )
    return {
        "device_model": device_model,
        "system_version": f"Android {android_release}",
        "app_version": _parse_telegram_version_from_dumpsys(dumpsys_output),
    }


def run_adb_command(command: str, udid: str | None = None, adb_path: str | None = None) -> None:
    """Run a single shell command on the connected Android device via ADB.

    The command is executed inside an interactive "adb shell" with "su"
    to obtain root privileges. Raises RuntimeError on failure.
    """
    output = run_adb_shell_command(
        command=command,
        udid=udid,
        adb_path=adb_path,
    )
    logging.info("ADB command ran (root): %s", output)


def connect_adb(udid: str, max_attempts: int = 3, adb_path: str | None = None) -> bool:
    """Try to establish an ADB connection to the given device.

    The function will run ``adb connect`` up to ``max_attempts`` times and
    validate that the device responds with ``echo online``.

    :param udid: Device serial / host:port pair.
    :param max_attempts: Maximum number of connection attempts.
    :return: True if the device responds as online, False otherwise.
    """
    resolved_adb_path = adb_path or ADB_PATH

    for attempt in range(1, max_attempts + 1):
        logging.info("Attempt %s/%s to connect to %s", attempt, max_attempts, udid)
        connect_result = subprocess.run(
            [resolved_adb_path, "connect", udid],
            capture_output=True,
            text=True,
            check=False,
        )
        if connect_result.returncode != 0:
            logging.warning(
                "Error connecting to ADB for %s: %s",
                udid,
                (connect_result.stderr or connect_result.stdout).strip(),
            )
        else:
            logging.info(
                "ADB connect response for %s: %s",
                udid,
                connect_result.stdout.strip(),
            )
            probe_result = _adb_run(
                ["shell", "echo", "online"],
                udid=udid,
                adb_path=resolved_adb_path,
                timeout=10,
            )
            if probe_result.returncode == 0 and "online" in probe_result.stdout.strip():
                try:
                    root_mode = ensure_adb_root(udid=udid, adb_path=resolved_adb_path)
                    logging.info("ADB root mode for %s: %s", udid, root_mode)
                    return True
                except Exception as root_exc:
                    logging.error("Connected to %s but failed to obtain root: %s", udid, root_exc)

        time.sleep(2)

    logging.error("Failed to connect to %s after %s attempts", udid, max_attempts)
    return False


def generate_number(nqty):
    number = ''
    for _ in range(nqty):
        number += str(random.randint(0, 9))

    return number


def get_device_info(udid: str) -> dict:
    """Collect basic device information via ADB.

    The function queries Android version, device model, Telegram app version and
    system language. It also tries to map the model to a more user-friendly
    "full name" using the REAL_DEVICES catalogue.

    :param udid: Device serial / host:port pair.
    :return: Dict with keys: ``model``, ``full_model``, ``android``, ``tg``, ``sys_lang``.
    """
    fingerprint = get_device_fingerprint(udid=udid, adb_path=ADB_PATH)
    device_model = fingerprint["device_model"]
    tg_version = fingerprint["app_version"]
    system_version = fingerprint["system_version"]
    sys_lang = run_adb_shell_command(
        "getprop persist.sys.locale",
        udid=udid,
        adb_path=ADB_PATH,
    ) or "Unknown"

    # Map model to a full readable name
    try:
        full_model = next(device['full_name'] for device in REAL_DEVICES if device['model'] == device_model)
    except StopIteration:
        full_model = device_model or "Unknown"
        logging.warning("Model %s not found in REAL_DEVICES catalogue", device_model)

    return {
        'model': device_model,
        'full_model': full_model,
        'android': system_version,
        'tg': tg_version,
        'sys_lang': sys_lang
    }


def generate_samsung_imei():
    tac = "35" + ''.join(random.choice('0123456789') for _ in range(6))  # TAC РґР»СЏ Samsung
    serial = ''.join(random.choice('0123456789') for _ in range(6))
    temp = tac + serial
    sum_odd = sum(int(temp[i]) for i in range(0, len(temp), 2))
    sum_even = sum(int(d) * 2 if int(d) * 2 < 10 else int(d) * 2 - 9 for d in temp[1::2])
    check_digit = (10 - (sum_odd + sum_even) % 10) % 10
    return temp + str(check_digit)


def generate_samsung_mac():
    oui_list = ["00:03:7A", "00:0D:6F", "00:12:FB", "00:1D:6A"]  # Р РµР°Р»СЊРЅС‹Рµ OUI Samsung
    oui = random.choice(oui_list)
    nic = ':'.join('{:02x}'.format(random.randint(0, 255)) for _ in range(3))
    return oui + ":" + nic


def generate_boottime_sequence(now=None, shift=None):
    if now is None:
        now = datetime.now()

    if shift is None:
        shift = random.randint(0, 60)

    new_time = now - timedelta(minutes=shift)
    return [
        int((new_time - now).total_seconds() * 1000) for minutes in range(-5, 0)
    ]


def change_imei():
    try:
        # Р“РµРЅРµСЂР°С†РёСЏ РЅРѕРІРѕРіРѕ IMEI
        new_imei = generate_samsung_imei()

        process = subprocess.Popen(
            [ADB_PATH, "shell"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        commands = [
            "su",
            "svc data disable",
            "svc wifi disable",
            "svc bluetooth disable",
            "svc nfc disable",
            "settings put global airplane_mode_on 1",
            "am broadcast -a android.intent.action.AIRPLANE_MODE --ez state true",
            "sleep 2",
            "am broadcast -n com.example.app/.AirplaneModeReceiver --ez state true",
            f"service call iphonesubinfo 7 i32 0 s16 {new_imei}",
            "svc data enable",
            "svc wifi enable",
            "svc bluetooth enable",
            "svc nfc enable",
            "settings put global airplane_mode_on 0",
            "am broadcast -a android.intent.action.AIRPLANE_MODE --ez state false",
            "stop ril-daemon",
            "start ril-daemon",
            "exit",
            "exit"
        ]

        try:
            stdout, stderr = process.communicate('\n'.join(commands), timeout=30)
            if stderr and process.returncode not in (0, None):
                logging.error("IMEI change failed: %s", stderr.strip())
                raise subprocess.CalledProcessError(process.returncode, commands, stderr=stderr)

            logging.info("New IMEI generated: %s", new_imei)
        except subprocess.TimeoutExpired as e:
            logging.error("IMEI change timeout: %s", e)
            process.kill()
            raise
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to change IMEI: {e.stderr}")
        raise RuntimeError(f"Failed to change IMEI: {e.stderr}")


def generate_android_id():
    return ''.join(secrets.choice('0123456789abcdef') for _ in range(16))


def generate_android_build_id_past():
    start = datetime.now() - timedelta(days=365 * 2)
    end = datetime.now() - timedelta(days=30)

    random_date = start + (end - start) * random.random()

    build_id = random.choice(string.ascii_uppercase)
    build_id += random.choice(string.ascii_uppercase)
    build_id += random_date.strftime('%y%m%d')

    return build_id


def generate_and_set_user_agent() -> str | None:
    """Generate and apply a random User-Agent string on the device.

    Picks a value from the ``USER_AGENTS`` catalogue and updates the built-in
    browser's user agent via ADB + ``settings put``. Returns the new User-Agent
    on success or ``None`` if something went wrong.
    """
    try:
        new_user_agent = random.choice(USER_AGENTS)
        process = subprocess.Popen(
            f'"{ADB_PATH}" shell',
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        commands = [
            "su",
            f"settings put global http_user_agent '{new_user_agent}'",
            "exit",
            "exit"
        ]
        stdout, stderr = process.communicate("\n".join(commands), timeout=10)
        if stderr:
            logging.error(f"Failed to set User-Agent with su: {stderr}")
            return None
        logging.info(f"User-Agent changed to {new_user_agent}")
        return new_user_agent
    except Exception as e:
        logging.error(f"Error setting User-Agent: {str(e)}")
        return None


def change_setting(level, setting_name, value, su=True):
    try:
        command = f"settings put {level} {setting_name} {value}"
        if su:
            run_adb_shell_command(command, adb_path=ADB_PATH, timeout=10)
        else:
            result = _adb_run(
                ["shell", "settings", "put", level, setting_name, str(value)],
                adb_path=ADB_PATH,
                timeout=10,
            )
            if result.returncode != 0:
                raise RuntimeError((result.stderr or result.stdout or "").strip())
        logging.info(f"Setting {setting_name} changed to {value}")
    except subprocess.TimeoutExpired as e:
        logging.error(f"Timeout when changing setting {setting_name}: {e}")
        raise
    except Exception as e:
        logging.error(f"Error changing setting {setting_name}: {e}")
        raise RuntimeError(f"Error changing setting {setting_name}: {e}")


def change_prop(setting_name, value, su=True):
    try:
        command = f"setprop {setting_name} {value}"
        if su:
            run_adb_shell_command(command, adb_path=ADB_PATH, timeout=10)
        else:
            result = _adb_run(
                ["shell", "setprop", setting_name, str(value)],
                adb_path=ADB_PATH,
                timeout=10,
            )
            if result.returncode != 0:
                raise RuntimeError((result.stderr or result.stdout or "").strip())
        logging.info(f"Prop {setting_name} changed to {value}")
    except subprocess.TimeoutExpired as e:
        logging.error(f"Timeout when changing prop {setting_name}: {e}")
        raise
    except Exception as e:
        logging.error(f"Error changing prop {setting_name}: {e}")
        raise RuntimeError(f"Error changing prop {setting_name}: {e}")


def set_random_timezone():
    try:
        timezones = ["America/New_York", "America/Los_Angeles", "America/Chicago"]
        new_timezone = random.choice(timezones)
        process = subprocess.Popen(
            [ADB_PATH, "shell"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        commands = [
            "su",
            f"setprop persist.sys.timezone {new_timezone}",
            "exit",
            "exit"
        ]
        stdout, stderr = process.communicate("\n".join(commands), timeout=10)
        if stderr:
            logging.error(f"Failed to set timezone with su: {stderr}")
            return
        logging.info(f"Timezone changed to {new_timezone}")
    except Exception as e:
        logging.error(f"Error setting timezone: {str(e)}")


def compare_emulator_settings(udid1, udid2):
    try:
        adb_path = ADB_PATH

        def get_settings(udid, levels=('secure', 'global', 'system')):
            settings = {}
            for level in levels:
                cmd = f'"{adb_path}" -s {udid} shell settings list {level}'
                process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
                output, error = process.communicate()
                if error:
                    logging.error(f"Error reading {level} settings for {udid}: {error.decode()}")
                    continue
                lines = output.decode().splitlines()
                settings[level] = {}
                for line in lines:
                    if '=' in line:
                        k, v = line.split('=', 1)
                        settings[level][k] = v
            return settings

        settings1 = get_settings(udid1)
        settings2 = get_settings(udid2)

        differences = {'secure': {}, 'global': {}, 'system': {}}

        for level in differences.keys():
            keys = set(settings1[level].keys()).union(set(settings2[level].keys()))
            for key in keys:
                val1 = settings1[level].get(key)
                val2 = settings2[level].get(key)
                if val1 != val2:
                    differences[level][key] = {'udid1': val1, 'udid2': val2}

        for level, diffs in differences.items():
            if diffs:
                logging.info("Differences found in %s settings (%s keys)", level, len(diffs))
                for key, values in diffs.items():
                    logging.info(
                        "Setting %s differs: %s=%r | %s=%r",
                        key,
                        udid1,
                        values["udid1"],
                        udid2,
                        values["udid2"],
                    )

        logging.info(
            f"РЎСЂР°РІРЅРµРЅРёРµ РЅР°СЃС‚СЂРѕРµРє РґР»СЏ {udid1} Рё {udid2} Р·Р°РІРµСЂС€РµРЅРѕ... РќР°Р№РґРµРЅРѕ СЂР°Р·Р»РёС‡РёР№: {sum(len(d) for d in differences.values())}")
        return differences

    except Exception as e:
        logging.error(f"РћС€РёР±РєР° РїСЂРё СЃСЂР°РІРЅРµРЅРёРё РЅР°СЃС‚СЂРѕРµРє: {e}")
        return {}


def generate_stable_secret():
    groups = [''.join(random.choices(string.hexdigits.lower(), k=4)) for _ in range(8)]
    return ':'.join(groups)


def generate_mac_address():
    # РЎРѕР·РґР°РµРј СЃРїРёСЃРѕРє РёР· 6 СЃР»СѓС‡Р°Р№РЅС‹С… С€РµСЃС‚РЅР°РґС†Р°С‚РµСЂРёС‡РЅС‹С… С‡РёСЃРµР» (0-255)
    mac_parts = [random.randint(0x00, 0xFF) for _ in range(6)]
    mac_address = ":".join(f"{part:02X}" for part in mac_parts)
    return mac_address


def generate_x509_token():
    try:
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )

        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, u"US"),
            x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, u"California"),
            x509.NameAttribute(NameOID.LOCALITY_NAME, u"Mountain View"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, u"Google Inc."),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, u"Android"),
            x509.NameAttribute(NameOID.COMMON_NAME, u"Android Build"),
        ])

        certificate = x509.CertificateBuilder()\
            .subject_name(subject)\
            .issuer_name(issuer)\
            .public_key(private_key.public_key())\
            .serial_number(x509.random_serial_number())\
            .not_valid_before(datetime.utcnow())\
            .not_valid_after(datetime.utcnow() + timedelta(days=365 * 10))\
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=None),
                critical=True
            )\
            .sign(private_key, hashes.SHA256())

        certificate_der = certificate.public_bytes(Encoding.DER)
        certificate_b64 = base64.b64encode(certificate_der).decode()

        return certificate_b64

    except Exception as e:
        logging.error(f"Error generating X.509 token: {e}")
        return None


def reset_data(udid: str, app_for_clear: str, prefix: str | None = None) -> bool:
    """Reset application and device-level identifiers on the Android device.

    This helper will:
      * stop and clear the given application package,
      * reset the Advertising ID,
      * tweak a number of Android settings and system properties (ID, build id,
        language, country, timezone, IMEI, Wi-Fi MAC, etc.).

    It is tailored for Telegram but can be used with any package name that
    supports the same reset pattern.

    :param udid: Device serial / host:port pair.
    :param app_for_clear: Base application package name to clear (e.g. ``org.telegram.messenger``).
    :param prefix: Optional suffix to append to the package name (for cloned apps).
    :return: True on success, False otherwise.
    """
    if prefix:
        app_for_clear += prefix
    try:
        # Check connected devices
        result = subprocess.run([ADB_PATH, "devices"], capture_output=True, text=True)
        if "device" not in result.stdout:
            logging.error("No devices found. Ensure USB Debugging is enabled and device is connected.")
            return False

        # If UDID is provided, use it when constructing adb prefix
        adb_prefix = [ADB_PATH, "-s", udid] if udid else [ADB_PATH]

        # Stop the target app
        subprocess.run(adb_prefix + ["shell", "am", "force-stop", app_for_clear], check=True)
        logging.info("Target app closed successfully")

        # Clear app data
        subprocess.run(adb_prefix + ["shell", "pm", "clear", app_for_clear], check=True)
        logging.info("Target app data cleared successfully")

        # Reset Advertising ID
        subprocess.run(adb_prefix + ["shell", "settings", "delete", "secure", "advertising_id"], check=True)
        logging.info("Advertising ID reset successfully")
        # РЎР±СЂРѕСЃ Advertising ID
        subprocess.run(adb_prefix + ["shell", "pm", "clear", "com.google.android.gms"], check=True)
        logging.info("Google Play Services data cleared successfully")

        new_android_id = generate_android_id()
        new_build_id = generate_android_build_id_past()
        new_cert = generate_x509_token()
        new_mac = generate_mac_address()
        boottimes = generate_boottime_sequence()

        device = random.choice(REAL_DEVICES)

        change_setting('secure', 'android_id', new_android_id)
        change_setting('secure', 'advertising_id', new_android_id)
        change_setting('secure', 'config_update_certificate', new_cert)
        change_setting('global', 'database_creation_buildid', new_build_id)
        change_prop('persist.sys.language', 'en')
        change_prop('persist.sys.country', 'US')
        change_prop('persist.sys.locale', 'en-US')
        change_prop('ro.product.cpu.abi', device['cpu_abi'])
        change_prop('wifi.interface.mac', new_mac)
        change_prop('ro.hardware', device['hardware'])
        change_prop('ro.product.model', device['model'])
        change_prop('ro.product.board', device['board'])
        change_prop('ro.product.name', device['name'])
        change_prop('ro.boottime.init', boottimes[-1])
        set_random_timezone()
        change_imei()
        run_adb_command('am broadcast -a android.intent.action.LOCALE_CHANGED')

        return True
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to reset Telegram data: {e}")
        return False
    except Exception as e:
        logging.error(f"Error during Telegram data reset: {e}")
        return False


if __name__ == '__main__':
    reset_data('XED4C18515000819', 'org.telegram.messenger')

