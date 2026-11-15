"""ET lists → captures → idea extraction/ranking → batch; Kite MCP read-only guard and OAuth URL."""
import asyncio
import json
import time
import types
from urllib.parse import parse_qs, urlparse

import pytest

from ca_platform import ideas
from ca_platform.contracts import CaptureBatch, IdeaLLM, IdeasLLM
from ca_platform.data import kite_mcp, nse
from ca_platform.gateway import CallResult
from ca_platform.inbox import ingest_batch, queue_list
from ca_platform.ledger import get_ledger

EQ = ("SYMBOL,NAME OF COMPANY,SERIES,DATE OF LISTING,PAID UP VALUE,MARKET LOT, ISIN NUMBER,FACE VALUE\n"
      "TCS,Tata Consultancy Services Limited,EQ,25-AUG-2004,1,1,INE467B01029,1\n"
      "HDFCBANK,HDFC Bank Limited,EQ,08-NOV-1995,1,1,INE040A01034,1\n"
      "LT,Larsen & Toubro Limited,EQ,23-JUN-1995,2,1,INE018A01030,2\n")

ART = {
    "a1": ("https://economictimes.indiatimes.com/prime/money/five-stocks/articleshow/1001.cms",
           ["Brokerage XYZ recommends buying Tata Consultancy Services with a target of Rs 4,800 over 12 months.",
            "HDFC Bank is rated accumulate by XYZ as deposit growth improves."]),
    "a2": ("https://economictimes.indiatimes.com/prime/money/picks/articleshow/1002.cms",
           ["Analysts at ABC suggest Tata Consultancy Services is a buy on the AI deal pipeline.",
            "Larsen & Toubro looks expensive and ABC would avoid it for now."]),
}


@pytest.fixture(autouse=True)
def equity_master():
    nse.EQ_FILE.write_text(EQ, encoding="utf-8")
    yield


def _capture(qid, url, passages, cid):
    return CaptureBatch.model_validate({"research_session_id": "s-" + cid, "extension_version": "0.4.0", "items": [
        {"capture_id": cid, "url": url, "title": "ET Prime picks " + cid, "captured_at": "2026-11-15T10:00:00Z",
         "passages": [{"id": f"P{i + 1}", "text": t} for i, t in enumerate(passages)], "usage_attested": True, "queue_id": qid}]})


def _setup_lists_and_capture():
    r = ideas.save_lists(news=["https://economictimes.indiatimes.com/markets/x/articleshow/2001.cms"],
                         ideas=[ART["a1"][0], ART["a2"][0], "https://example.com/not-et"])
    assert r["saved"] == {"news_articles": 1, "idea_articles": 2} and r["rejected"]
    q = {x["canonical_url"]: x["queue_id"] for x in queue_list("pending")}
    for k, (url, ps) in ART.items():
        if url in q:  # first time: captured articles are not re-queued on later saves
            assert ingest_batch(_capture(q[url], url, ps, k))["items"][0]["status"] == "accepted"
    assert ideas.save_lists(news=[], ideas=[ART["a1"][0], ART["a2"][0]])["queued"] == 0   # nothing re-queued
    ideas.save_lists(news=["https://economictimes.indiatimes.com/markets/x/articleshow/2001.cms"],
                     ideas=[ART["a1"][0], ART["a2"][0]])
    st = ideas.lists_status()
    assert [x["state"] for x in st["idea_articles"]] == ["captured", "captured"]
    assert st["news_articles"][0]["state"] == "pending"


class FakeGW:
    """Returns ideas with quotes copied from the passages (verbatim) — like a well-behaved model."""
    def role_cfg(self, role):
        return {"model": "fake-ideas"}

    async def structured(self, *, role, stage, system, user, output_model, semantic_check=None):
        out = []
        if "1001" in user or "XYZ recommends" in user:
            out = [IdeaLLM(company_mention="Tata Consultancy Services", stance="buy", target_price=4800, horizon_text="12 months",
                           recommended_by="Brokerage XYZ", rationale="Buy with target", passage_id="P1",
                           quote="recommends buying Tata Consultancy Services with a target"),
                   IdeaLLM(company_mention="HDFC Bank", stance="accumulate", recommended_by="XYZ", rationale="Deposits",
                           passage_id="P2", quote="HDFC Bank is rated accumulate by XYZ")]
        else:
            out = [IdeaLLM(company_mention="Tata Consultancy Services", stance="buy", recommended_by="ABC", rationale="AI deals",
                           passage_id="P1", quote="Tata Consultancy Services is a buy on the AI deal pipeline"),
                   IdeaLLM(company_mention="Larsen & Toubro", stance="avoid", recommended_by="ABC", rationale="Expensive",
                           passage_id="P2", quote="Larsen & Toubro looks expensive")]
        parsed = IdeasLLM(article_type="stock_recommendations", ideas=out)
        assert not semantic_check(parsed)      # quotes must verify against the captured passages
        return CallResult(parsed=parsed, record={})


def test_lists_capture_extract_rank(monkeypatch):
    _setup_lists_and_capture()
    monkeypatch.setattr("ca_platform.gateway.missing_keys", lambda roles: [])
    monkeypatch.setattr("ca_platform.prompting.system_prompt", lambda role: ("SYS", "ideas@1.0.0"))
    res = asyncio.run(ideas.extract(FakeGW()))
    assert res["processed"] == 2 and not res["errors"]
    get_ledger().execute("INSERT OR REPLACE INTO bhav_daily VALUES ('TCS','EQ','2026-11-13',4000,4000,4050,3980,4000,1,1,1,'t')")
    r = asyncio.run(ideas.rank(5))
    syms = [x["symbol"] for x in r["top"]]
    assert syms[0] == "TCS"                                   # positive in two articles
    assert syms[-1] == "LT" and r["top"][-1]["negative_articles"] == 1
    tcs = r["top"][0]
    assert tcs["positive_articles"] == 2 and tcs["upside_pct"] == 20.0     # 4800 vs bhav close 4000
    assert all(m["quote"] for m in tcs["mentions"])
    again = asyncio.run(ideas.extract(FakeGW()))
    assert again["skipped_already_done"] == 2                # cached per source version


def test_heuristic_fallback_without_model():
    _setup_lists_and_capture()
    res = asyncio.run(ideas.extract(None, force=True))
    assert "heuristic" in res["method"]
    r = asyncio.run(ideas.rank(10))
    assert {x["symbol"] for x in r["top"]} >= {"TCS"}
    assert all(m["stance"] == "neutral_mention" for x in r["top"] for m in x["mentions"])


def test_demo_batch_runs_each_pick_and_writes_report():
    async def go():
        bid = ideas.start_batch(["TCS", "HDFCBANK"], "medium", 1, "demo", ideas_snapshot=[{"symbol": "TCS", "why": "x", "score": 6}])
        for _ in range(240):
            await asyncio.sleep(0.5)
            b = ideas.get_batch(bid)
            if b["status"] == "completed":
                return b
        raise AssertionError("batch did not finish")
    b = asyncio.run(go())
    assert len(b["items"]) == 2 and all(i.get("verdict") for i in b["items"])
    assert b["report_path"] and open(b["report_path"], encoding="utf-8").read().startswith("# Batch report")


def test_extension_heartbeat_shows_connected():
    from fastapi.testclient import TestClient
    from ca_platform.service import app, pairing_token
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    assert c.get("/api/health").json()["extension"]["connected"] in (False, True)
    r = c.get("/v1/research/queue", headers={"x-ca-token": pairing_token(), "origin": "chrome-extension://abcdef",
                                            "x-ca-ext-version": "0.5.0", "x-ca-ext-id": "abcdef"})
    assert r.status_code == 200
    ext = c.get("/api/health").json()["extension"]
    assert ext["connected"] and ext["version"] == "0.5.0" and ext["id"] == "abcdef"


def test_company_matching_short_names_and_bylines():
    nse.EQ_FILE.write_text(EQ + "DLF,DLF Limited,EQ,05-JUL-2007,2,1,INE271C01023,2\n"
                                "VIPULLTD,Vipul Limited,EQ,23-APR-2015,1,1,INE946H01037,1\n", encoding="utf-8")
    r = nse.resolve_companies("DLF Ltd | 7 | Buy | 22 | 9%. HDFC Bank rises.")
    assert {c["symbol"] for c in r["resolved"]} == {"DLF", "HDFCBANK"}
    assert not nse.resolve_companies("The dlf brand alone is not enough")["resolved"]   # needs a Ltd/Limited suffix
    rec = ingest_batch(_capture(None, "https://economictimes.indiatimes.com/z/articleshow/77.cms",
                                ["Godrej and DLF Ltd lead the realty pack.", "Disclaimer: This article has been written by Vipul Kumar."],
                                "byline"))
    syms = {c["symbol"] for c in rec["items"][0]["companies"]["resolved"]}
    assert "DLF" in syms and "VIPULLTD" not in syms


def test_topic_url_from_equity_master():
    assert ideas.topic_url("TCS") == "https://economictimes.indiatimes.com/topic/tata-consultancy-services"
    assert ideas.topic_url("NOPE") is None


# --------------------------------------------------------------------- Kite MCP ---

def test_kite_mcp_policy_is_tool_plus_mode(monkeypatch):
    monkeypatch.setattr(kite_mcp, "_load_token", lambda: {"access_token": "x"})
    refused = [("orders", {"mode": "place"}), ("orders", {"mode": "list"}), ("gtt", {"mode": "create"}),
               ("alerts", {"mode": "list"}), ("session", {"mode": "logout"}), ("portfolio", {"mode": "convert"}),
               ("market", {}), ("help", {"key": "/"})]
    for tool, args in refused:
        with pytest.raises(PermissionError):
            asyncio.run(kite_mcp.call(tool, args))
    for tool, mode in [("portfolio", "holdings"), ("market", "historical"), ("market", "ltp"), ("session", "status")]:
        assert kite_mcp.allowed(tool, {"mode": mode})


def test_kite_mcp_rejects_unknown_args_after_schema_change():
    kite_mcp._TOOLS_CACHE["tools"] = {"market": {"properties": {"mode": {}, "instrument_token": {}, "interval": {}}}}
    kite_mcp._check_args("market", {"mode": "historical", "instrument_token": 1, "interval": "day"})
    with pytest.raises(kite_mcp.KiteMCPError):
        kite_mcp._check_args("market", {"mode": "historical", "token_v2": 1})


def test_kite_mcp_parse_and_login_url(monkeypatch):
    res = types.SimpleNamespace(is_error=False, structured_content=None,
                                content=[types.SimpleNamespace(type="text", text=json.dumps({"data": [{"tradingsymbol": "TCS"}]}))])
    assert kite_mcp._data(kite_mcp._parse(res)) == [{"tradingsymbol": "TCS"}]
    err = types.SimpleNamespace(is_error=True, structured_content=None, content=[types.SimpleNamespace(type="text", text="boom")])
    with pytest.raises(kite_mcp.KiteMCPError):
        kite_mcp._parse(err)
    monkeypatch.setattr(kite_mcp, "_client_id", lambda uri: "client-123")
    url = kite_mcp.start_login("http://127.0.0.1:8765/kite-mcp/callback")
    q = parse_qs(urlparse(url).query)
    assert url.startswith("https://mcp.kite.trade/authorize?")
    assert q["code_challenge_method"] == ["S256"] and q["client_id"] == ["client-123"] and q["state"][0] in kite_mcp._PENDING
    with pytest.raises(kite_mcp.KiteMCPError):
        kite_mcp.complete_login("code", "unknown-state")
