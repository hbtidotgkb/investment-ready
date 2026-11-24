"""Deterministic rules engine (spec §9). Runs twice: preflight (before judgment) and final
enforcement (after the Head's proposal). Thresholds come from config/rules.yaml; no prompt
can override them. Unknown inputs never pass silently: a rule whose input is missing is
`unknown`, which blocks an actionable purchase but still allows research."""
from __future__ import annotations

import math
from datetime import date
from typing import Any, Optional

from ..contracts import Bundle
from ..settings import config

PASS, BLOCKED, UNKNOWN, WARN, NA = "pass", "blocked", "unknown", "warn", "not_applicable"

SUBSTANTIVE = {"surveillance", "pledge", "governance", "audit", "liquidity", "bearish_pattern"}
ENTRY_RULES = {"reward_risk", "benchmark"}
# Gates about YOUR setup, not about the stock. When only these block a BUY, the verdict stays BUY
# and is marked "on hold until setup is complete" — relabelling it "insufficient data" would hide
# what the panel concluded about the stock.
SETUP_STATUSES = {"tax_policy_unverified"}


def is_setup_gate(r: dict) -> bool:
    return r["rule"] == "policies_confirmed" or r.get("setup") is True


def _r(rule: str, result: str, detail: str, refs: list[str] | None = None, value: Any = None) -> dict:
    return {"rule": rule, "result": result, "detail": detail, "input_refs": refs or [], "value": value}


def _calc(bundle: Bundle, name: str):
    for c in bundle.calculations:
        if c.name == name:
            return c
    return None


def _fact(bundle: Bundle, metric: str):
    for f in bundle.facts:
        if f.metric == metric and f.status == "available":
            return f
    return None


def trading_days_between(d0: date, d1: date) -> int:
    n, d = 0, d0
    while d < d1:
        d = date.fromordinal(d.toordinal() + 1)
        if d.weekday() < 5:
            n += 1
    return n


# ------------------------------------------------------------------ preflight ---

def preflight(bundle: Bundle) -> list[dict]:
    rc = config("rules")["vetoes"]
    user = config("user")
    out: list[dict] = []

    if bundle.mode == "demo":
        out.append(_r("demo_mode", BLOCKED, "DEMO run on fictional synthetic data — never actionable."))
    out.append(_r("policies_confirmed", PASS if user.get("policies_confirmed") else BLOCKED,
                  "Horizon/risk/cash policies confirmed in config/user.yaml" if user.get("policies_confirmed")
                  else "Set policies_confirmed: true in config/user.yaml after reviewing horizons, risk base and limits."))

    s = bundle.surveillance or {}
    st = s.get("status", "unknown")
    if st == "listed":
        out.append(_r("surveillance", BLOCKED, f"On NSE surveillance list: {s.get('stage') or s.get('detail', '')}", s.get("refs")))
    elif st == "not_listed":
        out.append(_r("surveillance", PASS, f"Not on ASM/GSM per {s.get('source', 'source')}", s.get("refs")))
    else:
        out.append(_r("surveillance", UNKNOWN, "Surveillance status could not be established — required before a buy."))

    adv = _calc(bundle, "adv20_crore")
    if adv is None or adv.result is None:
        out.append(_r("liquidity", UNKNOWN, "20-day average traded value unavailable."))
    else:
        th = rc["liquidity"]["min_adv20_crore"]
        out.append(_r("liquidity", PASS if adv.result >= th else BLOCKED,
                      f"20-day avg traded value Rs {adv.result:,.2f} cr vs minimum Rs {th} cr", [adv.id], adv.result))

    last = bundle.price.get("date")
    if not last:
        out.append(_r("stale_data", UNKNOWN, "No price data."))
    else:
        age = trading_days_between(date.fromisoformat(last), date.fromisoformat(bundle.decision_at[:10]))
        mx = rc["stale_data"]["max_price_age_trading_days"]
        out.append(_r("stale_data", PASS if age <= mx else BLOCKED,
                      f"Last price {last} is {age} trading day(s) before the decision date (max {mx}).", value=age))

    ca_flags = [f for f in bundle.quality_flags if "corporate action" in f.lower()]
    out.append(_r("data_quality", BLOCKED if ca_flags else PASS,
                  "; ".join(ca_flags) if ca_flags else "No unresolved corporate-action or material data conflicts flagged."))

    out.append(pledge_rule(bundle, rc["pledge"]))
    out.append(gate_rule(evidence_gate(bundle)))
    return out


# ------------------------------------------------------------ evidence gate ---
# Code, not the model, decides whether there is enough evidence to reach a verdict.
# Critical items depend on the horizon; important items only lower confidence.

RESULT_METRICS = ("eps", "revenue", "total_income", "net_profit_attributable")
VALUATION_CALCS = ("eps_ttm", "bvps")
VALUATION_FACTS = ("pe_ttm", "pb", "eps")


def _age_days(period_end: str, decision_at: str) -> int:
    return (date.fromisoformat(decision_at[:10]) - date.fromisoformat(period_end[:10])).days


def evidence_gate(bundle: Bundle) -> dict:
    rc = config("rules")["vetoes"]
    short = bundle.horizon == "short"
    items: list[dict] = []

    def item(key, label, ok, detail, how):
        items.append({"key": key, "label": label, "ok": bool(ok), "detail": detail, "how_to_get": how})

    st = (bundle.surveillance or {}).get("status", "unknown")
    item("surveillance", "Surveillance status known (NSE ASM/GSM lists)", st in ("listed", "not_listed"),
         f"status: {st}", "Fetched automatically from NSE each run; if NSE refuses, record it with a source in data/manual/<SYMBOL>.yaml")
    need = 60 if short else 200
    bars = len(bundle.candles)
    item("price_history", f"At least {need} days of daily prices", bars >= need, f"{bars} daily bars in the bundle",
         "Connect Kite MCP (Portfolio page), or run `ca ingest-bhav --days 260`")
    if short:
        tech = [n for n in ("rsi14", "atr14") if (_calc(bundle, n) is None or _calc(bundle, n).result is None)]
        has_feat = any(e.kind == "computed_feature" and e.title.startswith("Price & technicals") for e in bundle.evidence)
        ok = has_feat and not tech
        item("technicals", "Technical analysis computed (candlesticks, chart patterns, support/resistance, RSI/MACD)", ok,
             "computed" if ok else "missing: " + (", ".join(tech) or "technicals evidence"),
             "Needs the daily price series; computed automatically once prices load")
    else:
        fresh_max = rc["stale_data"].get("quarterly_freshness_days", 100)
        res = [f for f in bundle.facts if f.metric in RESULT_METRICS and f.status == "available" and f.period_end]
        latest = max((f.period_end for f in res), default=None)
        age = _age_days(latest, bundle.decision_at) if latest else None
        item("results_fresh", f"Latest quarterly results no older than {fresh_max} days",
             age is not None and age <= fresh_max,
             f"latest results for the period ended {latest} ({age} days old)" if latest else "no results figures in the bundle",
             "Read automatically from NSE XBRL filings; if missing, the company may not have filed — add figures in data/manual/<SYMBOL>.yaml")
        calc_ok = [n for n in VALUATION_CALCS if _calc(bundle, n) is not None and _calc(bundle, n).result is not None]
        fact_ok = [m for m in VALUATION_FACTS if _fact(bundle, m) is not None]
        item("valuation_inputs", "EPS or book value computable", calc_ok or fact_ok,
             ("computed: " + ", ".join(calc_ok + fact_ok)) if (calc_ok or fact_ok) else "neither trailing EPS nor book value could be computed",
             "Needs four consecutive quarterly filings (EPS) or a balance sheet (book value) on NSE")
        lb = rc["audit"]["lookback_months"] + 3
        aud = [f for f in bundle.facts if f.metric == "audit_opinion_unmodified" and f.status == "available"
               and f.value is not None and f.period_end and _months_between(f.period_end, bundle.decision_at) <= lb]
        item("audit_opinion", f"Audit opinion from the last {lb} months", aud,
             (f"{'unmodified' if aud[0].value == 1 else 'MODIFIED'} opinion for the period ended {aud[0].period_end}") if aud
             else "no audited opinion within the window",
             "Read from audited results XBRL (annual results); or add it with a source in data/manual/<SYMBOL>.yaml")
    pl = [f for f in bundle.facts if f.metric == "promoter_pledge_pct" and f.status in ("available", "not_applicable")]
    item("shareholding", "Shareholding and promoter pledge known", pl,
         f"as of {max(f.period_end or '' for f in pl)}" if pl else "no shareholding/pledge figures",
         "Read from the shareholding-pattern XBRL on NSE")
    missing = [i for i in items if not i["ok"]]
    return {"horizon": bundle.horizon, "items": items, "complete": not missing, "missing": [i["key"] for i in missing]}


def gate_rule(gate: dict) -> dict:
    if gate["complete"]:
        return _r("evidence_gate", PASS, f"All {len(gate['items'])} critical evidence items for the {gate['horizon']} horizon are present.")
    miss = [i for i in gate["items"] if not i["ok"]]
    return _r("evidence_gate", UNKNOWN, "Critical evidence missing: " + "; ".join(f"{i['label']} ({i['detail']})" for i in miss))


def confidence(bundle: Bundle, gate: dict) -> dict:
    """High, lowered one step per important gap, never below Low. Set by code, not the model.
    Insider trades are not a gap: NSE's feed is broken, and promoter pledge/holding from filings covers most of it."""
    cov = bundle.coverage or {}
    gaps: list[str] = []
    sr = cov.get("sr_plus", "missing")
    if sr == "stale":
        gaps.append("SR Plus report is stale — save a fresh one")
    elif sr != "available":
        gaps.append("No usable SR Plus report (analyst consensus, estimate revisions)")
    if cov.get("news") != "available":
        gaps.append("No news coverage in the bundle")
    elif cov.get("news_quality") == "press_only":
        gaps.append("News is press-only — no filed or confirmed events")
    if bundle.horizon != "short":
        if _fact(bundle, "contingent_liabilities") is None:
            gaps.append("Contingent liabilities not found")
        if cov.get("related_party") != "available":
            gaps.append("Related-party transaction detail missing")
        if cov.get("is_bank") and _fact(bundle, "slippages_cr") is None:
            gaps.append("Bank slippages / restructured book not found")
    levels = ["high", "medium", "low"]
    return {"level": levels[min(len(gaps), 2)], "gaps": gaps}


RED_FLAG_CHANGE = {
    "surveillance": "Removal from the NSE surveillance (ASM/GSM) list",
    "pledge": "Promoter pledge back below the limit and no longer rising",
    "governance": "Governance score back above the minimum with the severe findings resolved",
    "audit": "An unmodified audit opinion in the next audited results",
    "liquidity": "20-day average traded value back above the minimum",
    "bearish_pattern": "The bearish pattern failing (price recovering above its neckline)",
}


def verdict_problems(head: dict, close: Optional[float], decision_at: str) -> list[str]:
    """Each verdict must say what happens next. Used to repair the Head's output and checked again in enforce."""
    d, errs = head.get("decision"), []
    if d == "wait":
        t = head.get("trigger")
        if not t:
            errs.append("A WAIT must give a trigger: kind 'price' (price_level + direction above/below), 'date' (YYYY-MM-DD) "
                        "or 'event' (the filing/announcement to wait for), with a plain-words description")
        elif t.get("kind") == "price":
            lv, dr = t.get("price_level"), t.get("direction")
            if not lv or lv <= 0 or dr not in ("above", "below"):
                errs.append("A price trigger needs price_level > 0 and direction 'above' or 'below'")
            elif close and ((dr == "below" and lv >= close) or (dr == "above" and lv <= close)):
                errs.append(f"The price trigger ({dr} {lv}) is already met at the last close {close} — decide buy or avoid instead")
        elif t.get("kind") == "date":
            try:
                days = (date.fromisoformat((t.get("date") or "")[:10]) - date.fromisoformat(decision_at[:10])).days
                if not 0 < days <= 400:
                    errs.append("A date trigger must be after the decision date and within about a year")
            except ValueError:
                errs.append("A date trigger needs date as YYYY-MM-DD")
        elif len((t.get("description") or "").strip()) < 15:
            errs.append("An event trigger must describe the specific filing or announcement to wait for")
    elif d == "avoid":
        if not (head.get("avoid_reason") or "").strip():
            errs.append("An AVOID must state avoid_reason (the specific problem)")
        if not head.get("would_change_view"):
            errs.append("An AVOID must list would_change_view (what would make you reconsider)")
    elif d == "insufficient_data":
        if len((head.get("insufficiency_reason") or "").strip()) < 20:
            errs.append("INSUFFICIENT_DATA must state insufficiency_reason naming the specific evidence that is missing or conflicting")
    return errs


def code_trigger(plan: dict, blocking: list[dict], rr_min: float) -> dict:
    """When the RULES turn a buy into a wait, code states the trigger itself."""
    t_low, stop = plan.get("target_low"), plan.get("stop")
    if any(r["rule"] == "reward_risk" for r in blocking) and t_low and stop:
        lv = rr_level(t_low, stop, rr_min)   # rounded DOWN so reward/risk is met at the level
        return {"kind": "price", "price_level": lv, "direction": "below", "date": None, "set_by": "code",
                "description": f"Price at or below Rs {lv:,.2f}, where reward/risk to the target reaches {rr_min}"}
    first = next((r for r in blocking if not is_setup_gate(r)), blocking[0] if blocking else None)
    return {"kind": "event", "price_level": None, "direction": None, "date": None, "set_by": "code",
            "description": (f"Entry condition met: {first['rule'].replace('_', ' ')} — {first['detail']}" if first
                            else "Entry condition met")}


def pledge_rule(bundle: Bundle, rc: dict) -> dict:
    """Level and trend from the filed shareholding series (latest first). Manual facts win ties."""
    pf = [f for f in bundle.facts if f.metric == "promoter_pledge_pct" and f.status in ("available", "not_applicable")]
    pf.sort(key=lambda f: f.period_end or "", reverse=True)
    if not pf:
        return _r("pledge", UNKNOWN, "Promoter pledge not in bundle; Forensic review must establish it.")
    latest = pf[0]
    if latest.status == "not_applicable":
        return _r("pledge", PASS, f"No promoter group reported as of {latest.period_end} — promoter pledge not applicable.",
                  [latest.evidence_id])
    series = [f for f in pf if f.status == "available" and f.value is not None]
    th, need = rc["max_pct_of_promoter"], rc.get("rising_quarters", 2)
    rising = 0
    while rising + 1 < len(series) and series[rising].value > series[rising + 1].value:
        rising += 1
    trend = ", ".join(f"{f.period_end}: {f.value:.2f}%" for f in series[:4])
    refs = [f.evidence_id for f in series[:4] if f.evidence_id]
    if latest.value > th:
        return _r("pledge", BLOCKED, f"Promoter pledge {latest.value:.2f}% of promoter holding vs max {th}% ({trend})", refs, latest.value)
    if latest.value > 0 and rising >= need:
        return _r("pledge", BLOCKED, f"Promoter pledge rising for {rising} consecutive quarters ({trend}); limit is {need}", refs, latest.value)
    return _r("pledge", PASS, f"Promoter pledge {latest.value:.2f}% of promoter holding vs max {th}%"
              + (f" ({trend})" if len(series) > 1 else ""), refs, latest.value)


def _months_between(d0: str, d1: str) -> float:
    return (date.fromisoformat(d1[:10]) - date.fromisoformat(d0[:10])).days / 30.44


# ---------------------------------------------------------- governance rubric ---

PENALTY = {("fail", "critical"): 45, ("fail", "major"): 25, ("fail", "minor"): 10,
           ("watch", "critical"): 20, ("watch", "major"): 10, ("watch", "minor"): 3}
FORENSIC_CHECKS = 10


def governance_score(checks: list[dict]) -> dict:
    """Rubric governance@1, computed in code from the Forensic CA's check classifications."""
    assessed = [c for c in checks if c["result"] in ("pass", "watch", "fail", "not_applicable")]
    coverage = min(len(assessed) / FORENSIC_CHECKS, 1.0)
    penalty = sum(PENALTY.get((c["result"], c["materiality"]), 0) for c in checks)
    severe = [c["name"] for c in checks if c["result"] == "fail" and c["materiality"] == "critical"]
    score = None if coverage < 0.6 else max(0, 100 - penalty)
    return {"rubric": "governance@1", "score": score, "coverage": round(coverage, 2), "penalty": penalty,
            "severe_findings": severe,
            "note": "Score withheld: evidence coverage below 60% of the checklist" if score is None else
                    "100 minus penalties for watch/fail findings weighted by materiality"}


# --------------------------------------------------------------- calculations ---

def scenario_returns(close: float, scenarios: list[dict], months: int) -> list[dict]:
    cost = config("rules")["execution"]["cost_per_side_pct"] * 2
    out = []
    for s in scenarios:
        p = s.get("price_assumption")
        if p is None or not close:
            out.append({"name": s["name"], "price": p, "gross_return_pct": None})
            continue
        g = (p / close - 1) * 100 - cost
        out.append({"name": s["name"], "price": p, "gross_return_pct": round(g, 2), "months": months})
    return out


def tax_view(gross_pct: Optional[float], months: int) -> dict:
    t = config("tax_rates")
    if gross_pct is None:
        return {"after_tax_pct": None, "status": "no_input"}
    if t.get("review_status") != "verified":
        return {"after_tax_pct": None, "status": "tax_policy_unverified",
                "detail": "config/tax_rates.yaml not verified — after-tax figure refused; pre-tax shown."}
    rate = (t["stcg"]["rate_pct"] if months <= t["stcg"]["holding_period_months_lte"] else t["ltcg"]["rate_pct"])
    rate *= 1 + t.get("cess_pct", 0) / 100
    after = gross_pct * (1 - rate / 100) if gross_pct > 0 else gross_pct
    return {"after_tax_pct": round(after, 2), "status": "approx", "rate_pct": round(rate, 2),
            "classification": "STCG" if months <= t["stcg"]["holding_period_months_lte"] else "LTCG",
            "detail": "Approx. Annual LTCG exemption not applied per pick (taxpayer-level)."}


def reward_risk(entry_low, entry_high, stop, target_low) -> Optional[float]:
    if None in (entry_low, entry_high, stop, target_low):
        return None
    mid = (entry_low + entry_high) / 2
    risk = mid - stop
    if risk <= 0:
        return None
    return round((target_low - mid) / risk, 2)


def reward_risk_by_target(plan: dict) -> dict:
    """Reward/risk against the low, middle and high target, so the effect of the target choice is visible."""
    tl, th = plan.get("target_low"), plan.get("target_high")
    out = {"low": reward_risk(plan.get("entry_low"), plan.get("entry_high"), plan.get("stop"), tl)}
    if tl is not None and th is not None and th > tl:
        out["mid"] = reward_risk(plan.get("entry_low"), plan.get("entry_high"), plan.get("stop"), round((tl + th) / 2, 2))
        out["mid_target"] = round((tl + th) / 2, 2)
        out["high"] = reward_risk(plan.get("entry_low"), plan.get("entry_high"), plan.get("stop"), th)
    return out


def rr_level(target: float, stop: float, rr_min: float) -> float:
    """Highest entry price at which reward/risk to `target` reaches `rr_min` (rounded down)."""
    return math.floor((target + rr_min * stop) / (1 + rr_min) * 100) / 100


def check_price_trigger(trigger: dict, plan: dict, rr_min: float, enforce_min: bool) -> tuple[dict, Optional[str]]:
    """Reward/risk if you buy AT the trigger level, to the low target. When the minimum applies (short-term)
    and a 'below' trigger misses it, code lowers the trigger to the level where it is met.
    Returns (trigger, adjustment note or None)."""
    lv, st, tl = trigger.get("price_level"), plan.get("stop"), plan.get("target_low")
    if trigger.get("kind") != "price" or not lv or st is None or tl is None or lv <= st:
        return trigger, None
    rr_at = round((tl - lv) / (lv - st), 2)
    trigger = dict(trigger, reward_risk_at_level=rr_at, reward_risk_min=rr_min, meets_minimum=rr_at >= rr_min)
    if not enforce_min or rr_at >= rr_min:
        return trigger, None
    if trigger.get("direction") == "below":
        new = rr_level(tl, st, rr_min)
        if st < new < lv:
            note = (f"The Head's trigger (below Rs {lv:,.2f}) gives reward/risk {rr_at} to the low target Rs {tl:,.2f}, "
                    f"under the minimum {rr_min}; code lowered it to Rs {new:,.2f}, where the minimum is met")
            return dict(trigger, price_level=new, reward_risk_at_level=round((tl - new) / (new - st), 2), meets_minimum=True,
                        set_by="code", head_level=lv,
                        description=f"Price at or below Rs {new:,.2f}, where reward/risk to the target reaches {rr_min} "
                                    f"(the Head proposed Rs {lv:,.2f}, where it is only {rr_at})"), note
    return trigger, (f"The Head's trigger ({trigger.get('direction')} Rs {lv:,.2f}) gives reward/risk {rr_at} to the low "
                     f"target, under the minimum {rr_min}: a buy at that level would fail the reward/risk rule unless "
                     f"the targets are higher")


def plan_valid(p: dict) -> list[str]:
    issues = []
    el, eh, st, tl, th = (p.get(k) for k in ("entry_low", "entry_high", "stop", "target_low", "target_high"))
    if None in (el, eh, st, tl):
        return ["Plan incomplete: entry, stop and target are required for a buy"]
    if any(x is not None and x <= 0 for x in (el, eh, st, tl, th)):
        issues.append("Plan contains a non-positive price level")
    if el > eh:
        issues.append("entry_low is above entry_high")
    if st >= el:
        issues.append("Stop is not below the entry zone")
    if tl <= eh:
        issues.append("Target is not above the entry zone")
    if th is not None and th < tl:
        issues.append("target_high below target_low")
    return issues


# ----------------------------------------------------------- final enforcement ---

def enforce(bundle: Bundle, pre: list[dict], head: dict, panel: dict) -> dict:
    """head: final HeadDecisionLLM dict. panel: {'forensic_checks', 'brains_ok', 'critic_ok',
    'open_blockers', 'citation_ok', 'patterns'}. Returns rule results + final action."""
    rc = config("rules")
    hz = config("horizons")["horizons"][bundle.horizon]
    months = hz["calendar_months"]
    results = [dict(r, phase="preflight") for r in pre if r["rule"] != "pledge"]

    gov = governance_score(panel.get("forensic_checks", []))
    if gov["score"] is None:
        results.append(_r("governance", UNKNOWN, f"Governance score withheld (coverage {gov['coverage']:.0%})."))
    else:
        mn = rc["vetoes"]["governance"]["min_score"]
        res = BLOCKED if gov["score"] < mn or gov["severe_findings"] else PASS
        results.append(_r("governance", res, f"Governance {gov['score']}/100 (rubric governance@1, coverage "
                          f"{gov['coverage']:.0%}) vs minimum {mn}" + (f"; severe: {', '.join(gov['severe_findings'])}"
                                                                       if gov["severe_findings"] else ""), value=gov["score"]))

    fc = {c["name"]: c for c in panel.get("forensic_checks", [])}
    aud = fc.get("auditor")
    lb = rc["vetoes"]["audit"]["lookback_months"]
    xa = next((f for f in bundle.facts if f.metric == "audit_opinion_unmodified" and f.status == "available"
               and f.value is not None and f.period_end
               and _months_between(f.period_end, bundle.decision_at) <= lb + 3), None)   # +3: filing lag after period end
    if aud is not None and aud["result"] == "fail":
        results.append(_r("audit", BLOCKED, f"Forensic auditor check: fail — {aud['note']}"))
    elif xa is not None and xa.value == 0:
        results.append(_r("audit", BLOCKED, f"Filed audit opinion is modified: {xa.reason}", [xa.evidence_id]))
    elif aud is not None and aud["result"] in ("pass", "watch"):
        results.append(_r("audit", PASS, f"Forensic auditor check: {aud['result']} — {aud['note']}"
                          + (f" (filed: {xa.reason})" if xa is not None else "")))
    elif xa is not None:
        results.append(_r("audit", PASS, f"Filed audit opinion: {xa.reason}", [xa.evidence_id]))
    else:
        results.append(_r("audit", UNKNOWN, "Auditor opinion not established — missing auditor evidence is not a clean audit."))

    pre_pledge = next((r for r in pre if r["rule"] == "pledge"), None)
    pl = fc.get("promoter_pledge_insider")
    if pre_pledge and pre_pledge["result"] != UNKNOWN:
        results.append(dict(pre_pledge, phase="preflight"))
    elif pl and pl["result"] in ("pass", "watch", "fail"):
        results.append(_r("pledge", BLOCKED if pl["result"] == "fail" else PASS, f"Forensic pledge/insider check: {pl['result']} — {pl['note']}"))
    else:
        results.append(_r("pledge", UNKNOWN, "Pledge level/trend not established."))

    bearish = [p for p in panel.get("patterns", []) if p["bias"] == "bearish" and p["status"] == "confirmed"]
    if bundle.horizon == "short" and bearish:
        results.append(_r("bearish_pattern", BLOCKED, f"Confirmed bearish pattern: {bearish[0]['name']}"))
    else:
        results.append(_r("bearish_pattern", PASS if not bearish else NA,
                          "No confirmed bearish pattern" if not bearish else "Bearish pattern noted; rule applies to short horizon only"))

    req_ok = panel.get("brains_ok") and panel.get("critic_ok") and not panel.get("open_blockers") and panel.get("citation_ok", True)
    why = []
    if not panel.get("brains_ok"):
        why.append("a required specialist report is missing or invalid")
    if not panel.get("critic_ok"):
        why.append("critique did not complete")
    if panel.get("open_blockers"):
        why.append(f"unresolved blocker(s): {', '.join(panel['open_blockers'])}")
    if not panel.get("citation_ok", True):
        why.append("citation integrity below threshold")
    results.append(_r("required_evidence", PASS if req_ok else BLOCKED,
                      "All required assessments complete; no open blockers" if req_ok else "; ".join(why)))

    plan = {k: head.get(k) for k in ("entry_low", "entry_high", "stop", "target_low", "target_high")}
    is_buy = head.get("decision") == "buy"
    close = bundle.price.get("close")
    rr = reward_risk(plan["entry_low"], plan["entry_high"], plan["stop"], plan["target_low"])
    calcs: dict = {"reward_risk": rr,       # the rule's number: against the LOW target (conservative)
                   "reward_risk_by_target": reward_risk_by_target(plan),
                   "reward_risk_min": rc["vetoes"]["reward_risk"]["min_short_term"]}
    if is_buy:
        issues = plan_valid(plan)
        if issues:
            results.append(_r("plan_validity", BLOCKED, "; ".join(issues)))
        mn = rc["vetoes"]["reward_risk"]["min_short_term"]
        if bundle.horizon == "short":
            results.append(_r("reward_risk", UNKNOWN if rr is None else (PASS if rr >= mn else BLOCKED),
                              "Reward/risk not computable" if rr is None else f"Reward/risk {rr} vs minimum {mn} (short-term)", value=rr))
        else:
            results.append(_r("reward_risk", NA if rr is None else PASS,
                              "Applies to short-term purchases" + (f"; computed {rr}" if rr is not None else ""), value=rr))
        base_target = plan["target_low"]
        gross = None
        if base_target and close:
            gross = (base_target / ((plan["entry_low"] + plan["entry_high"]) / 2) - 1) * 100 - rc["execution"]["cost_per_side_pct"] * 2
        bm_gross = rc["benchmark"]["assumed_annual_return_pct"] * months / 12
        tv, tb = tax_view(gross, months), tax_view(bm_gross, months)
        calcs.update({"base_gross_return_pct": None if gross is None else round(gross, 2),
                      "benchmark_gross_return_pct": round(bm_gross, 2), "tax_stock": tv, "tax_benchmark": tb})
        if tv["after_tax_pct"] is None or tb["after_tax_pct"] is None:
            bm = _r("benchmark", UNKNOWN,
                    f"After-tax comparison refused ({tv.get('status')}). Pre-tax: stock target-low "
                    f"{'n/a' if gross is None else f'{gross:.1f}%'} vs benchmark assumption {bm_gross:.1f}% over {months} months.")
            if tv.get("status") in SETUP_STATUSES and gross is not None and gross > bm_gross:
                bm["setup"] = True          # pre-tax already beats the benchmark; only the tax file is missing
            results.append(bm)
        else:
            ok = tv["after_tax_pct"] > tb["after_tax_pct"]
            results.append(_r("benchmark", PASS if ok else BLOCKED,
                              f"After-tax (approx) {tv['after_tax_pct']}% vs benchmark {tb['after_tax_pct']}% over {months} months "
                              f"(assumption {rc['benchmark']['assumed_annual_return_pct']}%/yr — not a forecast)"))

    blocking = [r for r in results if r["result"] in (BLOCKED, UNKNOWN)]
    final_decision = head.get("decision")
    adjustments = []
    actionable = False
    on_hold = None
    gate = evidence_gate(bundle)
    conf = confidence(bundle, gate)
    new_money = bundle.action in ("new_purchase", "additional_purchase")
    red = [r for r in results if r["rule"] in SUBSTANTIVE and r["result"] == BLOCKED]
    trigger = head.get("trigger")
    avoid_reason, would_change = head.get("avoid_reason"), list(head.get("would_change_view") or [])
    incomplete = None
    if new_money and red and final_decision != "avoid":
        # Step 1: a confirmed red flag about the stock means AVOID, whatever the Head proposed.
        adjustments.append(f"Head proposed {str(final_decision).upper()}; a confirmed red flag about the stock makes it AVOID: "
                           + "; ".join(f"{r['rule']} — {r['detail']}" for r in red))
        final_decision, is_buy = "avoid", False
        avoid_reason = "; ".join(f"{r['rule']}: {r['detail']}" for r in red)
        would_change = [RED_FLAG_CHANGE.get(r["rule"], f"{r['rule']} resolved") for r in red]
    elif new_money and not gate["complete"] and final_decision in ("buy", "wait"):
        # Step 2: critical evidence missing means INSUFFICIENT DATA for a buy or wait (an avoid may stand).
        adjustments.append(f"Head proposed {final_decision.upper()}; critical evidence is missing, so the result is "
                           "INSUFFICIENT DATA: " + ", ".join(gate["missing"]))
        final_decision, is_buy = "insufficient_data", False
    elif new_money and gate["complete"] and final_decision == "insufficient_data":
        # Step 3: evidence complete — the Head must decide. A refusal after one send-back leaves the run incomplete.
        incomplete = {"reason": head.get("insufficiency_reason") or head.get("summary") or "",
                      "note": "All critical evidence was present, but the Head declined to decide even after being asked again. "
                              "Review its reason; this run has no valid verdict."}
        adjustments.append("Evidence checklist complete but the Head did not decide — run marked INCOMPLETE")
    elif final_decision in ("wait", "avoid"):
        probs = verdict_problems(head, bundle.price.get("close"), bundle.decision_at)
        if probs:
            incomplete = {"reason": "; ".join(probs), "note": "The Head's verdict is missing what must come with it."}
            adjustments.append("Verdict incomplete: " + "; ".join(probs))
    if is_buy:
        if not blocking:
            actionable = True
        elif all(is_setup_gate(r) for r in blocking):
            on_hold = "setup"
            adjustments.append("Head proposed BUY and no rule about the stock objects; it is ON HOLD until your setup is "
                               "complete: " + "; ".join(f"{r['rule']} ({r['result']})" for r in blocking))
        else:
            names = {r["rule"] for r in blocking}
            if names & SUBSTANTIVE and any(r["result"] == BLOCKED for r in blocking if r["rule"] in SUBSTANTIVE):
                final_decision = "avoid"
            elif all(r["rule"] in ENTRY_RULES | {"plan_validity"} for r in blocking):
                final_decision = "wait"
            else:
                stock_blocks = [r for r in blocking if not is_setup_gate(r)]
                final_decision = "insufficient_data" if any(r["result"] == UNKNOWN or r["rule"] in
                                                            {"required_evidence", "stale_data", "data_quality", "demo_mode"}
                                                            for r in stock_blocks) else "wait"
            adjustments.append(f"Head proposed BUY; rules changed the permitted action to {final_decision.upper()} "
                               f"because: " + "; ".join(f"{r['rule']} ({r['result']})" for r in blocking))

    sizing = size_position(bundle, plan) if (actionable or on_hold) else None
    if actionable and sizing and sizing.get("status") != "ok":
        actionable = False
        final_decision = "wait"
        adjustments.append(f"Sizing: {sizing.get('detail')}")

    rr_min = rc["vetoes"]["reward_risk"]["min_short_term"]
    if final_decision == "wait" and not trigger:
        trigger = code_trigger(plan, blocking, rr_min)
    if final_decision == "wait" and trigger and trigger.get("kind") == "price":
        trigger, note = check_price_trigger(trigger, plan, rr_min, enforce_min=bundle.horizon == "short")
        if note:
            adjustments.append(note)
    has_levels = plan.get("entry_low") is not None and plan.get("stop") is not None
    show_plan = (actionable or on_hold or final_decision == "wait") and (is_buy or (final_decision == "wait" and has_levels))
    missing = [i for i in gate["items"] if not i["ok"]]
    return {"rule_results": results, "governance": gov, "calculations": calcs, "adjustments": adjustments,
            "final_action": {"decision": final_decision, "actionable": actionable and not incomplete, "on_hold": on_hold,
                             "plan": plan if show_plan else None, "sizing": sizing,
                             "trigger": trigger if final_decision == "wait" else None,
                             "avoid_reason": avoid_reason if final_decision == "avoid" else None,
                             "would_change_view": would_change if final_decision == "avoid" else [],
                             "missing_evidence": missing if final_decision == "insufficient_data" and not incomplete else [],
                             "incomplete": incomplete, "confidence": conf},
            "evidence_gate": gate,
            "blocking": [r["rule"] for r in blocking]}


def size_position(bundle: Bundle, plan: dict) -> dict:
    """Position size and tranches as a % of your total capital — every limit in the rules is a
    percentage, so no rupee amount is needed. When total_investment_capital_inr is configured, whole
    share quantities and rupee amounts are added on top of the same plan."""
    u, a = config("user")["capital"], config("rules")["allocation"]
    stock_share = (100 - u.get("core_pct", a["core_pct_default"])) / 100   # part of capital in individual stocks
    price = plan["entry_high"]
    stop_dist_pct = (price - plan["stop"]) / price * 100
    risk_pct = max(stop_dist_pct, a["min_risk_distance_pct"])               # min distance prevents oversized positions
    loss_cap_pct = a["planned_loss_at_stop_pct_of_capital"]
    by_risk = loss_cap_pct / risk_pct * 100                                 # % of capital
    by_cap = a["satellite_max_per_stock_pct"] * stock_share                 # % of capital
    floor = a["satellite_floor_pct"] * stock_share                          # % of capital
    pos = min(by_risk, by_cap)
    binding = "risk" if by_risk < by_cap else "per-stock cap"
    if pos < floor:
        return {"status": "below_floor",
                "detail": f"Position would be {pos:.1f}% of capital (the {loss_cap_pct}% loss-at-stop limit with a "
                          f"{risk_pct:.1f}% stop distance), below the {a['satellite_floor_pct']}% minimum of the stock "
                          f"portion ({floor:.1f}% of capital) — the stop is too wide for this position."}
    tr = a["tranches_three"]
    levels = [plan["entry_high"], round((plan["entry_low"] + plan["entry_high"]) / 2, 2), plan["entry_low"]]
    tranches = [{"tranche": f"T{i + 1}", "pct": pct, "pct_of_capital": round(pos * pct / 100, 2), "price": lv,
                 "above_stop": lv > plan["stop"]} for i, (pct, lv) in enumerate(zip(tr, levels))]
    out = {"status": "ok", "basis": "percent", "position_pct_of_capital": round(pos, 2),
           "pct_of_stock_portion": round(pos / stock_share, 2) if stock_share else None,
           "loss_at_stop_pct_of_capital": round(pos * stop_dist_pct / 100, 2), "max_loss_at_stop_pct_of_capital": loss_cap_pct,
           "tranches": tranches, "binding_constraint": binding,
           "note": "Loss at stop is not a maximum possible loss — gaps and slippage can exceed it."}
    cap = u.get("total_investment_capital_inr")
    if not cap:
        out["detail"] = "Sized as % of your capital; set total_investment_capital_inr to also see share quantities."
        return out
    sat = cap * stock_share
    qty = int(math.floor(cap * pos / 100 / price))
    value = qty * price
    if qty <= 0 or value < sat * a["satellite_floor_pct"] / 100:
        return {"status": "below_floor", "detail": f"Position Rs {value:,.0f} is below the {a['satellite_floor_pct']}% "
                                                   f"minimum of the stock portion (whole shares at Rs {price:,.2f})."}
    left = qty
    for i, t in enumerate(tranches):
        q = left if i == len(tranches) - 1 else int(qty * t["pct"] / 100)
        left -= q
        t["qty"] = q
    out.update({"basis": "capital", "qty": qty, "value_inr": round(value, 2), "satellite_inr": round(sat, 2),
                "pct_of_satellite": round(value / sat * 100, 2),
                "planned_loss_at_stop_inr": round(qty * (price - plan["stop"]), 2),
                "max_planned_loss_inr": round(cap * loss_cap_pct / 100, 2)})
    return out


def consistency(final: dict, head: dict) -> dict:
    """Whole-plan validation after enforcement (spec §9 'Stops, alerts and final consistency')."""
    issues = []
    fa = final["final_action"]
    if fa["actionable"]:
        if fa["decision"] != "buy":
            issues.append("Actionable flag set on a non-buy decision")
        issues += plan_valid(fa["plan"] or {})
        sz = fa.get("sizing") or {}
        if sz.get("status") == "ok":
            if sum(t["pct"] for t in sz["tranches"]) != 100:
                issues.append("Tranche shares do not add up to 100% of the position")
            if not all(t["above_stop"] for t in sz["tranches"]):
                issues.append("A tranche is not above the stop")
            if sz["loss_at_stop_pct_of_capital"] > sz["max_loss_at_stop_pct_of_capital"] + 1e-6:
                issues.append("Loss at stop exceeds the configured % of capital")
            if "qty" in sz:
                if sum(t["qty"] for t in sz["tranches"]) != sz["qty"]:
                    issues.append("Tranche quantities do not sum to the position")
                if sz["planned_loss_at_stop_inr"] > sz["max_planned_loss_inr"] + 1e-6:
                    issues.append("Planned loss at stop exceeds the configured ceiling")
    if head.get("decision") == "buy" and not fa["actionable"] and not final["adjustments"]:
        issues.append("Buy proposal not actionable but no rule adjustment recorded")
    if fa["decision"] in ("avoid", "insufficient_data") and fa.get("plan"):
        issues.append("Trade plan attached to a non-actionable avoid/insufficient decision")
    return {"result": "passed" if not issues else "failed", "issues": issues, "version": "plan-validator@1"}
