"""Ingest raw items for one stock: store articles once, link them to the stock, merge articles
about the same thing into events, and extract claims. Statuses and scores are NOT stored here —
dossier.build computes them for a given moment so historical views stay point-in-time."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta

from ..ledger import get_ledger
from ..settings import config, iso, now_ist
from . import fetch, taxonomy, watch

STOP = set("""a an the of to in on for and or by with from at as is are was were be been this that its it their his her
has have had will would can could into over after before than about up down said says say new also more most per cent
crore cr rs inr stock stocks share shares company ltd limited india indian today market markets""".split())
# NSE filing subjects share boilerplate ("has informed the Exchange regarding 'Press Release…'"); ignore it.
FILING_STOP = set("""informed exchange regarding release press updates update general submitted outcome board meeting
intimation dated copy attached pursuant regulation sebi listing obligations disclosure requirements regulations""".split())


def tokens(text: str, drop=()) -> set[str]:
    """Content-word stems (first 5 letters), so 'acquire' / 'acquisition' and 'talks' / 'talk' match."""
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    dropw = {w for d in drop for w in re.findall(r"[a-z0-9]+", d.lower())}
    return {w if w.isdigit() else w[:5] for w in words
            if len(w) > 2 and w not in STOP and w not in dropw and not (w.isdigit() and len(w) <= 3)}


def similarity(a: set, b: set) -> float:
    """Headlines about one event share few words: blend Jaccard with overlap (shared / smaller),
    the latter only when at least two words are shared."""
    if not a or not b:
        return 0.0
    shared = len(a & b)
    overlap = shared / min(len(a), len(b)) if shared >= 2 else 0.0
    return max(shared / len(a | b), 0.8 * overlap)


def _nid(led) -> int:
    return int(led.fetchone("SELECT nextval('nd_seq') AS n")["n"])


def _when(published_at: str | None, first_seen: str) -> datetime:
    from .dossier import _dt
    return _dt(published_at) or _dt(first_seen)


def _drop_reason(it: fetch.RawItem) -> str | None:
    cfg = config("newsdesk")
    text = f"{it.title} {it.body[:400]}".lower()
    paid = next((m for m in cfg.get("paid_markers", []) if m in text), None)
    if paid:
        return f"paid content ({paid})"
    junk = next((p for p in cfg.get("junk_patterns", []) if re.search(p, it.title.lower())), None)
    if junk:
        return f"low-quality item ({junk})"
    if it.tier == "unregistered" and cfg.get("registry_only_press", True):
        return "publisher not in source registry"
    if it.source == "nse_filing":
        r = fetch.routine(it.title, it.category)
        if r:
            return f"routine filing ({r})"
    return None


def ingest(symbol: str, items: list[fetch.RawItem]) -> dict:
    led = get_ledger()
    sym = symbol.upper()
    entry = watch.get(sym)
    now = iso()
    counts = {"new": 0, "upgraded": 0, "linked": 0, "dropped": 0, "events_new": 0, "events_joined": 0}
    seen_urls: set[str] = set()
    for it in items:
        if not it.url or not it.title or it.url in seen_urls:   # sources sometimes repeat a link in one batch
            continue
        seen_urls.add(it.url)
        h = hashlib.sha256(f"{it.title}\n{it.body}".encode()).hexdigest()
        row = led.fetchone("SELECT * FROM nd_articles WHERE url=?", [it.url])
        if row:
            aid = row["id"]
            if row["is_stub"] and not it.is_stub and it.body:   # full text arrived for a headline
                led.execute("UPDATE nd_articles SET body=?, is_stub=false, content_hash=?, fetched_at=? WHERE id=?",
                            [it.body, h, now, aid])
                led.execute("DELETE FROM nd_claims WHERE article_id=?", [aid])
                counts["upgraded"] += 1
            dropped = row["dropped_reason"]
        else:
            import duckdb
            aid = _nid(led)
            dropped = _drop_reason(it)
            try:
                led.execute("INSERT INTO nd_articles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            [aid, it.url, it.source, it.publisher, it.tier, it.title[:500], it.body, it.published_at, now, now,
                             it.category, it.is_stub, h, dropped, it.via])
                counts["new"] += 1
            except duckdb.ConstraintException:       # stored meanwhile (e.g. by the background worker)
                row = led.fetchone("SELECT id, dropped_reason FROM nd_articles WHERE url=?", [it.url])
                if not row:
                    continue
                aid, dropped = row["id"], row["dropped_reason"]
        sal = watch.salience(entry, it.title, it.body, tagged=sym in it.tagged)
        if sal < 0.3:
            continue
        led.execute("INSERT OR REPLACE INTO nd_links VALUES (?,?,?)", [aid, sym, sal])
        counts["linked"] += 1
        if dropped:
            counts["dropped"] += 1
            continue
        official = it.tier == "T1_official"
        lab = taxonomy.label(it.title, it.body, it.category, official)
        if not led.fetchone("SELECT 1 AS x FROM nd_claims WHERE article_id=? AND symbol=?", [aid, sym]):
            for c in lab["claims"]:
                led.execute("INSERT INTO nd_claims VALUES (?,?,?,?,?,?)", [_nid(led), aid, sym, c["text"], c["amount_cr"], c["metric"]])
        if led.fetchone("SELECT 1 AS x FROM nd_event_articles ea JOIN nd_events e ON e.id=ea.event_id "
                        "WHERE ea.article_id=? AND e.symbol=?", [aid, sym]):
            continue
        art = led.fetchone("SELECT first_seen_at, published_at FROM nd_articles WHERE id=?", [aid])
        when = _when(art["published_at"], art["first_seen_at"])
        toks = tokens(f"{it.title} {it.body[:500]}", drop=(entry.get("aliases") or []) + (entry.get("link_only") or []))
        if official:
            toks -= {w[:5] for w in FILING_STOP}
        ev = _find_event(led, sym, lab, toks, when, official)
        if ev is None:
            eid = _nid(led)
            led.execute("INSERT INTO nd_events VALUES (?,?,?,?,?,?,?,?,?)",
                        [eid, sym, lab["event_type"], it.title[:300], lab["direction"], lab["fact_type"],
                         json.dumps(sorted(toks)[:200]), when.isoformat(), now])
            counts["events_new"] += 1
        else:
            eid = ev["id"]
            sig = sorted(set(json.loads(ev["sig"] or "[]")) | toks)[:300]
            upd = {"sig": json.dumps(sig), "last_updated": now, "first_seen": min(ev["first_seen"], when.isoformat())}
            if official and lab["event_type"] != "clarification":
                upd["fact_type"] = "fact"
            led.execute(f"UPDATE nd_events SET {', '.join(k + '=?' for k in upd)} WHERE id=?", [*upd.values(), eid])
            counts["events_joined"] += 1
        led.execute("INSERT OR IGNORE INTO nd_event_articles VALUES (?,?)", [eid, aid])
    return counts


def _find_event(led, sym: str, lab: dict, toks: set, when: datetime, official: bool = False):
    """Press joins any matching event; a FILING joins an event that already holds a filing only
    when the two are near-identical (separate filings are separate events, e.g. two press releases)."""
    clar = lab["event_type"] == "clarification"
    window = timedelta(days=7 if clar else 3)
    rows = led.fetchall("SELECT * FROM nd_events WHERE symbol=?", [sym])
    with_filing = {r["event_id"] for r in led.fetchall(
        "SELECT DISTINCT ea.event_id FROM nd_event_articles ea JOIN nd_articles a ON a.id=ea.article_id "
        "JOIN nd_events e ON e.id=ea.event_id WHERE e.symbol=? AND a.tier='T1_official'", [sym])} if official else set()
    best, best_sim = None, 0.0
    for ev in rows:
        from .dossier import _dt
        if abs((_dt(ev["first_seen"]) - when).total_seconds()) > window.total_seconds():
            continue
        sim = similarity(toks, set(json.loads(ev["sig"] or "[]")))
        need = 0.2 if clar else 0.25 if ev["event_type"] == lab["event_type"] else 0.5
        if ev["id"] in with_filing:
            need = max(need, 0.7)
        if sim >= need and sim > best_sim:
            best, best_sim = ev, sim
    return best


def refresh(symbol: str, log=lambda m: None, days: int | None = None) -> dict:
    """Fetch every source for one stock and ingest. Returns per-source counts and failures."""
    sym = symbol.upper()
    entry = watch.get(sym)
    days = days or int(config("newsdesk").get("window_days", 90))
    report = {"symbol": sym, "sources": {}, "failed": []}
    label = {"nse_filings": "NSE filings", "press": "Press search", "feeds": "Publisher feeds", "captures": "Your ET captures"}
    for name, fn in [("nse_filings", lambda: fetch.nse_filings(sym, log)),
                     ("press", lambda: fetch.press(entry, min(days, 90), log)),
                     ("feeds", lambda: fetch.feeds(entry, log)),
                     ("captures", lambda: fetch.captures(sym))]:
        try:
            if name != "nse_filings":
                log(f"{label[name]}: fetching…")
            items = fn()
            res = ingest(sym, items)
            report["sources"][name] = {"items": len(items), **res}
            log(f"{label[name]}: {len(items)} item(s) · {res['new']} new · {res['linked']} about {sym} · "
                f"{res['dropped']} set aside · {res['events_new']} new event(s)")
        except Exception as e:
            report["failed"].append(f"{name}: {type(e).__name__}: {str(e)[:150]}")
            log(f"{name} failed: {type(e).__name__}")
    get_ledger().execute("INSERT OR REPLACE INTO nd_state VALUES (?,?,?)", [sym, iso(), json.dumps(report)])
    return report


def is_fresh(symbol: str) -> bool:
    row = get_ledger().fetchone("SELECT last_refresh FROM nd_state WHERE symbol=?", [symbol.upper()])
    if not row:
        return False
    now = now_ist()
    market = now.weekday() < 5 and (9, 15) <= (now.hour, now.minute) <= (22, 0)
    mins = config("newsdesk")["freshness_minutes"]["market" if market else "other"]
    return (now - datetime.fromisoformat(row["last_refresh"])) < timedelta(minutes=mins)
