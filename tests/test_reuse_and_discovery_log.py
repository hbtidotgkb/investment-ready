"""Cross-day report reuse (reuse.py) and incremental discovery (discovery_log.py). Offline."""
import uuid

from ca_platform import discovery_log as DL
from ca_platform import reuse as RU
from ca_platform.contracts import BrainReportLLM, Bundle, Cite, Statement
from ca_platform.evidence import EvidenceFactory, from_discovery
from ca_platform.ledger import get_ledger

AUDIT = "The auditor issued an unmodified opinion on the financial statements for FY26."
PLEDGE = "Promoters have pledged no shares as of the September 2026 quarter."


def bundle(day: str, symbol: str, pe: str = "P/E is 24.1 on the last close.", shift: int = 0,
           announcement: str | None = None, audit: str = AUDIT) -> Bundle:
    fac = EvidenceFactory()
    for i in range(shift):   # extra volatile items earlier in the bundle shift every id
        fac.add(kind="news_summary", title=f"Summary {i}", publisher="newsdesk", tier="SYS_computed", passages=[f"summary {day} {i}"],
                retrieved_via="news")
    fac.add(kind="computed_fundamental", title="Valuation", publisher="code", tier="SYS_computed", passages=[pe],
            retrieved_via="filings")
    fac.add(kind="exchange_filing", title="Annual report FY26", publisher="nseindia.com", tier="T1_official",
            passages=[audit, PLEDGE], retrieved_via="filings")
    if announcement:
        fac.add(kind="exchange_announcement", title="Board outcome", publisher="nseindia.com", tier="T1_official",
                passages=[announcement], retrieved_via="filings")
    b = Bundle(mode="live", symbol=symbol, company_name=symbol, decision_at=f"{day}T20:00:00+05:30",
               horizon="medium", action="new_purchase", evidence=fac.items)
    b.bundle_id = uuid.uuid4().hex
    return b


def pid(b: Bundle, text: str) -> str:
    return next(p.id for e in b.evidence for p in e.passages if p.text == text)


def report(b: Bundle, cite_pe: bool = False) -> BrainReportLLM:
    a = pid(b, AUDIT)
    cites = [Cite(evidence_id=a.split(":")[0], passage_id=a, quote="unmodified opinion")]
    if cite_pe:
        p = next(p.id for e in b.evidence if e.kind == "computed_fundamental" for p in e.passages)
        cites.append(Cite(evidence_id=p.split(":")[0], passage_id=p, quote="P/E"))
    return BrainReportLLM(specialist_conclusion="neutral", headline=f"Clean audit (see {a}).",
                          statements=[Statement(kind="observation", text=f"Unmodified opinion per {a}.", cites=cites)],
                          checks=[], red_flags=[], missing_evidence=[], stances=[], scenarios=[], would_change_mind=[])


def ok(_rep):
    return []


def test_forensic_report_reused_when_only_prices_and_ids_changed():
    led, sym = get_ledger(), "REUSE1"
    b1 = bundle("2026-11-20", sym)
    RU.store(led, "forensic", b1, "run-day1", "SYS", report(b1), "claude-opus-5-5")
    b2 = bundle("2026-11-23", sym, pe="P/E is 22.7 on the last close.", shift=2)   # price moved, ids shifted
    hit = RU.find(led, "forensic", b2, "SYS", ok)
    assert hit is not None
    rep, info = hit
    new_audit = pid(b2, AUDIT)
    assert new_audit != pid(b1, AUDIT)                                     # ids really moved
    assert rep.statements[0].cites[0].passage_id == new_audit             # citation re-pointed to identical text
    assert new_audit in rep.headline and new_audit in rep.statements[0].text   # prose ids re-pointed too
    assert info["run_id"] == "run-day1"


def test_not_reused_when_a_new_filing_arrived():
    led, sym = get_ledger(), "REUSE2"
    b1 = bundle("2026-11-20", sym)
    RU.store(led, "forensic", b1, "run-a", "SYS", report(b1), "m")
    b2 = bundle("2026-11-23", sym, announcement="Board approved a related-party transaction with a promoter firm.")
    assert RU.find(led, "forensic", b2, "SYS", ok) is None


def test_not_reused_when_a_cited_passage_changed():
    led, sym = get_ledger(), "REUSE3"
    b1 = bundle("2026-11-20", sym)
    RU.store(led, "forensic", b1, "run-a", "SYS", report(b1, cite_pe=True), "m")
    b2 = bundle("2026-11-23", sym, pe="P/E is 22.7 on the last close.")   # the cited P/E line is different today
    assert RU.find(led, "forensic", b2, "SYS", ok) is None


def test_not_reused_when_prompt_changed_too_old_or_validator_fails():
    led, sym = get_ledger(), "REUSE4"
    b1 = bundle("2026-11-01", sym)
    RU.store(led, "forensic", b1, "run-a", "SYS", report(b1), "m")
    assert RU.find(led, "forensic", bundle("2026-11-23", sym), "SYS", ok) is None          # 22 days > 14
    b3 = bundle("2026-11-05", sym)
    assert RU.find(led, "forensic", b3, "DIFFERENT SYSTEM PROMPT", ok) is None
    assert RU.find(led, "forensic", b3, "SYS", lambda r: ["defect"]) is None
    assert RU.find(led, "forensic", b3, "SYS", ok) is not None


def test_only_configured_roles_and_live_mode_reuse():
    led, sym = get_ledger(), "REUSE5"
    b1 = bundle("2026-11-20", sym)
    RU.store(led, "quant", b1, "run-a", "SYS", report(b1), "m")          # quant not configured -> not stored
    assert RU.find(led, "quant", bundle("2026-11-21", sym), "SYS", ok) is None
    demo = bundle("2026-11-20", sym).model_copy(update={"mode": "demo"})
    RU.store(led, "forensic", demo, "run-d", "SYS", report(demo), "m")
    assert led.fetchone("SELECT COUNT(*) AS n FROM report_reuse WHERE symbol=? AND run_id='run-d'", [sym])["n"] == 0


def cite(url, text, title="T"):
    return {"url": url, "title": title, "cited_text": text, "claim": "c"}


def test_discovery_plan_full_then_incremental_chain():
    led, sym = get_ledger(), "DISC1"
    assert DL.plan(led, sym, "news", "2026-11-20T10:00")["mode"] == "full"
    DL.record(led, sym, "news", "2026-11-20T10:00", "full", {"citations": [cite("https://a.com/1", "old fact")],
                                                              "results": [], "record": {"x": 1}})
    assert DL.plan(led, sym, "news", "2026-11-20T18:00")["mode"] == "full"     # same day never uses today as history
    p = DL.plan(led, sym, "news", "2026-11-22T10:00")
    assert p["mode"] == "incremental" and p["since"] == "2026-11-20" and p["base"] == "2026-11-20"
    note = DL.incremental_note(p, "2026-11-22")
    assert "after 2026-11-19" in note and "https://a.com/1" in note
    DL.record(led, sym, "news", "2026-11-22T10:00", "incremental", {"citations": [cite("https://b.com/2", "new fact")],
                                                                     "results": []})
    p3 = DL.plan(led, sym, "news", "2026-11-23T10:00")
    assert p3["since"] == "2026-11-22" and len(p3["prior"]) == 2
    assert DL.plan(led, sym, "news", "2026-11-23T10:00", loop=2)["mode"] == "full"   # targeted loop: full prompt
    assert DL.plan(led, sym, "news", "2026-12-10T10:00")["mode"] == "full"           # last full > 14 days ago


def test_discovery_merge_carries_earlier_sources_forward_once():
    p = {"prior": [("2026-11-20", {"citations": [cite("https://a.com/1", "old fact"), cite("https://a.com/1", "old fact")],
                                   "results": [{"url": "https://a.com/1", "title": "A"}]})]}
    out, first = DL.merged(p, {"citations": [cite("https://a.com/1", "old fact"), cite("https://b.com/2", "new fact")],
                               "results": []}, "2026-11-23")
    assert [c["cited_text"] for c in out["citations"]] == ["old fact", "new fact"]
    assert first == {"https://a.com/1": "2026-11-20", "https://b.com/2": "2026-11-23"}
    fac = EvidenceFactory()
    made = from_discovery(fac, out, "news", "2026-11-23T10:00", first)
    old = next(e for e in made if e.url == "https://a.com/1")
    new = next(e for e in made if e.url == "https://b.com/2")
    assert old.first_seen_at == "2026-11-20" and "carried forward" in old.notes
    assert new.first_seen_at == "2026-11-23T10:00" and "carried forward" not in new.notes
