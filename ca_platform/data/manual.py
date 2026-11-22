"""User-maintained facts file: data/manual/<SYMBOL>.yaml (see docs/examples/manual_facts.example.yaml).

This is the authoritative-fact path until XBRL filing ingestion lands: you copy figures
from the official filing, record the filing URL/date, and mark `verified: true` only after
checking them yourself. Every fact becomes a typed FinancialFact with a cited passage.
"""
from __future__ import annotations

from typing import Any

import yaml

from ..contracts import FinancialFact
from ..evidence import EvidenceFactory, fmt
from ..settings import MANUAL

METRIC_LABELS = {
    "revenue": "Revenue from operations",
    "net_profit_attributable": "Net profit attributable to owners",
    "net_profit_underlying": "Underlying net profit (ex-exceptional)",
    "operating_cash_flow": "Operating cash flow",
    "eps": "EPS",
    "pe_ttm": "P/E (TTM)",
    "pb": "P/B",
    "roe_pct": "Return on equity",
    "debt_to_equity": "Debt to equity",
    "promoter_holding_pct": "Promoter holding",
    "promoter_pledge_pct": "Promoter pledge (% of promoter holding)",
    "fii_holding_pct": "FPI holding",
    "dii_holding_pct": "DII holding",
    "mf_holding_pct": "Mutual fund holding",
    "other_income": "Other income",
    "receivable_days": "Receivable days",
    "contingent_liabilities": "Contingent liabilities",
    "audit_opinion_unmodified": "Audit opinion unmodified (1 = yes, 0 = modified)",
    "slippages_cr": "Fresh slippages (banks)",
}


def load(symbol: str) -> dict[str, Any] | None:
    path = MANUAL / f"{symbol.upper()}.yaml"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def to_evidence(doc: dict, fac: EvidenceFactory, decision_at: str) -> tuple[list[FinancialFact], list[str], dict]:
    """Returns (facts, flags, surveillance). Excludes sources published after decision_at."""
    facts: list[FinancialFact] = []
    flags: list[str] = []
    src_ev: dict[str, Any] = {}
    sources = {s["id"]: dict(s, published_at=str(s["published_at"]) if s.get("published_at") else None)
               for s in doc.get("sources", [])}
    by_src: dict[str, list[tuple[str, dict | None]]] = {}
    for f in doc.get("facts", []):
        label = METRIC_LABELS.get(f["metric"], f["metric"])
        unit = f.get("unit", "")
        val = f.get("value")
        txt = (f"{label} for the {f.get('period', 'period')} ending {f.get('period_end', 'n/a')}"
               f"{' (' + f['scope'] + ')' if f.get('scope') else ''}: "
               f"{'not reported' if val is None else (fmt(val) + (' ' + unit if unit else ''))}.")
        by_src.setdefault(f.get("source", "_unsourced"), []).append((txt, f))
    for n in doc.get("notes", []):
        by_src.setdefault(n.get("source", "_unsourced"), []).append((n["text"], None))

    for sid, rows in by_src.items():
        s = sources.get(sid, {"title": "Unsourced manual entry", "publisher": "user", "tier": "T5_aggregator"})
        if s.get("published_at") and s["published_at"] > decision_at:
            flags.append(f"Manual source '{sid}' published after decision time — excluded (point-in-time rule)")
            continue
        if sid not in sources:
            flags.append("Some manual facts have no source — treated as aggregator-grade, unverified")
        ev = fac.add(kind="manual_fact" if s.get("tier") != "T1_official" else "exchange_filing",
                     title=s.get("title", sid), publisher=s.get("publisher", ""), tier=s.get("tier", "T5_aggregator"),
                     url=s.get("url"), published_at=s.get("published_at"), passages=[r[0] for r in rows],
                     retrieved_via="manual_facts_file",
                     notes="Copied by the user from the source into data/manual; 'verified' means user-checked.")
        src_ev[sid] = ev
        for i, (txt, f) in enumerate(rows):
            if f is None:
                continue
            facts.append(FinancialFact(
                id=f"fact-{len(facts) + 1}", metric=f["metric"], value=f.get("value"),
                status="available" if f.get("value") is not None else "missing",
                reason=None if f.get("value") is not None else "Not reported in source",
                unit=f.get("unit", ""), scope=f.get("scope"), period=f.get("period"),
                period_end=str(f.get("period_end")) if f.get("period_end") else None,
                evidence_id=ev.id, verified=bool(f.get("verified")) and s.get("tier") == "T1_official"))
    surv = doc.get("surveillance") or {"status": "unknown"}
    return facts, flags, surv
