"""L1 contract evals: citation verification and code-side objection resolution."""
from ca_platform.citations import CitationIndex, audit
from ca_platform.contracts import (Bundle, Cite, CritiqueLLM, Evidence, HeadDecisionLLM, HeadFinalLLM, Objection,
                                   ObjectionResponse, Passage, SettlingTest, Statement)
from ca_platform.panel import resolve


def _bundle():
    ev = Evidence(id="ev-1", kind="exchange_filing", title="Q2 results", publisher="NSE", tier="T1_official",
                  first_seen_at="2026-11-15T10:00:00+05:30", ingested_at="2026-11-15T10:00:00+05:30",
                  passages=[Passage(id="ev-1:P1", text="Net profit attributable to owners rose 12% to Rs 1,234 crore."),
                            Passage(id="ev-1:P2", text="The auditor issued an unmodified opinion.")])
    return Bundle(mode="demo", symbol="X", company_name="X Ltd", decision_at="2026-11-15T15:00:00+05:30",
                  horizon="medium", action="new_purchase", evidence=[ev])


def test_citation_statuses():
    idx = CitationIndex(_bundle())
    assert idx.check(Cite(evidence_id="ev-1", passage_id="ev-1:P1", quote="rose 12% to Rs 1234 crore")) == "verified"
    assert idx.check(Cite(evidence_id="ev-1", passage_id="ev-1:P1", quote="rose 15% to Rs 1,234 crore")) == "quote_mismatch"
    assert idx.check(Cite(evidence_id="ev-1", passage_id="ev-1:P1")) == "id_only"
    assert idx.check(Cite(evidence_id="ev-9", passage_id=None, quote="anything")) == "unknown_evidence"
    assert idx.check(Cite(evidence_id="ev-1", passage_id="ev-1:P7", quote="x y z w")) == "unknown_passage"
    # quote from the wrong passage of the same evidence is a mismatch
    assert idx.check(Cite(evidence_id="ev-1", passage_id="ev-1:P1", quote="unmodified opinion")) == "quote_mismatch"


def test_audit_flags_uncited_conclusions_and_bad_cites():
    idx = CitationIndex(_bundle())
    d = HeadDecisionLLM(decision="wait", summary="s", evidence_sufficiency="partial", assumptions=[], entry_conditions=[],
                        invalidation_conditions=[], material_dissent=[], portfolio_limitations=[],
                        reasons=[Statement(kind="conclusion", text="uncited", cites=[]),
                                 Statement(kind="observation", text="bad", cites=[Cite(evidence_id="ev-1", passage_id="ev-1:P2", quote="qualified opinion")])])
    a = audit(d, idx)
    assert a["uncited_material"] == 1
    assert a["counts"]["quote_mismatch"] == 1
    assert len(a["defects"]) == 2


def _final(responses):
    d = HeadDecisionLLM(decision="wait", summary="s", evidence_sufficiency="partial", reasons=[], assumptions=[],
                        entry_conditions=[], invalidation_conditions=[], material_dissent=[], portfolio_limitations=[])
    return HeadFinalLLM(responses=responses, final=d)


def _critique():
    return CritiqueLLM(checklist=["facts"], objections=[
        Objection(id="obj-1", severity="blocker", type="factual", text="t", cites=[], decision_impact="i",
                  settling_test=SettlingTest(description="d")),
        Objection(id="obj-2", severity="blocker", type="factual", text="t", cites=[], decision_impact="i",
                  settling_test=SettlingTest(description="d")),
        Objection(id="obj-3", severity="major", type="logic", text="t", cites=[], decision_impact="i",
                  settling_test=SettlingTest(description="d"))])


def test_rejection_without_verified_citation_is_unresolved():
    idx = CitationIndex(_bundle())
    fin = _final([
        ObjectionResponse(objection_id="obj-1", action="reject", test_result="passed", explanation="I disagree",
                          cites=[], decision_effect="none"),
        ObjectionResponse(objection_id="obj-2", action="reject", test_result="passed", explanation="Filing shows it",
                          cites=[Cite(evidence_id="ev-1", passage_id="ev-1:P2", quote="issued an unmodified opinion")],
                          decision_effect="none"),
    ])
    r = resolve(_critique(), fin, idx)
    eff = {row["objection"]["id"]: row["effective_result"] for row in r["rows"]}
    assert eff == {"obj-1": "unresolved", "obj-2": "passed", "obj-3": "unresolved"}  # obj-3 had no response
    assert r["open_blockers"] == ["obj-1"]
    assert r["open_major"] == ["obj-3"]
