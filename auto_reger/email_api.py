from __future__ import annotations

import logging
import re
import time
from typing import Dict, Tuple

import requests


LOGGER = logging.getLogger(__name__)


class EmailApi:
    """
    Minimal wrapper for kopeechka.store temporary email API.

    Supported operations:
      * allocate an email inbox for Telegram flow,
      * poll and parse confirmation code from message text.
    """

    def __init__(
        self,
        api_token: str,
        base_url: str = "https://api.kopeechka.store",
        request_timeout: int = 20,
    ) -> None:
        """
        :param api_token: kopeechka API token.
        :param base_url: API base URL.
        :param request_timeout: HTTP timeout in seconds.
        """
        token = str(api_token).strip()
        if not token:
            raise ValueError("Kopeechka API token is required.")

        self.api_token = token
        self.base_url = base_url.rstrip("/")
        self.request_timeout = int(request_timeout)

    def get_email(self, site: str = "telegram.org", mail_type: str = "OUTLOOK") -> Tuple[str, str]:
        """
        Reserve a mailbox at kopeechka for a target site.

        :param site: Target website domain, default `telegram.org`.
        :param mail_type: Mail provider type, default `OUTLOOK`.
        :return: Tuple `(task_id, email_address)`.
        """
        LOGGER.info("Requesting mailbox from Kopeechka for site=%s mail_type=%s", site, mail_type)
        payload = self._request(
            "mailbox-get-email",
            {
                "site": site,
                "mail_type": mail_type,
            },
        )

        task_id = str(payload.get("id") or payload.get("mail_id") or "").strip()
        email_address = str(payload.get("mail") or payload.get("email") or "").strip()
        if not task_id or not email_address:
            raise RuntimeError(f"Kopeechka returned unexpected mailbox payload: {payload!r}")

        LOGGER.info("Kopeechka mailbox allocated: id=%s email=%s", task_id, email_address)
        return task_id, email_address

    def wait_for_email_code(self, task_id: str, timeout: int = 120) -> str:
        """
        Poll kopeechka mailbox until a 5-6 digit verification code is received.

        :param task_id: ID from `get_email`.
        :param timeout: Max waiting time in seconds.
        :return: Verification code.
        :raises TimeoutError: If code was not found before timeout.
        """
        if not str(task_id).strip():
            raise ValueError("task_id is required.")

        deadline = time.time() + int(timeout)
        LOGGER.info("Waiting for email code task_id=%s timeout=%ss", task_id, timeout)

        while time.time() < deadline:
            payload = self._request(
                "mailbox-get-message",
                {
                    "id": str(task_id),
                    "full": 1,
                },
                fail_on_error=False,
            )

            status = str(payload.get("status", "")).upper()
            value = str(payload.get("value", "")).upper()
            if status == "ERROR" or value in {"WAIT_LINK", "WAITING", "WAIT_MAIL", "WAIT"}:
                time.sleep(5.0)
                continue

            fullmessage = str(payload.get("fullmessage", "")).strip()
            message = str(payload.get("mail", "") or payload.get("message", "")).strip()
            text = " ".join(part for part in (fullmessage, message) if part).strip()
            if not text:
                time.sleep(5.0)
                continue

            match = re.search(r"\b(\d{5,6})\b", text)
            if match:
                code = match.group(1)
                LOGGER.info("Email code received for task_id=%s", task_id)
                return code

            time.sleep(5.0)

        raise TimeoutError(f"No email code received from Kopeechka for task_id={task_id} in {timeout}s.")

    def _request(
        self,
        endpoint: str,
        params: Dict[str, object],
        fail_on_error: bool = True,
    ) -> Dict[str, object]:
        query = {
            "token": self.api_token,
            "api": "2.0",
            "type": "json",
            **params,
        }
        url = f"{self.base_url}/{endpoint}"
        response = requests.get(url, params=query, timeout=self.request_timeout)
        response.raise_for_status()
        payload: Dict[str, object] = response.json()

        if fail_on_error and str(payload.get("status", "")).upper() == "ERROR":
            raise RuntimeError(f"Kopeechka API error at {endpoint}: {payload}")
        return payload
