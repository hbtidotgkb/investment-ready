"""Filed financials (NSE XBRL): parsing, point-in-time filing choice, ratios computed in code,
reconciliation-gated verification, pledge/audit rules, routing. Offline — synthetic XBRL, no network."""
from datetime import date, timedelta
from xml.sax.saxutils import escape

from ca_platform.contracts import Bundle, FinancialFact
from ca_platform.data import nse_xbrl as X
from ca_platform.evidence import EvidenceFactory
from ca_platform.features import fundamentals as F
from ca_platform.prompting import brain_view
from ca_platform.rules import engine as R

NS = 'xmlns="http://www.xbrl.org/2003/instance" xmlns:in-capmkt="http://x/capmkt" xmlns:xbrldi="http://xbrl.org/2006/xbrldi"'


def _ctx(cid, start=None, end=None, instant=None, member=None, typed=False):
    seg = ""
    if member or typed:
        seg = "<segment>" + (f'<xbrldi:explicitMember dimension="in-capmkt:Axis">in-capmkt:{member}</xbrldi:explicitMember>' if member else "") \
              + ('<xbrldi:typedMember dimension="in-capmkt:T"><x>1</x></xbrldi:typedMember>' if typed else "") + "</segment>"
    per = f"<instant>{instant}</instant>" if instant else f"<startDate>{start}</startDate><endDate>{end}</endDate>"
    return f'<context id="{cid}"><entity><identifier scheme="x">X</identifier>{seg}</entity><period>{per}</period></context>'


def results_xbrl(q_end: date, vals: dict, bs: dict | None = None, ytd: dict | None = None, text: dict | None = None,
                 auditors=("B S R & Co. LLP",), rpt=(), fy_start: date | None = None) -> bytes:
    q_start = date(q_end.year, q_end.month - 2, 1)
    fy_start = fy_start or date(q_end.year if q_end.month > 3 else q_end.year - 1, 4, 1)
    parts = [_ctx("OneD", q_start, q_end), _ctx("FourD", fy_start, q_end), _ctx("OneI", instant=q_end),
             _ctx("PY_I", instant=date(fy_start.year, 3, 31))]
    for i, _ in enumerate(auditors, 1):
        parts.append(_ctx(f"D_Auditor{i}", q_start, q_end, typed=True))
    for i, _ in enumerate(rpt, 1):
        parts.append(_ctx(f"D_RelatedPartyTransaction{i}", q_start, q_end, typed=True))
    facts = [f'<in-capmkt:{k} contextRef="OneD" unitRef="INR" decimals="-7">{v}</in-capmkt:{k}>' for k, v in vals.items()]
    facts += [f'<in-capmkt:{k} contextRef="FourD" unitRef="INR" decimals="-7">{v}</in-capmkt:{k}>' for k, v in (ytd or {}).items()]
    facts += [f'<in-capmkt:{k} contextRef="OneI" unitRef="INR" decimals="-7">{v}</in-capmkt:{k}>' for k, v in (bs or {}).items()]
    facts += [f'<in-capmkt:{k} contextRef="OneD">{v}</in-capmkt:{k}>' for k, v in (text or {}).items()]
    facts += [f'<in-capmkt:AuditorsFirmName contextRef="D_Auditor{i}">{escape(a)}</in-capmkt:AuditorsFirmName>'
              for i, a in enumerate(auditors, 1)]
    facts += [f'<in-capmkt:AmountOfRelatedPartyTransactionDuringTheReportingPeriod contextRef="D_RelatedPartyTransaction{i}" '
              f'unitRef="INR" decimals="-7">{a}</in-capmkt:AmountOfRelatedPartyTransactionDuringTheReportingPeriod>'
              for i, a in enumerate(rpt, 1)]
    return f"<?xml version='1.0'?><xbrl {NS}>{''.join(parts)}{''.join(facts)}</xbrl>".encode()


CR = 10_000_000


def nonbank_q(q_end: date, rev: float, eps: float, pat: float, with_bs: bool, audited="Audited", recon_ok=True,
              exceptional=0.0, auditors=("B S R & Co. LLP",)):
    oi = 10.0
    pbt = pat / 0.75
    tax = pbt - pat
    vals = {"RevenueFromOperations": rev * CR, "OtherIncome": oi * CR, "Income": (rev + oi + (0 if recon_ok else 500)) * CR,
            "ProfitBeforeTax": pbt * CR, "TaxExpense": tax * CR, "ProfitLossForPeriodFromContinuingOperations": pat * CR,
            "ProfitLossForPeriod": pat * CR, "ProfitOrLossAttributableToOwnersOfParent": pat * CR,
            "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations": eps, "ExceptionalItemsBeforeTax": exceptional * CR,
            "PaidUpValueOfEquityShareCapital": 100 * CR, "FaceValueOfEquityShareCapital": 1}
    bs = {"EquityAttributableToOwnersOfParent": 4000 * CR, "Equity": 4100 * CR, "TradeReceivablesCurrent": 900 * CR,
          "BorrowingsCurrent": 200 * CR, "BorrowingsNoncurrent": 300 * CR} if with_bs else None
    ytd = {"CashFlowsFromUsedInOperatingActivities": 2 * pat * 0.9 * CR, "ProfitLossForPeriod": 2 * pat * CR} if with_bs else None
    text = {"WhetherResultsAreAuditedOrUnaudited": audited, "NatureOfReportStandaloneConsolidated": "Consolidated",
            "DeclarationOfUnmodifiedOpinionOrStatementOnImpactOfAuditQualification": "Declaration of unmodified opinion"}
    p = X.parse_results(results_xbrl(q_end, vals, bs, ytd, text, auditors=auditors, rpt=(5 * CR, 7 * CR)))
    p["filing"] = {"broadcast_at": (q_end + timedelta(days=20)).isoformat() + "T18:00:00+05:30", "quarter_end": q_end.isoformat(),
                   "scope": "consolidated", "audited": audited, "xbrl": f"https://nsearchives.nseindia.in/corporate/xbrl/T_{q_end}.xml",
                   "ixbrl": None}
    return p


ENDS = [date(2026, 9, 30), date(2026, 6, 30), date(2026, 3, 31), date(2025, 12, 31), date(2025, 9, 30)]


def nonbank_snap(**kw):
    qs = [nonbank_q(e, rev=1000 + 50 * (4 - i), eps=10.0 + (4 - i), pat=100 + 10 * (4 - i), with_bs=e.month in (3, 9), **kw)
          for i, e in enumerate(ENDS)]
    return {"symbol": "ACME", "as_of": "2026-11-20T10:00:00+05:30", "scope": "consolidated", "quarters": qs,
            "shareholding": [], "failed": [], "notes": [], "insider_trades": {"status": "unavailable", "reason": "test"}}


def test_parse_results_picks_quarter_ytd_and_balance_sheet():
    p = nonbank_q(ENDS[0], rev=1200, eps=14, pat=140, with_bs=True)
    assert p["meta"]["period_end"] == "2026-09-30" and p["meta"]["period_start"] == "2026-07-01"
    assert p["meta"]["ytd_start"] == "2026-04-01" and p["meta"]["bs_date"] == "2026-09-30"
    assert p["q"]["RevenueFromOperations"] == 1200 * CR and p["ytd"]["CashFlowsFromUsedInOperatingActivities"] > 0
    assert p["bs"]["TradeReceivablesCurrent"] == 900 * CR
    assert p["auditors"] == ["B S R & Co. LLP"] and p["rpt"]["rows"] == 2
    assert "unmodified" in p["meta"]["DeclarationOfUnmodifiedOpinionOrStatementOnImpactOfAuditQualification"]
    assert not p["meta"]["is_bank"]


def test_ratios_computed_in_code_with_inputs():
    fund = F.build(nonbank_snap(), close=500.0)
    m = {x["name"]: x for x in fund["metrics"]}
    eps_ttm = 14 + 13 + 12 + 11
    assert m["eps_ttm"]["value"] == eps_ttm and m["eps_ttm"]["q"] == [0, 1, 2, 3]
    assert abs(m["pe_ttm"]["value"] - 500 / eps_ttm) < 1e-9
    shares = 100 * CR / 1
    assert abs(m["pb"]["value"] - 500 / (4000 * CR / shares)) < 1e-9
    assert abs(m["revenue_yoy_pct"]["value"] - (1200 / 1000 - 1) * 100) < 1e-9          # vs same quarter a year ago
    assert abs(m["roe_pct"]["value"] - (140 + 130 + 120 + 110) / 4000 * 100) < 1e-9
    assert abs(m["receivable_days"]["value"] - 900 / (1200 + 1150 + 1100 + 1050) * 365) < 1e-9
    assert abs(m["debt_to_equity"]["value"] - 500 / 4100) < 1e-9
    assert abs(m["ocf_to_profit"]["value"] - 0.9) < 1e-9
    assert all(r["ok"] for r in fund["reconciliation"]) and fund["audit"]["status"] == "unmodified"


def test_missing_quarter_means_no_pe_not_an_estimate():
    snap = nonbank_snap()
    del snap["quarters"][2]                                     # gap: Mar-2026 filing missing
    m = {x["name"]: x for x in F.build(snap, close=500.0)["metrics"]}
    assert m["eps_ttm"]["value"] is None and "consecutive" in m["eps_ttm"]["note"]
    assert m["pe_ttm"]["value"] is None


def test_negative_eps_pe_not_meaningful():
    snap = nonbank_snap()
    for p in snap["quarters"]:
        p["q"]["DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations"] = -2.0
    m = {x["name"]: x for x in F.build(snap, close=500.0)["metrics"]}
    assert m["pe_ttm"]["status"] == "not_applicable" and m["pe_ttm"]["value"] is None


def test_unreconciled_filing_is_flagged_and_not_verified():
    snap = nonbank_snap(recon_ok=False)
    fund = F.build(snap, close=500.0)
    assert fund["reconciliation"][0]["ok"] is False and any("does not reconcile" in f for f in fund["flags"])
    facts, calcs, info = F.to_evidence(EvidenceFactory(), snap, fund, "Acme")
    rev = next(f for f in facts if f.metric == "revenue")
    assert not rev.verified


def test_auditor_spelling_variants_are_not_a_change():
    assert F.auditor_key("B S R & Co. LLP") == F.auditor_key("BSR and Co LLP")
    snap = nonbank_snap()
    snap["quarters"][4]["auditors"] = ["Deloitte Haskins & Sells LLP"]
    assert any(f.startswith("Auditor change") for f in F.build(snap, 500.0)["flags"])


def test_bank_metrics_pcr_and_no_other_income_share():
    def bq(e, gn_amt, gn, nn, adv):
        vals = {"InterestEarned": 800 * CR, "OtherIncome": 150 * CR, "Income": 950 * CR, "ProfitLossFromOrdinaryActivitiesBeforeTax": 300 * CR,
                "TaxExpense": 75 * CR, "ProfitLossFromOrdinaryActivitiesAfterTax": 225 * CR, "ProfitLossForThePeriod": 225 * CR,
                "DilutedEarningsPerShareAfterExtraordinaryItems": 3.0, "GrossNonPerformingAssets": gn_amt * CR,
                "PercentageOfGrossNpa": gn, "PercentageOfNpa": nn, "CET1Ratio": 0.17, "ReturnOnAssets": 0.005,
                "PaidUpValueOfEquityShareCapital": 75 * CR, "FaceValueOfEquityShareCapital": 1}
        p = X.parse_results(results_xbrl(e, vals, {"Capital": 75 * CR, "ReservesAndSurplus": 9000 * CR, "Advances": adv * CR,
                                                   "Deposits": 30000 * CR}))
        p["filing"] = {"broadcast_at": f"{e}T18:00:00+05:30", "quarter_end": str(e), "scope": "standalone", "audited": "Un-Audited",
                       "xbrl": "https://nsearchives.nseindia.in/corporate/xbrl/B.xml", "ixbrl": None}
        return p
    qs = [bq(e, 400, 0.0120 if i == 0 else 0.0135, 0.0040, 25000) for i, e in enumerate(ENDS)]
    assert qs[0]["meta"]["is_bank"]
    fund = F.build({"quarters": qs, "shareholding": [], "scope": "standalone"}, close=150.0)
    m = {x["name"]: x for x in fund["metrics"]}
    assert m["gnpa_pct"]["value"] == 1.2 and "year ago 1.35%" in m["gnpa_pct"]["note"]
    assert abs(m["pcr_pct_approx"]["value"] - (1 - 0.004 * 25000 / 400) * 100) < 1e-9
    assert abs(m["cet1_pct"]["value"] - 17.0) < 1e-9
    assert "other_income_share_pct" not in m and "ocf_to_profit" not in m
    assert abs(m["pb"]["value"] - 150 / (9075 * CR / (75 * CR))) < 1e-9


def _shp(date_, promoter_shares, pledged, total=1000.0, member_extra=True):
    parts = []
    facts = []
    for i, (mem, sh, pct) in enumerate([("ShareholdingPatternMember", total, 1.0),
                                        ("ShareholdingOfPromoterAndPromoterGroupMember", promoter_shares, promoter_shares / total),
                                        ("InstitutionsForeignMember", 100.0, 0.1), ("MutualFundsOrUTIMember", 50.0, 0.05)]):
        parts.append(_ctx(f"C{i}", instant=date_, member=mem))
        facts.append(f'<in-capmkt:NumberOfShares contextRef="C{i}" unitRef="shares" decimals="0">{sh}</in-capmkt:NumberOfShares>')
        facts.append(f'<in-capmkt:ShareholdingAsAPercentageOfTotalNumberOfShares contextRef="C{i}" unitRef="pure" decimals="INF">{pct}</in-capmkt:ShareholdingAsAPercentageOfTotalNumberOfShares>')
        if mem.startswith("ShareholdingOfPromoter"):
            facts.append(f'<in-capmkt:NumberOfSharesEncumberedUnderPledged contextRef="C{i}" unitRef="shares" decimals="0">{pledged}</in-capmkt:NumberOfSharesEncumberedUnderPledged>')
    if member_extra:   # a per-holder context (typed member) must be ignored
        parts.append(_ctx("H1", instant=date_, member="ShareholdingOfPromoterAndPromoterGroupMember", typed=True))
        facts.append('<in-capmkt:NumberOfShares contextRef="H1" unitRef="shares" decimals="0">1</in-capmkt:NumberOfShares>')
    p = X.parse_shareholding(f"<xbrl {NS}>{''.join(parts)}{''.join(facts)}</xbrl>".encode())
    p["filing"] = {"broadcast_at": f"{date_}T18:00:00+05:30", "date": str(date_), "xbrl": "https://nsearchives.nseindia.in/corporate/xbrl/S.xml"}
    return p


def _pledge_bundle(series, decision="2026-11-20T10:00:00+05:30"):
    snap = {"quarters": [], "scope": "consolidated", "shareholding": [_shp(d, p_sh, pl) for d, p_sh, pl in series]}
    fund = F.build(snap, None)
    facts, _, _ = F.to_evidence(EvidenceFactory(), snap, fund, "Acme")
    return Bundle(mode="live", symbol="X", company_name="X", decision_at=decision, horizon="medium", action="new_purchase", facts=facts), fund


def test_shareholding_pledge_level_trend_and_no_promoter():
    b, fund = _pledge_bundle([(date(2026, 9, 30), 600.0, 6.0), (date(2026, 6, 30), 600.0, 3.0), (date(2026, 3, 31), 600.0, 0.0)])
    assert fund["shareholding"][0]["promoter_pct"] == 60.0 and fund["shareholding"][0]["pledge_pct_of_promoter"] == 1.0
    rule = R.pledge_rule(b, {"max_pct_of_promoter": 10.0, "rising_quarters": 2})
    assert rule["result"] == "blocked" and "rising for 2" in rule["detail"]                 # 0 → 0.5 → 1.0%
    b2, _ = _pledge_bundle([(date(2026, 9, 30), 600.0, 90.0)])
    assert R.pledge_rule(b2, {"max_pct_of_promoter": 10.0})["result"] == "blocked"         # 15% of promoter holding
    b3, _ = _pledge_bundle([(date(2026, 9, 30), 0.0, 0.0)])
    r3 = R.pledge_rule(b3, {"max_pct_of_promoter": 10.0})
    assert r3["result"] == "pass" and "not applicable" in r3["detail"]
    b4, _ = _pledge_bundle([(date(2026, 9, 30), 600.0, 0.0), (date(2026, 6, 30), 600.0, 0.0)])
    assert R.pledge_rule(b4, {"max_pct_of_promoter": 10.0, "rising_quarters": 2})["result"] == "pass"


def test_manual_fact_wins_a_tie():
    b, _ = _pledge_bundle([(date(2026, 9, 30), 600.0, 0.0)])
    manual = FinancialFact(id="fact-1", metric="promoter_pledge_pct", value=25.0, status="available", period_end="2026-09-30",
                           evidence_id="ev-m", verified=True)
    b = b.model_copy(update={"facts": [manual] + b.facts})
    assert R.pledge_rule(b, {"max_pct_of_promoter": 10.0})["result"] == "blocked"


def _audit(facts, decision="2026-11-20T10:00:00+05:30", checks=()):
    b = Bundle(mode="live", symbol="X", company_name="X", decision_at=decision, horizon="medium", action="new_purchase",
               price={"close": 100.0, "date": decision[:10]}, facts=facts, surveillance={"status": "not_listed"})
    out = R.enforce(b, R.preflight(b), {"decision": "wait"}, {"forensic_checks": list(checks), "brains_ok": True, "critic_ok": True,
                                                              "open_blockers": [], "citation_ok": True, "patterns": []})
    return next(r for r in out["rule_results"] if r["rule"] == "audit")


def test_audit_rule_uses_filed_declaration():
    f = lambda v, pe="2026-09-30": FinancialFact(id="a", metric="audit_opinion_unmodified", value=v, status="available",  # noqa: E731
                                                 period_end=pe, evidence_id="ev-1", reason="'Declaration' in audited results", verified=True)
    assert _audit([f(1.0)])["result"] == "pass"
    assert _audit([f(0.0)])["result"] == "blocked"
    assert _audit([f(1.0, "2025-03-31")])["result"] == "unknown"                             # too old for the 12-month lookback
    assert _audit([f(1.0)], checks=[{"name": "auditor", "result": "fail", "materiality": "critical", "note": "resigned"}])["result"] == "blocked"


def test_point_in_time_filing_choice_and_revisions():
    rows = [
        {"qe_Date": "30-SEP-2026", "consolidated": "Consolidated", "broadcast_Date": "08-Oct-2026 22:07:33", "xbrl": "https://a/c_q2.xml"},
        {"qe_Date": "30-SEP-2026", "consolidated": "Consolidated", "broadcast_Date": "25-Oct-2026 10:00:00", "xbrl": "https://a/c_q2_rev.xml"},
        {"qe_Date": "30-SEP-2026", "consolidated": "Standalone", "broadcast_Date": "08-Oct-2026 22:07:30", "xbrl": "https://a/s_q2.xml"},
        {"qe_Date": "30-JUN-2026", "consolidated": "Consolidated", "broadcast_Date": "09-Jul-2026 18:36:12", "xbrl": "https://a/c_q1.xml"},
    ]
    picked, scope, _ = X.choose_results(rows, "2026-10-20T10:00:00+05:30", 5)
    assert scope == "consolidated" and [r["xbrl"] for r in picked] == ["https://a/c_q2.xml", "https://a/c_q1.xml"]   # revision not yet filed
    picked, _, _ = X.choose_results(rows, "2026-11-20T10:00:00+05:30", 5)
    assert picked[0]["xbrl"] == "https://a/c_q2_rev.xml"
    picked, _, _ = X.choose_results(rows, "2026-10-01T10:00:00+05:30", 5)
    assert [r["_qe"] for r in picked] == ["2026-06-30"]                                       # Q2 not yet filed
    bank = [dict(r, xbrl=r["xbrl"].replace("https://a/", "https://a/BANKING_")) for r in rows]
    assert X.choose_results(bank, "2026-11-20T10:00:00+05:30", 5)[1] == "standalone"


def test_only_official_archive_urls_are_fetched():
    class Boom:
        def get(self, url):
            raise AssertionError("must not fetch")
    assert X.fetch_xbrl(Boom(), "https://evil.example/corporate/xbrl/x.xml") is None
    assert X.fetch_xbrl(Boom(), "https://nsearchives.nseindia.in/corporate/xbrl/../../x.txt") is None


def test_evidence_routing_to_quant_and_forensic_not_news():
    snap = nonbank_snap()
    fund = F.build(snap, 500.0)
    fac = EvidenceFactory()
    facts, calcs, info = F.to_evidence(fac, snap, fund, "Acme")
    comp = next(e for e in fac.items if e.kind == "computed_fundamental")
    assert any(p.text.startswith("P/E (trailing)") and "inputs ev-1, ev-2, ev-3, ev-4" in p.text for p in comp.passages)
    assert any("Insider trades: not available" in p.text for p in comp.passages)
    assert {c.name for c in calcs} >= {"pe_ttm", "pb", "roe_pct", "eps_ttm"}
    assert info["computed_evidence_id"] == comp.id and len(info["quarters"]) == 5
    b = Bundle(mode="live", symbol="ACME", company_name="Acme", decision_at="2026-11-20T10:00:00+05:30", horizon="medium",
               action="new_purchase", evidence=fac.items, facts=facts)
    q_view, _ = brain_view("quant", b, [], [])
    f_view, _ = brain_view("forensic", b, [], [])
    n_view, _ = brain_view("news", b, [], [])
    assert "P/E (trailing)" in q_view and "Receivable days" in f_view
    assert "P/E (trailing)" not in n_view
