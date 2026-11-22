"""Newsdesk fetchers. Each returns RawItems and never touches the database.

  nse_filings  NSE corporate announcements (official, T1) + the filed PDF's text (honest client)
  press        Google News search by the stock's aliases, credited to the real publisher (tiered)
  feeds        Publisher RSS feeds filtered to the stock's names (headlines)
  captures     Your browser captures (ET Prime etc.) linked to the stock, attested only

A publication time that the source does not give stays None — never "now" (spec §6).
"""
from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import feedparser

from ..data.sources import PoliteClient, domain_of, tier_for
from ..settings import IST, RAW, config

FILING_TEXT = RAW / "filings"
FILING_TEXT.mkdir(parents=True, exist_ok=True)


@dataclass
class RawItem:
    source: str            # nse_filing | press | feed | capture
    url: str
    title: str
    body: str = ""
    publisher: str = ""
    tier: str = "T3_other_press"
    published_at: str | None = None
    category: str | None = None
    is_stub: bool = False  # headline only
    tagged: tuple = field(default_factory=tuple)
    via: str = ""


def _nse_time(s: str | None) -> str | None:
    if not s:
        return None
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s.strip(), fmt).replace(tzinfo=IST).isoformat()
        except ValueError:
            continue
    return None


def _feed_time(e) -> str | None:
    for k in ("published", "updated"):
        if e.get(k):
            try:
                return parsedate_to_datetime(e[k]).isoformat()
            except (TypeError, ValueError):
                return None
    return None


def routine(title: str, category: str | None) -> str | None:
    text = f"{title} {category or ''}".lower()
    return next((p for p in config("newsdesk").get("routine_filings", []) if re.search(p, text)), None)


def pdf_text(client: PoliteClient, url: str) -> str:
    """Text of a filed PDF (cached by URL). '' for scanned/unreadable/oversize files."""
    cfg = config("newsdesk")["filings"]
    cache = FILING_TEXT / (hashlib.sha256(url.encode()).hexdigest()[:24] + ".txt")
    if cache.exists():
        return cache.read_text(encoding="utf-8")
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    r = client.get(url)
    if r.status_code != 200 or r.content[:4] != b"%PDF" or len(r.content) > 12_000_000:
        return ""
    try:
        reader = PdfReader(io.BytesIO(r.content))
        parts = []
        for page in reader.pages[: cfg.get("pdf_max_pages", 6)]:
            parts.append(page.extract_text() or "")
            if sum(map(len, parts)) >= cfg.get("pdf_max_chars", 20000):
                break
        text = " ".join(" ".join(parts).split())[: cfg.get("pdf_max_chars", 20000)]
    except Exception:
        text = ""
    cache.write_text(text, encoding="utf-8")
    return text


def nse_filings(symbol: str, log=lambda m: None) -> list[RawItem]:
    from ..data import nse
    cfg = config("newsdesk")["filings"]
    s = nse.NSESession()
    out: list[RawItem] = []
    try:
        log("NSE announcements: requesting…")
        anns = s.announcements(symbol, days=int(cfg.get("days", 90)))
        if anns is None:
            log("NSE announcements unavailable (NSE refused or timed out)")
            return []
        pdf_budget = int(cfg.get("max_pdfs_per_refresh", 8)) if cfg.get("read_pdf", True) else 0
        new_pdfs = [a for a in anns if (a.get("attchmntFile") or "").lower().endswith(".pdf")
                    and not routine(a.get("desc") or "", a.get("desc"))
                    and not (FILING_TEXT / (hashlib.sha256(a["attchmntFile"].strip().encode()).hexdigest()[:24] + ".txt")).exists()]
        to_read = min(len(new_pdfs), pdf_budget)
        log(f"NSE announcements: {len(anns)} found · {to_read} new filing PDF(s) to read this time"
            + (f" ({len(new_pdfs) - to_read} more on later refreshes)" if len(new_pdfs) > to_read else ""))
        read_n = 0
        client = PoliteClient(min_interval=1.0)
        try:
            for a in anns:
                cat = (a.get("desc") or "").strip()
                subject = " ".join((a.get("attchmntText") or "").split())
                title = f"{cat}: {subject[:160]}" if subject else cat
                url = (a.get("attchmntFile") or "").strip() or f"https://www.nseindia.com/companies-listing/corporate-filings-announcements#{symbol}-{a.get('seq_id', '')}"
                body = subject
                if url.lower().endswith(".pdf") and pdf_budget > 0 and not routine(title, cat):
                    cached = (FILING_TEXT / (hashlib.sha256(url.encode()).hexdigest()[:24] + ".txt")).exists()
                    if not cached:
                        read_n += 1
                        log(f"Reading filing PDF {read_n} of {to_read}: {cat[:60]}")
                    text = pdf_text(client, url)
                    if not cached:
                        pdf_budget -= 1
                        log(f"  {'read ' + format(len(text), ',') + ' characters' if text else 'no text layer (scanned or unreadable)'}")
                    if text:
                        body = f"{subject}\n\n{text}" if subject else text
                out.append(RawItem("nse_filing", url, title, body, "NSE", "T1_official",
                                   _nse_time(a.get("an_dt") or a.get("sort_date")), cat, False, (symbol.upper(),),
                                   "NSE corporate announcements"))
        finally:
            client.close()
        log(f"NSE filings: {len(out)} announcements")
    finally:
        s.close()
    return out


def press(entry: dict, days: int = 30, log=lambda m: None) -> list[RawItem]:
    names = [a for a in entry.get("aliases") or [] if len(a) >= 3][:4] or [entry["name"]]
    q = "(" + " OR ".join(f'"{a}"' for a in names) + f") when:{days}d"
    url = f"https://news.google.com/rss/search?q={quote_plus(q)}&hl=en-IN&gl=IN&ceid=IN:en"
    c = PoliteClient(min_interval=0.5)
    out = []
    try:
        feed = feedparser.parse(c.get(url).text)
        for e in feed.entries:
            src = e.get("source") or {}
            href = src.get("href") if isinstance(src, dict) else None
            pub = (src.get("title") if isinstance(src, dict) else None) or domain_of(href)
            title = re.sub(r"\s+-\s+[^-]+$", "", e.get("title", "")).strip()
            if not title or not e.get("link"):
                continue
            tier = tier_for(href, default="")
            out.append(RawItem("press", e["link"], title, "", pub, tier or "unregistered",
                               _feed_time(e), None, True, (), f"Google News search · {domain_of(href) or 'unknown publisher'}"))
        log(f"Press search: {len(out)} headlines for {', '.join(names)}")
    except Exception as ex:
        log(f"Press search unavailable ({type(ex).__name__})")
    finally:
        c.close()
    return out


def feeds(entry: dict, log=lambda m: None) -> list[RawItem]:
    names = (entry.get("aliases") or []) + (entry.get("link_only") or [])
    c = PoliteClient(min_interval=0.3, timeout=8.0)     # a hanging feed must not stall the refresh
    out = []
    try:
        for f in config("sources").get("rss_feeds", []):
            try:
                feed = feedparser.parse(c.get(f["url"]).text)
            except Exception as ex:
                log(f"{f['name']}: unavailable ({type(ex).__name__}) — skipped")
                continue
            for e in feed.entries:
                t = f"{e.get('title', '')} {e.get('summary', '')}"
                if any(re.search(rf"(?<![A-Za-z]){re.escape(n)}(?![A-Za-z])", t, re.I) for n in names if len(n) >= 3):
                    out.append(RawItem("feed", e.get("link", ""), e.get("title", "").strip(), "", f["name"],
                                       tier_for(e.get("link") or f"https://{f['domain']}"), _feed_time(e), None, True, (), f["name"]))
    finally:
        c.close()
    log(f"Publisher feeds: {len(out)} matching headlines")
    return [i for i in out if i.url]


def captures(symbol: str) -> list[RawItem]:
    import json
    from ..ledger import get_ledger
    rows = get_ledger().fetchall("SELECT * FROM research_sources ORDER BY version DESC")
    seen, out = set(), []
    for r in rows:
        if r["source_id"] in seen:
            continue
        seen.add(r["source_id"])
        links = json.loads(r["company_links"] or "{}")
        if symbol.upper() not in [x["symbol"] for x in links.get("resolved", [])]:
            continue
        if r["usage_status"] != "personal_research_user_attested":
            continue
        ps = json.loads(r["passages"] or "[]")
        out.append(RawItem("capture", r["canonical_url"], r["title"] or r["canonical_url"], "\n\n".join(p["text"] for p in ps),
                           r["publisher"], "T4_capture_unverified", r["published_at"], None, False, (symbol.upper(),),
                           f"browser capture {r['source_id']} v{r['version']}"))
    return out
