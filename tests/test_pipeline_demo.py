"""Integration: the full pipeline in DEMO mode (scripted brains, real validators/rules/ledger)."""
import asyncio

from ca_platform.events import REGISTRY, STAGES
from ca_platform.ledger import get_ledger
from ca_platform.orchestrator import run_analysis


def test_demo_run_end_to_end():
    r = asyncio.run(run_analysis("", "medium", mode="demo"))
    assert r["status"] == "completed", r
    dr = r["decision_record"]
    ev = REGISTRY[r["run_id"]].history
    ended = {e["stage"]: e["status"] for e in ev if e["type"] == "stage_end"}
    assert set(ended) == {s["id"] for s in STAGES}            # every stage reported progress
    assert all(v in ("completed",) for v in ended.values()), ended
    # demo must never be actionable, and the Critic's open blocker must survive into the rules
    assert dr["final_action"]["actionable"] is False
    assert dr["head_draft"]["decision"] == "buy" and dr["head_proposal"]["decision"] == "wait"
    assert dr["resolution"]["open_blockers"] == ["obj-1"]
    blocked = {x["rule"] for x in dr["rule_results"] if x["result"] == "blocked"}
    assert {"demo_mode", "required_evidence"} <= blocked
    assert dr["consistency_check"]["result"] == "passed"
    # every brain citation verified against the frozen bundle
    for e in ev:
        if e["type"] == "stage_end" and e["stage"] in ("quant", "news_brain", "forensic"):
            assert e["data"]["audit"]["score"] == 1.0
    led = get_ledger()
    assert led.fetchone("SELECT COUNT(*) AS n FROM verdicts WHERE run_id=?", [r["run_id"]])["n"] == 1
    assert len(dr["learn"]) == 2 and "Analysis, not investment advice" in dr["whatsapp"]


def test_second_run_receives_prior_decision_memory():
    from ca_platform.orchestrator import prior_decisions
    asyncio.run(run_analysis("", "medium", mode="demo"))
    prior = prior_decisions("DEMOCO")
    assert prior and prior[0]["final_decision"] == "wait" and prior[0]["invalidation_conditions"]
