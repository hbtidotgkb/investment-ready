"""Live progress of Newsdesk refreshes, shared by the page, the CLI path and the background worker.
One refresh per stock at a time; the page polls /api/newsdesk/progress/{symbol}."""
from __future__ import annotations

import asyncio

from ..settings import iso
from . import pipeline

PROGRESS: dict[str, dict] = {}
_TASKS: set = set()


def get(symbol: str) -> dict:
    return PROGRESS.get(symbol.upper()) or {"symbol": symbol.upper(), "running": False, "steps": []}


def start(symbol: str, days: int | None = None, who: str = "page") -> asyncio.Task | None:
    """Begin a background refresh unless one is already running for this stock."""
    sym = symbol.upper()
    cur = PROGRESS.get(sym)
    if cur and cur["running"]:
        return cur.get("_task")
    p = {"symbol": sym, "running": True, "started": iso(), "finished": None, "steps": [], "error": None,
         "report": None, "who": who}
    PROGRESS[sym] = p

    def log(m: str):
        p["steps"].append({"t": iso()[11:19], "m": m})
        del p["steps"][:-200]

    async def run():
        log(f"Refresh started ({who})")
        try:
            p["report"] = await asyncio.to_thread(pipeline.refresh, sym, log, days)
            failed = p["report"]["failed"]
            log("Done" + (f" — {len(failed)} source(s) failed: {'; '.join(failed)}" if failed else " — dossier ready"))
        except Exception as e:  # shown on the page, never swallowed
            p["error"] = f"{type(e).__name__}: {str(e)[:200]}"
            log(f"Failed: {p['error']}")
        finally:
            p["running"] = False
            p["finished"] = iso()

    t = asyncio.get_running_loop().create_task(run())
    p["_task"] = t
    _TASKS.add(t)
    t.add_done_callback(_TASKS.discard)
    return t


def public(p: dict) -> dict:
    return {k: v for k, v in p.items() if not k.startswith("_")}
