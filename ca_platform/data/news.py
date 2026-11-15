"""Headline discovery from permitted RSS feeds (spec §6 News). Headlines, publisher, date
and link only — no full-text scraping. Each result is tagged with its source tier; items
from domains outside the registry are dropped."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import feedparser

from ..settings import config
from .sources import PoliteClient, domain_of, tier_for


def _published(entry) -> str | None:
    for key in ("published", "updated"):
        if entry.get(key):
            try:
                return parsedate_to_datetime(entry[key]).isoformat()
            except Exception:
                return None
    return None


def _mentions(text: str, terms: list[str]) -> bool:
    low = text.lower()
    return any(re.search(rf"\b{re.escape(t.lower())}\b", low) for t in terms if len(t) >= 3)


def search_headlines(company: str, symbol: str, days: int = 90, limit: int = 25) -> tuple[list[dict], list[str]]:
    """Return (items, log). Item: title, url, publisher, domain, tier, published_at, feed."""
    cfg = config("sources")
    terms = [company, symbol] + [w for w in [re.sub(r"\b(limited|ltd)\b\.?", "", company, flags=re.I).strip()] if w]
    since = datetime.now(timezone.utc) - timedelta(days=days)
    client = PoliteClient(min_interval=0.5)
    items, log = [], []
    try:
        if cfg.get("google_news_rss"):
            q = quote_plus(f'"{terms[-1] or company}" when:{days}d')
            url = f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
            try:
                r = client.get(url)
                feed = feedparser.parse(r.text)
                kept = 0
                for e in feed.entries:
                    src = e.get("source") or {}
                    href = src.get("href") if isinstance(src, dict) else None
                    dom = domain_of(href)
                    tier = tier_for(href, default="")
                    if not tier or tier.startswith("T5"):
                        continue
                    title = re.sub(r"\s+-\s+[^-]+$", "", e.get("title", "")).strip()
                    items.append({"title": title, "url": e.get("link"), "publisher": src.get("title") or dom,
                                  "domain": dom, "tier": tier, "published_at": _published(e),
                                  "feed": "Google News RSS (search)"})
                    kept += 1
                log.append(f"Google News RSS: {len(feed.entries)} results, {kept} from registry publishers")
            except Exception as ex:
                log.append(f"Google News RSS unavailable ({type(ex).__name__})")
        for f in cfg.get("rss_feeds", []):
            try:
                r = client.get(f["url"])
                feed = feedparser.parse(r.text)
                kept = 0
                for e in feed.entries:
                    text = f"{e.get('title', '')} {e.get('summary', '')}"
                    if not _mentions(text, terms):
                        continue
                    items.append({"title": e.get("title", "").strip(), "url": e.get("link"), "publisher": f["name"],
                                  "domain": f["domain"], "tier": tier_for(e.get("link") or f"https://{f['domain']}"),
                                  "published_at": _published(e), "feed": f["name"]})
                    kept += 1
                log.append(f"{f['name']}: {len(feed.entries)} items, {kept} mention {symbol}")
            except Exception as ex:
                log.append(f"{f['name']}: unavailable ({type(ex).__name__})")
    finally:
        client.close()

    # window filter + dedupe by normalised title (syndicated copies are not new confirmation)
    seen, out = set(), []
    for it in items:
        if it["published_at"]:
            try:
                if datetime.fromisoformat(it["published_at"]) < since:
                    continue
            except ValueError:
                pass
        key = re.sub(r"[^a-z0-9]", "", it["title"].lower())[:80]
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    tier_rank = {"T1_official": 0, "T2_reputable_press": 1, "T3_other_press": 2}
    out.sort(key=lambda x: x["published_at"] or "", reverse=True)
    out.sort(key=lambda x: tier_rank.get(x["tier"], 9))
    return out[:limit], log
