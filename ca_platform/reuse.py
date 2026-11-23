"""Reuse a specialist report across days while nothing material for it has changed.

Configured per role in user.yaml `analysis.reuse_reports_across_days` (default: forensic only, the
role whose inputs — filings, audit, pledge, governance — change only when something is filed).

A stored report is reused only when ALL of these hold:
  1. Same role, model, effort, prompt version, system prompt and horizon (config hash).
  2. Stored within `analysis.reuse_max_days` (default 14).
  3. Nothing new for this role: every non-price evidence item it sees today (and every filed fact)
     was already in the stored run's view. Items that merely dropped out of a window are fine.
     Price-driven items (P/E, technicals, the news summary) are not compared — but see 4.
  4. Every evidence/passage id the stored report mentions — in citations or in its prose — maps to a
     passage with IDENTICAL text in today's view (so a cited P/E line that changed blocks reuse).
  5. The re-pointed report passes the same validator a fresh report must pass.
Otherwise the role runs fresh. A reuse is always logged with the run it came from.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, timedelta
from typing import Optional

from .contracts import Bundle, BrainReportLLM
from .prompting import role_items
from .settings import canonical_hash, config, iso

# Inputs that move with the price or with any news at all — not "new information" for these roles.
VOLATILE_KINDS = {"computed_fundamental", "computed_feature", "news_summary", "price_series", "chart_pattern",
                  "company_profile"}
ID_RE = re.compile(r"\bev-\d+(?::P\d+)?\b")


def _cfg() -> dict:
    return config("user").get("analysis") or {}


def enabled(role: str) -> bool:
    return role in (_cfg().get("reuse_reports_across_days") or ["forensic"])


def config_hash(role: str, system: str, horizon: str) -> str:
    rc = config("models")["roles"][role]
    return canonical_hash({"role": role, "provider": rc.get("provider", "anthropic"), "model": rc["model"],
                           "effort": rc.get("effort"), "prompt": rc["prompt"], "horizon": horizon,
                           "system": hashlib.sha256(system.encode("utf-8")).hexdigest()})


def stable_inputs(role: str, b: Bundle) -> list[str]:
    """Fingerprints of everything material this role sees, independent of per-run ids."""
    out = []
    for e in role_items(role, b):
        if e.kind in VOLATILE_KINDS:
            continue
        out.append(canonical_hash({"kind": e.kind, "tier": e.tier, "publisher": e.publisher, "title": e.title,
                                   "url": e.url, "published_at": e.published_at, "content": e.content_hash}))
    if role in ("quant", "forensic"):   # these roles also read the typed filed facts
        for f in b.facts:
            out.append(canonical_hash(f.model_dump(exclude={"id", "evidence_id"}, exclude_none=True)))
    return sorted(set(out))


def _id_texts(report_json: str, b: Bundle) -> Optional[dict]:
    """id -> ("p", passage text) | ("e", evidence content hash) for every id the report mentions."""
    passages = {p.id: p.text for e in b.evidence for p in e.passages}
    evs = {e.id: e.content_hash for e in b.evidence}
    out = {}
    for i in set(ID_RE.findall(report_json)):
        if i in passages:
            out[i] = ["p", passages[i]]
        elif i in evs:
            out[i] = ["e", evs[i]]
        else:
            return None   # the report mentions an id that is not in its own bundle: never reuse it
    return out


def store(led, role: str, b: Bundle, run_id: str, system: str, report: BrainReportLLM, model: str) -> None:
    if b.mode != "live" or not enabled(role):
        return
    rj = report.model_dump_json()
    ids = _id_texts(rj, b)
    if ids is None:
        return
    led.execute("INSERT INTO report_reuse VALUES (?,?,?,?,?,?,?,?,?,?)",
                [role, b.symbol, config_hash(role, system, b.horizon), run_id, b.decision_at[:10], iso(), model,
                 json.dumps(stable_inputs(role, b)), json.dumps(ids, ensure_ascii=False), rj])


def find(led, role: str, b: Bundle, system: str, check) -> Optional[tuple[BrainReportLLM, dict]]:
    """(report re-pointed to today's ids, info) or None. `check(report) -> errors` is the normal validator."""
    if b.mode != "live" or not enabled(role):
        return None
    today = date.fromisoformat(b.decision_at[:10])
    cutoff = (today - timedelta(days=int(_cfg().get("reuse_max_days", 14)))).isoformat()
    rows = led.fetchall("SELECT * FROM report_reuse WHERE role=? AND symbol=? AND config_hash=? AND day>=? "
                        "ORDER BY created_at DESC LIMIT 5",
                        [role, b.symbol, config_hash(role, system, b.horizon), cutoff])
    if not rows:
        return None
    now = set(stable_inputs(role, b))
    allowed = {e.id for e in role_items(role, b)}
    by_text: dict[str, str] = {}
    by_hash: dict[str, str] = {}
    for e in b.evidence:
        if e.id not in allowed:
            continue
        by_hash.setdefault(e.content_hash, e.id)
        for p in e.passages:
            by_text.setdefault(p.text, p.id)
    for r in rows:
        new_inputs = now - set(json.loads(r["stable"]))
        if new_inputs:
            continue   # something new for this role since that report
        idmap = {}
        for old, (k, v) in json.loads(r["ids"]).items():
            new = by_text.get(v) if k == "p" else by_hash.get(v)
            if new is None:
                idmap = None
                break
            idmap[old] = new
        if idmap is None:
            continue   # a cited passage changed or is gone
        rj = ID_RE.sub(lambda m: idmap[m.group(0)], r["report"])
        try:
            rep = BrainReportLLM.model_validate_json(rj)
        except Exception:
            continue
        if check(rep):
            continue
        return rep, {"run_id": r["run_id"], "day": r["day"], "model": r["model"], "ids_repointed": len(idmap)}
    return None
