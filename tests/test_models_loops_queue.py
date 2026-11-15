"""Multi-provider model config, OpenAI adapter mapping, multi-loop runs, source ranking and the
ET article queue."""
import asyncio
import json
import types

import pytest
from fastapi.testclient import TestClient

from ca_platform import gateway as G
from ca_platform.contracts import CaptureBatch, CritiqueLLM, Objection, SettlingTest
from ca_platform.inbox import get_source, ingest_batch, link_symbol, queue_add, queue_list
from ca_platform.ledger import get_ledger
from ca_platform.service import app, pairing_token
from ca_platform.settings import read_override, reload_config, write_override


@pytest.fixture
def client():
    return TestClient(app, base_url="http://127.0.0.1:8765")


@pytest.fixture
def clean_override():
    before = read_override("models")
    yield
    write_override("models", before)


def H():
    return {"x-ca-token": pairing_token()}


def test_role_models_can_be_split_across_providers(client, clean_override):
    r = client.post("/api/models", headers=H(), json={"roles": {
        "critic": {"provider": "openai", "model": "my-openai-model", "effort": "high"}},
        "pricing": {"my-openai-model": {"input": 1.0, "output": 4.0}}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["roles"]["critic"]["provider"] == "openai"
    assert body["separation"]["head_critic_separated"] is True
    assert "+local-" in body["version"]
    assert G.price_of("my-openai-model") == {"input": 1.0, "output": 4.0}


def test_locked_roles_and_bad_input_rejected(client, clean_override):
    assert client.post("/api/models", headers=H(), json={"roles": {
        "discovery": {"provider": "openai", "model": "x", "effort": "low"}}}).status_code == 400
    assert client.post("/api/models", headers=H(), json={"roles": {
        "head": {"provider": "gemini", "model": "x", "effort": "low"}}}).status_code == 400
    assert client.post("/api/models", json={"roles": {}}).status_code == 401  # token required


def test_openai_adapter_maps_request_and_records(monkeypatch, clean_override):
    write_override("models", {"roles": {"critic": {"provider": "openai", "model": "fake-oa", "effort": "xhigh"}}})
    seen = {}

    class FakeResponses:
        async def parse(self, **kw):
            seen.update(kw)
            parsed = CritiqueLLM(checklist=["facts"], objections=[])
            return types.SimpleNamespace(output_parsed=parsed, model="fake-oa-2026", status="completed", id="resp_123",
                                         output=[], usage=types.SimpleNamespace(input_tokens=1000, output_tokens=200))

    gw = G.Gateway(get_ledger(), run_id="t-openai")
    gw._clients["openai"] = types.SimpleNamespace(responses=FakeResponses())
    monkeypatch.setattr(G, "get_secret", lambda k: "sk-test")
    res = asyncio.run(gw.structured(role="critic", stage="critic", system="SYS", user="USER", output_model=CritiqueLLM))
    assert isinstance(res.parsed, CritiqueLLM)
    assert seen["instructions"] == "SYS" and seen["input"] == [{"role": "user", "content": "USER"}]
    assert seen["text_format"] is CritiqueLLM and seen["store"] is False
    assert seen["reasoning"] == {"effort": "high"}          # xhigh mapped to OpenAI's top effort
    rec = res.record
    assert rec["provider"] == "openai" and rec["provider_request_id"] == "resp_123"
    assert rec["price_known"] is False                      # no price configured -> unknown, never guessed
    row = get_ledger().fetchone("SELECT cost_usd FROM request_records WHERE request_id=?", [rec["request_id"]])
    assert row["cost_usd"] is None


def test_demo_two_loops_carry_questions_and_change_results():
    from ca_platform.orchestrator import run_analysis
    from ca_platform.events import REGISTRY
    r = asyncio.run(run_analysis("", "medium", mode="demo", loops=2))
    assert r["status"] == "completed", r
    dr = r["decision_record"]
    l1, l2 = dr["loops"]
    assert l1["asked"] == [] and len(l2["asked"]) > 0                  # loop 2 searched for loop 1's open questions
    assert l2["new_evidence"] > 0 and l2["evidence_count"] > l1["evidence_count"]
    assert l2["governance_coverage"] > l1["governance_coverage"]       # forensic gaps filled in loop 2
    assert l2["objections"] < l1["objections"]                         # settled objection not repeated
    assert dr["loop"] == 2 and dr["parent_bundle_id"] == l1["bundle_id"]
    ev = REGISTRY[r["run_id"]].history
    assert {e["loop"] for e in ev if e["type"] == "stage_end"} == {1, 2}
    assert get_ledger().fetchone("SELECT COUNT(*) AS n FROM verdicts WHERE run_id=?", [r["run_id"]])["n"] == 1
    assert get_ledger().fetchone("SELECT COUNT(*) AS n FROM loop_records WHERE run_id=?", [r["run_id"]])["n"] == 2
    top = dr["top_sources"]
    assert 5 <= len(top) <= 10 and top == sorted(top, key=lambda s: -s["score"])
    assert top[0]["verified_citations"] >= top[-1]["verified_citations"]
    assert all(s["evidence_id"] for s in top)


def _cap(url, text, qid=None, cid="c", sid="s"):
    return CaptureBatch.model_validate({"research_session_id": sid, "extension_version": "0.3.0", "items": [
        {"capture_id": cid, "url": url, "title": "ET article", "captured_at": "2026-11-15T10:00:00Z",
         "passages": [{"id": "P1", "text": text}], "usage_attested": True, "queue_id": qid}]})


def test_queue_accepts_et_only_and_links_symbol_on_capture():
    r = queue_add(["https://economictimes.indiatimes.com/prime/x/articleshow/999.cms?utm_source=a",
                   "https://example.com/not-et", "javascript:alert(1)"], symbol="tcs")
    assert len(r["added"]) == 1 and len(r["rejected"]) == 2
    dup = queue_add(["https://economictimes.indiatimes.com/prime/x/articleshow/999.cms"])
    assert dup["added"] == [] and "Already queued" in dup["rejected"][0]["reason"]
    qid = r["added"][0]["queue_id"]
    rec = ingest_batch(_cap(r["added"][0]["url"], "An article that mentions no listed company by name.", qid, "qc", "qs"))
    it = rec["items"][0]
    assert it["status"] == "accepted"
    assert [c["symbol"] for c in it["companies"]["resolved"]] == ["TCS"]        # queue tag becomes an explicit link
    q = next(x for x in queue_list() if x["queue_id"] == qid)
    assert q["status"] == "captured" and q["source_id"] == it["source_id"]


def test_manual_symbol_link():
    rec = ingest_batch(_cap("https://economictimes.indiatimes.com/a/articleshow/5.cms", "Unlinked text body.", cid="m", sid="m"))
    sid = rec["items"][0]["source_id"]
    link_symbol(sid, "INFY")
    s = get_source(sid)
    assert any(c["symbol"] == "INFY" and c["matched"] == "user:user" for c in s["company_links"]["resolved"])


def test_queue_endpoints_need_token(client):
    assert client.get("/v1/research/queue").status_code == 401
    assert client.get("/v1/research/queue", headers=H()).status_code == 200
