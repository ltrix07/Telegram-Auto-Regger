from smsactivate.api import SMSActivateAPI
from datetime import datetime
import logging
import os
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional, List

from .utils import PROJECT_ROOT, LOG_FILE


logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(funcName)s() - %(message)s",
    encoding="utf-8",
)

# File used to track active SMS activations
ACTIVATIONS_FILE: Path = PROJECT_ROOT / "activations.json"


# ---------------------------------------------------------------------------
# Helpers for tracking activations locally
# ---------------------------------------------------------------------------

def _load_activations() -> List[Dict[str, Any]]:
    """
    Load the list of tracked activations from ACTIVATIONS_FILE.

    If the file does not exist or is invalid, an empty list is returned.
    """
    if not ACTIVATIONS_FILE.exists():
        return []
    try:
        with open(ACTIVATIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except Exception:
        logging.exception("Failed to read activations file %s", ACTIVATIONS_FILE)
    return []


def _save_activations(data: List[Dict[str, Any]]) -> None:
    """
    Persist the given list of activations to ACTIVATIONS_FILE.
    """
    try:
        ACTIVATIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(ACTIVATIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
    except Exception:
        logging.exception("Failed to write activations file %s", ACTIVATIONS_FILE)


def save_activation_to_json(activation_id: str, phone_number: str) -> None:
    """
    Append a new activation to the activations.json registry.

    This file is used to track which activations can later be safely cancelled
    (status=8) if they are no longer needed.

    :param activation_id: Provider activation id.
    :param phone_number: Phone number that was reserved.
    """
    data = _load_activations()
    now = datetime.utcnow().isoformat()
    data.append(
        {
            "activation_id": str(activation_id),
            "phone_number": str(phone_number),
            "created_at": now,
        }
    )
    _save_activations(data)
    logging.info("Tracked activation %s for phone %s", activation_id, phone_number)


def remove_activation_from_json(activation_id: str) -> None:
    """
    Remove an activation from the local activations.json registry.

    :param activation_id: Provider activation id to remove.
    """
    data = _load_activations()
    before = len(data)
    data = [row for row in data if str(row.get("activation_id")) != str(activation_id)]
    after = len(data)
    _save_activations(data)
    logging.info(
        "Removed activation %s from registry (before=%s, after=%s)",
        activation_id,
        before,
        after,
    )


def can_set_status_8(activation_id: str, min_age_seconds: int = 120) -> bool:
    """
    Determine whether we can safely set status=8 (cancel) for the given activation.

    Some providers do not allow cancelling an activation immediately after
    reservation. To avoid API errors, we only return True if the activation
    is at least ``min_age_seconds`` old.

    :param activation_id: Provider activation id.
    :param min_age_seconds: Minimal age in seconds to allow cancellation.
    :return: True if the activation is old enough, False otherwise.
    """
    data = _load_activations()
    for row in data:
        if str(row.get("activation_id")) != str(activation_id):
            continue
        created_at = row.get("created_at")
        if not created_at:
            return True  # if timestamp is missing, be optimistic
        try:
            created_dt = datetime.fromisoformat(created_at)
        except ValueError:
            return True
        age = (datetime.utcnow() - created_dt).total_seconds()
        return age >= min_age_seconds
    # Not found → nothing to cancel
    return False


# ---------------------------------------------------------------------------
# SMS API wrapper
# ---------------------------------------------------------------------------

class SmsApi(SMSActivateAPI):
    """
    Thin wrapper around `smsactivate.api.SMSActivateAPI` with a few convenience
    helpers used by this project.

    Features:
      * Read API key from a local file.
      * Support multiple compatible providers (e.g. sms-activate, grizzly-sms, hero-sms).
      * Helper methods for country lookup, number rental and status polling.
    """

    _SERVICE_ALIAS_MAP: Dict[str, str] = {
        "sms-activate": "sms-activate",
        "smsactivate": "sms-activate",
        "grizzly-sms": "grizzly-sms",
        "grizzlysms": "grizzly-sms",
        "grizzly_sms": "grizzly-sms",
        "hero-sms": "hero-sms",
        "herosms": "hero-sms",
        "hero_sms": "hero-sms",
    }

    _SERVICE_URLS: Dict[str, str] = {
        "sms-activate": "https://api.sms-activate.org/stubs/handler_api.php",
        "grizzly-sms": "https://api.grizzlysms.com/stubs/handler_api.php",
        "hero-sms": "https://hero-sms.com/stubs/handler_api.php",
    }

    _KNOWN_ERROR_CODES = {
        "NO_NUMBERS",
        "NO_BALANCE",
        "BAD_ACTION",
        "BAD_SERVICE",
        "BAD_KEY",
        "ERROR_SQL",
        "SQL_ERROR",
        "NO_ACTIVATION",
        "BAD_STATUS",
        "STATUS_CANCEL",
        "BANNED",
        "NO_CONNECTION",
        "ACCOUNT_INACTIVE",
        "NO_ID_RENT",
        "INVALID_PHONE",
        "STATUS_FINISH",
        "INCORECT_STATUS",
        "CANT_CANCEL",
        "ALREADY_FINISH",
        "ALREADY_CANCEL",
        "WRONG_OPERATOR",
        "NO_YULA_MAIL",
        "WHATSAPP_NOT_AVAILABLE",
        "NOT_INCOMING",
        "INVALID_ACTIVATION_ID",
        "WRONG_ADDITIONAL_SERVICE",
        "WRONG_ACTIVATION_ID",
        "WRONG_SECURITY",
        "REPEAT_ADDITIONAL_SERVICE",
        "NO_KEY",
        "OPERATORS_NOT_FOUND",
        "EARLY_CANCEL_DENIED",
        "ERROR",
    }

    def __init__(
        self,
        service: str,
        api_key_path: str,
        api_url: Optional[str] = None,
    ) -> None:
        """
        :param service: Provider identifier. Supported aliases include:
                        ``"sms-activate"``, ``"grizzly-sms"``, ``"hero-sms"``.
        :param api_key_path: Path to a file containing API key (single line).
        :param api_url: Optional full handler API URL override.
                        Priority: explicit ``api_url`` > URL by ``service``.
        """
        api_key_path = os.path.expanduser(api_key_path)
        if not os.path.exists(api_key_path):
            raise FileNotFoundError(f"API key file not found: {api_key_path}")

        with open(api_key_path, "r", encoding="utf-8") as f:
            api_key = f.read().strip()

        if not api_key:
            raise ValueError(f"API key file {api_key_path} is empty")

        super().__init__(api_key)

        self.service_name = self._normalize_service_name(service)
        resolved_api_url = str(api_url).strip() if api_url else self._SERVICE_URLS.get(self.service_name)
        if not resolved_api_url:
            raise ValueError(f"Unsupported SMS service: {service}")

        self._apply_provider_url(resolved_api_url)

        logging.info(
            "SmsApi initialised for service=%s key_file=%s api_url=%s",
            self.service_name,
            api_key_path,
            self.api_url,
        )

    # ------------------------------------------------------------------ utils

    @classmethod
    def _normalize_service_name(cls, service: str) -> str:
        """
        Convert user-provided provider alias into canonical service name.
        """
        normalized = str(service or "").strip().lower()
        resolved = cls._SERVICE_ALIAS_MAP.get(normalized)
        if not resolved:
            raise ValueError(f"Unsupported SMS service: {service}")
        return resolved

    def _apply_provider_url(self, api_url: str) -> None:
        """
        Ensure provider URL override is applied to the underlying dependency.

        `smsactivate==1.5` stores URL in private field `_SMSActivateAPI__api_url`.
        We set this field directly and also mirror to common alias fields for
        forward compatibility with other library versions.
        """
        self.api_url = api_url
        self.url = api_url
        self.base_url = api_url
        self._url = api_url
        setattr(self, "_SMSActivateAPI__api_url", api_url)

        applied_url = getattr(self, "_SMSActivateAPI__api_url", None)
        if applied_url != api_url:
            logging.warning("Failed to apply provider URL override, expected=%s got=%s", api_url, applied_url)

    @classmethod
    def _is_known_error_string(cls, value: str) -> bool:
        normalized = str(value or "").strip().upper()
        if not normalized:
            return False
        head = normalized.split(":", 1)[0].strip()
        return head in cls._KNOWN_ERROR_CODES

    @classmethod
    def _is_provider_error_response(cls, response: Any) -> bool:
        if isinstance(response, dict):
            if response.get("title"):
                return True
            error_value = str(response.get("error", "")).strip()
            return bool(error_value)
        if isinstance(response, str):
            return cls._is_known_error_string(response)
        return False

    @staticmethod
    def _format_provider_error(response: Any) -> str:
        if isinstance(response, dict):
            title = str(response.get("title", "")).strip()
            details = str(response.get("details", "")).strip()
            if title:
                return f"{title}: {details}" if details else title

            error_code = str(response.get("error", "")).strip()
            message = str(response.get("message", "")).strip()
            if error_code and message:
                return f"{error_code}: {message}"
            if error_code:
                return error_code

        if isinstance(response, str):
            return response.strip()

        return str(response)

    @staticmethod
    def _extract_verification_code(status_payload: Any) -> Optional[str]:
        if isinstance(status_payload, dict):
            candidates: List[Any] = [
                status_payload.get("smsCode"),
                status_payload.get("code"),
                status_payload.get("codeNumber"),
            ]

            sms_data = status_payload.get("sms")
            if isinstance(sms_data, dict):
                candidates.append(sms_data.get("code"))

            call_data = status_payload.get("call")
            if isinstance(call_data, dict):
                candidates.append(call_data.get("code"))

            for item in candidates:
                if item is None:
                    continue
                code = str(item).strip()
                if code:
                    return code
            return None

        if isinstance(status_payload, str):
            text = status_payload.strip()
            if text.upper().startswith("STATUS_OK") and ":" in text:
                _, _, maybe_code = text.partition(":")
                code = maybe_code.strip()
                return code or None

        return None

    def _cancel_activation_if_allowed(self, activation_id: str) -> None:
        if not can_set_status_8(activation_id):
            logging.info(
                "Skipping status=8 for activation %s: activation is too new for cancellation",
                activation_id,
            )
            return

        try:
            cancel_response = self.setStatus(activation_id, status=8)
            if self._is_provider_error_response(cancel_response):
                message = self._format_provider_error(cancel_response)
                if "EARLY_CANCEL_DENIED" in message.upper():
                    logging.info(
                        "Provider denied status=8 for activation %s: %s",
                        activation_id,
                        message,
                    )
                else:
                    logging.warning(
                        "Provider returned error for status=8 activation %s: %s",
                        activation_id,
                        message,
                    )
        except Exception:
            logging.exception("Failed to set status=8 for activation %s", activation_id)

    def _fetch_status_payload(self, activation_id: str) -> Any:
        """
        Fetch activation status using getStatusV2 if available, then fallback to getStatus.
        """
        if hasattr(self, "getStatusV2"):
            try:
                status_v2 = self.getStatusV2(activation_id=activation_id)
            except TypeError:
                status_v2 = self.getStatusV2(activation_id)
            except Exception:
                logging.exception("Error calling getStatusV2 for %s", activation_id)
            else:
                if isinstance(status_v2, dict):
                    status_v2_error = str(status_v2.get("error", "")).strip().upper()
                    if status_v2_error != "BAD_ACTION":
                        return status_v2
                elif isinstance(status_v2, str):
                    status_v2_head = status_v2.strip().upper().split(":", 1)[0].strip()
                    if status_v2_head != "BAD_ACTION":
                        return status_v2
                else:
                    return status_v2

                logging.info(
                    "getStatusV2 is not supported by provider for activation %s, fallback to getStatus",
                    activation_id,
                )

        try:
            return self.getStatus(id=activation_id)
        except TypeError:
            return self.getStatus(activation_id)

    def _get_country_id(self, country_name: Any) -> int:
        """
        Resolve a human-readable country name to provider country id.

        The method queries the provider using ``getCountries()`` and matches
        against the English name (``eng``) or Russian name (``rus``) from
        the response.

        :param country_name: Country name in English/Russian as returned by provider
                             or numeric country id.
        :return: Integer country id used by the provider.
        :raises ValueError: If the country cannot be resolved.
        """
        if isinstance(country_name, int):
            return country_name

        country_raw = str(country_name or "").strip()
        if country_raw.isdigit():
            return int(country_raw)

        if not country_raw:
            raise ValueError("Country name cannot be empty")

        countries = self.getCountries()
        if self._is_provider_error_response(countries):
            provider_error = self._format_provider_error(countries)
            raise ValueError(f"Failed to fetch countries from provider: {provider_error}")

        country_name_norm = country_raw.lower()

        if isinstance(countries, list):
            for item in countries:
                if not isinstance(item, dict):
                    continue
                eng = str(item.get("eng", "")).strip().lower()
                rus = str(item.get("rus", "")).strip().lower()
                if country_name_norm not in (eng, rus):
                    continue

                country_id = item.get("id")
                if country_id is None:
                    continue
                try:
                    return int(country_id)
                except (TypeError, ValueError):
                    continue

        elif isinstance(countries, dict):
            for cid, info in countries.items():
                if not isinstance(info, dict):
                    continue
                eng = str(info.get("eng", "")).strip().lower()
                rus = str(info.get("rus", "")).strip().lower()
                if country_name_norm in (eng, rus):
                    return int(cid)

        raise ValueError(
            f"Unknown country for SMS provider: {country_name!r}. "
            f"Supported countries payload type: {type(countries).__name__}"
        )

    # ----------------------------------------------------------------- public

    def get_numbers_status(self, service: str, country: str) -> Dict[str, Any]:
        """
        Get numbers availability status for a given service and country.

        This is a small convenience wrapper around ``getNumbersStatus`` that
        resolves country name to id and returns only one service entry.

        :param service: Service short name (e.g. "tg" for Telegram).
        :param country: Country name in English/Russian as understood by provider.
        :return: Dict with provider-specific status information for the service.
        """
        country_id = self._get_country_id(country)
        numbers_status = self.getNumbersStatus(country_id)
        return numbers_status.get(service, {})

    def verification_number(
        self,
        service: str,
        country: str,
        max_price: Optional[float] = None,
        country_id: Optional[int] = None,
        operator: Optional[str] = None,
    ) -> Dict[str, Any]:
        if country_id is None:
            resolved_country_id = self._get_country_id(country)
        else:
            try:
                resolved_country_id = int(country_id)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid country_id value: {country_id!r}") from exc

        kwargs: Dict[str, Any] = {"service": service, "country": int(resolved_country_id)}

        if operator:
            kwargs["operator"] = operator

        normalized_max_price: Optional[float] = None
        if max_price is not None:
            normalized_max_price = float(max_price)
            kwargs["maxPrice"] = normalized_max_price

        logging.info(
            "Requesting number: service=%s country=%s (id=%s) operator=%s max_price=%s",
            service,
            country,
            resolved_country_id,
            operator or "any",
            max_price,
        )

        try:
            # Метод getNumberV2 из базового класса SMSActivateAPI отправит эти kwargs в URL
            resp: Any = self.getNumberV2(**kwargs)
        except TypeError as e:
            if normalized_max_price is not None and "maxPrice" in str(e):
                fallback_kwargs = dict(kwargs)
                fallback_kwargs.pop("maxPrice", None)
                fallback_kwargs["max_price"] = normalized_max_price
                try:
                    resp = self.getNumberV2(**fallback_kwargs)
                except Exception as retry_error:
                    logging.exception("Error requesting number from SMS API: %s", retry_error)
                    return {"error": str(retry_error)}
            else:
                logging.exception("Error requesting number from SMS API: %s", e)
                return {"error": str(e)}
        except Exception as e:
            logging.exception("Error requesting number from SMS API: %s", e)
            return {"error": str(e)}

        if self._is_provider_error_response(resp):
            provider_error = self._format_provider_error(resp)
            logging.warning("SMS provider returned getNumberV2 error: %s", provider_error)
            return {"error": provider_error}

        if not isinstance(resp, dict):
            provider_error = self._format_provider_error(resp)
            logging.warning("Unexpected getNumberV2 response type: %s", type(resp).__name__)
            return {"error": provider_error}

        if "phoneNumber" not in resp and "number" in resp:
            resp["phoneNumber"] = resp["number"]

        if "activationId" not in resp and "id" in resp:
            resp["activationId"] = resp["id"]

        return resp

    def get_price(self, service: str, country: str) -> Dict[str, Any]:
        """
        Get current prices for a given service/country pair.

        Used in statistic collector to compare SMS costs between providers.

        :param service: Service short name (e.g. "tg").
        :param country: Country name in English/Russian.
        :return: Raw provider response from ``getPrices``.
        """
        country_id = self._get_country_id(country)
        try:
            resp = self.getPrices(service, country_id)
            return resp
        except Exception as e:
            logging.exception(
                "Error getting prices for service=%s country=%s (id=%s): %s",
                service,
                country,
                country_id,
                e,
            )
            return {}

    def check_verif_status(
        self,
        activation_id: str,
        timeout: int = 180,
        poll_interval: int = 5,
    ) -> str:
        """
        Poll the provider until an SMS code is received or timeout is reached.

        For most compatible services, this uses ``getStatusV2`` which usually
        returns a dict or a status string. If the code is successfully obtained,
        the activation is marked as finished (status=6).

        On timeout, the activation is cancelled with status=8 when possible.

        :param activation_id: Provider activation id to check.
        :param timeout: Maximum wait time in seconds.
        :param poll_interval: Delay between polling attempts in seconds.
        :return: SMS code as string if received, or an empty string otherwise.
        """
        deadline = time.time() + timeout

        logging.info(
            "Waiting for SMS code, activation_id=%s (timeout=%ss)",
            activation_id,
            timeout,
        )

        while time.time() < deadline:
            try:
                status = self._fetch_status_payload(activation_id)
            except Exception:
                logging.exception("Error fetching verification status for %s", activation_id)
                time.sleep(poll_interval)
                continue

            if not status:
                time.sleep(poll_interval)
                continue

            if self._is_provider_error_response(status):
                provider_error = self._format_provider_error(status)
                logging.warning(
                    "Provider returned status error for activation %s: %s",
                    activation_id,
                    provider_error,
                )
                return ""

            code = self._extract_verification_code(status)

            if isinstance(status, dict):
                current_status = (
                    status.get("status")
                    or status.get("statusText")
                    or status.get("verificationType")
                    or "unknown"
                )
            else:
                current_status = str(status)

            logging.debug("Activation %s status: %s", activation_id, current_status)

            if code:
                logging.info("Received SMS code for activation %s", activation_id)
                # Attempt to mark as successfully finished
                try:
                    self.setStatus(activation_id, status=6)
                except Exception:
                    logging.exception(
                        "Failed to set status=6 for activation %s",
                        activation_id,
                    )
                return code

            time.sleep(poll_interval)

        # Timeout reached – try to cancel the activation
        logging.warning(
            "No SMS code received for activation %s within %s seconds, cancelling",
            activation_id,
            timeout,
        )
        self._cancel_activation_if_allowed(activation_id)

        logging.warning("SMS code was not received within the timeout")
        return ""


if __name__ == "__main__":
    # Small manual test stub; replace path/service with your own if needed.
    api_key_file = os.environ.get("SMS_API_KEY_FILE", "sms_activate_api.txt")
    sms = SmsApi(service="sms-activate", api_key_path=api_key_file)
    logging.info("Balance: %s", sms.getBalance())
