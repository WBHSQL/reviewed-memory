"""Local configuration loading for the read-only flomo PoC."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional


class ConfigError(ValueError):
    """Raised when required local configuration is missing or invalid."""


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def load_dotenv(path: Path) -> Dict[str, str]:
    """Load a small, dependency-free subset of dotenv syntax.

    Existing process environment variables take precedence later in
    ``FlomoConfig.from_env``. Values are returned and are not written into
    the process environment.
    """

    if not path.exists():
        return {}

    values: Dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        if not key:
            continue
        values[key] = _unquote(value.strip())
    return values


def _read_int(name: str, value: str, minimum: int, maximum: Optional[int] = None) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigError("配置格式错误") from exc
    if parsed < minimum or (maximum is not None and parsed > maximum):
        raise ConfigError("配置范围错误")
    return parsed


@dataclass(frozen=True)
class FlomoConfig:
    authorization: str
    signing_secret: str
    api_base_url: str = "https://flomoapp.com/api/v1"
    timeout_seconds: int = 20
    page_size: int = 200
    max_pages: int = 50
    timezone: str = "8:0"

    @classmethod
    def from_env(cls, env_file: Path = Path(".env")) -> "FlomoConfig":
        dotenv_values = load_dotenv(env_file)

        def get(name: str, default: Optional[str] = None) -> Optional[str]:
            value = os.environ.get(name)
            if value is None:
                value = dotenv_values.get(name)
            if value is None:
                return default
            return value.strip()

        authorization = get("FLOMO_AUTHORIZATION")
        signing_secret = get("FLOMO_SIGNING_SECRET")
        if not authorization or not signing_secret:
            raise ConfigError("缺少必需配置")
        if "\r" in authorization or "\n" in authorization:
            raise ConfigError("Authorization 配置格式错误")
        if not authorization.lower().startswith("bearer "):
            authorization = f"Bearer {authorization}"

        api_base_url = (get("FLOMO_API_BASE_URL", cls.api_base_url) or cls.api_base_url).rstrip("/")
        timeout_seconds = _read_int(
            "FLOMO_TIMEOUT_SECONDS",
            get("FLOMO_TIMEOUT_SECONDS", str(cls.timeout_seconds)) or str(cls.timeout_seconds),
            minimum=1,
            maximum=120,
        )
        page_size = _read_int(
            "FLOMO_PAGE_SIZE",
            get("FLOMO_PAGE_SIZE", str(cls.page_size)) or str(cls.page_size),
            minimum=1,
            maximum=200,
        )
        max_pages = _read_int(
            "FLOMO_MAX_PAGES",
            get("FLOMO_MAX_PAGES", str(cls.max_pages)) or str(cls.max_pages),
            minimum=1,
            maximum=1000,
        )
        timezone = get("FLOMO_TIMEZONE", cls.timezone) or cls.timezone

        return cls(
            authorization=authorization,
            signing_secret=signing_secret,
            api_base_url=api_base_url,
            timeout_seconds=timeout_seconds,
            page_size=page_size,
            max_pages=max_pages,
            timezone=timezone,
        )
