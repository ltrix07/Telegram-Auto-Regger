from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import requests

from auto_reger.utils import CONFIG


LOGGER = logging.getLogger(__name__)


class ProxyApi:
    """Proxy-Seller Personal API client for resident proxies."""

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
        # Personal API root: https://proxy-seller.com/personal/api/v1/{key}
        self.base_url = f"https://proxy-seller.com/personal/api/v1/{self.api_key}" if self.api_key else ""
        self.api_url = str(api_url or cfg.get("url", "")).strip()
        self.rotate_url = str(rotate_url or cfg.get("rotate_url", "")).strip()
        self.method = str(method or cfg.get("method", "GET")).upper().strip() or "GET"

        timeout_value = request_timeout if request_timeout is not None else cfg.get(
            "timeout_seconds",
            20,
        )
        try:
            self.request_timeout = int(timeout_value)
        except (TypeError, ValueError):
            self.request_timeout = 20

        # Rotation state.
        self.proxies_cache: list[dict[str, str]] = []
        self.current_index = 0
        self.last_reset_time = 0.0
        self._loaded_country = ""
        self._lock = threading.Lock()
        self._geo_path = Path(__file__).resolve().parents[1] / "geo.json"
        self._geo_countries_cache: list[dict[str, Any]] | None = None
        self._geo_warning_shown = False

    @staticmethod
    def _normalize_country_token(value: str) -> str:
        return "".join(ch for ch in str(value or "").upper().strip() if ch.isalnum())

    def _resolve_country_code(self, country_input: str) -> str:
        raw_country = str(country_input or "").strip()
        if not raw_country:
            raise ValueError("country_code must not be empty.")

        normalized_input = self._normalize_country_token(raw_country)
        aliases = {
            "USA": "US",
            "UNITEDSTATES": "US",
            "UNITEDSTATESOFAMERICA": "US",
            "UK": "GB",
            "UNITEDKINGDOM": "GB",
            "GREATBRITAIN": "GB",
            "RUSSIA": "RU",
            "RUSSIANFEDERATION": "RU",
        }
        alias_code = aliases.get(normalized_input)
        if alias_code:
            return alias_code

        if self._geo_countries_cache is None:
            if not self._geo_path.exists():
                if not self._geo_warning_shown:
                    LOGGER.warning(
                        "geo.json not found at %s. Country resolver fallback is used; country input stays unchanged.",
                        self._geo_path,
                    )
                    self._geo_warning_shown = True
                return raw_country

            try:
                with self._geo_path.open("r", encoding="utf-8") as geo_file:
                    loaded = json.load(geo_file)
            except (OSError, json.JSONDecodeError) as exc:
                if not self._geo_warning_shown:
                    LOGGER.warning(
                        "Failed to read geo.json at %s (%s). Country resolver fallback is used; country input stays unchanged.",
                        self._geo_path,
                        exc,
                    )
                    self._geo_warning_shown = True
                return raw_country

            if not isinstance(loaded, list):
                raise ValueError(f"geo.json must contain a list of countries, got {type(loaded).__name__}.")
            self._geo_countries_cache = [item for item in loaded if isinstance(item, dict)]

        for country_item in self._geo_countries_cache:
            code = str(country_item.get("code", "")).strip().upper()
            name = str(country_item.get("name", "")).strip()
            if not code:
                continue
            if normalized_input == self._normalize_country_token(code):
                return code
            if name and normalized_input == self._normalize_country_token(name):
                return code

        raise ValueError(f"Country '{raw_country}' was not found in geo.json country database.")

    def get_proxy(self, country_code: str) -> dict[str, str]:
        """Return next proxy from resident list in round-robin mode."""
        resolved_code = self._resolve_country_code(country_code)

        with self._lock:
            now_ts = time.time()
            if self.last_reset_time <= 0:
                self.last_reset_time = now_ts
            elif now_ts - self.last_reset_time >= 1320:
                LOGGER.info(
                    "Proxy rotation reset triggered after 22 minutes for country=%s. Reset index to 0.",
                    resolved_code,
                )
                self.current_index = 0
                self.last_reset_time = now_ts

            if not self.proxies_cache or self._loaded_country != resolved_code:
                self._ensure_proxies_loaded(resolved_code)

            if not self.proxies_cache:
                raise RuntimeError(f"No proxies loaded for country={resolved_code}.")

            proxy = self.proxies_cache[self.current_index]
            self.current_index += 1
            if self.current_index >= len(self.proxies_cache):
                self.current_index = 0

            return {
                "ip": str(proxy["ip"]),
                "port": str(proxy["port"]),
                "user": str(proxy.get("user", "")),
                "pass": str(proxy.get("pass", "")),
                "type": "socks5",
            }

    def _ensure_proxies_loaded(self, country: str) -> None:
        """Load or create resident list by country and generate backconnect proxy ports."""
        if not self.api_key:
            raise RuntimeError("proxy_api.key is empty in config.yaml.")
        if not self.base_url:
            raise RuntimeError("Proxy-Seller Personal API base_url is empty.")

        resolved_code = self._normalize_country_token(country)
        if not resolved_code:
            raise RuntimeError(f"Invalid country code for resident list lookup: {country!r}")

        lists_payload = self._request_json("GET", "/resident/lists")
        target_list = self._find_list_by_country(lists_payload, resolved_code)

        if not target_list:
            LOGGER.info("Resident proxy list not found for country=%s. Creating a new list.", resolved_code)
            try:
                list_id = self._create_list(resolved_code)
            except Exception as exc:
                LOGGER.exception(
                    "Failed to create resident list for country=%s. "
                    "This may happen due to account limits or duplicate resources.",
                    resolved_code,
                )
                refreshed_payload = self._request_json("GET", "/resident/lists")
                target_list = self._find_list_by_country(refreshed_payload, resolved_code)
                if not target_list:
                    raise RuntimeError(
                        f"Failed to create resident list for country={resolved_code}. "
                        "No existing list was found after create failure."
                    ) from exc
                list_id = self._extract_list_id(target_list)
                LOGGER.info(
                    "Using existing resident list id=%s for country=%s after create failure.",
                    list_id or "unknown",
                    resolved_code,
                )
            else:
                refreshed_payload = self._request_json("GET", "/resident/lists")
                target_list = self._find_list_by_id(refreshed_payload, list_id) or self._find_list_by_country(
                    refreshed_payload,
                    resolved_code,
                )
        else:
            list_id = self._extract_list_id(target_list)
            LOGGER.info("Using existing resident list id=%s for country=%s.", list_id, resolved_code)

        if not target_list:
            raise RuntimeError(f"Resident proxy list metadata not found for country={resolved_code}.")

        list_id = self._extract_list_id(target_list)
        login = str(target_list.get("login", "")).strip()
        password = str(target_list.get("password", "")).strip()
        export_data = target_list.get("export")
        ports_raw = export_data.get("ports", 1000) if isinstance(export_data, dict) else 1000
        try:
            ports_count = int(ports_raw)
        except (TypeError, ValueError):
            LOGGER.warning(
                "Invalid ports count %r for list_id=%s country=%s. Falling back to 1000.",
                ports_raw,
                list_id or "unknown",
                resolved_code,
            )
            ports_count = 1000
        if ports_count < 1:
            LOGGER.warning(
                "Non-positive ports count %s for list_id=%s country=%s. Falling back to 1000.",
                ports_count,
                list_id or "unknown",
                resolved_code,
            )
            ports_count = 1000
        if not login or not password:
            raise RuntimeError(
                f"Resident list credentials are missing for list_id={list_id or 'unknown'}, country={resolved_code}."
            )

        host = "res.proxy-seller.com"
        self.proxies_cache = []
        for i in range(ports_count):
            port = 10000 + i
            self.proxies_cache.append(
                {
                    "ip": host,
                    "port": str(port),
                    "user": login,
                    "pass": password,
                    "type": "socks5",
                }
            )

        self.current_index = 0
        self.last_reset_time = time.time()
        self._loaded_country = resolved_code
        LOGGER.info(
            "Сгенерировано %s портов для %s (country=%s, list_id=%s).",
            ports_count,
            host,
            resolved_code,
            list_id or "unknown",
        )

    def _create_list(self, country: str) -> str:
        """Create a resident proxy list with fixed 1000 ports and 1200s rotation."""
        payload = {
            "title": f"AutoReger {country}",
            "whitelist": "",
            "geo": {"country": country},
            "export": {"ports": 1000, "ext": "txt"},
            "rotation": 1200,
        }

        LOGGER.info(
            "Creating resident list for country=%s with 1000 ports and rotation=1200s.",
            country,
        )
        response_payload = self._request_json("POST", "/resident/list/add", json_body=payload)
        list_id = self._extract_list_id(response_payload)
        if not list_id:
            raise RuntimeError(
                f"Proxy-Seller API did not return a list id after creation for country={country}. Payload: {response_payload!r}"
            )
        LOGGER.info("Resident list created for country=%s, list_id=%s.", country, list_id)
        return list_id

    def _request_json(self, method: str, endpoint: str, json_body: dict[str, Any] | None = None) -> Any:
        response = self._request(method=method, endpoint=endpoint, json_body=json_body)
        content_type = str(response.headers.get("Content-Type", "")).lower()
        if response.text.strip() and "application/json" not in content_type:
            try:
                parsed = response.json()
            except ValueError as exc:
                raise RuntimeError(
                    f"Expected JSON from endpoint={endpoint}, got non-JSON response: {response.text[:300]!r}"
                ) from exc
        else:
            try:
                parsed = response.json() if response.text.strip() else {}
            except ValueError as exc:
                raise RuntimeError(
                    f"Failed to decode JSON from endpoint={endpoint}. Response: {response.text[:300]!r}"
                ) from exc

        self._raise_api_payload_error(parsed, endpoint)
        return parsed

    def _request_text(self, method: str, endpoint: str) -> str:
        response = self._request(method=method, endpoint=endpoint)
        text = response.text.strip()
        if not text:
            raise RuntimeError(f"Empty response received from endpoint={endpoint}.")
        if text.startswith("{") and text.endswith("}"):
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                self._raise_api_payload_error(payload, endpoint)
        return text

    def _request(self, method: str, endpoint: str, json_body: dict[str, Any] | None = None) -> requests.Response:
        if not self.base_url:
            raise RuntimeError("Proxy-Seller Personal API base URL is not configured.")
        url = f"{self.base_url}{endpoint}"
        try:
            response = requests.request(
                method=method,
                url=url,
                json=json_body,
                timeout=self.request_timeout,
            )
        except requests.Timeout as exc:
            raise RuntimeError(f"Proxy-Seller API timeout on endpoint={endpoint}.") from exc
        except requests.RequestException as exc:
            raise RuntimeError(f"Proxy-Seller API request failed on endpoint={endpoint}: {exc}") from exc

        self._raise_http_error(response=response, endpoint=endpoint)
        return response

    def _raise_http_error(self, response: requests.Response, endpoint: str) -> None:
        if response.status_code < 400:
            return

        body = response.text.strip()
        body_lower = body.lower()
        if response.status_code in {402} or "not enough balance" in body_lower or "balance" in body_lower:
            raise RuntimeError(
                f"Proxy-Seller API rejected request due to low/empty balance on {endpoint} (HTTP {response.status_code})."
            )
        if response.status_code in {429} or "limit" in body_lower or "too many requests" in body_lower:
            raise RuntimeError(
                f"Proxy-Seller API rate/usage limit exceeded on {endpoint} (HTTP {response.status_code})."
            )
        if response.status_code in {401, 403}:
            raise RuntimeError(
                f"Proxy-Seller API unauthorized on {endpoint} (HTTP {response.status_code}). Check proxy_api.key."
            )

        preview = body[:300] if body else "no body"
        raise RuntimeError(
            f"Proxy-Seller API request failed on {endpoint} (HTTP {response.status_code}). Response: {preview!r}"
        )

    def _raise_api_payload_error(self, payload: Any, endpoint: str) -> None:
        if not isinstance(payload, dict):
            return

        message_parts = [
            str(payload.get("message", "")).strip(),
            str(payload.get("error", "")).strip(),
            str(payload.get("detail", "")).strip(),
            str(payload.get("description", "")).strip(),
        ]
        message = " ".join(part for part in message_parts if part).strip()
        status = str(payload.get("status", "")).strip().lower()
        code = str(payload.get("code", "")).strip().lower()
        merged = f"{status} {code} {message}".lower()

        if any(chunk in merged for chunk in ("not enough balance", "empty balance", "insufficient balance")):
            raise RuntimeError(f"Proxy-Seller API reported empty/low balance on {endpoint}: {message or payload!r}")

        if any(chunk in merged for chunk in ("limit", "too many requests", "rate")):
            raise RuntimeError(f"Proxy-Seller API reported limit exceeded on {endpoint}: {message or payload!r}")

        if status in {"error", "fail", "failed"}:
            raise RuntimeError(f"Proxy-Seller API returned error payload on {endpoint}: {payload!r}")

    def _extract_lists(self, payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]

        if not isinstance(payload, dict):
            return []

        data = payload.get("data", {})
        if isinstance(data, dict):
            items = data.get("items")
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]

        for key in ("items", "lists", "result"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                return [item for item in candidate if isinstance(item, dict)]
        return []

    def _find_list_by_country(self, payload: Any, country: str) -> dict[str, Any] | None:
        resolved_code = self._normalize_country_token(country)
        for item in self._extract_lists(payload):
            geo_data = item.get("geo", {})
            if not isinstance(geo_data, dict):
                continue
            item_country = self._normalize_country_token(str(geo_data.get("country", "")).strip())
            if not item_country or item_country != resolved_code:
                continue
            return item
        return None

    def _find_list_by_id(self, payload: Any, list_id: str) -> dict[str, Any] | None:
        normalized_id = str(list_id or "").strip()
        if not normalized_id:
            return None
        for item in self._extract_lists(payload):
            if self._extract_list_id(item) == normalized_id:
                return item
        return None

    @staticmethod
    def _extract_list_id(payload: Any) -> str:
        if isinstance(payload, dict):
            for key in ("id", "list_id", "listId"):
                value = payload.get(key)
                if value is not None and str(value).strip():
                    return str(value).strip()
            for key in ("data", "result", "item", "list"):
                nested = payload.get(key)
                nested_id = ProxyApi._extract_list_id(nested)
                if nested_id:
                    return nested_id
        return ""

    @staticmethod
    def _parse_downloaded_proxies(content: str) -> list[dict[str, str]]:
        proxies: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()

        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "://" in line:
                line = line.split("://", maxsplit=1)[1].strip()

            parts = [chunk.strip() for chunk in line.split(":") if chunk.strip()]
            if len(parts) < 2:
                continue

            host, port = parts[0], parts[1]
            if not port.isdigit():
                continue

            marker = (host, port)
            if marker in seen:
                continue
            seen.add(marker)
            proxies.append({"ip": host, "port": port, "type": "socks5"})

        return proxies

    def get_proxies(self) -> list[str]:
        """
        Backward-compatible helper for existing flow in cli_main.py.

        Returns list in `ip:port` format from loaded country cache.
        """
        country = str(CONFIG.get("registration", {}).get("default_country", "")).strip()
        if not country:
            LOGGER.error("registration.default_country is empty. Cannot load proxies.")
            return []

        if not self.api_key:
            LOGGER.error("proxy_api.key is empty in config.yaml. Cannot request proxies.")
            return []

        try:
            with self._lock:
                if not self.proxies_cache or self._loaded_country != country:
                    self._ensure_proxies_loaded(country)
                return [f"{proxy['ip']}:{proxy['port']}" for proxy in self.proxies_cache]
        except Exception:
            LOGGER.exception("Failed to load proxies for country=%s", country)
            return []

    def rotate_proxy(self, proxy_id_or_port: str | int) -> bool:
        """
        Compatibility method.

        Resident lists already auto-rotate by configured `rotation=1200`.
        """
        LOGGER.info(
            "rotate_proxy(%s) called. Explicit rotation is not used for resident auto-rotation lists.",
            proxy_id_or_port,
        )
        return True
