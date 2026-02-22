from __future__ import annotations

import time
from typing import Any, Dict

from auto_reger.device_controller import DeviceController
from auto_reger.registration import RegistrationError, TelegramRegistrator


class MockSmsApi:
    """
    Local debug stub to test full registration UI flow without spending SMS credits.
    """

    def __init__(self, phone_number: str = "+48123456789", sms_code: str = "12345", code_delay_seconds: int = 8):
        self.phone_number = phone_number
        self.sms_code = sms_code
        self.code_delay_seconds = code_delay_seconds
        self._activation_id = "mock-activation-id"
        self._start_time = time.time()

    def get_number(self, country_code: str) -> Dict[str, Any]:
        _ = country_code  # kept for interface compatibility
        self._start_time = time.time()
        return {
            "activation_id": self._activation_id,
            "phone_number": self.phone_number,
        }

    def get_code(self, activation_id: str | None = None) -> str:
        _ = activation_id
        if time.time() - self._start_time >= self.code_delay_seconds:
            return self.sms_code
        return ""


def names_generator() -> tuple[str, str]:
    return "Test", "User"


def _build_sms_api() -> Any:
    use_real_sms = input("Use real SMS API? (y/N): ").strip().lower() == "y"

    if not use_real_sms:
        return MockSmsApi()

    from auto_reger.sms_api import SmsApi

    service = input("SMS service [sms-activate]: ").strip() or "sms-activate"
    api_key_path = input("SMS API key file [secrets/sms_api_key.txt]: ").strip() or "secrets/sms_api_key.txt"
    sms_country = input("SMS country name [Poland]: ").strip() or "Poland"

    class RealSmsApiAdapter:
        def __init__(self, wrapped_api: SmsApi, country: str):
            self.wrapped_api = wrapped_api
            self.country = country

        def get_number(self, country_code: str) -> Dict[str, Any]:
            _ = country_code
            data = self.wrapped_api.verification_number(service="tg", country=self.country)
            return {
                "activation_id": data.get("activationId") or data.get("id"),
                "phone_number": data.get("phoneNumber") or data.get("phone") or data.get("number"),
            }

        def get_code(self, activation_id: str | None = None) -> str:
            if not activation_id:
                return ""
            try:
                status = self.wrapped_api.getStatusV2(activation_id=activation_id)
            except TypeError:
                status = self.wrapped_api.getStatusV2(activation_id)

            if isinstance(status, dict):
                code = status.get("smsCode") or status.get("code") or status.get("codeNumber")
                return str(code).strip() if code else ""

            text = str(status)
            if "STATUS_OK" in text and ":" in text:
                return text.split(":", 1)[1].strip()
            return ""

    sms_api = SmsApi(service=service, api_key_path=api_key_path)
    return RealSmsApiAdapter(sms_api, sms_country)


def main() -> None:
    serial = input("Enter ADB device serial: ").strip()
    if not serial:
        raise SystemExit("Device serial is required.")

    country_code = input("Enter country code [+48]: ").strip() or "+48"

    controller = DeviceController(serial)
    sms_api = _build_sms_api()
    registrator = TelegramRegistrator(device_controller=controller, sms_api=sms_api)

    try:
        result = registrator.register_account(country_code=country_code, names_generator=names_generator)
    except RegistrationError as exc:
        print(f"Registration failed: {exc}")
        raise SystemExit(1)

    print("Registration flow completed.")
    print(result)


if __name__ == "__main__":
    main()

