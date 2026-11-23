"""Incremental search-grounded discovery.

Every discovery search (filings, news) is the most expensive single step of a run because the
model reads every result page as input. Re-running a stock re-read the same 90 days of pages. Now
each day's findings are kept (table `discovery_log`) and a later run searches only for what is new
since the previous search, carrying the earlier sources forward unchanged (their passages are the
same verbatim `cited_text` the search API returned then).

Rules
  * A FULL search (the original prompt) runs when there is no full search for this stock and purpose
    within `analysis.discovery_full_every_days` (default 14) — so coverage is rebuilt regularly.
  * Otherwise an INCREMENTAL search asks only for items published after the last search day and
    lists the sources already found, so they are not fetched again.
  * Searches from today are never used as history: a same-day re-run sends the identical request
    and the gateway's day cache answers it for free.
  * Loop 2+ (targeted open questions) always runs the full prompt and is not logged as history.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from .settings import config, iso

MAX_LISTED_SOURCES = 40


def full_every_days() -> int:
    return int((config("user").get("analysis") or {}).get("discovery_full_every_days", 14))


def plan(led, symbol: str, purpose: str, decision_at: str, loop: int = 1) -> dict:
    """{"mode": "full"} or {"mode": "incremental", "since": day, "base": day, "prior": [(day, out), ...]}."""
    if loop > 1:
        return {"mode": "full"}
    today = date.fromisoformat(decision_at[:10])
    cutoff = (today - timedelta(days=full_every_days())).isoformat()
    rows = led.fetchall("SELECT day, kind, out FROM discovery_log WHERE symbol=? AND purpose=? AND day<? AND day>=? "
                        "ORDER BY day", [symbol, purpose, today.isoformat(), cutoff])
    fulls = [r for r in rows if r["kind"] == "full"]
    if not fulls:
        return {"mode": "full"}
    base = fulls[-1]["day"]
    chain = [r for r in rows if r["day"] >= base]
    return {"mode": "incremental", "since": chain[-1]["day"], "base": base,
            "prior": [(r["day"], json.loads(r["out"])) for r in chain]}


def record(led, symbol: str, purpose: str, decision_at: str, kind: str, out: dict) -> None:
    keep = {k: out.get(k) for k in ("text", "citations", "results", "served_model", "stop_reason")}
    led.execute("INSERT OR REPLACE INTO discovery_log VALUES (?,?,?,?,?,?)",
                [symbol, purpose, decision_at[:10], kind, iso(), json.dumps(keep, ensure_ascii=False)])


def known_sources(p: dict) -> list[str]:
    seen, lines = set(), []
    for _, out in p.get("prior", []):
        for c in out.get("citations") or []:
            if c["url"] not in seen:
                seen.add(c["url"])
                lines.append(f"- {(c.get('title') or c['url'])[:120]} ({c['url']})")
    return lines[:MAX_LISTED_SOURCES]


def incremental_note(p: dict, decision_day: str) -> str:
    """Appended to the full prompt in incremental mode."""
    since = date.fromisoformat(p["since"])
    after = (since - timedelta(days=1)).isoformat()     # one day of overlap for late-indexed pages
    known = known_sources(p)
    return ("\n\nAn earlier search on " + p["since"] + " already covered everything before that date. Search ONLY for "
            f"items published after {after} and on or before {decision_day}. If nothing material is new, say so and stop."
            + ("\nThese sources were already found — do not cite them again:\n" + "\n".join(known) if known else ""))


def merged(p: dict, new_out: dict, decision_day: str) -> tuple[dict, dict[str, str]]:
    """Earlier findings + today's, de-duplicated on (url, cited_text). Returns (out, first_seen_day_by_url)."""
    outs = [(d, o) for d, o in p.get("prior", [])] + [(decision_day, new_out)]
    cites, results, first_seen, seen_c, seen_r = [], [], {}, set(), set()
    for day, o in outs:
        for c in o.get("citations") or []:
            key = (c["url"], c["cited_text"])
            if key in seen_c:
                continue
            seen_c.add(key)
            cites.append(c)
            first_seen.setdefault(c["url"], day)
        for r in o.get("results") or []:
            if r["url"] not in seen_r:
                seen_r.add(r["url"])
                results.append(r)
    return dict(new_out, citations=cites, results=results), first_seen
