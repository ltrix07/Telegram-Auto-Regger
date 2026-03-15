from __future__ import annotations

import imaplib
import logging
import re
import threading
import time
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional, Tuple


LOGGER = logging.getLogger(__name__)


class NoEmailsLeftError(RuntimeError):
    """Raised when no usable email credentials are available in emails file."""


class EmailAuthorizationError(RuntimeError):
    """Raised when IMAP server rejects login/authorization."""


class EmailApi:
    """Local email credentials manager + IMAP polling for Telegram codes."""

    _EMAIL_FILE_LOCK = threading.Lock()
    _CODE_REGEX = re.compile(r"(?<!\d)(\d{5,6})(?!\d)")
    _IMAP_SERVER_BY_DOMAIN = {
        "outlook.com": "imap-mail.outlook.com",
        "hotmail.com": "imap-mail.outlook.com",
        "live.com": "imap-mail.outlook.com",
        "msn.com": "imap-mail.outlook.com",
        "rambler.ru": "imap.rambler.ru",
        "lenta.ru": "imap.rambler.ru",
        "myrambler.ru": "imap.rambler.ru",
        "autorambler.ru": "imap.rambler.ru",
        "gmail.com": "imap.gmail.com",
        "mail.ru": "imap.mail.ru",
        "inbox.ru": "imap.mail.ru",
        "bk.ru": "imap.mail.ru",
        "list.ru": "imap.mail.ru",
        "yandex.ru": "imap.yandex.ru",
        "yandex.com": "imap.yandex.com",
        "yahoo.com": "imap.mail.yahoo.com",
    }

    def __init__(
        self,
        emails_file: str | Path = "emails.txt",
        poll_interval_seconds: float = 7.0,
        imap_timeout_seconds: int = 20,
        max_messages_to_scan: int = 30,
    ) -> None:
        self.emails_file = Path(emails_file)
        self.poll_interval_seconds = max(5.0, float(poll_interval_seconds))
        self.imap_timeout_seconds = max(5, int(imap_timeout_seconds))
        self.max_messages_to_scan = max(1, int(max_messages_to_scan))
        LOGGER.info(
            "Local EmailApi initialized. emails_file=%s, poll_interval=%.1fs, imap_timeout=%ss",
            self.emails_file,
            self.poll_interval_seconds,
            self.imap_timeout_seconds,
        )

    def get_email(self, site: str = "telegram.org", mail_type: str = "OUTLOOK") -> Tuple[str, str]:
        """
        Allocate the first working `login:pass` entry from local file.
        Validates IMAP connection before returning. Invalid emails are discarded.
        """
        _ = site
        _ = mail_type
        while True:
            email_address = None
            password = None

            # 1. Take one email from file (thread-safe).
            with self._EMAIL_FILE_LOCK:
                if not self.emails_file.exists():
                    LOGGER.error("Emails file not found: %s", self.emails_file)
                    raise NoEmailsLeftError(f"Emails file not found: {self.emails_file}")

                raw_lines = self.emails_file.read_text(encoding="utf-8").splitlines()
                if not raw_lines:
                    LOGGER.error("Emails file is empty: %s", self.emails_file)
                    raise NoEmailsLeftError(f"Emails file is empty: {self.emails_file}")

                remaining_lines = list(raw_lines)
                while remaining_lines:
                    line = remaining_lines.pop(0).strip()
                    if not line:
                        continue
                    e, p = self._parse_login_password(line)
                    if e and p:
                        email_address = e
                        password = p
                        break

                self._write_back_lines(remaining_lines)

            # 2. File has no parseable credentials left.
            if not email_address or not password:
                LOGGER.error("No valid email credentials found in %s", self.emails_file)
                raise NoEmailsLeftError(f"No valid email credentials found in {self.emails_file}")

            # 3. IMAP pre-check outside lock, so other threads are not blocked.
            try:
                LOGGER.info("Pre-checking IMAP access for %s...", email_address)
                client = self._connect_imap(email_address, password)
                self._safe_logout(client)
                LOGGER.info("IMAP pre-check passed for %s. Allocating.", email_address)
                return email_address, password
            except EmailAuthorizationError as exc:
                LOGGER.warning(
                    "IMAP pre-check failed for %s, discarding and trying next. Error: %s",
                    email_address,
                    exc,
                )
                continue
            except Exception as exc:
                LOGGER.warning(
                    "Unexpected error during IMAP pre-check for %s: %s. Discarding.",
                    email_address,
                    exc,
                )
                continue

    def wait_for_email_code(self, email_address: str, password: str, timeout: int = 120) -> str:
        """
        Poll IMAP INBOX until a fresh Telegram message with verification code appears.

        :param email_address: Mailbox login.
        :param password: Mailbox password.
        :param timeout: Max waiting time in seconds.
        :return: Verification code (5 or 6 digits).
        """
        normalized_email = str(email_address).strip()
        normalized_password = str(password).strip()
        if not normalized_email or "@" not in normalized_email:
            raise ValueError("A valid email_address is required.")
        if not normalized_password:
            raise ValueError("Email password is required.")

        deadline = time.time() + int(timeout)
        started_at = time.time()
        imap_client: Optional[imaplib.IMAP4_SSL] = None

        LOGGER.info("Waiting for Telegram email code on %s (timeout=%ss)", normalized_email, timeout)
        while time.time() < deadline:
            try:
                if imap_client is None:
                    imap_client = self._connect_imap(email_address=normalized_email, password=normalized_password)
                code = self._search_code_in_inbox(imap_client=imap_client, started_at=started_at)
                if code:
                    LOGGER.info("Telegram email code received for %s", normalized_email)
                    return code
            except EmailAuthorizationError:
                self._safe_logout(imap_client)
                raise
            except imaplib.IMAP4.abort as exc:
                LOGGER.error("IMAP connection aborted for %s: %s. Reconnecting.", normalized_email, exc)
                self._safe_logout(imap_client)
                imap_client = None
            except Exception:
                LOGGER.exception("Failed while polling IMAP for %s", normalized_email)
                self._safe_logout(imap_client)
                imap_client = None

            sleep_seconds = min(self.poll_interval_seconds, max(0.0, deadline - time.time()))
            if sleep_seconds > 0:
                LOGGER.info("Email code not found yet for %s. Retrying in %.1fs", normalized_email, sleep_seconds)
                time.sleep(sleep_seconds)

        self._safe_logout(imap_client)
        raise TimeoutError(f"No Telegram email code received for {normalized_email}:{password} in {timeout}s.")

    def _resolve_imap_server(self, email_address: str) -> str:
        domain = email_address.split("@", 1)[1].lower()
        # Если домен есть в словаре (mail.ru, gmail, outlook) — берем его.
        # Если нет — по умолчанию считаем, что это кастомный домен от Firstmail.
        host = self._IMAP_SERVER_BY_DOMAIN.get(domain) or "imap.firstmail.ltd"
        LOGGER.info("Resolved IMAP host for %s: %s", domain, host)
        return host

    def _connect_imap(self, email_address: str, password: str) -> imaplib.IMAP4_SSL:
        host = self._resolve_imap_server(email_address)
        LOGGER.info("Connecting IMAP to host=%s user=%s", host, email_address)
        client = imaplib.IMAP4_SSL(host=host, port=993, timeout=self.imap_timeout_seconds)
        try:
            client.login(email_address, password)
        except imaplib.IMAP4.error as exc:
            self._safe_logout(client)
            LOGGER.error("IMAP authorization failed for %s on %s: %s", email_address, host, exc)
            raise EmailAuthorizationError(f"IMAP authorization failed for {email_address}") from exc
        return client

    def _search_code_in_inbox(self, imap_client: imaplib.IMAP4_SSL, started_at: float) -> Optional[str]:
        status, _ = imap_client.select("INBOX")
        if status != "OK":
            LOGGER.error("IMAP select INBOX failed with status=%s", status)
            return None

        status, data = imap_client.search(None, "ALL")
        if status != "OK" or not data:
            LOGGER.error("IMAP search failed with status=%s data=%r", status, data)
            return None

        message_ids = data[0].split()
        if not message_ids:
            return None

        for message_id in reversed(message_ids[-self.max_messages_to_scan :]):
            code = self._extract_code_from_message(imap_client=imap_client, message_id=message_id, started_at=started_at)
            if code:
                return code
        return None

    def _extract_code_from_message(
        self,
        imap_client: imaplib.IMAP4_SSL,
        message_id: bytes,
        started_at: float,
    ) -> Optional[str]:
        status, data = imap_client.fetch(message_id, "(RFC822)")
        if status != "OK" or not data:
            return None

        raw_message = None
        for chunk in data:
            if isinstance(chunk, tuple) and len(chunk) >= 2:
                raw_message = chunk[1]
                break
        if not raw_message:
            return None

        message = message_from_bytes(raw_message)
        from_header = self._decode_header(message.get("From", ""))
        subject = self._decode_header(message.get("Subject", ""))
        body = self._extract_body_text(message)
        if not self._looks_like_telegram_message(from_header=from_header, subject=subject, body=body):
            return None

        sent_timestamp = self._extract_message_timestamp(message)
        if sent_timestamp is not None and sent_timestamp < (started_at - 180):
            return None

        text_blob = f"{subject}\n{body}".strip()
        match = self._CODE_REGEX.search(text_blob)
        if not match:
            return None

        code = match.group(1)
        LOGGER.info(
            "Telegram email matched (message_id=%s, from=%s, subject=%s)",
            message_id.decode(errors="ignore"),
            from_header,
            subject[:120],
        )
        return code

    @staticmethod
    def _decode_header(value: str) -> str:
        try:
            return str(make_header(decode_header(value or ""))).strip()
        except Exception:
            return str(value or "").strip()

    @staticmethod
    def _extract_message_timestamp(message: Message) -> Optional[float]:
        raw_date = str(message.get("Date", "")).strip()
        if not raw_date:
            return None
        try:
            parsed = parsedate_to_datetime(raw_date)
        except Exception:
            return None
        if parsed is None:
            return None
        try:
            return parsed.timestamp()
        except Exception:
            return None

    @classmethod
    def _looks_like_telegram_message(cls, from_header: str, subject: str, body: str) -> bool:
        from_lower = from_header.lower()
        if "telegram" in from_lower:
            return True

        merged = f"{subject}\n{body}".lower()
        return "telegram" in merged

    def _extract_body_text(self, message: Message) -> str:
        text_parts: list[str] = []
        if message.is_multipart():
            for part in message.walk():
                content_disposition = str(part.get("Content-Disposition", "")).lower()
                if "attachment" in content_disposition:
                    continue

                content_type = str(part.get_content_type() or "").lower()
                if content_type not in {"text/plain", "text/html"}:
                    continue
                payload = part.get_payload(decode=True)
                decoded = self._decode_payload(payload, part.get_content_charset())
                if content_type == "text/html":
                    decoded = self._strip_html(decoded)
                if decoded:
                    text_parts.append(decoded)
        else:
            payload = message.get_payload(decode=True)
            decoded = self._decode_payload(payload, message.get_content_charset())
            if str(message.get_content_type() or "").lower() == "text/html":
                decoded = self._strip_html(decoded)
            if decoded:
                text_parts.append(decoded)

        return "\n".join(text_parts).strip()

    @staticmethod
    def _decode_payload(payload: object, charset: Optional[str]) -> str:
        if payload is None:
            return ""
        if isinstance(payload, str):
            return payload
        if not isinstance(payload, (bytes, bytearray)):
            return str(payload)

        encodings = [charset, "utf-8", "cp1251", "latin-1"]
        seen: set[str] = set()
        for encoding in encodings:
            if not encoding:
                continue
            if encoding in seen:
                continue
            seen.add(encoding)
            try:
                return bytes(payload).decode(encoding, errors="replace")
            except (LookupError, UnicodeDecodeError):
                continue
        return bytes(payload).decode("utf-8", errors="replace")

    @staticmethod
    def _strip_html(html: str) -> str:
        no_script = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", html, flags=re.IGNORECASE | re.DOTALL)
        no_tags = re.sub(r"<[^>]+>", " ", no_script)
        return re.sub(r"\s+", " ", no_tags).strip()

    def _write_back_lines(self, lines: list[str]) -> None:
        output = "\n".join(lines).strip("\n")
        if output:
            output += "\n"
        self.emails_file.write_text(output, encoding="utf-8")

    @staticmethod
    def _parse_login_password(line: str) -> Tuple[str, str]:
        # Normalize common separators to colon for provider-agnostic parsing.
        normalized_line = str(line).replace(";", ":").replace("|", ":").replace("\t", ":")
        parts = [chunk.strip() for chunk in normalized_line.split(":")]
        if len(parts) < 2:
            return "", ""
        login = parts[0]
        password = parts[1]
        if not login or not password or "@" not in login:
            return "", ""
        return login, password

    @staticmethod
    def _safe_logout(imap_client: Optional[imaplib.IMAP4_SSL]) -> None:
        if imap_client is None:
            return
        try:
            imap_client.logout()
        except Exception:
            pass
