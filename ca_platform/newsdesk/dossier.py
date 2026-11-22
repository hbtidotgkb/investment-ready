"""Build a stock's news dossier for a given moment (point-in-time), and turn it into bundle
evidence. Claim statuses, independence and scores are computed here from stored articles, so a
dossier for a past decision time only uses what had been seen and published by then.

Statuses (per claim, then per event):
  filed            the claim comes from an official filing
  confirmed        a filing states the SAME metric within 5%
  contradicted     a filing states the same metric with a different figure
  denied           a clarification filing on the event denies the report
  filing_on_event  a filing exists on this event, but this figure wasn't checked against it
  investigative    a governance/accounting report from the press, no filing yet
  unconfirmed      press only
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta

from ..ledger import get_ledger
from ..settings import config, iso, now_ist
from . import taxonomy
from .pipeline import similarity, tokens
from .watch import get as watch_get

DENIAL = re.compile(r"\b(denies|deny|no such|not aware|baseless|factually incorrect|not true|no (proposal|decision)|"
                    r"speculative in nature)\b", re.I)
STATUS_ORDER = ["denied", "contradicted", "confirmed", "filed", "filing_on_event", "investigative", "unconfirmed"]
STATUS_LABEL = {"filed": "Company filing", "confirmed": "Confirmed by filing", "contradicted": "Doesn't match the filing",
                "denied": "Denied by the company", "filing_on_event": "Filing on same event (figure not checked)",
                "investigative": "Investigative report", "unconfirmed": "Press only"}
STRONG = {"filed", "confirmed"}


def _close(a, b, tol=0.05):
    return a and b and abs(a - b) / max(a, b) <= tol


def _dt(s: str | None):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        from ..settings import IST
        d = d.replace(tzinfo=IST)
    return d


MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _future_date(text: str, after: date) -> date | None:
    """First date in a scheduling notice that falls on/after `after`."""
    cands = []
    for d, m, y in re.findall(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9}),?\s+(\d{4})\b", text):
        if m[:3].lower() in MONTHS:
            cands.append((int(y), MONTHS[m[:3].lower()], int(d)))
    for m, d, y in re.findall(r"\b([A-Za-z]{3,9})\s+(\d{1,2}),\s+(\d{4})\b", text):
        if m[:3].lower() in MONTHS:
            cands.append((int(y), MONTHS[m[:3].lower()], int(d)))
    for d, m, y in re.findall(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})\b", text):
        cands.append((int(y), int(m), int(d)))
    out = []
    for y, m, d in cands:
        try:
            dd = date(y, m, d)
        except ValueError:
            continue
        if dd >= after:
            out.append(dd)
    return min(out) if out else None


def build(symbol: str, days: int | None = None, as_of: str | None = None) -> dict:
    led = get_ledger()
    sym = symbol.upper()
    cfg = config("newsdesk")
    days = int(days or cfg.get("window_days", 90))
    as_of_dt = _dt(as_of) or now_ist()
    start = as_of_dt - timedelta(days=days)
    entry = watch_get(sym)
    trust = cfg.get("trust", {})

    arts = {a["id"]: a for a in led.fetchall(
        "SELECT a.*, l.salience FROM nd_articles a JOIN nd_links l ON l.article_id=a.id WHERE l.symbol=?", [sym])}
    visible = {}
    dropped: dict[str, int] = {}
    for aid, a in arts.items():
        if (_dt(a["first_seen_at"]) or as_of_dt) > as_of_dt:
            continue                                            # not yet seen at this moment
        pub = _dt(a["published_at"])
        if pub and pub > as_of_dt:
            continue                                            # published after the decision time
        if (pub or _dt(a["first_seen_at"])) < start:
            continue
        if a["dropped_reason"]:
            key = a["dropped_reason"].split(" (")[0]
            dropped[key] = dropped.get(key, 0) + 1
            continue
        visible[aid] = a

    claims_by_art: dict[int, list] = {}
    for c in led.fetchall("SELECT * FROM nd_claims WHERE symbol=?", [sym]):
        if c["article_id"] in visible:
            claims_by_art.setdefault(c["article_id"], []).append(c)

    ev_rows = {e["id"]: e for e in led.fetchall("SELECT * FROM nd_events WHERE symbol=?", [sym])}
    members: dict[int, list] = {}
    for ea in led.fetchall("SELECT ea.* FROM nd_event_articles ea JOIN nd_events e ON e.id=ea.event_id WHERE e.symbol=?", [sym]):
        if ea["article_id"] in visible:
            members.setdefault(ea["event_id"], []).append(visible[ea["article_id"]])

    events, coming_up = [], []
    for eid, arts_e in members.items():
        ev = ev_rows[eid]
        filings = [a for a in arts_e if a["tier"] == "T1_official"]
        press = [a for a in arts_e if a["tier"] != "T1_official"]
        etype = ev["event_type"]
        if etype == "scheduled" and filings:
            for f in filings:
                d = _future_date(f"{f['title']} {f['body'] or ''}"[:4000], as_of_dt.date())
                if d:
                    coming_up.append({"date": d.isoformat(), "what": f["title"][:160], "article_id": f["id"]})
            continue
        denial = next((f for f in filings if taxonomy.classify(f["title"], f["body"] or "", f["category"], True) == "clarification"
                       and DENIAL.search(f"{f['title']} {(f['body'] or '')[:3000]}")), None)
        filed_amounts = [(c["amount_cr"], c["metric"], f["id"]) for f in filings for c in claims_by_art.get(f["id"], []) if c["amount_cr"]]
        claim_rows = []
        for a in arts_e:
            for c in claims_by_art.get(a["id"], []):
                if a["tier"] == "T1_official":
                    st, basis = "filed", a["id"]
                elif denial is not None:
                    st, basis = "denied", denial["id"]
                elif c["amount_cr"] and c["metric"] and any(m == c["metric"] for _, m, _ in filed_amounts):
                    same = [(amt, fid) for amt, m, fid in filed_amounts if m == c["metric"]]
                    hit = next((fid for amt, fid in same if _close(amt, c["amount_cr"])), None)
                    st, basis = ("confirmed", hit) if hit else ("contradicted", same[0][1])
                elif filings:
                    st, basis = "filing_on_event", filings[0]["id"]
                elif etype in ("governance_flag", "accounting_concern"):
                    st, basis = "investigative", None
                else:
                    st, basis = "unconfirmed", None
                claim_rows.append({"text": c["text"], "amount_cr": c["amount_cr"], "metric": c["metric"], "status": st,
                                   "article_id": a["id"], "basis_article_id": basis})
        statuses = {r["status"] for r in claim_rows}
        status = next((s for s in STATUS_ORDER if s in statuses), "filed" if filings else "unconfirmed")

        # Independence: wording-alike press articles are ONE story, however many outlets carry it.
        stories: list[set] = []
        for p in press:
            t = tokens(p["title"], drop=entry.get("aliases") or [])
            if not any(similarity(t, s) >= 0.6 for s in stories):
                stories.append(t)
        n_press_stories = len(stories)
        best_press = max((trust.get(p["tier"], 0.6) for p in press), default=0.0)
        conf = 0.97 if filings else min(0.75, best_press + 0.03 * max(0, n_press_stories - 1))
        conf = {"denied": conf * 0.25, "contradicted": conf * 0.4, "investigative": min(conf, 0.8)}.get(status, conf)
        ftype = "fact" if filings else ev["fact_type"]
        if ftype == "speculation" and status not in STRONG:
            conf *= 0.7
        if ftype == "opinion":
            conf *= 0.6
        if not filings and all(p["is_stub"] for p in press):
            conf *= 0.8

        base, half_life, _ = taxonomy.EVENT_TYPES.get(etype, taxonomy.EVENT_TYPES["other"])
        amt = max((r["amount_cr"] or 0 for r in claim_rows), default=0)
        mat = base + (min(0.3, 3 * amt / entry["market_cap_cr"]) if amt and entry.get("market_cap_cr") else (0.1 if amt >= 1000 else 0))
        mat = min(1.0, mat)
        first = min(_dt(a["published_at"]) or _dt(a["first_seen_at"]) for a in arts_e)
        age_h = max(0.0, (as_of_dt - first).total_seconds() / 3600)
        rec = 0.5 ** (age_h / half_life)
        events.append({"event_id": eid, "type": etype, "title": ev["title"], "direction": ev["direction"], "fact_type": ftype,
                       "status": status, "status_label": STATUS_LABEL[status], "first_seen": first.isoformat(),
                       "filings": len(filings), "press_articles": len(press), "independent_press_stories": n_press_stories,
                       "publishers": sorted({a["publisher"] for a in arts_e if a["publisher"]}),
                       "headline_only": not filings and all(p["is_stub"] for p in press),
                       "materiality": round(mat, 3), "confidence": round(conf, 3), "recency": round(rec, 3),
                       "score": round(mat * conf * rec, 4), "claims": claim_rows,
                       "article_ids": [a["id"] for a in sorted(arts_e, key=lambda a: (a["tier"] != "T1_official", a["is_stub"]))]})

    events.sort(key=lambda e: -e["score"])
    events = events[: int(cfg.get("max_events", 15))]
    coming_up.sort(key=lambda c: c["date"])

    # numbered citations in ranked order
    cites, num = {}, {}
    for e in events:
        e["citations"] = []
        for aid in e["article_ids"]:
            if aid not in num:
                num[aid] = len(num) + 1
                a = visible[aid]
                cites[str(num[aid])] = {"article_id": aid, "publisher": a["publisher"], "tier": a["tier"], "source": a["source"],
                                        "title": a["title"], "url": a["url"], "published_at": a["published_at"],
                                        "first_seen_at": a["first_seen_at"], "headline_only": bool(a["is_stub"]),
                                        "content_hash": a["content_hash"], "via": a["via"]}
            e["citations"].append(num[aid])
    for c in coming_up:
        if c["article_id"] not in num:
            num[c["article_id"]] = len(num) + 1
            a = visible[c["article_id"]]
            cites[str(num[c["article_id"]])] = {"article_id": a["id"], "publisher": a["publisher"], "tier": a["tier"], "source": a["source"],
                                                "title": a["title"], "url": a["url"], "published_at": a["published_at"],
                                                "first_seen_at": a["first_seen_at"], "headline_only": bool(a["is_stub"]),
                                                "content_hash": a["content_hash"], "via": a["via"]}
        c["citation"] = num[c["article_id"]]

    check_first = [f"{e['title']} — {e['status_label']}" for e in events if e["status"] in ("contradicted", "denied")]
    not_known = [f"{e['title']} — material, press only" for e in events if e["status"] == "unconfirmed" and e["materiality"] >= 0.5]
    if not any(c["tier"] == "T1_official" for c in cites.values()):
        not_known.append(f"No exchange filings for {sym} in the last {days} days (or NSE was unreachable).")
    state = led.fetchone("SELECT last_refresh, last_counts FROM nd_state WHERE symbol=?", [sym])
    statuses = [e["status"] for e in events]
    quality = {"events": len(events), "filed_or_confirmed": sum(s in STRONG for s in statuses),
               "filing_on_event": statuses.count("filing_on_event"), "press_only": statuses.count("unconfirmed"),
               "against": statuses.count("contradicted") + statuses.count("denied"),
               "headline_only_events": sum(e["headline_only"] for e in events), "dropped": dropped,
               "articles_used": len(visible)}
    return {"schema_version": "newsdesk-dossier@1", "symbol": sym, "name": entry["name"], "as_of": as_of_dt.isoformat(),
            "window_days": days, "built_at": iso(), "last_refresh": state["last_refresh"] if state else None,
            "refresh_report": json.loads(state["last_counts"]) if state and state["last_counts"] else None,
            "events": events, "coming_up": coming_up, "check_first": check_first, "not_known": not_known,
            "citations": cites, "quality": quality}


def article(article_id: int) -> dict | None:
    return get_ledger().fetchone("SELECT * FROM nd_articles WHERE id=?", [int(article_id)])


# ---------------------------------------------------------------- bundle evidence ---

def _chunks(text: str, size: int = 700, limit: int = 12) -> list[str]:
    sents = re.split(r"(?<=[.!?])\s+", " ".join((text or "").split()))
    out, cur = [], ""
    for s in sents:
        if len(cur) + len(s) > size and cur:
            out.append(cur.strip())
            cur = ""
            if len(out) >= limit:
                break
        cur += " " + s
    if cur.strip() and len(out) < limit:
        out.append(cur.strip())
    return out


def _price_move(candles: list[dict], since: str) -> float | None:
    if not candles:
        return None
    d = since[:10]
    before = [c for c in candles if c["date"] <= d]
    if not before:
        return None
    return round((candles[-1]["close"] / before[-1]["close"] - 1) * 100, 1)


def to_evidence(fac, dossier: dict, candles: list[dict] | None = None, skip_urls: set | None = None) -> dict:
    """Add the dossier to a bundle: one verbatim-text evidence item per cited article (official
    filings as exchange_announcement, press as news_article, captures as browser_capture), plus one
    computed 'Newsdesk summary' whose passages state each event's status, so brains can cite them."""
    skip_urls = skip_urls or set()
    made, by_article = 0, {}
    ev_of = {}
    for e in dossier["events"]:
        for n in e["citations"]:
            ev_of.setdefault(n, e)
    for n, c in dossier["citations"].items():
        if c["url"] in skip_urls:
            continue
        a = article(c["article_id"]) or {}
        e = ev_of.get(int(n))
        etype = e["type"] if e else "scheduled"
        kind = "exchange_announcement" if c["tier"] == "T1_official" else "browser_capture" if c["source"] == "capture" else "news_article"
        limit = 40 if kind == "browser_capture" else 12
        passages = [c["title"]] + ([] if c["headline_only"] else _chunks(a.get("body") or "", limit=limit))
        note = (f"Newsdesk citation [{n}] · event type {etype}"
                + (f" · event status: {e['status_label']} · {e['independent_press_stories']} independent press stor"
                   f"{'y' if e['independent_press_stories'] == 1 else 'ies'}, {e['filings']} filing(s)" if e else " · scheduling notice")
                + (" · headline only (article text not retrieved)" if c["headline_only"] else ""))
        ev = fac.add(kind=kind, title=c["title"], publisher=c["publisher"] or "", tier=c["tier"], url=c["url"],
                     passages=passages, published_at=c["published_at"], first_seen_at=c["first_seen_at"],
                     access_status="capture_unverified" if kind == "browser_capture" else "public",
                     retrieved_via=f"newsdesk:{c['source']}:{etype}", notes=note)
        by_article[c["article_id"]] = ev.id
        made += 1
    lines = []
    for i, e in enumerate(dossier["events"], 1):
        mv = _price_move(candles or [], e["first_seen"])
        refs = ", ".join(by_article[dossier["citations"][str(n)]["article_id"]] for n in e["citations"]
                         if dossier["citations"][str(n)]["article_id"] in by_article)
        lines.append(f"Event {i}: {e['title'][:160]} | type {e['type']} | status: {e['status_label']} | "
                     f"{e['filings']} filing(s), {e['independent_press_stories']} independent press stor"
                     f"{'y' if e['independent_press_stories'] == 1 else 'ies'} | first seen {e['first_seen'][:10]}"
                     + (f" | price moved {mv:+.1f}% since then" if mv is not None else "") + (f" | sources {refs}" if refs else ""))
    for c in dossier["coming_up"]:
        lines.append(f"Coming up {c['date']}: {c['what']}")
    for x in dossier["check_first"]:
        lines.append(f"Check first: {x}")
    for x in dossier["not_known"]:
        lines.append(f"Not yet known: {x}")
    if lines:
        fac.add(kind="news_summary", title=f"Newsdesk summary for {dossier['symbol']} (computed)", publisher="ca_platform newsdesk",
                tier="SYS_computed", passages=lines, retrieved_via="newsdesk:summary",
                notes=f"Computed from the dossier as of {dossier['as_of']}; statuses by code (metric-matched claim checks).")
    return {"evidence_items": made, "events": len(dossier["events"]), "coming_up": len(dossier["coming_up"])}
