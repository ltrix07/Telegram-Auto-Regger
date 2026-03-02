from __future__ import annotations

import logging
from pathlib import Path

import requests

LOGGER = logging.getLogger(__name__)


class TelegramNotifier:
    """Best-effort Telegram Bot API notifier for critical runtime errors."""

    MAX_CAPTION_LENGTH = 1024

    def __init__(self, bot_token: str, chat_id: str, timeout: float = 15.0) -> None:
        token = str(bot_token or "").strip()
        target_chat_id = str(chat_id or "").strip()
        if not token:
            raise ValueError("Telegram bot token is required.")
        if not target_chat_id:
            raise ValueError("Telegram chat_id is required.")

        self.bot_token = token
        self.chat_id = target_chat_id
        self.timeout = float(timeout)
        self._api_base = f"https://api.telegram.org/bot{self.bot_token}"
        self._session = requests.Session()

    @classmethod
    def _truncate_caption(cls, text: str) -> str:
        normalized = str(text or "").strip() or "Unknown runtime error."
        if len(normalized) <= cls.MAX_CAPTION_LENGTH:
            return normalized

        suffix = "\n...[truncated]"
        limit = cls.MAX_CAPTION_LENGTH - len(suffix)
        if limit <= 0:
            return normalized[: cls.MAX_CAPTION_LENGTH]
        return f"{normalized[:limit]}{suffix}"

    def send_error_alert(self, error_message: str, screenshot_path: str | None = None) -> bool:
        """
        Send critical alert to Telegram.

        Uses ``sendPhoto`` when screenshot file exists; falls back to
        ``sendMessage`` with the same text if image send fails.
        """
        caption = self._truncate_caption(error_message)

        try:
            if screenshot_path:
                image_file = Path(str(screenshot_path).strip())
                if image_file.exists() and image_file.is_file():
                    with image_file.open("rb") as photo:
                        response = self._session.post(
                            f"{self._api_base}/sendPhoto",
                            data={
                                "chat_id": self.chat_id,
                                "caption": caption,
                            },
                            files={"photo": (image_file.name, photo, "image/png")},
                            timeout=self.timeout,
                        )
                    if response.ok:
                        return True
                    LOGGER.error(
                        "Telegram sendPhoto failed: status=%s body=%s",
                        response.status_code,
                        (response.text or "")[:500],
                    )
                else:
                    LOGGER.warning("Screenshot file not found for alert: %s", image_file)

            response = self._session.post(
                f"{self._api_base}/sendMessage",
                data={
                    "chat_id": self.chat_id,
                    "text": caption,
                },
                timeout=self.timeout,
            )
            if response.ok:
                return True

            LOGGER.error(
                "Telegram sendMessage failed: status=%s body=%s",
                response.status_code,
                (response.text or "")[:500],
            )
            return False
        except requests.RequestException:
            LOGGER.exception("Network error while sending Telegram alert.")
            return False
        except Exception:
            LOGGER.exception("Unexpected error while sending Telegram alert.")
            return False

    def send_video_alert(self, error_message: str, video_path: str) -> bool:
        """
        Send critical alert with MP4 video to Telegram.

        Uses ``sendVideo`` when file exists; falls back to ``sendMessage`` on failure.
        """
        caption = self._truncate_caption(error_message)

        try:
            if video_path:
                image_file = Path(str(video_path).strip())
                if image_file.exists() and image_file.is_file():
                    with image_file.open("rb") as video:
                        response = self._session.post(
                            f"{self._api_base}/sendVideo",
                            data={
                                "chat_id": self.chat_id,
                                "caption": caption,
                            },
                            files={"video": (image_file.name, video, "video/mp4")},
                            timeout=self.timeout + 30.0,
                        )
                    if response.ok:
                        return True
                    LOGGER.error(
                        "Telegram sendVideo failed: status=%s body=%s",
                        response.status_code,
                        (response.text or "")[:500],
                    )
                else:
                    LOGGER.warning("Video file not found for alert: %s", image_file)

            response = self._session.post(
                f"{self._api_base}/sendMessage",
                data={
                    "chat_id": self.chat_id,
                    "text": caption,
                },
                timeout=self.timeout,
            )
            if response.ok:
                return True

            LOGGER.error(
                "Telegram sendMessage failed: status=%s body=%s",
                response.status_code,
                (response.text or "")[:500],
            )
            return False
        except requests.RequestException:
            LOGGER.exception("Network error while sending Telegram video alert.")
            return False
        except Exception:
            LOGGER.exception("Unexpected error while sending Telegram video alert.")
            return False
