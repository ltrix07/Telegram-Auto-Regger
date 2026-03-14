from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Dict

from .registration import TelegramRegistrator, RegistrationError
from .device_controller import DeviceController

LOGGER = logging.getLogger(__name__)


class TelegramRegistratorWithVideo(TelegramRegistrator):
    """
    Extends TelegramRegistrator to support split video recording during registration.
    This avoids the 3-minute recording limit by pausing the recording while waiting
    for the SMS code.
    """

    def __init__(
        self,
        device_controller: DeviceController,
        sms_api: Any,
        sms_poll_interval_seconds: float = 2.0,
        sms_timeout_seconds: int = 180,
        video_output_dir: str | Path = 'videos'
    ) -> None:
        super().__init__(
            device_controller=device_controller,
            sms_api=sms_api,
            sms_poll_interval_seconds=sms_poll_interval_seconds,
            sms_timeout_seconds=sms_timeout_seconds,
        )
        self.video_output_dir = Path(video_output_dir)
        self.video_output_dir.mkdir(parents=True, exist_ok=True)

    def register_account_with_video(
        self,
        country_code: str,
        names_generator: Callable[[], Any] | Iterable[Any],
        proxy_ip: Optional[str] = None,
        proxy_port: Optional[int] = None,
        proxy_user: Optional[str] = None,
        proxy_pass: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Performs Telegram registration with a two-part video recording.

        - Part 1: Records from the start until the code is requested.
        - Part 2: Records from code submission to the end of the flow.
        """
        # === ТУТ ЛОГИКА ПОДГОТОВКИ, аналог вашего register_account ===
        normalized_code = self._normalize_country_code(country_code)
        
        # Запрос номера телефона у SMS-сервиса (без UI-действий)
        activation_id, full_phone_number = self._request_number(normalized_code)
        national_number = self._extract_local_number(full_phone_number, normalized_code)
        
        # Создаем уникальные имена для видеофайлов
        timestamp = int(time.time())
        record_part1_path = self.video_output_dir / f"{full_phone_number}_{timestamp}_part1_request.mp4"
        record_part2_path = self.video_output_dir / f"{full_phone_number}_{timestamp}_part2_login.mp4"

        # =============================================================
        # === ЧАСТЬ 1: ЗАПУСК ЗАПИСИ И ЗАПРОС КОДА                  ===
        # =============================================================
        LOGGER.info("Starting video recording for part 1: Code Request")
        # Вместо прямого вызова adb используем методы из DeviceController
        record_proc_part1 = self.device_controller.start_recording(
            remote_path=f"/sdcard/record_part1_{timestamp}.mp4"
        )

        try:
            try:
                # === ТУТ ВАШ UI-КОД ДЛЯ ВВОДА НОМЕРА ===
                # Примерная последовательность на основе вашего скрипта registration.py
                self.device_controller.cleanup_telegram()
                if proxy_ip and proxy_port:
                    self.device_controller.set_telegram_proxy_via_intent(
                        host=proxy_ip,
                        port=proxy_port,
                        username=proxy_user,
                        password=proxy_pass,
                    )
                self.device_controller.open_telegram()
                self._ensure_telegram_opened()

                self._click_any(self.START_BUTTON_SELECTORS, "Start Messaging button")
                
                self._fill_text(self.PHONE_CODE_SELECTOR, normalized_code, "country code input")
                self._click(self.PHONE_NUMBER_SELECTOR, "phone number input")
                self._fill_text(self.PHONE_NUMBER_SELECTOR, national_number, "phone number input")
                self._click_any(self.NEXT_BUTTON_SELECTORS, "Next/Done button")
                # === КОНЕЦ UI-КОДА ДЛЯ ВВОДА НОМЕРА ===

                LOGGER.info("Reached the 'Enter Code' screen.")
            except Exception as e:
                LOGGER.error("Error during registration part 1: %s", e, exc_info=True)
                raise RegistrationError(
                    f"Failure during registration part 1: {e}",
                    video_paths={"video_part1": str(record_part1_path)},
                ) from e
        finally:
            # Останавливаем первую запись ВНЕ зависимости от успеха
            LOGGER.info("Stopping video recording for part 1.")
            self.device_controller.stop_recording_and_pull(
                proc=record_proc_part1,
                remote_path=f"/sdcard/record_part1_{timestamp}.mp4",
                local_path=str(record_part1_path),
            )
            LOGGER.info(f"Part 1 video saved to {record_part1_path}")

        # =============================================================
        # === ОЖИДАНИЕ SMS-КОДА (БЕЗ ЗАПИСИ)                        ===
        # =============================================================
        LOGGER.info("Waiting for SMS code from activation service (no recording)...")
        try:
            sms_code = self._wait_for_sms_code(activation_id=activation_id)
            LOGGER.info("Successfully received SMS code.")
        except RegistrationError as e:
            LOGGER.error("Failed to receive SMS code: %s", e)
            # Если код не пришел, вторая часть видео не будет записана, и мы выйдем.
            e.video_paths["video_part1"] = str(record_part1_path)
            raise

        # =============================================================
        # === ЧАСТЬ 2: ВВОД КОДА И ЗАВЕРШЕНИЕ РЕГИСТРАЦИИ           ===
        # =============================================================
        LOGGER.info("Starting video recording for part 2: Login and Finalization")
        record_proc_part2 = self.device_controller.start_recording(
            remote_path=f"/sdcard/record_part2_{timestamp}.mp4"
        )
        
        telethon_code = None
        try:
            try:
                # === ТУТ ВАШ UI-КОД ДЛЯ ВВОДА КОДА И РЕГИСТРАЦИИ ===
                self._fill_text(self.CODE_SELECTOR, sms_code, "SMS code input")

                # Может появиться экран с именем/фамилией
                try:
                    first_name, last_name = self._generate_names(names_generator)
                    self._fill_text(self.FIRST_NAME_SELECTOR, first_name, "first name field", timeout=5.0)
                    self._fill_text(self.LAST_NAME_SELECTOR, last_name, "last name field")
                    self._try_click_any(self.NEXT_BUTTON_SELECTORS, timeout=3.0)
                except RegistrationError:
                    LOGGER.info("First/last name screen was not detected, skipping.")

                # Обработка всплывающих окон (разрешения, контакты и т.д.)
                # Этот метод можно вызывать в разных точках, если нужно
                if hasattr(self.device_controller, '_handle_post_action_popups'):
                     self.device_controller._handle_post_action_popups(rounds=3, include_accept=True)

                LOGGER.info("Registration/login completed. Proceeding to get Telethon code.")
                
                # --- Логика получения кода для Telethon ---
                # Эта часть предполагает, что у вас где-то есть код, который инициирует
                # отправку кода авторизации в системный чат Telegram.
                # Здесь мы просто ждем этот код на экране.
                
                # 1. Открываем системный чат Telegram
                self.device_controller.open_telegram_system_chat()
                
                # 2. Читаем код с экрана
                telethon_code = self.device_controller.read_telegram_code_from_screen(timeout=60)
                LOGGER.info("Successfully retrieved Telethon login code: %s", telethon_code)

                # === КОНЕЦ UI-КОДА ДЛЯ РЕГИСТРАЦИИ ===
            except Exception as e:
                LOGGER.error("Error during registration part 2: %s", e, exc_info=True)
                raise RegistrationError(
                    f"Failure during registration part 2: {e}",
                    video_paths={
                        "video_part1": str(record_part1_path),
                        "video_part2": str(record_part2_path),
                    },
                ) from e
            
            return {
                "activation_id": activation_id or "",
                "phone_number": full_phone_number,
                "country_code": normalized_code,
                "sms_code": sms_code,
                "telethon_code": telethon_code,
                "video_part1": str(record_part1_path),
                "video_part2": str(record_part2_path),
            }

        finally:
            # Останавливаем вторую запись ВНЕ зависимости от успеха
            LOGGER.info("Stopping video recording for part 2.")
            self.device_controller.stop_recording_and_pull(
                proc=record_proc_part2,
                remote_path=f"/sdcard/record_part2_{timestamp}.mp4",
                local_path=str(record_part2_path),
            )
            LOGGER.info(f"Part 2 video saved to {record_part2_path}")
