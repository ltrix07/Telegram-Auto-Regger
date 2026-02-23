from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

from auto_reger.device_controller import DeviceController

LOGGER = logging.getLogger(__name__)


class RegistrationError(RuntimeError):
    """Raised when registration flow fails."""


class TelegramRegistrator:
    TELEGRAM_PACKAGE = "org.telegram.messenger"
    UI_TIMEOUT_SECONDS = 10.0

    START_BUTTON_SELECTORS = (
        {"text": "Start Messaging"},
        {"resourceId": "org.telegram.messenger:id/login_btn"},
    )
    PHONE_CODE_SELECTOR = {"resourceId": "org.telegram.messenger:id/login_phone_code_text"}
    PHONE_NUMBER_SELECTOR = {"resourceId": "org.telegram.messenger:id/login_phone_number_text"}
    NEXT_BUTTON_SELECTORS = (
        {"description": "Done"},
        {"resourceId": "org.telegram.messenger:id/login_btn"},
    )
    CODE_SELECTOR = {"resourceId": "org.telegram.messenger:id/login_code_text"}
    FIRST_NAME_SELECTOR = {"resourceId": "org.telegram.messenger:id/first_name_field"}
    LAST_NAME_SELECTOR = {"resourceId": "org.telegram.messenger:id/last_name_field"}
    TERMS_SELECTORS = (
        {"textMatches": "(?i).*accept.*"},
        {"textMatches": "(?i).*agree.*"},
        {"textMatches": "(?i).*ok.*"},
        {"textMatches": "(?i).*continue.*"},
        {"textMatches": "(?i).*next.*"},
    )
    PROXY_ENABLE_TIMEOUT_SECONDS = 7.0
    PROXY_ENABLE_SELECTORS = (
        {
            "xpath": (
                "//android.widget.TextView["
                "@text='ENABLE' or @text='Enable' or @text='ENABLE PROXY' or @text='Enable Proxy' "
                "or @text='\u0412\u041a\u041b\u042e\u0427\u0418\u0422\u042c' "
                "or @text='\u0412\u043a\u043b\u044e\u0447\u0438\u0442\u044c' "
                "or @text='\u0412\u041a\u041b\u042e\u0427\u0418\u0422\u042c \u041f\u0420\u041e\u041a\u0421\u0418' "
                "or @text='\u0412\u043a\u043b\u044e\u0447\u0438\u0442\u044c \u043f\u0440\u043e\u043a\u0441\u0438'"
                "]"
            )
        },
        {"textMatches": "(?iu)^enable( proxy)?$"},
        {"textMatches": "(?iu)^turn on( proxy)?$"},
        {
            "textMatches": (
                "(?iu)^"
                "\u0432\u043a\u043b\u044e\u0447\u0438\u0442\u044c"
                "( \u043f\u0440\u043e\u043a\u0441\u0438)?$"
            )
        },
        {"resourceId": "android:id/button1"},
        {"resourceId": "org.telegram.messenger:id/button1"},
        {"resourceId": "org.telegram.messenger:id/button_positive"},
        {"resourceId": "org.telegram.messenger:id/positive_button"},
    )

    def __init__(
        self,
        device_controller: DeviceController,
        sms_api: Any,
        sms_poll_interval_seconds: float = 2.0,
        sms_timeout_seconds: int = 180,
    ) -> None:
        self.device_controller = device_controller
        self.sms_api = sms_api
        self.sms_poll_interval_seconds = sms_poll_interval_seconds
        self.sms_timeout_seconds = sms_timeout_seconds

    def register_account(
        self,
        country_code: str,
        names_generator: Callable[[], Any] | Iterable[Any],
        proxy_ip: Optional[str] = None,
        proxy_port: Optional[str] = None,
    ) -> Dict[str, str]:
        normalized_code = self._normalize_country_code(country_code)
        self.device_controller.airplane_mode_toggle()

        proxy_host = str(proxy_ip or "").strip()
        proxy_port_value = str(proxy_port or "").strip()
        if proxy_host and proxy_port_value and hasattr(self.device_controller, "set_telegram_proxy_via_intent"):
            intent_applied = self.device_controller.set_telegram_proxy_via_intent(proxy_host, proxy_port_value)
            if intent_applied:
                try:
                    self._wait_and_enable_proxy_popup(timeout=self.PROXY_ENABLE_TIMEOUT_SECONDS)
                except TimeoutError:
                    LOGGER.warning(
                        "Telegram proxy popup was not detected on time; continuing registration flow."
                    )
            else:
                LOGGER.warning(
                    "Failed to trigger Telegram proxy intent for %s:%s; continuing registration flow.",
                    proxy_host,
                    proxy_port_value,
                )

        activation_id, full_phone_number = self._request_number(normalized_code)
        national_number = self._extract_local_number(full_phone_number, normalized_code)

        self.device_controller.cleanup_telegram()
        self.device_controller.open_telegram()
        self._ensure_telegram_opened()

        self._click_any(self.START_BUTTON_SELECTORS, "Start Messaging button")

        self._fill_text(self.PHONE_CODE_SELECTOR, normalized_code, "country code input")
        self._click(self.PHONE_NUMBER_SELECTOR, "phone number input")
        self._fill_text(self.PHONE_NUMBER_SELECTOR, national_number, "phone number input")

        self._click_any(self.NEXT_BUTTON_SELECTORS, "Next/Done button")

        sms_code = self._wait_for_sms_code(activation_id=activation_id)
        self._fill_text(self.CODE_SELECTOR, sms_code, "SMS code input")

        first_name, last_name = self._generate_names(names_generator)
        self._fill_text(self.FIRST_NAME_SELECTOR, first_name, "first name field")
        self._fill_text(self.LAST_NAME_SELECTOR, last_name, "last name field")

        self._try_click_any(self.TERMS_SELECTORS, timeout=2.0)

        return {
            "activation_id": activation_id or "",
            "phone_number": full_phone_number,
            "country_code": normalized_code,
            "sms_code": sms_code,
            "first_name": first_name,
            "last_name": last_name,
        }

    def _ensure_telegram_opened(self) -> None:
        deadline = time.time() + self.UI_TIMEOUT_SECONDS
        current_package = ""
        while time.time() < deadline:
            current_package = self.device_controller.get_current_package()
            if current_package == self.TELEGRAM_PACKAGE:
                return
            time.sleep(0.5)
        raise RegistrationError(
            f"Telegram is not in foreground. Current package: {current_package or 'unknown'}"
        )

    def _click(self, selector: Dict[str, Any], description: str, timeout: float = UI_TIMEOUT_SECONDS) -> None:
        try:
            self.device_controller.click(selector, timeout=timeout)
        except Exception as exc:
            raise RegistrationError(f"{description} was not found within 10 seconds.") from exc

    def _fill_text(
        self,
        selector: Dict[str, Any],
        text: str,
        description: str,
        timeout: float = UI_TIMEOUT_SECONDS,
    ) -> None:
        try:
            self.device_controller.fill_text(selector, text, timeout=timeout)
        except Exception as exc:
            raise RegistrationError(f"{description} was not found within 10 seconds.") from exc

    def _click_any(self, selectors: Iterable[Dict[str, Any]], description: str) -> None:
        if self._try_click_any(selectors, timeout=self.UI_TIMEOUT_SECONDS):
            return
        raise RegistrationError(f"{description} was not found within 10 seconds.")

    def _try_click_any(self, selectors: Iterable[Dict[str, Any]], timeout: float) -> bool:
        selectors_list = list(selectors)
        if not selectors_list:
            return False

        deadline = time.time() + timeout
        for selector in selectors_list:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                self.device_controller.click(selector, timeout=remaining)
                return True
            except Exception:
                continue
        return False

    def _wait_and_enable_proxy_popup(self, timeout: float = PROXY_ENABLE_TIMEOUT_SECONDS) -> None:
        if hasattr(self.device_controller, "enable_telegram_proxy_popup"):
            self.device_controller.enable_telegram_proxy_popup(timeout=timeout)
            return

        deadline = time.time() + max(timeout, 0.5)
        while time.time() < deadline:
            for selector in self.PROXY_ENABLE_SELECTORS:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                try:
                    self.device_controller.click(selector, timeout=min(1.0, max(remaining, 0.1)))
                    return
                except Exception:
                    continue
            time.sleep(0.2)

        raise TimeoutError("Telegram proxy popup button was not found within timeout.")

    def _request_number(self, country_code: str) -> Tuple[Optional[str], str]:
        payload: Any

        if hasattr(self.sms_api, "get_number"):
            payload = self.sms_api.get_number(country_code)
        elif hasattr(self.sms_api, "verification_number"):
            payload = self.sms_api.verification_number(service="tg", country=country_code)
        elif hasattr(self.sms_api, "getNumber"):
            payload = self.sms_api.getNumber(service="tg", country=country_code)
        else:
            raise RegistrationError("SMS API does not provide a supported number rent method.")

        activation_id, phone_number = self._extract_activation_and_phone(payload)
        if not phone_number:
            raise RegistrationError(f"Failed to obtain phone number from SMS API payload: {payload!r}")
        return activation_id, phone_number

    @staticmethod
    def _extract_activation_and_phone(payload: Any) -> Tuple[Optional[str], Optional[str]]:
        if isinstance(payload, dict):
            activation_id = payload.get("activation_id") or payload.get("activationId") or payload.get("id")
            phone_number = (
                payload.get("phone_number")
                or payload.get("phoneNumber")
                or payload.get("phone")
                or payload.get("number")
            )
            return (str(activation_id) if activation_id is not None else None), (
                str(phone_number) if phone_number is not None else None
            )

        if isinstance(payload, (tuple, list)):
            if len(payload) >= 2:
                return str(payload[0]), str(payload[1])
            if len(payload) == 1:
                return None, str(payload[0])

        if isinstance(payload, str):
            return None, payload

        return None, None

    def _wait_for_sms_code(self, activation_id: Optional[str]) -> str:
        deadline = time.time() + self.sms_timeout_seconds

        while time.time() < deadline:
            code = self._fetch_sms_code_once(activation_id)
            if code:
                return code
            time.sleep(self.sms_poll_interval_seconds)

        raise RegistrationError("SMS code was not received in time.")

    def _fetch_sms_code_once(self, activation_id: Optional[str]) -> str:
        if hasattr(self.sms_api, "get_code"):
            try:
                raw = self.sms_api.get_code(activation_id) if activation_id else self.sms_api.get_code()
            except TypeError:
                raw = self.sms_api.get_code()
            return self._extract_code(raw)

        if hasattr(self.sms_api, "getStatusV2") and activation_id:
            try:
                raw = self.sms_api.getStatusV2(activation_id=activation_id)
            except TypeError:
                raw = self.sms_api.getStatusV2(activation_id)
            return self._extract_code(raw)

        return ""

    @staticmethod
    def _extract_code(raw: Any) -> str:
        if raw is None:
            return ""

        if isinstance(raw, dict):
            value = raw.get("code") or raw.get("smsCode") or raw.get("codeNumber")
            return str(value).strip() if value else ""

        text = str(raw).strip()
        if not text:
            return ""

        if ":" in text and "STATUS_OK" in text:
            _, _, right = text.partition(":")
            return right.strip()

        if text.isdigit():
            return text

        match = re.search(r"\b(\d{4,8})\b", text)
        return match.group(1) if match else ""

    @staticmethod
    def _normalize_country_code(country_code: str) -> str:
        digits = re.sub(r"\D", "", str(country_code))
        if not digits:
            raise RegistrationError(f"Invalid country code: {country_code!r}")
        return f"+{digits}"

    @staticmethod
    def _extract_local_number(full_phone_number: str, country_code: str) -> str:
        phone_digits = re.sub(r"\D", "", str(full_phone_number))
        cc_digits = re.sub(r"\D", "", str(country_code))

        if phone_digits.startswith(cc_digits):
            local = phone_digits[len(cc_digits) :]
        else:
            local = phone_digits

        if not local:
            raise RegistrationError(f"Invalid phone number received from SMS API: {full_phone_number!r}")
        return local

    @staticmethod
    def _generate_names(names_generator: Callable[[], Any] | Iterable[Any]) -> Tuple[str, str]:
        generated: Any

        if callable(names_generator):
            generated = names_generator()
        else:
            try:
                generated = next(iter(names_generator))
            except Exception:
                generated = None

        first_name = "John"
        last_name = "Doe"

        if isinstance(generated, dict):
            first_name = str(generated.get("first_name") or generated.get("first") or first_name)
            last_name = str(generated.get("last_name") or generated.get("last") or last_name)
        elif isinstance(generated, (tuple, list)):
            if len(generated) >= 2:
                first_name = str(generated[0] or first_name)
                last_name = str(generated[1] or last_name)
            elif len(generated) == 1:
                first_name = str(generated[0] or first_name)
        elif isinstance(generated, str) and generated.strip():
            parts = generated.strip().split(maxsplit=1)
            first_name = parts[0]
            if len(parts) > 1:
                last_name = parts[1]

        return first_name.strip() or "John", last_name.strip() or "Doe"
