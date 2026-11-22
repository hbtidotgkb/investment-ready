"""L0 code evals: rules engine invariants (spec §9, §13 L0 allocation invariants)."""
import copy

import pytest

from ca_platform.contracts import Bundle, Calculation, Evidence, FinancialFact, Passage
from ca_platform.rules import engine as R
from ca_platform.settings import config as real_config


def test_plan_validity_and_reward_risk():
    good = {"entry_low": 98, "entry_high": 100, "stop": 92, "target_low": 120, "target_high": 130}
    assert R.plan_valid(good) == []
    assert R.reward_risk(98, 100, 92, 120) == round((120 - 99) / (99 - 92), 2)
    bad = dict(good, stop=99)
    assert "Stop is not below the entry zone" in R.plan_valid(bad)
    assert R.plan_valid(dict(good, target_low=None))  # incomplete
    assert R.reward_risk(98, 100, 101, 120) is None   # stop above entry => no R/R


def test_governance_rubric_withholds_on_low_coverage_and_penalises():
    few = [{"name": "auditor", "result": "pass", "materiality": "critical"}]
    assert R.governance_score(few)["score"] is None
    checks = [{"name": f"c{i}", "result": "pass", "materiality": "major"} for i in range(8)]
    checks += [{"name": "x", "result": "fail", "materiality": "major"}, {"name": "y", "result": "watch", "materiality": "minor"}]
    g = R.governance_score(checks)
    assert g["score"] == 100 - 25 - 3 and g["coverage"] == 1.0
    sev = checks + [{"name": "auditor", "result": "fail", "materiality": "critical"}]
    assert R.governance_score(sev)["severe_findings"] == ["auditor"]


@pytest.fixture
def capital(monkeypatch):
    def cfg(name):
        c = copy.deepcopy(real_config(name))
        if name == "user":
            c["capital"]["total_investment_capital_inr"] = 1_000_000
            c["policies_confirmed"] = True
        if name == "tax_rates":
            c["review_status"] = "verified"
        return c
    monkeypatch.setattr(R, "config", cfg)
    return cfg


@pytest.mark.parametrize("stop", [97.9, 95.0, 80.0])  # 97.9 = very tight stop -> min-distance denominator binds
def test_sizing_invariants(capital, stop):
    b = Bundle(mode="live", symbol="X", company_name="X", decision_at="2026-11-15T15:00:00+05:30", horizon="medium",
               action="new_purchase")
    plan = {"entry_low": 98.0, "entry_high": 100.0, "stop": stop, "target_low": 125.0, "target_high": 140.0}
    s = R.size_position(b, plan)
    if s["status"] != "ok":
        return
    sat = 1_000_000 * 0.5
    assert s["value_inr"] <= sat * 0.30 + 1e-6                      # per-stock cap
    assert s["planned_loss_at_stop_inr"] <= s["max_planned_loss_inr"] + 1e-6   # 2% ceiling
    assert sum(t["qty"] for t in s["tranches"]) == s["qty"]           # tranches sum
    assert all(t["above_stop"] for t in s["tranches"])                # no tranche below stop
    assert s["value_inr"] >= sat * 0.08                                # floor
    # tight stop must not create an oversized position (min risk-distance denominator)
    assert s["qty"] <= int(1_000_000 * 0.02 / (100 * 0.03)) + 1


def _complete_evidence():
    """Everything the evidence gate needs for any horizon."""
    candles = [{"date": f"d{i}", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1} for i in range(260)]
    calcs = [Calculation(id="c1", name="adv20_crore", formula="f", result=50.0),
             Calculation(id="c2", name="rsi14", formula="f", result=55.0), Calculation(id="c3", name="atr14", formula="f", result=2.0),
             Calculation(id="c4", name="eps_ttm", formula="f", result=5.0)]
    facts = [FinancialFact(id="f1", metric="revenue", value=100.0, status="available", period_end="2026-09-30", evidence_id="ev-1"),
             FinancialFact(id="f2", metric="audit_opinion_unmodified", value=1.0, status="available", period_end="2026-03-31",
                           evidence_id="ev-1", reason="unmodified"),
             FinancialFact(id="f3", metric="promoter_pledge_pct", value=0.0, status="available", period_end="2026-09-30", evidence_id="ev-2")]
    ev = [Evidence(id="ev-9", kind="computed_feature", title="Price & technicals (computed)", publisher="x", tier="SYS_computed",
                   first_seen_at="2026-11-16T10:00:00+05:30", ingested_at="2026-11-16T10:00:00+05:30", content_hash="h",
                   passages=[Passage(id="ev-9:P1", text="RSI 55")])]
    return {"candles": candles, "calculations": calcs, "facts": facts, "evidence": ev}


def _bundle(**kw):
    b = Bundle(mode="live", symbol="X", company_name="X", decision_at="2026-11-16T15:00:00+05:30", horizon="short",
               action="new_purchase", price={"close": 100.0, "date": "2026-11-16"},
               surveillance={"status": "not_listed", "source": "test"}, **_complete_evidence())
    return b.model_copy(update=kw)


def _clean_checks():
    names = ["ocf_vs_profit", "receivable_inventory_days", "other_income_exceptional", "related_party_transactions",
             "contingent_liabilities", "auditor", "promoter_pledge_insider", "capitalised_costs_cwip", "dilution", "tax_rate"]
    return [{"name": n, "result": "pass", "materiality": "major", "note": "ok"} for n in names]


HEAD_BUY = {"decision": "buy", "entry_low": 98.0, "entry_high": 100.0, "stop": 94.0, "target_low": 125.0, "target_high": 140.0}
PANEL_OK = {"brains_ok": True, "critic_ok": True, "open_blockers": [], "citation_ok": True, "patterns": []}


def test_clean_buy_becomes_actionable_with_sizing(capital):
    b = _bundle()
    pre = R.preflight(b)
    out = R.enforce(b, pre, HEAD_BUY, PANEL_OK | {"forensic_checks": _clean_checks()})
    assert out["final_action"]["actionable"], out["rule_results"]
    assert out["final_action"]["sizing"]["status"] == "ok"
    assert R.consistency(out, HEAD_BUY)["result"] == "passed"


def test_open_blocker_and_missing_roles_block_buy(capital):
    b = _bundle()
    out = R.enforce(b, R.preflight(b), HEAD_BUY, PANEL_OK | {"forensic_checks": _clean_checks(), "open_blockers": ["obj-1"]})
    assert not out["final_action"]["actionable"]
    assert "required_evidence" in out["blocking"]
    out2 = R.enforce(b, R.preflight(b), HEAD_BUY, PANEL_OK | {"forensic_checks": [], "brains_ok": False})
    assert out2["final_action"]["decision"] == "insufficient_data"


def test_surveillance_veto_turns_buy_into_avoid(capital):
    b = _bundle(surveillance={"status": "listed", "stage": "ASM Stage I"})
    out = R.enforce(b, R.preflight(b), HEAD_BUY, PANEL_OK | {"forensic_checks": _clean_checks()})
    assert out["final_action"]["decision"] == "avoid" and out["final_action"]["plan"] is None


def test_low_reward_risk_short_term_means_wait(capital):
    b = _bundle()
    head = dict(HEAD_BUY, target_low=105.0)
    out = R.enforce(b, R.preflight(b), head, PANEL_OK | {"forensic_checks": _clean_checks()})
    assert out["final_action"]["decision"] in ("wait", "insufficient_data") and not out["final_action"]["actionable"]
    assert any(r["rule"] == "reward_risk" and r["result"] == "blocked" for r in out["rule_results"])


def test_unverified_tax_policy_refuses_after_tax_and_blocks(monkeypatch):
    def cfg(name):
        c = copy.deepcopy(real_config(name))
        if name == "user":
            c["capital"]["total_investment_capital_inr"] = 1_000_000
            c["policies_confirmed"] = True
        return c  # tax_rates left as needs_user_verification
    monkeypatch.setattr(R, "config", cfg)
    b = _bundle()
    out = R.enforce(b, R.preflight(b), HEAD_BUY, PANEL_OK | {"forensic_checks": _clean_checks()})
    bm = next(r for r in out["rule_results"] if r["rule"] == "benchmark")
    assert bm["result"] == "unknown" and not out["final_action"]["actionable"]


def test_stale_price_blocks():
    b = _bundle(price={"close": 100.0, "date": "2026-11-05"})
    pre = {r["rule"]: r for r in R.preflight(b)}
    assert pre["stale_data"]["result"] == "blocked"


def test_setup_only_blocks_keep_the_buy_verdict_on_hold(monkeypatch):
    """policies_confirmed:false, unverified tax file and no capital are about YOUR setup, not the stock:
    the verdict stays BUY (not actionable, on hold) instead of turning into 'insufficient data'."""
    def cfg(name):
        c = copy.deepcopy(real_config(name))
        if name == "user":
            c["policies_confirmed"] = False
            c["capital"]["total_investment_capital_inr"] = None
        return c
    monkeypatch.setattr(R, "config", cfg)
    b = _bundle(horizon="medium")
    out = R.enforce(b, R.preflight(b), HEAD_BUY, PANEL_OK | {"forensic_checks": _clean_checks()})
    fa = out["final_action"]
    assert fa["decision"] == "buy" and fa["on_hold"] == "setup" and not fa["actionable"]
    assert fa["plan"] is not None and "ON HOLD" in out["adjustments"][0]
    # a stock-specific block still wins over setup: surveillance listing → avoid
    b2 = _bundle(horizon="medium", surveillance={"status": "listed", "stage": "ASM Stage I"})
    out2 = R.enforce(b2, R.preflight(b2), HEAD_BUY, PANEL_OK | {"forensic_checks": _clean_checks()})
    assert out2["final_action"]["decision"] == "avoid" and not out2["final_action"]["on_hold"]
    # missing stock evidence still means insufficient data, even with setup incomplete
    out3 = R.enforce(b, R.preflight(b), HEAD_BUY, PANEL_OK | {"forensic_checks": [], "brains_ok": False})
    assert out3["final_action"]["decision"] == "insufficient_data"


# ---------------------------------------------------------------- verdict design (decision-design@1) ---

def _decide(b, head, checks=None, capital_fixture=True):
    return R.enforce(b, R.preflight(b), head, PANEL_OK | {"forensic_checks": _clean_checks() if checks is None else checks})


WAIT_PRICE = {"decision": "wait", "trigger": {"kind": "price", "price_level": 90.0, "direction": "below",
                                              "description": "Pullback to support near Rs 90"}}


def test_gate_items_depend_on_horizon():
    short = R.evidence_gate(_bundle(horizon="short"))
    med = R.evidence_gate(_bundle(horizon="medium"))
    assert short["complete"] and med["complete"]
    assert {i["key"] for i in short["items"]} == {"surveillance", "price_history", "technicals", "shareholding"}
    assert {"results_fresh", "valuation_inputs", "audit_opinion"} <= {i["key"] for i in med["items"]}
    # short-term: 60 bars and computed technicals are enough; results/audit are not required
    b = _bundle(horizon="short", candles=_bundle().candles[:70], facts=[f for f in _bundle().facts if f.metric == "promoter_pledge_pct"])
    assert R.evidence_gate(b)["complete"]
    assert not R.evidence_gate(b.model_copy(update={"horizon": "medium"}))["complete"]


def test_stale_results_make_medium_horizon_insufficient(capital):
    old = [f.model_copy(update={"period_end": "2026-06-30"}) if f.metric == "revenue" else f for f in _bundle().facts]
    b = _bundle(horizon="medium", facts=old)                         # 139 days before 2026-11-16 > 100
    g = R.evidence_gate(b)
    assert g["missing"] == ["results_fresh"] and "139 days old" in g["items"][2]["detail"]
    out = _decide(b, WAIT_PRICE)
    fa = out["final_action"]
    assert fa["decision"] == "insufficient_data" and [m["key"] for m in fa["missing_evidence"]] == ["results_fresh"]
    assert fa["missing_evidence"][0]["how_to_get"]


def test_red_flag_means_avoid_even_when_head_says_wait(capital):
    b = _bundle(surveillance={"status": "listed", "stage": "ASM Stage II"})
    fa = _decide(b, WAIT_PRICE)["final_action"]
    assert fa["decision"] == "avoid" and "surveillance" in fa["avoid_reason"]
    assert any("surveillance" in w.lower() for w in fa["would_change_view"])
    # a red flag outranks missing evidence: avoid, not insufficient
    b2 = _bundle(surveillance={"status": "listed", "stage": "GSM"}, facts=[], horizon="medium")
    assert _decide(b2, {"decision": "insufficient_data", "insufficiency_reason": "no results figures in the bundle"})["final_action"]["decision"] == "avoid"


def test_head_refusal_with_complete_evidence_is_incomplete(capital):
    b = _bundle(horizon="medium")
    out = _decide(b, {"decision": "insufficient_data", "insufficiency_reason": "The two order-value filings conflict (2,100 vs 2,600 crore)"})
    fa = out["final_action"]
    assert fa["decision"] == "insufficient_data" and fa["incomplete"] and "conflict" in fa["incomplete"]["reason"]
    assert not fa["actionable"]


def test_wait_needs_a_checkable_trigger():
    close, when = 100.0, "2026-11-16T15:00:00+05:30"
    assert R.verdict_problems({"decision": "wait"}, close, when)                                    # no trigger
    assert R.verdict_problems({"decision": "wait", "trigger": {"kind": "price", "price_level": 110, "direction": "below",
                                                               "description": "x"}}, close, when)   # already met
    assert not R.verdict_problems(WAIT_PRICE, close, when)
    assert R.verdict_problems({"decision": "wait", "trigger": {"kind": "date", "date": "2026-11-01", "description": "x"}}, close, when)
    assert not R.verdict_problems({"decision": "wait", "trigger": {"kind": "date", "date": "2027-01-15",
                                                                   "description": "Q3 results"}}, close, when)
    assert R.verdict_problems({"decision": "wait", "trigger": {"kind": "event", "description": "news"}}, close, when)
    assert R.verdict_problems({"decision": "avoid", "avoid_reason": "x"}, close, when)              # no would_change_view
    assert R.verdict_problems({"decision": "insufficient_data"}, close, when)


def test_wait_without_trigger_reaching_rules_is_incomplete(capital):
    fa = _decide(_bundle(), {"decision": "wait"})["final_action"]
    assert fa["incomplete"] and "trigger" in fa["incomplete"]["reason"]


def test_rules_made_wait_gets_a_code_trigger(capital):
    b = _bundle()
    head = dict(HEAD_BUY, target_low=105.0)                    # reward/risk too low for a short-term buy
    fa = _decide(b, head)["final_action"]
    assert fa["decision"] == "wait" and fa["trigger"]["set_by"] == "code" and fa["trigger"]["kind"] == "price"
    lv = fa["trigger"]["price_level"]
    assert 2.0 <= (105.0 - lv) / (lv - 94.0) < 2.01               # at that price reward/risk reaches the minimum


def test_confidence_is_set_by_code():
    b = _bundle(horizon="medium", coverage={"insider_trades": "available", "news": "available", "related_party": "available"})
    facts = b.facts + [FinancialFact(id="c", metric="contingent_liabilities", value=5.0, status="available", period_end="2026-03-31")]
    assert R.confidence(b.model_copy(update={"facts": facts}), {})["level"] == "high"
    one = R.confidence(b, {})
    assert one["level"] == "medium" and one["gaps"] == ["Contingent liabilities not found"]
    assert R.confidence(_bundle(horizon="medium"), {})["level"] == "low"       # several gaps; never below low
    bank = b.model_copy(update={"facts": facts, "coverage": dict(b.coverage, is_bank=True)})
    assert "Bank slippages / restructured book not found" in R.confidence(bank, {})["gaps"]
