"""Newsdesk module: taxonomy, matching, merging, metric-aware claim checks, independence,
point-in-time dossiers and bundle evidence routing. Offline — no network."""
from datetime import timedelta

from ca_platform.contracts import Bundle
from ca_platform.evidence import EvidenceFactory
from ca_platform.newsdesk import dossier, pipeline, taxonomy, watch
from ca_platform.newsdesk.fetch import RawItem
from ca_platform.prompting import brain_view
from ca_platform.settings import iso, now_ist

T0 = now_ist() - timedelta(days=2)


def at(hours: float) -> str:
    return (T0 + timedelta(hours=hours)).isoformat()


def setup_stock(sym="ACME"):
    watch.upsert({"symbol": sym, "name": "Acme Infra Limited", "aliases": ["Acme Infra"], "link_only": ["Acme"],
                  "exclude": ["Acme Power"], "market_cap_cr": 20000, "watch": True})


def filing(title, body, h, cat=None, sym="ACME"):
    return RawItem("nse_filing", f"https://nsearchives.nseindia.com/corporate/{sym}_{abs(hash(title)) % 10**8}.pdf", title, body,
                   "NSE", "T1_official", at(h), cat, False, (sym,), "test")


def press(title, h, pub="Business Standard", tier="T2_reputable_press", body="", url=None):
    return RawItem("press", url or f"https://news.example/{abs(hash(title + pub)) % 10**9}", title, body, pub, tier, at(h),
                   None, not body, (), "test")


def test_taxonomy_metrics_and_opinion():
    lab = taxonomy.label("Acme Infra bags Rs 2,100 crore order from NHAI", "", None, False)
    assert lab["event_type"] == "order_win" and lab["amount_cr"] == 2100 and lab["metric"] == "order_value"
    assert taxonomy.label("Acme Infra Q2 net profit rises 45% to Rs 320 crore")["metric"] == "profit"
    assert taxonomy.classify("Buy Acme Infra; target of Rs 2530: Geojit") == "analyst_view"
    assert taxonomy.classify("Acme Infra Price Target 2026: Q2 FY27 drivers") == "analyst_view"
    assert taxonomy.fact_type("Acme shares jump 4% after Q2 profit rises 45%") == "fact"     # reported figure = fact
    assert taxonomy.classify("Clarification on news item", official=False) != "clarification"  # only a filing clarifies


def test_remove_keeps_names_and_readd_restores():
    watch.upsert({"symbol": "RMV", "name": "Remove Me Ltd", "aliases": ["Remove Me"], "exclude": ["Remove Me Not"], "watch": True})
    assert any(e["symbol"] == "RMV" for e in watch.watchlist())
    watch.remove("RMV")
    assert not any(e["symbol"] == "RMV" for e in watch.watchlist())      # no background refresh
    back = watch.upsert({"symbol": "RMV", "watch": True})
    assert back["exclude"] == ["Remove Me Not"] and back["aliases"] == ["Remove Me"]


def test_progress_reports_steps(monkeypatch):
    import asyncio
    from ca_platform.newsdesk import progress

    def fake_refresh(sym, log, days=None):
        log("NSE announcements: 3 found · 1 new filing PDF(s) to read this time")
        log("Reading filing PDF 1 of 1: Outcome of Board Meeting")
        return {"symbol": sym, "sources": {}, "failed": []}
    monkeypatch.setattr(progress.pipeline, "refresh", fake_refresh)

    async def go():
        t = progress.start("PRG", who="test")
        assert progress.get("PRG")["running"]
        assert progress.start("PRG") is t               # one refresh per stock at a time
        await t
        return progress.public(progress.get("PRG"))
    p = asyncio.run(go())
    msgs = [s["m"] for s in p["steps"]]
    assert not p["running"] and p["finished"] and "_task" not in p
    assert any("Reading filing PDF 1 of 1" in m for m in msgs) and msgs[-1].startswith("Done")


def test_salience_excludes_lookalikes():
    setup_stock()
    e = watch.get("ACME")
    assert watch.salience(e, "Acme Infra wins order") == 1.0
    assert watch.salience(e, "Acme Power slumps 5%") == 0.0          # excluded look-alike
    assert watch.salience(e, "ACME shares up") == 0.0                # link_only is case-sensitive
    assert watch.salience(e, "Acme shares up") == 1.0


def test_metric_aware_claim_checks_independence_and_separation():
    setup_stock()
    pipeline.ingest("ACME", [
        filing("Award of Order: Acme Infra Limited has informed the Exchange regarding receipt of order",
               "The Company has received a letter of award worth Rs 2,100 crore from NHAI for a highway project.", 0,
               "Award of Order / Receipt of Order"),
        press("Acme Infra bags Rs 2,100 crore order from NHAI", 2),
        press("Acme Infra bags Rs 2,100 crore order from NHAI", 3, pub="Mint"),                 # syndicated copy
        press("Acme Infra wins Rs 2,600 crore NHAI highway order", 4, pub="Moneycontrol"),      # wrong figure
        press("Acme Infra revenue seen at Rs 9,000 crore on new NHAI order", 5, pub="CNBC-TV18"),  # different metric
        filing("Updates: Acme Infra Limited has informed the Exchange regarding Press Release - new metro project launch",
               "Acme Infra launches its metro rail systems unit.", 30),
        filing("Updates: Acme Infra Limited has informed the Exchange regarding Press Release - credit rating reaffirmed by CRISIL",
               "CRISIL reaffirmed the long-term rating.", 31),
    ])
    d = dossier.build("ACME", 30)
    order = next(e for e in d["events"] if e["type"] == "order_win")
    st = {c["text"][:40]: c["status"] for c in order["claims"]}
    assert "filed" in st.values()
    assert any(v == "confirmed" for k, v in st.items() if "2,100" in k or "2100" in k)
    assert any(v == "contradicted" for k, v in st.items() if "2,600" in k)
    assert order["status"] == "contradicted"
    # a REVENUE figure is never compared with the filed ORDER value (spec cross-check rule 6)
    rev = [c for e in d["events"] for c in e["claims"] if c["amount_cr"] == 9000]
    assert rev and all(c["metric"] == "revenue" and c["status"] not in ("confirmed", "contradicted") for c in rev)
    # two outlets with the same headline are one story; the 2,600 piece is a second, differently worded story
    assert order["press_articles"] == 3 and order["independent_press_stories"] == 2
    # two different press-release filings stay separate events (boilerplate subject is ignored)
    assert len([e for e in d["events"] if e["filings"] and e["type"] != "order_win"]) == 2
    assert d["check_first"]


def test_denial_only_via_clarification_filing():
    setup_stock("DENY")
    watch.upsert({"symbol": "DENY", "name": "Deny Corp Limited", "aliases": ["Deny Corp"], "watch": True})
    pipeline.ingest("DENY", [
        press("Deny Corp in talks to acquire rival, sources say", 1),
        filing("Clarification: Deny Corp Limited clarifies news item on acquisition talks",
               "With reference to the news item on acquisition talks, the company denies any such proposal; the report is baseless.",
               20, "Clarification", sym="DENY"),
    ])
    e = dossier.build("DENY", 30)["events"][0]
    assert e["status"] == "denied" and e["confidence"] < 0.3


def test_point_in_time_and_unknown_dates():
    setup_stock("PIT")
    watch.upsert({"symbol": "PIT", "name": "Pit Labs Limited", "aliases": ["Pit Labs"], "watch": True})
    early = press("Pit Labs wins Rs 500 crore defence order", 1)
    future = press("Pit Labs board approves Rs 300 crore QIP", 24 * 5)          # 'published' after now: never visible yet
    nodate = RawItem("press", "https://news.example/pit-nodate", "Pit Labs appoints new CFO", "", "Mint",
                     "T2_reputable_press", None, None, True, (), "test")
    pipeline.ingest("PIT", [early, future, nodate])
    from ca_platform.ledger import get_ledger
    row = get_ledger().fetchone("SELECT published_at FROM nd_articles WHERE url=?", [nodate.url])
    assert row["published_at"] is None                                          # never back-filled with "now"
    titles = {e["title"] for e in dossier.build("PIT", 30)["events"]}
    assert titles == {"Pit Labs wins Rs 500 crore defence order", "Pit Labs appoints new CFO"}
    # actual-system point in time: before these articles were first seen, the dossier is empty
    assert dossier.build("PIT", 30, as_of=at(10))["events"] == []


def test_dossier_to_evidence_and_brain_routing():
    setup_stock()
    d = dossier.build("ACME", 30)
    fac = EvidenceFactory()
    out = dossier.to_evidence(fac, d, candles=[{"date": at(-30)[:10], "close": 100.0}, {"date": iso()[:10], "close": 110.0}])
    kinds = {e.kind for e in fac.items}
    assert {"exchange_announcement", "news_article", "news_summary"} <= kinds and out["evidence_items"] >= 5
    summ = next(e for e in fac.items if e.kind == "news_summary")
    assert any("status:" in p.text and "independent press" in p.text for p in summ.passages)
    assert any("price moved +10.0%" in p.text for p in summ.passages)
    # every news_article passage is verbatim source text (headline), not our annotation
    assert all(not p.text.startswith("Event ") for e in fac.items if e.kind == "news_article" for p in e.passages)
    b = Bundle(mode="live", symbol="ACME", company_name="Acme Infra", decision_at=iso(), horizon="medium",
               action="new_purchase", evidence=fac.items)
    q_view, _ = brain_view("quant", b, [], [])
    f_view, _ = brain_view("forensic", b, [], [])
    n_view, _ = brain_view("news", b, [], [])
    assert "Newsdesk summary" in q_view and "Newsdesk summary" in f_view and "Newsdesk summary" in n_view
    assert "bags Rs 2,100 crore order" in n_view
    assert "bags Rs 2,100 crore order" not in q_view and "bags Rs 2,100 crore order" not in f_view   # press stays with News
    assert "letter of award" in f_view                                                                # filings reach Forensic
