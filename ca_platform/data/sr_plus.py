"""LSEG Stock Reports Plus reports among the user's stock PDFs (printed from ETPrime).

The PDFs are usually image-only, so a model transcribes each one once (cached by content hash in
data/sr_plus/<SYMBOL>/, see data/docs.py). Code then decides whether a transcription can be used:
the company must match, the report must be recent, and the report's own arithmetic (surprise %,
target upside, 90-day estimate change, analyst counts) must recompute. The newest usable report is
the current one; older reports become a history of how consensus moved. Values are third-party
data and opinion — attributed, never our valuation. The files are LSEG copyright and stay local.
"""
from __future__ import annotations

import re
import typing
from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field

from ..settings import config

EXTRACTOR = "sr_plus@2"
USED = ("available", "stale")


# ------------------------------------------------------------------ schema ---

class SRMetric(BaseModel):
    name: str
    value: Optional[float] = None
    unit: Optional[str] = None
    period: Optional[str] = None
    page: Optional[int] = None


class SRSurprise(BaseModel):
    type: Optional[str] = None
    announce_date: Optional[str] = None
    period_end: Optional[str] = None
    actual_eps: Optional[float] = None
    mean_eps: Optional[float] = None
    surprise_pct: Optional[float] = None


class SREstimateTrend(BaseModel):
    period: str                      # e.g. "Q 26-12", "Y 2027", "Price Target"
    current: Optional[float] = None
    days_30_ago: Optional[float] = None
    days_90_ago: Optional[float] = None
    change_90d_pct: Optional[float] = None


class SREstimate(BaseModel):
    period: str                      # e.g. "26-12" (quarter) or "2027" (year)
    mean: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    analysts: Optional[int] = None
    growth_pct: Optional[float] = None


class SRPeer(BaseModel):
    ticker: str
    average_score: Optional[int] = None
    price: Optional[float] = None
    ret_1m_pct: Optional[float] = None
    ret_3m_pct: Optional[float] = None
    ret_1y_pct: Optional[float] = None
    market_cap_text: Optional[str] = None
    trailing_pe: Optional[float] = None
    forward_pe: Optional[float] = None
    dividend_yield_pct: Optional[float] = None
    net_margin_pct: Optional[float] = None
    ltg_forecast_pct: Optional[float] = None
    ibes_mean: Optional[str] = None
    analysts: Optional[int] = None


class SRHighlight(BaseModel):
    section: str
    text: str
    page: Optional[int] = None


class SRPlusLLM(BaseModel):
    company_line: Optional[str] = None          # "INFOSYS (INFY-IN)"
    ticker: Optional[str] = None                # "INFY-IN"
    industry: Optional[str] = None
    report_date: Optional[str] = None           # YYYY-MM-DD
    last_close: Optional[float] = None
    last_close_date: Optional[str] = None
    high_52w: Optional[float] = None
    low_52w: Optional[float] = None
    trailing_pe: Optional[float] = None
    forward_pe: Optional[float] = None
    annual_dividend: Optional[float] = None
    dividend_yield_pct: Optional[float] = None
    roe_pct: Optional[float] = None
    ltg_forecast_pct: Optional[float] = None
    institutional_ownership_pct: Optional[float] = None
    ret_1m_pct: Optional[float] = None
    ret_3m_pct: Optional[float] = None
    market_cap_text: Optional[str] = None
    annual_revenue_text: Optional[str] = None
    avg_daily_volume_text: Optional[str] = None
    score_average: Optional[int] = None
    score_earnings: Optional[int] = None
    score_fundamental: Optional[int] = None
    score_relative_valuation: Optional[int] = None
    score_risk: Optional[int] = None
    score_price_momentum: Optional[int] = None
    ibes_mean: Optional[Literal["strong_buy", "buy", "hold", "reduce", "sell", "strong_sell"]] = None
    ibes_analysts: Optional[int] = None
    ibes_strong_buy: Optional[int] = None
    ibes_buy: Optional[int] = None
    ibes_hold: Optional[int] = None
    ibes_sell: Optional[int] = None
    ibes_strong_sell: Optional[int] = None
    target_mean: Optional[float] = None
    target_high: Optional[float] = None
    target_low: Optional[float] = None
    target_vs_current_pct: Optional[float] = None
    target_analysts: Optional[int] = None
    surprises_positive_last4: Optional[int] = None
    surprises_negative_last4: Optional[int] = None
    surprises_inline_last4: Optional[int] = None
    surprise_avg_last4_pct: Optional[float] = None
    revisions_up_4w: Optional[int] = None
    revisions_down_4w: Optional[int] = None
    revisions_avg_up_pct: Optional[float] = None
    revisions_avg_down_pct: Optional[float] = None
    broker_upgrades_120d: Optional[int] = None
    broker_downgrades_120d: Optional[int] = None
    next_report_date: Optional[str] = None
    fiscal_year_end: Optional[str] = None
    surprise_detail: list[SRSurprise] = Field(default_factory=list)
    estimate_trend: list[SREstimateTrend] = Field(default_factory=list)
    eps_quarterly: list[SREstimate] = Field(default_factory=list)
    eps_annual: list[SREstimate] = Field(default_factory=list)
    revenue_annual: list[SREstimate] = Field(default_factory=list)
    metrics: list[SRMetric] = Field(default_factory=list)
    peers: list[SRPeer] = Field(default_factory=list)
    highlights: list[SRHighlight] = Field(default_factory=list)
    unreadable: list[str] = Field(default_factory=list)


# What the model returns: printed text only, every field required, no optional or union types
# (structured outputs reject schemas with many optional parameters). Code converts the text.

class TxValue(BaseModel):
    key: str
    text: str
    page: int


class TxRow(BaseModel):
    table: str
    page: int
    cells: list[str]


class TxHighlight(BaseModel):
    section: str
    text: str
    page: int


class SRTranscriptLLM(BaseModel):
    values: list[TxValue]
    rows: list[TxRow]
    highlights: list[TxHighlight]
    unreadable: list[str]


TABLES: dict[str, tuple[str, type[BaseModel]]] = {
    "surprise_detail": ("surprise_detail", SRSurprise), "estimate_trend": ("estimate_trend", SREstimateTrend),
    "eps_quarterly": ("eps_quarterly", SREstimate), "eps_annual": ("eps_annual", SREstimate),
    "revenue_annual": ("revenue_annual", SREstimate), "peers": ("peers", SRPeer), "metrics": ("metrics", SRMetric),
}
TABLE_COLUMNS = {t: [c for c in m.model_fields if c != "page"] for t, (_, m) in TABLES.items()}
SCALAR_KEYS = [k for k, f in SRPlusLLM.model_fields.items() if typing.get_origin(f.annotation) is not list]
EMPTY = {"", "-", "--", "—", "–", "n/a", "na", "nr", "null", "none"}
IBES = {"strong buy": "strong_buy", "buy": "buy", "hold": "hold", "reduce": "reduce", "sell": "sell", "strong sell": "strong_sell"}
MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def parse_num(text: str | None) -> Optional[float]:
    t = (text or "").strip().lower().replace(",", "").replace("−", "-").replace("rs", "").replace("inr", "")
    t = t.replace("%", "").replace("x", "").strip()
    if t in EMPTY:
        return None
    neg = t.startswith("(") and t.endswith(")")
    m = re.fullmatch(r"\(?([+-]?\d+(?:\.\d+)?)\)?", t)
    if not m:
        return None
    v = float(m.group(1))
    return -abs(v) if neg else v


def parse_date(text: str | None) -> Optional[str]:
    t = (text or "").strip()
    if t.lower() in EMPTY:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
        return t
    m = re.fullmatch(r"(\d{4})[- ]([A-Za-z]{3})[A-Za-z]*[- ](\d{1,2})", t)        # 2026-Nov-22 / 2026 November 20
    if m and m.group(2).lower() in MONTHS:
        return f"{m.group(1)}-{MONTHS[m.group(2).lower()]:02d}-{int(m.group(3)):02d}"
    return t                                                                   # e.g. "27-03" fiscal labels stay as printed


def _convert(text: str, ann, name: str):
    base = [a for a in typing.get_args(ann) if a is not type(None)] or [ann]
    kind = base[0]
    if typing.get_origin(kind) is Literal:
        return IBES.get((text or "").strip().lower())
    if kind is int:
        v = parse_num(text)
        return None if v is None else int(round(v))
    if kind is float:
        return parse_num(text)
    if (text or "").strip().lower() in EMPTY:
        return None
    return parse_date(text) if ("date" in name or name == "period_end") else text.strip()


def normalize(tx: dict) -> tuple[dict, list[str]]:
    """Printed text → typed report. Returns (data, notes about anything that could not be converted)."""
    d: dict = {k: None for k in SCALAR_KEYS}
    notes: list[str] = []
    fields = SRPlusLLM.model_fields
    for v in tx.get("values", []):
        k = v["key"].strip()
        if k not in fields or k not in SCALAR_KEYS:
            notes.append(f"ignored unknown key '{k}' (page {v.get('page')})")
            continue
        val = _convert(v["text"], fields[k].annotation, k)
        if val is None and v["text"].strip().lower() not in EMPTY:
            notes.append(f"page {v.get('page')}: '{k}' printed as '{v['text']}' could not be read as a value")
        d[k] = val
    for field, _ in TABLES.values():
        d[field] = []
    for r in tx.get("rows", []):
        t = r["table"].strip()
        if t not in TABLES:
            notes.append(f"ignored row of unknown table '{t}'")
            continue
        field, model = TABLES[t]
        cols = TABLE_COLUMNS[t]
        cells = list(r["cells"]) + [""] * (len(cols) - len(r["cells"]))
        if len(r["cells"]) > len(cols):
            notes.append(f"page {r['page']}: {t} row has {len(r['cells'])} cells, expected {len(cols)} — extra cells ignored")
        row = {c: _convert(cells[i], model.model_fields[c].annotation, c) for i, c in enumerate(cols)}
        if "page" in model.model_fields:
            row["page"] = r["page"]
        key = cols[0]
        if row.get(key) is None:
            notes.append(f"page {r['page']}: {t} row without its first column skipped")
            continue
        d[field].append(row)
    d["highlights"] = [dict(h) for h in tx.get("highlights", [])]
    d["unreadable"] = list(tx.get("unreadable", [])) + notes
    return SRPlusLLM.model_validate(d).model_dump(), notes


async def transcribe(gw, title: str, sha: str, b64: str) -> dict:
    """One model call over the whole report; the caller caches the result by file hash."""
    from ..prompting import load_prompt
    body, meta = load_prompt(config("models")["roles"]["sr_plus"]["prompt"])
    res = await gw.structured(
        role="sr_plus", stage="filings", system=body, output_model=SRTranscriptLLM,
        user="Transcribe this Stock Reports Plus report: values, table rows and highlights exactly as printed.",
        documents=[{"title": title, "sha256": sha, "data": b64}])
    raw = res.parsed.model_dump()
    data, notes = normalize(raw)
    return {"extractor": EXTRACTOR, "prompt_version": meta.get("version"),
            "model": res.record.get("served_model") or res.record.get("model"),
            "cost_usd": res.record.get("cost_usd"), "data": data, "transcript": raw, "conversion_notes": notes}


# ------------------------------------------------------------------ checks ---

def _norm_name(s: str | None) -> str:
    s = re.sub(r"\(.*?\)", " ", (s or "").upper())
    s = re.sub(r"\b(LIMITED|LTD|THE|INDIA|INDUSTRIES|CORPORATION|CORP|COMPANY|CO)\b", " ", s)
    return " ".join(re.sub(r"[^A-Z0-9 ]", " ", s).split())


def names_match(a: str | None, b: str | None) -> bool:
    x, y = _norm_name(a), _norm_name(b)
    return bool(x and y and (x == y or x.split()[0] == y.split()[0]))


def company_matches(d: dict, symbol: str, company: str | None) -> tuple[bool, str]:
    tk = (d.get("ticker") or "").upper()
    if not tk and d.get("company_line"):
        m = re.search(r"\(([^)]+)\)", d["company_line"])
        tk = m.group(1).upper() if m else ""
    base = tk.split("-")[0].strip()
    if base and base == symbol.upper():
        return True, f"report ticker {tk} matches {symbol}"
    if names_match(d.get("company_line"), company):
        return True, f"report company '{d.get('company_line')}' matches '{company}' (ticker {tk or 'unread'})"
    return False, f"report is for '{d.get('company_line') or tk or 'an unreadable company'}', not {symbol}"


def _close(a: float | None, b: float | None, tol: float) -> Optional[bool]:
    if a is None or b is None:
        return None
    return abs(a - b) <= tol


def arithmetic_checks(d: dict) -> list[dict]:
    """The report's own printed numbers must recompute — a test of the transcription."""
    out: list[dict] = []

    def add(name, ok, detail):
        if ok is not None:
            out.append({"check": name, "ok": bool(ok), "detail": detail})

    dist = [d.get(k) for k in ("ibes_strong_buy", "ibes_buy", "ibes_hold", "ibes_sell", "ibes_strong_sell")]
    if d.get("ibes_analysts") is not None and all(x is not None for x in dist):
        add("analyst_count", sum(dist) == d["ibes_analysts"],
            f"recommendation bars sum to {sum(dist)}; report says {d['ibes_analysts']} analysts")
    if d.get("target_mean") and d.get("last_close") and d.get("target_vs_current_pct") is not None:
        up = (d["target_mean"] / d["last_close"] - 1) * 100
        add("target_upside", _close(up, d["target_vs_current_pct"], 0.3),
            f"mean target {d['target_mean']:,.2f} ÷ last close {d['last_close']:,.2f} − 1 = {up:.2f}%; printed {d['target_vs_current_pct']}%")
    if d.get("target_low") is not None and d.get("target_high") is not None and d.get("target_mean") is not None:
        add("target_range", d["target_low"] <= d["target_mean"] <= d["target_high"],
            f"low {d['target_low']:,.2f} ≤ mean {d['target_mean']:,.2f} ≤ high {d['target_high']:,.2f}")
    for s in d.get("surprise_detail") or []:
        if s.get("actual_eps") is not None and s.get("mean_eps") and s.get("surprise_pct") is not None:
            v = (s["actual_eps"] / s["mean_eps"] - 1) * 100
            add(f"surprise_{s.get('period_end') or '?'}", _close(v, s["surprise_pct"], 0.15),
                f"period {s.get('period_end')}: actual {s['actual_eps']} vs mean {s['mean_eps']} = {v:.2f}%; printed {s['surprise_pct']}%")
    for t in d.get("estimate_trend") or []:
        if t.get("current") is not None and t.get("days_90_ago") and t.get("change_90d_pct") is not None:
            v = (t["current"] / t["days_90_ago"] - 1) * 100
            add(f"estimate_trend_{t['period']}", _close(v, t["change_90d_pct"], 0.3),
                f"{t['period']}: {t['current']} vs {t['days_90_ago']} 90 days ago = {v:.2f}%; printed {t['change_90d_pct']}%")
    n4 = [d.get(k) for k in ("surprises_positive_last4", "surprises_negative_last4", "surprises_inline_last4")]
    if all(x is not None for x in n4):
        add("surprise_counts", sum(n4) <= 4, f"last-4-quarter surprise counts sum to {sum(n4)} (at most 4)")
    for k in ("score_average", "score_earnings", "score_fundamental", "score_relative_valuation", "score_risk", "score_price_momentum"):
        if d.get(k) is not None:
            add(k, 1 <= d[k] <= 10, f"{k.replace('score_', '').replace('_', ' ')} score {d[k]} on the 1–10 scale")
    return out


def assess(rec: dict, symbol: str, company: str | None, decision_at: str,
           our_close: float | None = None, our_pe: float | None = None) -> dict:
    """Code decides whether a transcribed report may be used, and records why."""
    d, cfg = rec["data"], config("sources").get("sr_plus", {})
    checks = arithmetic_checks(d)
    failed = [c for c in checks if not c["ok"]]
    warnings: list[str] = []
    ok_co, co_detail = company_matches(d, symbol, company)
    age = None
    try:
        age = (date.fromisoformat(decision_at[:10]) - date.fromisoformat(d["report_date"])).days
    except (TypeError, ValueError, KeyError):
        pass
    if not ok_co:
        status = "mismatch"
    elif age is None:
        status, warnings = "unreliable", warnings + ["report date could not be read"]
    elif age < 0:
        status, warnings = "unreliable", warnings + [f"report is dated {d['report_date']}, after the decision date"]
    elif len(failed) >= 2:
        status = "unreliable"
    elif age > cfg.get("max_age_days", 30):
        status = "expired"
    elif age > cfg.get("warn_after_days", 14):
        status = "stale"
    else:
        status = "available"
    if failed:
        warnings.append("Report numbers that did not recompute: " + "; ".join(c["detail"] for c in failed))
    if our_close and d.get("last_close"):
        diff = (d["last_close"] / our_close - 1) * 100
        if abs(diff) > cfg.get("close_tolerance_pct", 3.0):
            warnings.append(f"Report's last close Rs {d['last_close']:,.2f} ({d.get('last_close_date') or 'date unread'}) is "
                            f"{diff:+.1f}% from our last close Rs {our_close:,.2f} — its valuation and upside figures are at that price")
    if our_pe and d.get("trailing_pe"):
        diff = (d["trailing_pe"] / our_pe - 1) * 100
        if abs(diff) > cfg.get("pe_tolerance_pct", 15.0):
            warnings.append(f"Report's trailing P/E {d['trailing_pe']} differs {diff:+.0f}% from ours ({our_pe:.1f}x from filings) — "
                            "earnings bases differ; filings win for our numbers")
    reason = {"mismatch": co_detail, "expired": f"report is {age} days old (limit {cfg.get('max_age_days', 30)})",
              "unreliable": "; ".join(warnings) or f"{len(failed)} report numbers did not recompute",
              "stale": f"report is {age} days old — used, but flagged as stale", "available": f"report is {age} day(s) old"}[status]
    return {"status": status, "used": status in USED, "reason": reason, "company_check": co_detail, "age_days": age,
            "checks": checks, "failed_checks": len(failed), "warnings": warnings}


HISTORY_FIELDS = ("score_average", "score_earnings", "ibes_mean", "ibes_analysts", "target_mean", "last_close",
                  "revisions_up_4w", "revisions_down_4w", "broker_upgrades_120d", "broker_downgrades_120d", "forward_pe")


def select(recs: list[dict], symbol: str, company: str | None, decision_at: str,
           our_close: float | None = None, our_pe: float | None = None, errors: list[str] | None = None) -> dict:
    """All SR Plus reports for the stock → the current one (newest usable) plus a dated history.
    recs: [{"file", "sha256", "extracted_at", "model", "data"}]."""
    errors = errors or []
    if not recs:
        return {"status": "missing" if not errors else "unread", "used": False, "history": [], "warnings": errors,
                "reason": "; ".join(errors) or f"no SR Plus report among the PDFs in data/sr_plus/{symbol.upper()}/"}
    assessed = [r | {"assessment": assess(r, symbol, company, decision_at, our_close, our_pe)} for r in recs]
    usable = [r for r in assessed if r["assessment"]["used"]]
    pick = max(usable or assessed, key=lambda r: r["data"].get("report_date") or "")
    a = pick["assessment"]
    # History: every same-company, not-future, arithmetically sound report — old ones included, that is the point.
    hist = sorted((r for r in assessed if r["assessment"]["status"] in ("available", "stale", "expired")),
                  key=lambda r: r["data"].get("report_date") or "")
    history = [{"report_date": r["data"].get("report_date"), "file": r["file"], "status": r["assessment"]["status"],
                **{k: r["data"].get(k) for k in HISTORY_FIELDS}} for r in hist]
    return {"status": a["status"], "used": a["used"], "reason": a["reason"], "file": pick["file"], "sha256": pick["sha256"],
            "extracted_at": pick["extracted_at"], "model": pick.get("model"), "report_date": pick["data"].get("report_date"),
            "age_days": a["age_days"], "checks": a["checks"], "warnings": a["warnings"] + errors,
            "company_check": a["company_check"], "data": pick["data"], "history": history,
            "others": [{"file": r["file"], "report_date": r["data"].get("report_date"), "status": r["assessment"]["status"],
                        "reason": r["assessment"]["reason"]} for r in assessed if r is not pick]}


# ---------------------------------------------------------------- evidence ---

def _f(x, nd=2) -> str:
    return "n/a" if x is None else f"{x:,.{nd}f}"


def _pct(x) -> str:
    return "n/a" if x is None else f"{x:+.1f}%"


def passages(info: dict) -> list[str]:
    d = info["data"]
    rd = d.get("report_date") or "unknown date"
    src = f"(LSEG Stock Reports Plus dated {rd})"
    p: list[str] = []
    p.append(f"Stock Reports Plus for {d.get('company_line') or d.get('ticker')}, report date {rd}, last close in the report "
             f"Rs {_f(d.get('last_close'))} on {d.get('last_close_date') or 'n/a'}; trailing P/E {_f(d.get('trailing_pe'), 1)}, "
             f"forward P/E {_f(d.get('forward_pe'), 1)}, dividend yield {_f(d.get('dividend_yield_pct'), 1)}%, ROE {_f(d.get('roe_pct'), 1)}%, "
             f"long-term growth forecast {_f(d.get('ltg_forecast_pct'), 1)}%, institutional ownership {_f(d.get('institutional_ownership_pct'), 1)}%.")
    sc = [("Earnings", "score_earnings"), ("Fundamental", "score_fundamental"), ("Relative valuation", "score_relative_valuation"),
          ("Risk", "score_risk"), ("Price momentum", "score_price_momentum")]
    p.append(f"LSEG average score {d.get('score_average') or 'n/a'}/10; components: "
             + ", ".join(f"{n} {d.get(k) if d.get(k) is not None else 'n/a'}" for n, k in sc)
             + f" (1–10 scale: 8–10 positive, 4–7 neutral, 1–3 negative) {src}.")
    if d.get("ibes_mean"):
        p.append(f"I/B/E/S mean analyst recommendation: {d['ibes_mean'].replace('_', ' ')} from {d.get('ibes_analysts') or 'n/a'} analysts "
                 f"(strong buy {d.get('ibes_strong_buy')}, buy {d.get('ibes_buy')}, hold {d.get('ibes_hold')}, sell {d.get('ibes_sell')}, "
                 f"strong sell {d.get('ibes_strong_sell')}) {src}.")
    if d.get("target_mean"):
        p.append(f"12-month analyst price target: mean Rs {_f(d['target_mean'])}, high Rs {_f(d.get('target_high'))}, low Rs {_f(d.get('target_low'))}, "
                 f"from {d.get('target_analysts') or 'n/a'} analysts; printed as {_pct(d.get('target_vs_current_pct'))} versus the report's last close "
                 f"{src}. This is the analysts' opinion, not our valuation.")
    p.append(f"Earnings surprises over the last 4 quarters: {d.get('surprises_positive_last4')} positive (>2%), {d.get('surprises_negative_last4')} "
             f"negative (<-2%), {d.get('surprises_inline_last4')} in line; average surprise {_f(d.get('surprise_avg_last4_pct'))}% {src}.")
    p.append(f"Consensus estimate revisions over the last 4 weeks: {d.get('revisions_up_4w')} up (average {_f(d.get('revisions_avg_up_pct'))}%), "
             f"{d.get('revisions_down_4w')} down (average {_f(d.get('revisions_avg_down_pct'))}%). Broker recommendation changes over the last "
             f"120 days: {d.get('broker_upgrades_120d')} upgrades, {d.get('broker_downgrades_120d')} downgrades {src}.")
    if d.get("surprise_detail"):
        p.append("Reported EPS versus consensus: " + "; ".join(
            f"period ended {s.get('period_end')} (announced {s.get('announce_date')}): actual {_f(s.get('actual_eps'))} vs mean {_f(s.get('mean_eps'))}, "
            f"{_pct(s.get('surprise_pct'))} ({s.get('type') or 'n/a'})" for s in d["surprise_detail"]) + f" {src}.")
    if d.get("estimate_trend"):
        p.append("Consensus mean estimate trend: " + "; ".join(
            f"{t['period']}: now {_f(t.get('current'))}, 30 days ago {_f(t.get('days_30_ago'))}, 90 days ago {_f(t.get('days_90_ago'))} "
            f"({_pct(t.get('change_90d_pct'))} over 90 days)" for t in d["estimate_trend"])
                 + f"; next expected report {d.get('next_report_date') or 'n/a'} {src}.")
    est = [("quarterly EPS", d.get("eps_quarterly")), ("annual EPS", d.get("eps_annual")), ("annual revenue", d.get("revenue_annual"))]
    for label, rows in est:
        if rows:
            p.append(f"Consensus {label} estimates: " + "; ".join(
                f"{r['period']}: mean {_f(r.get('mean'))} (high {_f(r.get('high'))}, low {_f(r.get('low'))}"
                + (f", growth {_f(r.get('growth_pct'), 1)}%" if r.get("growth_pct") is not None else "")
                + f", {r.get('analysts') or 'n/a'} analysts)" for r in rows) + f" {src}.")
    groups: dict[str, list[str]] = {}
    for m in d.get("metrics") or []:
        g, _, n = m["name"].partition(":")
        if not n:
            g, n = "Other", m["name"]
        unit = m.get("unit") or ""
        suffix = unit if unit in ("%", "x") else (f" {unit}" if unit else "")
        groups.setdefault(g.strip(), []).append(f"{n.strip()} {_f(m.get('value'))}{suffix}"
                                                + (f" ({m['period']})" if m.get("period") else ""))
    for g, items in groups.items():
        p.append(f"LSEG {g.lower()} indicators: " + "; ".join(items) + f" {src}.")
    if d.get("peers"):
        p.append("Peer comparison: " + "; ".join(
            f"{x['ticker']}: score {x.get('average_score')}, 1-year {_pct(x.get('ret_1y_pct'))}, trailing P/E {_f(x.get('trailing_pe'), 1)}, "
            f"forward P/E {_f(x.get('forward_pe'), 1)}, net margin {_f(x.get('net_margin_pct'), 1)}%, consensus {x.get('ibes_mean') or 'n/a'}"
            for x in d["peers"]) + f" {src}.")
    for h in (d.get("highlights") or [])[:8]:
        p.append(f"LSEG highlight ({h['section']}): {h['text']}")
    older = [h for h in info.get("history", []) if h["report_date"] != d.get("report_date")]
    if older:
        p.append("Earlier SR Plus reports you saved (how consensus moved): " + "; ".join(
            f"{h['report_date']}: average score {h.get('score_average')}, earnings score {h.get('score_earnings')}, consensus "
            f"{(h.get('ibes_mean') or 'n/a').replace('_', ' ')} ({h.get('ibes_analysts')} analysts), mean target Rs {_f(h.get('target_mean'))}, "
            f"revisions {h.get('revisions_up_4w')} up / {h.get('revisions_down_4w')} down, broker changes "
            f"{h.get('broker_upgrades_120d')} up / {h.get('broker_downgrades_120d')} down" for h in older) + ".")
    ok = sum(c["ok"] for c in info.get("checks", []))
    p.append(f"Code checks on this transcription: {ok} of {len(info.get('checks', []))} report figures recompute; "
             f"{info.get('company_check')}; status {info['status']} ({info['reason']})."
             + (" Warnings: " + " | ".join(info["warnings"]) if info.get("warnings") else ""))
    return p


def to_evidence(fac, info: dict, symbol: str):
    d = info["data"]
    return fac.add(kind="broker_research", title=f"{symbol}: LSEG Stock Reports Plus, report dated {d.get('report_date')}",
                   publisher="LSEG Stock Reports Plus (via ETPrime)", tier="T2_licensed_data",
                   passages=passages(info), published_at=(d.get("report_date") or "") + "T00:00:00+05:30" if d.get("report_date") else None,
                   access_status="user_supplied_pdf", usage_status="personal_research_user_attested",
                   retrieved_via="sr_plus", url=None,
                   notes=(f"Your PDF data/sr_plus/{symbol}/{info['file']} (sha256 {info['sha256'][:12]}…), transcribed by "
                          f"{info.get('model')} on {info.get('extracted_at')} ({EXTRACTOR}); passages written by code from the "
                          "transcription. Third-party data and analyst opinion; LSEG copyright, personal use only."))
