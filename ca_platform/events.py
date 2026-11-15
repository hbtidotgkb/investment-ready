"""Run progress events. Every stage transition and sub-step is emitted here, streamed to
the UI over Server-Sent Events, and persisted to the ledger so a reload replays them."""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from .settings import iso

# Canonical stage list — the UI progress tracker renders these in order.
STAGES: list[dict[str, str]] = [
    {"id": "context",    "phase": "evidence", "title": "Decision context & preflight", "who": "code"},
    {"id": "prices",     "phase": "evidence", "title": "Market data (Kite → bhavcopy)", "who": "code"},
    {"id": "features",   "phase": "evidence", "title": "Technicals & chart patterns", "who": "code"},
    {"id": "filings",    "phase": "evidence", "title": "Exchange data & filings discovery", "who": "code+claude"},
    {"id": "news",       "phase": "evidence", "title": "News discovery (tiered sources)", "who": "code+claude"},
    {"id": "inbox",      "phase": "evidence", "title": "Research inbox (ET Prime captures)", "who": "code"},
    {"id": "bundle",     "phase": "evidence", "title": "Freeze evidence bundle", "who": "code"},
    {"id": "quant",      "phase": "brains",   "title": "Quant brain", "who": "claude"},
    {"id": "news_brain", "phase": "brains",   "title": "News & Narrative brain", "who": "claude"},
    {"id": "forensic",   "phase": "brains",   "title": "Forensic CA brain", "who": "claude"},
    {"id": "anonymise",  "phase": "panel",    "title": "Anonymise reports (A/B/C)", "who": "code"},
    {"id": "head_draft", "phase": "panel",    "title": "Head CA draft", "who": "claude"},
    {"id": "critic",     "phase": "panel",    "title": "Critic CA review", "who": "claude"},
    {"id": "resolution", "phase": "panel",    "title": "Head response & settling tests", "who": "claude"},
    {"id": "rules",      "phase": "rules",    "title": "Rules engine (vetoes, sizing, tax)", "who": "code"},
    {"id": "consistency", "phase": "rules",   "title": "Whole-plan consistency check", "who": "code"},
    {"id": "outputs",    "phase": "outputs",  "title": "Ledger, report & WhatsApp text", "who": "code"},
]
PHASES = [
    {"id": "evidence", "title": "Evidence"},
    {"id": "brains", "title": "Specialist brains"},
    {"id": "panel", "title": "Head & Critic"},
    {"id": "rules", "title": "Rules"},
    {"id": "outputs", "title": "Outputs"},
]


class RunEvents:
    """Fan-out of events for one run."""

    def __init__(self, run_id: str, persist: Callable[[dict], None] | None = None):
        self.run_id = run_id
        self.seq = 0
        self.history: list[dict] = []
        self.subscribers: list[asyncio.Queue] = []
        self.persist = persist
        self.stage_started: dict[str, float] = {}
        self.done = False
        self.loop = 1  # current analysis loop; stamped on every event

    def emit(self, type_: str, stage: str | None = None, message: str = "",
             status: str | None = None, data: Any = None) -> dict:
        self.seq += 1
        ev = {"run_id": self.run_id, "seq": self.seq, "ts": iso(), "type": type_, "loop": self.loop,
              "stage": stage, "status": status, "message": message, "data": data}
        if type_ == "stage_start" and stage:
            self.stage_started[stage] = time.monotonic()
        if type_ == "stage_end" and stage and stage in self.stage_started:
            ev["duration_ms"] = int((time.monotonic() - self.stage_started[stage]) * 1000)
        self.history.append(ev)
        if self.persist:
            try:
                self.persist(ev)
            except Exception:
                pass
        for q in list(self.subscribers):
            q.put_nowait(ev)
        if type_ == "run_end":
            self.done = True
        return ev

    # convenience
    def start(self, stage: str, message: str = "") -> None:
        self.emit("stage_start", stage, message, "running")

    def log(self, stage: str, message: str, data: Any = None) -> None:
        self.emit("log", stage, message, None, data)

    def end(self, stage: str, status: str = "completed", message: str = "", data: Any = None) -> None:
        self.emit("stage_end", stage, message, status, data)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        for ev in self.history:
            q.put_nowait(ev)
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self.subscribers:
            self.subscribers.remove(q)


REGISTRY: dict[str, RunEvents] = {}
