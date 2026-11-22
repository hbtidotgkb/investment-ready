"""L0 code evals: rules engine invariants (spec §9, §13 L0 allocation invariants)."""
import copy

import pytest

from ca_platform.contracts import Bundle, Calculation
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


def _bundle(**kw):
    b = Bundle(mode="live", symbol="X", company_name="X", decision_at="2026-11-16T15:00:00+05:30", horizon="short",
               action="new_purchase", price={"close": 100.0, "date": "2026-11-16"},
               calculations=[Calculation(id="c1", name="adv20_crore", formula="f", result=50.0)],
               surveillance={"status": "not_listed", "source": "test"})
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
