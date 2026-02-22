from __future__ import annotations

import logging
from typing import Any, Dict, List

import requests

from auto_reger.utils import CONFIG


LOGGER = logging.getLogger(__name__)


class ProxyApi:
    """
    Proxy provider API client.

    Designed as a generic wrapper so provider-specific endpoints can be swapped
    from config without changing registration flow.
    """

    def __init__(
        self,
        api_key: str | None = None,
        api_url: str | None = None,
        rotate_url: str | None = None,
        method: str | None = None,
        request_timeout: int | None = None,
    ) -> None:
        cfg = CONFIG.get("proxy_api", {})

        self.api_key = str(api_key or cfg.get("key", "")).strip()
        self.api_url = str(
            api_url or cfg.get("url", "https://proxy-seller.com/api/v1/proxy/list")
        ).strip()
        self.rotate_url = str(rotate_url or cfg.get("rotate_url", "")).strip()
        self.method = str(method or cfg.get("method", "GET")).upper().strip() or "GET"

        timeout_value = request_timeout if request_timeout is not None else cfg.get("timeout_seconds", 20)
        try:
            self.request_timeout = int(timeout_value)
        except (TypeError, ValueError):
            self.request_timeout = 20

    def get_proxies(self) -> list[str]:
        """
        Fetch proxy list from provider API.

        Returns:
            List of proxies as strings, for example:
            - ip:port
            - socks5:ip:port
            - socks5:ip:port:user:pass
        """
        if not self.api_key:
            LOGGER.error("proxy_api.key is empty in config.yaml. Cannot request proxies.")
            return []
        if not self.api_url:
            LOGGER.error("proxy_api.url is empty in config.yaml. Cannot request proxies.")
            return []

        request_kwargs = self._build_request_kwargs()

        try:
            response = requests.request(
                method=self.method,
                url=self.api_url,
                timeout=self.request_timeout,
                **request_kwargs,
            )
            if response.status_code in {401, 403}:
                LOGGER.error("Proxy API rejected credentials (HTTP %s). Check proxy_api.key.", response.status_code)
                return []

            response.raise_for_status()
            payload: Any = response.json()
        except requests.Timeout:
            LOGGER.exception("Proxy API request timed out after %ss", self.request_timeout)
            return []
        except requests.RequestException:
            LOGGER.exception("Proxy API request failed")
            return []
        except ValueError:
            LOGGER.exception("Proxy API returned non-JSON response")
            return []

        proxies = self._extract_proxies(payload)
        if not proxies:
            LOGGER.error("Proxy API returned empty proxy list.")
            return []

        LOGGER.info("Loaded %s proxies from API.", len(proxies))
        return proxies

    def rotate_proxy(self, proxy_id_or_port: str | int) -> bool:
        """
        Attempt forced proxy/IP rotation.

        This is a provider-specific operation. Keep this method as a generic
        extension point and configure `proxy_api.rotate_url` when available.
        """
        identifier = str(proxy_id_or_port).strip()
        if not identifier:
            LOGGER.error("rotate_proxy called with empty identifier.")
            return False

        if not self.api_key:
            LOGGER.error("proxy_api.key is empty. Cannot rotate proxy.")
            return False

        if not self.rotate_url:
            LOGGER.warning("proxy_api.rotate_url is not configured. Rotation skipped.")
            return False

        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "X-API-KEY": self.api_key,
        }
        payload = {
            "key": self.api_key,
            "proxy_id": identifier,
            "port": identifier,
        }

        try:
            response = requests.post(
                url=self.rotate_url,
                json=payload,
                headers=headers,
                timeout=self.request_timeout,
            )
            if response.status_code in {401, 403}:
                LOGGER.error("Proxy rotation failed: unauthorized (HTTP %s).", response.status_code)
                return False
            response.raise_for_status()
        except requests.Timeout:
            LOGGER.exception("Proxy rotation request timed out after %ss", self.request_timeout)
            return False
        except requests.RequestException:
            LOGGER.exception("Proxy rotation request failed")
            return False

        try:
            rotate_payload: Any = response.json()
        except ValueError:
            LOGGER.info("Proxy rotation endpoint returned non-JSON response; treating as success.")
            return True

        if isinstance(rotate_payload, dict):
            status = str(rotate_payload.get("status", "")).lower()
            if status in {"error", "fail", "failed"}:
                LOGGER.error("Proxy rotation API error: %s", rotate_payload)
                return False

        return True

    def _build_request_kwargs(self) -> Dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "X-API-KEY": self.api_key,
        }

        if self.method == "POST":
            return {"headers": headers, "json": {"key": self.api_key}}

        return {"headers": headers, "params": {"key": self.api_key}}

    def _extract_proxies(self, payload: Any) -> List[str]:
        items: Any = payload
        if isinstance(payload, dict):
            status = str(payload.get("status", "")).lower()
            if status in {"error", "fail", "failed"}:
                LOGGER.error("Proxy API responded with error payload: %s", payload)
                return []

            for key in ("proxies", "data", "items", "result"):
                candidate = payload.get(key)
                if isinstance(candidate, list):
                    items = candidate
                    break
                if isinstance(candidate, dict):
                    nested = candidate.get("proxies")
                    if isinstance(nested, list):
                        items = nested
                        break

        if not isinstance(items, list):
            LOGGER.error("Unexpected proxy API payload format: %r", payload)
            return []

        result: List[str] = []
        seen: set[str] = set()

        for item in items:
            proxy_str = self._parse_proxy_item(item)
            if not proxy_str:
                continue
            if proxy_str not in seen:
                seen.add(proxy_str)
                result.append(proxy_str)

        return result

    @staticmethod
    def _parse_proxy_item(item: Any) -> str:
        if isinstance(item, str):
            return item.strip()

        if not isinstance(item, dict):
            return ""

        if isinstance(item.get("proxy"), str):
            return str(item["proxy"]).strip()

        host = str(item.get("ip") or item.get("host") or "").strip()
        port = str(item.get("port") or "").strip()
        proxy_type = str(item.get("type") or item.get("protocol") or "").strip().lower()
        username = str(item.get("username") or item.get("user") or item.get("login") or "").strip()
        password = str(item.get("password") or item.get("pass") or "").strip()

        if not host or not port:
            return ""

        if proxy_type:
            base = f"{proxy_type}:{host}:{port}"
        else:
            base = f"{host}:{port}"

        if username or password:
            return f"{base}:{username}:{password}"
        return base
