#!/usr/bin/env bash
# init-device.sh — пост-загрузочная инициализация Android через ADB.
#
# Выполняет после старта ReDroid:
#   1. Ждёт sys.boot_completed=1
#   2. Устанавливает случайный android_id
#   3. Имитирует состояние батареи реального устройства
#
# Использование:
#   bash init-device.sh [--host 127.0.0.1] [--port 5555] [--timeout 120]
#
# Переменные среды (альтернатива флагам):
#   ADB_HOST, ADB_PORT, BOOT_TIMEOUT
set -euo pipefail

# ── Аргументы / дефолты ───────────────────────────────────────────────────────
ADB_HOST="${ADB_HOST:-127.0.0.1}"
ADB_PORT="${ADB_PORT:-5555}"
BOOT_TIMEOUT="${BOOT_TIMEOUT:-120}"   # секунды
POLL_INTERVAL=3

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)    ADB_HOST="$2";    shift 2 ;;
    --port)    ADB_PORT="$2";    shift 2 ;;
    --timeout) BOOT_TIMEOUT="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

DEVICE="${ADB_HOST}:${ADB_PORT}"

# ── Вспомогательные функции ───────────────────────────────────────────────────

log()  { echo "[$(date +%H:%M:%S)] $*"; }
fail() { echo "[$(date +%H:%M:%S)] ERROR: $*" >&2; exit 1; }

adb_shell() {
  # Обёртка: завершается с ненулевым кодом если adb возвращает ошибку
  adb -s "${DEVICE}" shell "$@" 2>/dev/null | tr -d '\r'
}

# ── Проверка зависимостей ─────────────────────────────────────────────────────
command -v adb  >/dev/null 2>&1 || fail "adb не найден в PATH"
command -v openssl >/dev/null 2>&1 || fail "openssl не найден в PATH"

# ── 1. Подключение к устройству ───────────────────────────────────────────────
log "Подключение к ${DEVICE}..."
adb connect "${DEVICE}" > /dev/null 2>&1

# ── 2. Ожидание полной загрузки Android ──────────────────────────────────────
log "Ожидание sys.boot_completed (таймаут: ${BOOT_TIMEOUT}с)..."
elapsed=0

until [[ "$(adb_shell getprop sys.boot_completed)" == "1" ]]; do
  if (( elapsed >= BOOT_TIMEOUT )); then
    fail "Устройство ${DEVICE} не загрузилось за ${BOOT_TIMEOUT}с."
  fi
  sleep "${POLL_INTERVAL}"
  (( elapsed += POLL_INTERVAL ))
  log "  ...ждём (${elapsed}/${BOOT_TIMEOUT}с)"
done

log "Android загружен за ${elapsed}с."

# Дополнительная пауза — ждём пока Package Manager станет доступен
sleep 3

# ── 3. Случайный android_id ───────────────────────────────────────────────────
# 8 байт = 16 hex-символов нижнего регистра (формат Android)
ANDROID_ID=$(openssl rand -hex 8)
log "Устанавливаем android_id: ${ANDROID_ID}"
adb_shell settings put secure android_id "${ANDROID_ID}"

# Верификация
STORED_ID=$(adb_shell settings get secure android_id)
if [[ "${STORED_ID}" != "${ANDROID_ID}" ]]; then
  fail "Верификация android_id провалилась: ожидалось ${ANDROID_ID}, получено ${STORED_ID}"
fi
log "android_id подтверждён: ${STORED_ID}"

# ── 4. Имитация состояния батареи ────────────────────────────────────────────
# Диапазоны: level 65–95%, temperature 28–32°C (API: *10), status=3 (discharging)
BATTERY_LEVEL=$(( RANDOM % 31 + 65 ))        # 65–95
BATTERY_TEMP=$(( RANDOM % 50 + 280 ))         # 280–330 (28.0–33.0°C * 10)

log "Настройка батареи: ${BATTERY_LEVEL}%, temp=$(( BATTERY_TEMP / 10 )).$(( BATTERY_TEMP % 10 ))°C"

# Отключаем все источники питания → имитируем автономную работу
adb_shell dumpsys battery set ac 0
adb_shell dumpsys battery set usb 0
adb_shell dumpsys battery set wireless 0

# Устанавливаем уровень и температуру
adb_shell dumpsys battery set level "${BATTERY_LEVEL}"
adb_shell dumpsys battery set temperature "${BATTERY_TEMP}"

# status: 1=unknown, 2=charging, 3=discharging, 4=not charging, 5=full
adb_shell dumpsys battery set status 3

# ── 5. Итоговый отчёт ────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════"
echo " Устройство   : ${DEVICE}"
echo " android_id   : ${ANDROID_ID}"
echo " Battery      : ${BATTERY_LEVEL}% (discharging)"
echo " Temperature  : $(( BATTERY_TEMP / 10 )).$(( BATTERY_TEMP % 10 ))°C"
echo " AC / USB / Wireless: OFF"
echo "════════════════════════════════════════"
log "Инициализация завершена. Устройство готово к тестам."
