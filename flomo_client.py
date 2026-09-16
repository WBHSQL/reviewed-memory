"""Read-only flomo Web API client for the first PoC stage."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from config import FlomoConfig


class FlomoClientError(RuntimeError):
    """Base class for safe, user-facing PoC errors."""

    def __init__(self, error_type: str, status_code: Optional[int] = None) -> None:
        super().__init__(error_type)
        self.error_type = error_type
        self.status_code = status_code


class PaginationError(FlomoClientError):
    """Raised when the API cursor does not advance or the page cap is hit."""


class FlomoClient:
    """A read-only client for the flomo Web API."""

    def __init__(self, config: FlomoConfig) -> None:
        self.config = config

    @staticmethod
    def _format_value(value: Any) -> str:
        if isinstance(value, bool):
            return "1" if value else "0"
        return str(value)

    def _make_sign(self, params: Mapping[str, Any]) -> str:
        """Create the Web API MD5 signature without exposing its inputs."""

        pairs: List[str] = []
        for key in sorted(params):
            value = params[key]
            if value is None or value == "":
                continue
            if isinstance(value, (list, tuple)):
                for item in sorted(str(item) for item in value if item is not None):
                    pairs.append(f"{key}[]={self._format_value(item)}")
            else:
                pairs.append(f"{key}={self._format_value(value)}")
        signing_text = "&".join(pairs) + self.config.signing_secret
        return hashlib.md5(signing_text.encode("utf-8")).hexdigest()

    def _signed_params(self, params: Mapping[str, Any]) -> Dict[str, Any]:
        signed: Dict[str, Any] = dict(params)
        signed.update(
            {
                "timestamp": int(time.time()),
                "api_key": "flomo_web",
                "app_version": "4.0",
                "platform": "web",
                "webp": "1",
            }
        )
        signed["sign"] = self._make_sign(signed)
        return signed

    @staticmethod
    def _error_type(status_code: Optional[int], api_code: Any = None) -> str:
        if status_code == 401 or api_code == -10:
            return "认证失败"
        if status_code == 403:
            return "签名或权限失败"
        if status_code == 429:
            return "限流"
        if status_code is not None and status_code >= 500:
            return "服务端错误"
        if status_code is None:
            return "网络错误"
        if api_code is not None:
            return "API错误"
        return "HTTP错误"

    @staticmethod
    def _safe_count(data: Any) -> int:
        if isinstance(data, list):
            return len(data)
        if isinstance(data, dict):
            return 1
        return 0

    @staticmethod
    def _log_success(status_code: int, count: int) -> None:
        print(f"请求成功 | 状态码={status_code} | memo数量={count}")

    @staticmethod
    def _log_failure(status_code: Optional[int], error_type: str) -> None:
        status = str(status_code) if status_code is not None else "无"
        print(f"请求失败 | 状态码={status} | 错误类型={error_type}")

    def _request(self, path: str, params: Optional[Mapping[str, Any]] = None) -> Any:
        request_params = dict(params or {})
        signed_params = self._signed_params(request_params)
        query = urlencode(signed_params, doseq=True)
        url = f"{self.config.api_base_url}{path}?{query}"
        request = Request(
            url,
            method="GET",
            headers={
                "Accept": "application/json",
                "Authorization": self.config.authorization,
                "User-Agent": "flomo-poc/0.1",
            },
        )

        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                status_code = int(response.status)
                raw_body = response.read()
        except HTTPError as exc:
            error_type = self._error_type(exc.code)
            self._log_failure(exc.code, error_type)
            raise FlomoClientError(error_type, exc.code) from None
        except (URLError, TimeoutError, OSError):
            error_type = self._error_type(None)
            self._log_failure(None, error_type)
            raise FlomoClientError(error_type) from None

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            error_type = "响应格式错误"
            self._log_failure(status_code, error_type)
            raise FlomoClientError(error_type, status_code) from None

        if not isinstance(payload, dict):
            error_type = "响应格式错误"
            self._log_failure(status_code, error_type)
            raise FlomoClientError(error_type, status_code)

        api_code = payload.get("code")
        if api_code != 0:
            error_type = self._error_type(status_code, api_code)
            self._log_failure(status_code, error_type)
            raise FlomoClientError(error_type, status_code)

        data = payload.get("data")
        self._log_success(status_code, self._safe_count(data))
        return data

    def get_latest_memos(self, limit: int = 5) -> List[Dict[str, Any]]:
        """Fetch the latest memo list and return at most ``limit`` records."""

        if limit < 1:
            raise ValueError("limit must be positive")
        data = self._request(
            "/memo/latest_updated_desc",
            {"tz": self.config.timezone},
        )
        if not isinstance(data, list):
            raise FlomoClientError("响应格式错误", 200)
        return [item for item in data[:limit] if isinstance(item, dict)]

    def _get_updated_page(
        self,
        limit: int,
        latest_updated_at: int,
        latest_slug: str,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {
            "limit": limit,
            "latest_updated_at": latest_updated_at,
            "tz": self.config.timezone,
        }
        if latest_slug:
            params["latest_slug"] = latest_slug
        data = self._request("/memo/updated/", params)
        if not isinstance(data, list):
            raise FlomoClientError("响应格式错误", 200)
        return [item for item in data if isinstance(item, dict)]

    @staticmethod
    def _timestamp_seconds(value: Any) -> int:
        if isinstance(value, (int, float)):
            numeric = int(value)
            return numeric // 1000 if numeric > 10_000_000_000 else numeric

        text = str(value).strip()
        if text.isdigit():
            numeric = int(text)
            return numeric // 1000 if numeric > 10_000_000_000 else numeric

        normalized = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise PaginationError("分页游标格式错误") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())

    def sync_memos(
        self,
        page_size: Optional[int] = None,
        max_pages: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Exercise cursor pagination entirely in memory; no writes are made."""

        page_size = page_size or self.config.page_size
        max_pages = max_pages or self.config.max_pages
        if not 1 <= page_size <= 200 or max_pages < 1:
            raise ValueError("invalid pagination settings")

        latest_updated_at = 0
        latest_slug = ""
        collected: Dict[str, Dict[str, Any]] = {}

        for _page_number in range(1, max_pages + 1):
            page = self._get_updated_page(page_size, latest_updated_at, latest_slug)
            if not page:
                return list(collected.values())

            for memo in page:
                slug = memo.get("slug")
                if slug:
                    collected[str(slug)] = memo

            last = page[-1]
            next_slug = str(last.get("slug") or "")
            if not next_slug or "updated_at" not in last:
                raise PaginationError("分页游标缺失")
            next_updated_at = self._timestamp_seconds(last["updated_at"])
            if next_updated_at == latest_updated_at and next_slug == latest_slug:
                raise PaginationError("分页游标未推进")
            latest_updated_at = next_updated_at
            latest_slug = next_slug

            if len(page) < page_size:
                return list(collected.values())

        raise PaginationError("分页未完成")

    def get_memo(self, slug: str) -> Dict[str, Any]:
        """Fetch one memo by slug without printing its content."""

        if not slug or "/" in slug or "?" in slug or "#" in slug:
            raise ValueError("invalid slug")
        data = self._request(f"/memo/{quote(slug, safe='')}", {})
        if not isinstance(data, dict):
            raise FlomoClientError("响应格式错误", 200)
        return data
