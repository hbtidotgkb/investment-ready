"""Security boundaries (spec §16, N14) and browser-intake negative cases (spec §7 acceptance test)."""
import pytest
from fastapi.testclient import TestClient

from ca_platform.contracts import CaptureBatch
from ca_platform.data.kite import ReadOnlyKite
from ca_platform.inbox import ingest_batch
from ca_platform.service import app, pairing_token


class FakeKite:
    def holdings(self):
        return []

    def place_order(self, **kw):
        raise AssertionError("must never be reachable")


def test_kite_wrapper_is_read_only():
    k = ReadOnlyKite(FakeKite())
    assert k.holdings() == []
    for op in ("place_order", "modify_order", "cancel_order", "place_gtt", "exit_order"):
        with pytest.raises(PermissionError):
            getattr(k, op)
    with pytest.raises(PermissionError):
        k.anything = 1


def _batch(text="Alpha. Beta gamma delta.", url="https://economictimes.indiatimes.com/prime/x/articleshow/1.cms?utm_source=a",
           cid="c1", attested=True, sid="rs-1"):
    return CaptureBatch.model_validate({
        "research_session_id": sid, "extension_version": "0.2.0",
        "items": [{"capture_id": cid, "url": url, "title": "T", "captured_at": "2026-11-15T10:00:00Z",
                   "passages": [{"id": "P1", "text": text}], "usage_attested": attested}]})


def test_intake_accept_duplicate_version_reject():
    r1 = ingest_batch(_batch())
    it = r1["items"][0]
    assert it["status"] == "accepted" and it["version"] == 1
    assert ingest_batch(_batch())["replayed"] is True                       # identical batch => same receipt
    r_dup = ingest_batch(_batch(cid="c2", sid="rs-2"))["items"][0]          # same content, new batch => duplicate
    assert r_dup["status"] == "duplicate" and r_dup["source_id"] == it["source_id"]
    r_v2 = ingest_batch(_batch(text="Alpha. Changed text now.", cid="c3", sid="rs-3"))["items"][0]
    assert r_v2["status"] == "accepted" and r_v2["version"] == 2           # changed content => new version, no overwrite
    bad = ingest_batch(_batch(url="javascript:alert(1)", cid="c4", sid="rs-4"))["items"][0]
    assert bad["status"] == "rejected"
    big = _batch(cid="c5", sid="rs-5")
    big.items[0].passages = [big.items[0].passages[0].model_copy(update={"id": f"P{i}", "text": f"{i} " + "x" * 19000})
                             for i in range(5)]
    huge = ingest_batch(big)["items"][0]
    assert huge["status"] == "rejected" and "limit" in huge["reason"]


def test_tracking_params_stripped_from_identity():
    a = ingest_batch(_batch(text="Unique one.", url="https://economictimes.indiatimes.com/a.cms?utm_source=x", sid="u1", cid="u1"))
    b = ingest_batch(_batch(text="Unique one.", url="https://economictimes.indiatimes.com/a.cms?utm_medium=y", sid="u2", cid="u2"))
    assert a["items"][0]["source_id"] == b["items"][0]["source_id"]


def test_unattested_capture_is_stored_but_marked_not_for_model():
    r = ingest_batch(_batch(text="Private.", url="https://economictimes.indiatimes.com/p.cms", attested=False, sid="p", cid="p"))
    assert r["items"][0]["usage_status"] == "unattested_not_for_model"


@pytest.fixture
def client():
    return TestClient(app, base_url="http://127.0.0.1:8765")


def test_service_requires_loopback_host(client):
    assert client.get("/v1/health").status_code == 200
    evil = TestClient(app, base_url="http://attacker.example")
    assert evil.get("/v1/health").status_code == 421


def test_mutations_require_token_and_extension_is_scoped(client):
    body = _batch(text="Token test.", sid="t", cid="t").model_dump()
    assert client.post("/v1/research/captures", json=body).status_code == 401
    ok = client.post("/v1/research/captures", json=body, headers={"x-ca-token": pairing_token()})
    assert ok.status_code == 200 and ok.json()["items"][0]["status"] == "accepted"
    ext = {"origin": "chrome-extension://abc", "x-ca-token": pairing_token()}
    assert client.post("/api/runs", json={"mode": "demo"}, headers=ext).status_code == 403   # extension can't start runs via /api
    web = {"origin": "https://evil.example", "x-ca-token": pairing_token()}
    assert client.post("/v1/research/captures", json=body, headers=web).status_code == 403


def test_oversized_payload_rejected(client):
    r = client.post("/v1/research/captures", content=b"x" * 10, headers={"x-ca-token": pairing_token(),
                                                                         "content-length": str(5_000_000)})
    assert r.status_code == 413
