"""Cost levers in the gateway: cache pricing, the Head's cached prefix, reuse of an unchanged
analyst answer, and the half-price Batch API path (with its fall back to a standard call).
All offline: a fake Anthropic client stands in for the API."""
import asyncio
import json
import types

import pytest

from ca_platform import gateway as G
from ca_platform.contracts import CritiqueLLM
from ca_platform.ledger import get_ledger

NS = types.SimpleNamespace
ROLE = "claims"   # an Anthropic role; the output model below is just a small schema


def usage(inp=100, out=10, read=0, write=0):
    return NS(input_tokens=inp, output_tokens=out, cache_read_input_tokens=read, cache_creation_input_tokens=write,
              server_tool_use=None)


class FakeAnthropic:
    def __init__(self, batch_result="succeeded", stop_reason="end_turn"):
        self.sync_calls, self.batch_params = [], []
        outer = self

        class Messages:
            async def parse(self, **kw):
                outer.sync_calls.append(kw)
                return NS(parsed_output=CritiqueLLM(checklist=["sync"], objections=[]), model="claude-opus-5-5",
                          stop_reason="end_turn", stop_details=None, usage=usage(read=50_000), _request_id="req_sync")

        class Batches:
            async def create(self, requests):
                outer.batch_params.append(requests[0]["params"])
                outer.cid = requests[0]["custom_id"]
                return NS(id="msgbatch_1", processing_status="in_progress")

            async def retrieve(self, bid):
                return NS(id=bid, processing_status="ended")

            async def results(self, bid):
                async def gen():
                    if batch_result == "succeeded":
                        msg = NS(model="claude-opus-5-5", stop_reason=stop_reason, stop_details=None, usage=usage(),
                                 content=[NS(type="thinking", thinking=""),
                                          NS(type="text", text=json.dumps({"checklist": ["batch"], "objections": []}))])
                        yield NS(custom_id=outer.cid, result=NS(type="succeeded", message=msg))
                    else:
                        yield NS(custom_id=outer.cid, result=NS(type=batch_result))
                return gen()

        self.beta = NS(messages=Messages())
        self.messages = NS(batches=Batches())


@pytest.fixture
def gw_factory(monkeypatch):
    monkeypatch.setattr(G, "get_secret", lambda k: "sk-test")

    async def no_sleep(_):
        return None
    monkeypatch.setattr(G.asyncio, "sleep", no_sleep)

    def make(run_id, fake, batch_api=False):
        gw = G.Gateway(get_ledger(), run_id=run_id, batch_api=batch_api)
        gw._clients["anthropic"] = fake
        return gw
    return make


def test_cost_uses_model_cache_read_price_and_batch_discount():
    u = usage(inp=1_000_000, out=0, read=1_000_000, write=0)
    assert G.cost_of("claude-opus-5-5", u) == (pytest.approx(4.20), True)          # cache hits at $0.20, not 0.1x
    assert G.cost_of("claude-opus-5-5", u, batch=True) == (pytest.approx(2.10), True)
    assert G.cost_of("claude-opus-5-5", usage(inp=0, out=0), web_searches=10, batch=True)[0] == pytest.approx(0.10)


def test_cache_prefix_is_marked_and_cache_tokens_recorded(gw_factory):
    fake = FakeAnthropic()
    gw = gw_factory("t-cache", fake)
    res = asyncio.run(gw.structured(role=ROLE, stage="head_draft", system="SYS", user="SHARED-CONTEXT tail",
                                    output_model=CritiqueLLM, cache_prefix="SHARED-CONTEXT"))
    blocks = fake.sync_calls[0]["messages"][0]["content"]
    assert blocks[0] == {"type": "text", "text": "SHARED-CONTEXT", "cache_control": {"type": "ephemeral"}}
    assert blocks[1] == {"type": "text", "text": " tail"}
    row = get_ledger().fetchone("SELECT cache_read_tokens, cost_usd FROM request_records WHERE request_id=?",
                                [res.record["request_id"]])
    assert row["cache_read_tokens"] == 50_000
    assert row["cost_usd"] == pytest.approx((100 * 4 + 50_000 * 0.20 + 10 * 20) / 1e6)


def test_unchanged_input_reuses_earlier_answer_across_runs(gw_factory):
    fake = FakeAnthropic()
    key = "evidence v1 | 2026-11-23 | <bundle>"
    first = asyncio.run(gw_factory("t-reuse-1", fake).structured(
        role=ROLE, stage="quant", system="SYS", user="evidence v1 | 2026-11-23T20:19 | bundle-aaa",
        output_model=CritiqueLLM, reuse_key=key))
    second = asyncio.run(gw_factory("t-reuse-2", fake).structured(
        role=ROLE, stage="quant", system="SYS", user="evidence v1 | 2026-11-23T20:28 | bundle-bbb",
        output_model=CritiqueLLM, reuse_key=key))
    assert len(fake.sync_calls) == 1                     # second run paid nothing
    assert second.record["cache_hit"] and second.parsed == first.parsed
    third = asyncio.run(gw_factory("t-reuse-3", fake).structured(
        role=ROLE, stage="quant", system="SYS", user="evidence v2", output_model=CritiqueLLM,
        reuse_key="evidence v2 | 2026-11-23 | <bundle>"))
    assert len(fake.sync_calls) == 2 and not third.record.get("cache_hit")   # changed evidence -> new call


def test_batch_api_path_parses_and_bills_half(gw_factory):
    fake = FakeAnthropic()
    gw = gw_factory("t-batch", fake, batch_api=True)
    res = asyncio.run(gw.structured(role=ROLE, stage="quant", system="SYS", user="U-batch", output_model=CritiqueLLM))
    assert res.parsed.checklist == ["batch"] and not fake.sync_calls
    params = fake.batch_params[0]
    assert "fallbacks" not in params and params["output_config"]["format"]["type"] == "json_schema"
    row = get_ledger().fetchone("SELECT batch_api, cost_usd, provider_request_id FROM request_records WHERE request_id=?",
                                [res.record["request_id"]])
    assert row["batch_api"] is True and row["provider_request_id"] == "msgbatch_1"
    assert row["cost_usd"] == pytest.approx((100 * 4 + 10 * 20) / 1e6 / 2)


@pytest.mark.parametrize("outcome", ["expired", "errored"])
def test_failed_batch_falls_back_to_standard_call(gw_factory, outcome):
    fake = FakeAnthropic(batch_result=outcome)
    gw = gw_factory(f"t-batch-{outcome}", fake, batch_api=True)
    res = asyncio.run(gw.structured(role=ROLE, stage="quant", system="SYS", user=f"U-{outcome}", output_model=CritiqueLLM))
    assert res.parsed.checklist == ["sync"] and len(fake.sync_calls) == 1
    assert fake.sync_calls[0]["fallbacks"] == "default"


def test_batch_refusal_is_reasked_with_refusal_fallback(gw_factory):
    fake = FakeAnthropic(stop_reason="refusal")
    gw = gw_factory("t-batch-refusal", fake, batch_api=True)
    res = asyncio.run(gw.structured(role=ROLE, stage="quant", system="SYS", user="U-refusal", output_model=CritiqueLLM))
    assert res.parsed.checklist == ["sync"] and len(fake.sync_calls) == 1
    rows = get_ledger().fetchall("SELECT status, batch_api FROM request_records WHERE run_id='t-batch-refusal' ORDER BY created_at")
    assert [(r["status"], r["batch_api"]) for r in rows] == [("refused", True), ("ok", False)]   # refused tokens still counted


@pytest.mark.parametrize("body,expected", [
    ({"symbol": "TCS", "mode": "live", "batch_api": True}, True),
    ({"symbol": "TCS", "mode": "live"}, False),                     # standard price unless asked
    ({"symbol": "", "mode": "demo", "batch_api": True}, False),     # demo never queues model calls
])
def test_single_run_half_price_choice_reaches_the_run(monkeypatch, body, expected):
    from fastapi.testclient import TestClient
    from ca_platform import service

    seen = {}

    async def fake_run(symbol, horizon, action, mode, run_id=None, loops=1, batch_api=False):
        seen["batch_api"] = batch_api
    monkeypatch.setattr(service, "run_analysis", fake_run)
    with TestClient(service.app, base_url="http://127.0.0.1:8765") as c:
        r = c.post("/api/runs", json=body, headers={"x-ca-token": service.pairing_token()})
        assert r.status_code == 200, r.text
    assert seen["batch_api"] is expected
