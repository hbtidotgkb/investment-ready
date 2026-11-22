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
    return out


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
    calcs: dict = {"reward_risk": rr}
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
            results.append(_r("benchmark", UNKNOWN,
                              f"After-tax comparison refused ({tv.get('status')}). Pre-tax: stock target-low "
                              f"{'n/a' if gross is None else f'{gross:.1f}%'} vs benchmark assumption {bm_gross:.1f}% over {months} months."))
        else:
            ok = tv["after_tax_pct"] > tb["after_tax_pct"]
            results.append(_r("benchmark", PASS if ok else BLOCKED,
                              f"After-tax (approx) {tv['after_tax_pct']}% vs benchmark {tb['after_tax_pct']}% over {months} months "
                              f"(assumption {rc['benchmark']['assumed_annual_return_pct']}%/yr — not a forecast)"))

    blocking = [r for r in results if r["result"] in (BLOCKED, UNKNOWN)]
    final_decision = head.get("decision")
    adjustments = []
    actionable = False
    if is_buy:
        if not blocking:
            actionable = True
        else:
            names = {r["rule"] for r in blocking}
            if names & SUBSTANTIVE and any(r["result"] == BLOCKED for r in blocking if r["rule"] in SUBSTANTIVE):
                final_decision = "avoid"
            elif all(r["rule"] in ENTRY_RULES | {"plan_validity"} for r in blocking):
                final_decision = "wait"
            else:
                final_decision = "insufficient_data" if any(r["result"] == UNKNOWN or r["rule"] in
                                                            {"required_evidence", "stale_data", "data_quality", "demo_mode", "policies_confirmed"}
                                                            for r in blocking) else "wait"
            adjustments.append(f"Head proposed BUY; rules changed the permitted action to {final_decision.upper()} "
                               f"because: " + "; ".join(f"{r['rule']} ({r['result']})" for r in blocking))

    sizing = size_position(bundle, plan) if actionable else None
    if actionable and sizing and sizing.get("status") != "ok":
        actionable = False
        final_decision = "wait"
        adjustments.append(f"Sizing: {sizing.get('detail')}")

    return {"rule_results": results, "governance": gov, "calculations": calcs, "adjustments": adjustments,
            "final_action": {"decision": final_decision, "actionable": actionable,
                             "plan": plan if (actionable or final_decision == "wait") and is_buy else None,
                             "sizing": sizing},
            "blocking": [r["rule"] for r in blocking]}


def size_position(bundle: Bundle, plan: dict) -> dict:
    """Whole-share sizing under the satellite caps and planned-loss-at-stop ceiling."""
    u, a = config("user")["capital"], config("rules")["allocation"]
    cap = u.get("total_investment_capital_inr")
    if not cap:
        return {"status": "no_capital", "detail": "total_investment_capital_inr not configured — research-only sizing."}
    sat = cap * (1 - u.get("core_pct", a["core_pct_default"]) / 100)
    price = plan["entry_high"]
    risk_per_share = max(price - plan["stop"], price * a["min_risk_distance_pct"] / 100)
    max_loss = cap * a["planned_loss_at_stop_pct_of_capital"] / 100
    by_risk = max_loss / risk_per_share
    by_cap = sat * a["satellite_max_per_stock_pct"] / 100 / price
    qty = int(math.floor(min(by_risk, by_cap)))
    value = qty * price
    if qty <= 0 or value < sat * a["satellite_floor_pct"] / 100:
        return {"status": "below_floor", "detail": f"Position Rs {value:,.0f} is below the {a['satellite_floor_pct']}% satellite floor."}
    tr = a["tranches_three"]
    levels = [plan["entry_high"], round((plan["entry_low"] + plan["entry_high"]) / 2, 2), plan["entry_low"]]
    tranches, left = [], qty
    for i, (pct, lv) in enumerate(zip(tr, levels)):
        q = left if i == len(tr) - 1 else int(qty * pct / 100)
        left -= q
        tranches.append({"tranche": f"T{i + 1}", "pct": pct, "price": lv, "qty": q, "above_stop": lv > plan["stop"]})
    return {"status": "ok", "qty": qty, "value_inr": round(value, 2), "satellite_inr": round(sat, 2),
            "pct_of_satellite": round(value / sat * 100, 2),
            "planned_loss_at_stop_inr": round(qty * (price - plan["stop"]), 2), "max_planned_loss_inr": round(max_loss, 2),
            "tranches": tranches, "binding_constraint": "risk" if by_risk < by_cap else "per-stock cap",
            "note": "Planned loss at stop is not a maximum possible loss — gaps and slippage can exceed it."}


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
            if sum(t["qty"] for t in sz["tranches"]) != sz["qty"]:
                issues.append("Tranche quantities do not sum to the position")
            if not all(t["above_stop"] for t in sz["tranches"]):
                issues.append("A tranche is not above the stop")
            if sz["planned_loss_at_stop_inr"] > sz["max_planned_loss_inr"] + 1e-6:
                issues.append("Planned loss at stop exceeds the configured ceiling")
    if head.get("decision") == "buy" and not fa["actionable"] and not final["adjustments"]:
        issues.append("Buy proposal not actionable but no rule adjustment recorded")
    if fa["decision"] in ("avoid", "insufficient_data") and fa.get("plan"):
        issues.append("Trade plan attached to a non-actionable avoid/insufficient decision")
    return {"result": "passed" if not issues else "failed", "issues": issues, "version": "plan-validator@1"}
