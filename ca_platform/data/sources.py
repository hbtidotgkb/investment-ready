"""Source-quality registry helpers and polite HTTP."""
from __future__ import annotations

import re
import time
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from ..settings import config, user_agent

TRACKING_PARAMS = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|ref_src|from$|_ga)")


def domain_of(url: str | None) -> str:
    if not url:
        return ""
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def tier_for(url: str | None, default: str = "T3_other_press") -> str:
    host = domain_of(url)
    domains: dict = config("sources").get("domains", {})
    while host:
        if host in domains:
            return domains[host]
        host = host.split(".", 1)[1] if "." in host else ""
    return default


def tier_label(tier: str) -> str:
    return config("sources").get("tiers", {}).get(tier, {}).get("label", tier)


def canonical_url(url: str) -> str:
    """Strip tracking params and fragments without changing source identity."""
    p = urlparse(url.strip())
    if p.scheme not in ("http", "https"):
        raise ValueError(f"unsupported URL scheme: {p.scheme!r}")
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k)]
    host = p.netloc.lower()
    return urlunparse((p.scheme, host, p.path.rstrip("/") or "/", "", urlencode(q), ""))


class PoliteClient:
    """Rate-limited client: identifies itself, waits between requests, backs off on errors."""

    def __init__(self, min_interval: float = 1.0, timeout: float = 20.0, headers: dict | None = None):
        h = {"User-Agent": user_agent(), "Accept": "*/*"}
        h.update(headers or {})
        self.client = httpx.Client(timeout=timeout, headers=h, follow_redirects=True)
        self.min_interval = min_interval
        self._last = 0.0

    def get(self, url: str, **kw) -> httpx.Response:
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        return self.client.get(url, **kw)

    def close(self) -> None:
        self.client.close()
