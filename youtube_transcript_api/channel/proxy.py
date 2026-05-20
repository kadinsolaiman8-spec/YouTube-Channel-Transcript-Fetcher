"""Proxy configuration for channel transcript export."""

from __future__ import annotations

import os
from typing import Optional

from youtube_transcript_api.proxies import (
    GenericProxyConfig,
    ProxyConfig,
    WebshareProxyConfig,
)

# Channel exports: fewer urllib 429 retries than library default (10) to avoid
# hammering timedtext when already rate-limited.
_DEFAULT_WEBSHARE_RETRIES = 2


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _webshare_retries_when_blocked() -> int:
    raw = _env("WEBSHARE_PROXY_RETRIES")
    if not raw:
        return _DEFAULT_WEBSHARE_RETRIES
    try:
        return max(0, int(raw))
    except ValueError:
        return _DEFAULT_WEBSHARE_RETRIES


def _webshare_proxy_config(username: str, password: str) -> WebshareProxyConfig:
    return WebshareProxyConfig(
        proxy_username=username,
        proxy_password=password,
        retries_when_blocked=_webshare_retries_when_blocked(),
    )


def proxy_config_from_env() -> Optional[ProxyConfig]:
    """Build proxy config from environment variables (after load_local_env)."""
    webshare_user = _env("WEBSHARE_PROXY_USERNAME")
    webshare_pass = _env("WEBSHARE_PROXY_PASSWORD")
    if webshare_user and webshare_pass:
        return _webshare_proxy_config(webshare_user, webshare_pass)

    http_url = _env("HTTP_PROXY") or _env("http_proxy")
    https_url = _env("HTTPS_PROXY") or _env("https_proxy")
    if http_url or https_url:
        return GenericProxyConfig(
            http_url=http_url or None,
            https_url=https_url or None,
        )
    return None


def proxy_config_label() -> Optional[str]:
    """Human-readable hint when proxy env vars are set (no secrets)."""
    if _env("WEBSHARE_PROXY_USERNAME") and _env("WEBSHARE_PROXY_PASSWORD"):
        return "Using Webshare proxy from environment"
    if (
        _env("HTTP_PROXY")
        or _env("HTTPS_PROXY")
        or _env("http_proxy")
        or _env("https_proxy")
    ):
        return "Using HTTP proxy from environment"
    return None


def resolve_proxy_config(
    *,
    http_proxy: str = "",
    https_proxy: str = "",
    webshare_proxy_username: Optional[str] = None,
    webshare_proxy_password: Optional[str] = None,
) -> Optional[ProxyConfig]:
    """CLI flags override environment when provided."""
    if webshare_proxy_username is not None or webshare_proxy_password is not None:
        username = webshare_proxy_username or ""
        password = webshare_proxy_password or ""
        if username and password:
            return _webshare_proxy_config(username, password)

    if http_proxy or https_proxy:
        return GenericProxyConfig(
            http_url=http_proxy or None,
            https_url=https_proxy or None,
        )

    return proxy_config_from_env()
