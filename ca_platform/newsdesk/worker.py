"""Background polling for Newsdesk watchlist stocks while `ca serve` runs (laptop only, spec §15).
Market and post-market hours poll more often (results are filed in the evening). After the
laptop sleeps, the next tick simply catches up: each stock refreshes when it is no longer fresh."""
from __future__ import annotations

import asyncio
import logging

from ..settings import config, iso, now_ist
from . import pipeline, watch

log = logging.getLogger("ca.newsdesk.worker")
STATUS: dict = {"running": False, "last_sweep": None, "last_error": None, "next_in_minutes": None}


def _interval_minutes() -> int:
    w = config("newsdesk").get("worker", {})
    t = now_ist()
    hm = (t.hour, t.minute)
    if t.weekday() < 5 and (9, 15) <= hm < (15, 30):
        return int(w.get("market_minutes", 10))
    if t.weekday() < 5 and (15, 30) <= hm < (22, 0):
        return int(w.get("post_market_minutes", 10))
    return int(w.get("other_minutes", 60))


async def sweep() -> dict:
    done = {}
    for e in watch.watchlist():
        sym = e["symbol"]
        if pipeline.is_fresh(sym):
            continue
        try:
            r = await asyncio.to_thread(pipeline.refresh, sym)
            done[sym] = {k: v.get("new", 0) for k, v in r["sources"].items()}
        except Exception as ex:  # one stock failing never stops the sweep
            STATUS["last_error"] = f"{sym}: {type(ex).__name__}: {str(ex)[:120]}"
    STATUS["last_sweep"] = iso()
    return done


async def run_forever():
    if not config("newsdesk").get("worker", {}).get("enabled", True):
        return
    STATUS["running"] = True
    await asyncio.sleep(20)            # let the server finish starting
    while True:
        try:
            await sweep()
        except Exception as ex:
            STATUS["last_error"] = f"{type(ex).__name__}: {str(ex)[:120]}"
        mins = _interval_minutes()
        STATUS["next_in_minutes"] = mins
        await asyncio.sleep(mins * 60)
