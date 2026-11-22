"""Newsdesk watchlist and stock matching (Newsdesk's aliases / link_only / related / exclude).

Saved to data/settings/newsdesk_watchlist.yaml (git-ignored, editable by hand or in the UI).
A stock not on the list gets a default entry from the NSE equity master, so any symbol works.
Excludes are removed from the text BEFORE matching, so "Reliance Power" never matches "Reliance".
"""
from __future__ import annotations

import re

import yaml

from ..data import nse
from ..settings import SETTINGS_DIR, iso

WATCH_FILE = SETTINGS_DIR / "newsdesk_watchlist.yaml"
_SUFFIX = re.compile(r"\b(limited|ltd\.?)\b", re.I)


def _load() -> list[dict]:
    if not WATCH_FILE.exists():
        return []
    with open(WATCH_FILE, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or []


def _save(rows: list[dict]) -> None:
    WATCH_FILE.write_text(yaml.safe_dump(rows, sort_keys=False, allow_unicode=True), encoding="utf-8")


def default_entry(symbol: str) -> dict:
    m = nse.lookup(symbol) or {}
    name = m.get("name") or symbol.upper()
    core = " ".join(_SUFFIX.sub("", name).split()).strip(" .,")
    aliases = [a for a in {core, name} if a]
    return {"symbol": symbol.upper(), "name": name, "isin": m.get("isin"), "aliases": aliases, "link_only": [],
            "related": [], "exclude": [], "market_cap_cr": None, "watch": False}


def get(symbol: str) -> dict:
    sym = symbol.upper().strip()
    for r in _load():
        if r["symbol"] == sym:
            return default_entry(sym) | r
    return default_entry(sym)


def watchlist() -> list[dict]:
    return [default_entry(r["symbol"]) | r for r in _load() if r.get("watch", True)]


def upsert(entry: dict) -> dict:
    sym = entry["symbol"].upper().strip()
    if not sym or not re.fullmatch(r"[A-Z0-9&\-]{1,20}", sym):
        raise ValueError("symbol must be an NSE symbol")
    existing = next((r for r in _load() if r["symbol"] == sym), {})
    clean = dict(existing) | {"symbol": sym, "watch": bool(entry.get("watch", True)), "updated_at": iso()}
    for k in ("name",):
        if entry.get(k):
            clean[k] = str(entry[k])[:120]
    for k in ("aliases", "link_only", "related", "exclude"):
        if k in entry:
            clean[k] = [str(x).strip()[:80] for x in (entry[k] or []) if str(x).strip()][:30]
    if entry.get("market_cap_cr") not in (None, ""):
        clean["market_cap_cr"] = float(entry["market_cap_cr"])
    rows = [r for r in _load() if r["symbol"] != sym] + [clean]
    _save(rows)
    return get(sym)


def remove(symbol: str) -> None:
    """Stop watching (no background refresh). Custom names are kept, so re-adding restores them;
    stored news stays in the ledger and is still used by analyses of this stock."""
    rows = _load()
    for r in rows:
        if r["symbol"] == symbol.upper():
            r["watch"] = False
            r["updated_at"] = iso()
    _save(rows)


def salience(entry: dict, title: str, body: str = "", tagged: bool = False) -> float:
    """1.0 about the company · 0.6 named in the body · 0.4 only a subsidiary/asset · 0 passing/none."""
    if tagged:
        return 1.0
    text_t, text_b = title or "", (body or "")[:6000]
    for ex in entry.get("exclude") or []:
        pat = re.compile(re.escape(ex), re.I)
        text_t, text_b = pat.sub(" ", text_t), pat.sub(" ", text_b)

    def hits(names, text, case=False):
        n = 0
        for a in names or []:
            if len(a) < 2:
                continue
            n += len(re.findall(rf"(?<![A-Za-z0-9]){re.escape(a)}(?![A-Za-z0-9])", text, 0 if case else re.I))
        return n
    if hits(entry.get("aliases"), text_t) or hits(entry.get("link_only"), text_t, case=True):
        return 1.0
    body_hits = hits(entry.get("aliases"), text_b) + hits(entry.get("link_only"), text_b, case=True)
    if body_hits >= 2:
        return 0.8
    if body_hits == 1:
        return 0.6 if len(text_b) < 1500 else 0.35
    if hits(entry.get("related"), text_t) or hits(entry.get("related"), text_b):
        return 0.4
    return 0.0
