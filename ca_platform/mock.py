"""DEMO-mode gateway: scripted, deterministic outputs that go through the SAME validators,
citation checks, resolution logic and rules engine as live model output. Delays simulate
model latency so the progress tracker can be seen working. Never used in live mode."""
from __future__ import annotations

import asyncio
import uuid

from .contracts import (BrainReportLLM, Bundle, Check, Cite, CritiqueLLM, HeadDecisionLLM, HeadFinalLLM,
                        HorizonStance, Objection, ObjectionResponse, Scenario, SettlingTest, Statement)
from .gateway import CallResult
from .settings import iso


class MockGateway:
    available = True

    def __init__(self, ledger, run_id: str, bundle: Bundle, log=None, loop: int = 1):
        self.ledger, self.run_id, self.b = ledger, run_id, bundle
        self.log = log or (lambda s, m: None)
        self.loop = loop
        self.total_cost = 0.0
        self.unknown_cost_calls = 0
        self.fallback_events: list = []
        self.critique: CritiqueLLM | None = None

    def available_for(self, role):
        return True

    # -- helpers
    def cite(self, title_part: str, i: int = 0, words: int = 9) -> Cite:
        for e in self.b.evidence:
            if title_part.lower() in e.title.lower() and len(e.passages) > i:
                p = e.passages[i]
                return Cite(evidence_id=e.id, passage_id=p.id, quote=" ".join(p.text.split()[:words]))
        e = self.b.evidence[0]
        return Cite(evidence_id=e.id, passage_id=e.passages[0].id, quote=" ".join(e.passages[0].text.split()[:6]))

    def find(self, text: str, words: int = 9) -> Cite:
        """Cite the first passage containing `text` (robust to passage ordering)."""
        for e in self.b.evidence:
            for p in e.passages:
                if text.lower() in p.text.lower():
                    return Cite(evidence_id=e.id, passage_id=p.id, quote=" ".join(p.text.split()[:words]))
        raise KeyError(f"demo passage not found: {text}")

    def role_cfg(self, role):
        return {"model": "mock-demo", "prompt": f"{role}@2.0.0"}

    async def structured(self, *, role, stage, system, user, output_model, semantic_check=None):
        self.log(stage, f"DEMO: scripted {role} output (no model call, $0.00)")
        await asyncio.sleep({"quant": 2.2, "news": 2.8, "forensic": 3.4, "head": 2.0, "critic": 2.4}.get(role, 1.0))
        obj = getattr(self, f"_{role}")(output_model)
        errs = semantic_check(obj) if semantic_check else []
        if errs:
            self.log(stage, f"DEMO output validation issues: {errs[:2]}")
        rec = {"request_id": f"req-{uuid.uuid4().hex[:12]}", "role": role, "model": "mock-demo", "provider": "demo",
               "served_model": "mock-demo", "prompt_version": f"{role}@2.0.0", "input_tokens": len(user) // 4,
               "output_tokens": len(obj.model_dump_json()) // 4, "cost_usd": 0.0, "price_known": True, "latency_ms": 0,
               "cache_hit": False, "created_at": iso(), "attempts": [{"attempt": 0, "errors": errs}]}
        self.ledger.execute("INSERT INTO request_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            [rec["request_id"], self.run_id, role, "mock-demo", rec["prompt_version"], "demo", None,
                             rec["input_tokens"], rec["output_tokens"], 0, 0.0, 0, "ok", iso(), "{}", obj.model_dump_json()])
        return CallResult(parsed=obj, record=rec)

    # -- scripted roles
    def _quant(self, _m):
        c = self.b.price.get("close") or 1200
        pat = next((p for p in self.b.patterns if p["name"] == "double_bottom"), None)
        stop = pat["pattern_stop"] if pat else round(c * 0.92, 2)
        return BrainReportLLM(
            specialist_conclusion="supportive",
            headline="Price is basing in a forming double bottom above its 200-day average; valuation is above its own history.",
            statements=[
                Statement(kind="observation", text="Trend: the stock trades above its 200-day average.", cites=[self.cite("Price & technicals", 2)]),
                Statement(kind="observation", text="A double bottom is forming; it is not yet confirmed.", cites=[self.cite("Chart patterns", 0)]),
                Statement(kind="observation", text="Trailing P/E is about 31.5x.", cites=[self.find("P/E (TTM)")]),
                Statement(kind="interpretation", text="A close above the neckline on strong volume would confirm the base.", cites=[]),
                Statement(kind="conclusion", text="Setup is constructive but entry should wait for confirmation or a pullback to support.",
                          cites=[self.cite("Price & technicals", 8)]),
            ],
            checks=[
                Check(name="trend_vs_sma200", result="pass", materiality="major", note="Close above SMA200.", cites=[self.cite("Price & technicals", 2)]),
                Check(name="momentum_rsi_macd", result="watch", materiality="minor", note="Momentum neutral.", cites=[self.cite("Price & technicals", 3)]),
                Check(name="pattern_status", result="watch", materiality="major", note="Double bottom forming, unconfirmed.", cites=[self.cite("Chart patterns", 0)]),
                Check(name="support_resistance_clearance", result="watch", materiality="minor", note="Resistance near the neckline.", cites=[self.cite("Price & technicals", 8)]),
                Check(name="liquidity_adv20", result="pass", materiality="major", note="Liquidity adequate.", cites=[self.cite("Price & technicals", 6)]),
                Check(name="valuation_assessable", result="watch", materiality="major", note="Only P/E available; above own history per a capture.", cites=[self.find("P/E (TTM)")]),
                Check(name="data_freshness", result="pass", materiality="major", note="Bars current.", cites=[self.cite("Price & technicals", 1)]),
            ],
            red_flags=[], missing_evidence=["Sector valuation comparison", "Five-year P/E history from filings"],
            stances=[HorizonStance(horizon="short", stance="neutral", rationale="Unconfirmed pattern."),
                     HorizonStance(horizon="medium", stance="attractive", rationale="Base with order-book support if confirmed."),
                     HorizonStance(horizon="long", stance="neutral", rationale="Valuation already above history.")],
            scenarios=[Scenario(name="bear", price_assumption=round(stop * 0.97, 2), rationale="Pattern fails; retest of lows.", cites=[self.cite("Chart patterns", 0)]),
                       Scenario(name="base", price_assumption=pat["measured_target"] if pat else round(c * 1.08, 2), rationale="Measured move of the double bottom (technical).", cites=[self.cite("Chart patterns", 0)]),
                       Scenario(name="bull", price_assumption=round(c * 1.2, 2), rationale="Rerating on order execution (assumption).", cites=[])],
            entry_low=round(c * 0.975, 2), entry_high=round(c * 1.0, 2), stop=stop, governance_score=None,
            would_change_mind=["Close below the pattern stop", "Results showing margin dilution from rail orders"])

    def _news(self, _m):
        r = BrainReportLLM(
            specialist_conclusion="supportive",
            headline="A Rs 420 crore order was officially disclosed; margins on it are unknown and a broker view is attributed only.",
            statements=[
                Statement(kind="observation", text="The order win is confirmed by an exchange disclosure, subject to a definitive contract.",
                          cites=[self.cite("order win disclosure", 0)]),
                Statement(kind="observation", text="Press reports the order is executed over 18 months; margins not disclosed.", cites=[self.cite("wins Rs 420 crore", 0)]),
                Statement(kind="observation", text="A captured article says rail margins have historically been lower than export margins.", cites=[self.cite("order book enough", 1)]),
                Statement(kind="interpretation", text="Revenue visibility improves; margin impact is uncertain.", cites=[]),
                Statement(kind="conclusion", text="Cannot establish whether priced in.", cites=[self.cite("Price reaction", 0)]),
            ],
            checks=[
                Check(name="material_events_identified", result="pass", materiality="major", note="Order win (major, positive, 18 months).", cites=[self.cite("order win disclosure", 0)]),
                Check(name="official_confirmation_of_press_claims", result="pass", materiality="major", note="Exchange disclosure confirms value; press detail on duration unconfirmed.", cites=[self.cite("order win disclosure", 0)]),
                Check(name="broker_views_attributed", result="pass", materiality="minor", note="Brokerage add rating, target Rs 1,450 — attributed opinion.", cites=[self.cite("Brokerage starts", 0)]),
                Check(name="priced_in_assessment", result="insufficient_evidence", materiality="major", note="No documented prior expectation in bundle.", cites=[]),
                Check(name="catalyst_timing", result="watch", materiality="minor", note="Definitive contract pending.", cites=[self.cite("order win disclosure", 0)]),
                Check(name="source_quality_mix", result="watch", materiality="minor", note="One official item; others are press/capture.", cites=[self.cite("order book enough", 0)]),
            ],
            red_flags=[], missing_evidence=["Order margin disclosure", "Consensus expectations before the order"],
            stances=[HorizonStance(horizon="medium", stance="attractive", rationale="Order adds visibility.")],
            scenarios=[], would_change_mind=["Contract not signed within the stated period", "Margin guidance cut"])
        return self._news_loop(r) if self.loop > 1 else r

    def _news_loop(self, r: BrainReportLLM) -> BrainReportLLM:
        """Loop 2+: targeted discovery found the order-margin disclosure."""
        m = self.find("expected to be about 11%")
        r.statements.insert(2, Statement(kind="observation", text="Management guides the rail-order EBITDA margin to about 11%, below the recent 14% average.", cites=[m]))
        r.headline = "Order win is officially disclosed; newly found guidance says its margin (~11%) is below the company average."
        r.specialist_conclusion = "neutral"
        r.missing_evidence = ["Consensus expectations before the order"]
        r.checks.append(Check(name="margin_disclosure", result="watch", materiality="major", note="Order margin below average — dilutive.", cites=[m]))
        return r

    def _forensic(self, _m):
        F = self.find
        r = BrainReportLLM(
            specialist_conclusion="neutral",
            headline="Cash conversion and audit look clean; headline growth flatters underlying growth because of a land-sale gain.",
            statements=[
                Statement(kind="observation", text="Underlying profit growth is 6% versus 14% headline.", cites=[F("excluding a one-time gain")]),
                Statement(kind="observation", text="Auditor gave an unmodified opinion.", cites=[F("unmodified opinion")]),
                Statement(kind="conclusion", text="No material red flag; growth quality should be judged on the underlying figure.", cites=[F("excluding a one-time gain"), F("0.92 times")]),
            ],
            checks=[
                Check(name="ocf_vs_profit", result="pass", materiality="major", note="OCF 0.92x profit over three years.", cites=[F("0.92 times")]),
                Check(name="receivable_inventory_days", result="insufficient_evidence", materiality="major", note="Not in bundle.", cites=[]),
                Check(name="other_income_exceptional", result="watch", materiality="major", note="One-time land gain lifts headline growth.", cites=[F("Exceptional item")]),
                Check(name="related_party_transactions", result="watch", materiality="minor", note="2.1% of revenue with promoter-group logistics firm.", cites=[F("Related-party")]),
                Check(name="contingent_liabilities", result="insufficient_evidence", materiality="major", note="Not in bundle.", cites=[]),
                Check(name="auditor", result="pass", materiality="critical", note="Unmodified opinion, no emphasis of matter.", cites=[F("unmodified opinion")]),
                Check(name="promoter_pledge_insider", result="pass", materiality="critical", note="No pledge over four quarters.", cites=[F("no shares pledged")]),
                Check(name="capitalised_costs_cwip", result="insufficient_evidence", materiality="minor", note="Not in bundle.", cites=[]),
                Check(name="dilution", result="pass", materiality="minor", note="Promoter holding unchanged.", cites=[F("no shares pledged")]),
                Check(name="tax_rate", result="insufficient_evidence", materiality="minor", note="Not in bundle.", cites=[]),
            ],
            red_flags=[], missing_evidence=["Receivable/inventory days", "Contingent liabilities", "Tax rate reconciliation"],
            stances=[], scenarios=[], governance_score=None,
            would_change_mind=["Receivable days rising faster than revenue", "Auditor change or qualification"])
        if self.loop > 1:  # targeted discovery answered two coverage gaps
            for c in r.checks:
                if c.name == "receivable_inventory_days":
                    c.result, c.note, c.cites = "watch", "Receivable days 62 vs 58 with revenue +12% — slight stretch.", [F("Receivable days were 62")]
                if c.name == "contingent_liabilities":
                    c.result, c.note, c.cites = "pass", "4% of net worth, mostly performance guarantees.", [F("Contingent liabilities were 4%")]
            r.missing_evidence = ["Tax rate reconciliation", "Capital work-in-progress ageing"]
            r.statements.append(Statement(kind="observation", text="Receivable days rose to 62 from 58.", cites=[F("Receivable days were 62")]))
        return r

    def _head(self, model):
        c = self.b.price.get("close") or 1200
        pat = next((p for p in self.b.patterns if p["name"] == "double_bottom"), None)
        draft = HeadDecisionLLM(
            decision="buy", summary="Constructive base with an officially disclosed order; buy in tranches near support with a pattern stop.",
            reasons=[Statement(kind="observation", text="Order win officially disclosed.", cites=[self.cite("order win disclosure", 0)]),
                     Statement(kind="observation", text="Price above its 200-day average.", cites=[self.cite("Price & technicals", 2)]),
                     Statement(kind="conclusion", text="No forensic red flag found in the evidence available.", cites=[self.find("unmodified opinion")])],
            evidence_sufficiency="partial", entry_low=round(c * 0.975, 2), entry_high=round(c, 2),
            stop=pat["pattern_stop"] if pat else round(c * 0.92, 2),
            target_low=pat["measured_target"] if pat else round(c * 1.08, 2), target_high=round(c * 1.2, 2), head_score=58,
            assumptions=["Base target is the technical measured move, not a valuation"],
            entry_conditions=["Daily close above the neckline on 1.5x average volume, or a pullback into the entry zone"],
            invalidation_conditions=["Close below the pattern stop", "Definitive contract not signed"],
            material_dissent=["Forensic: headline growth flattered by a one-time gain"],
            portfolio_limitations=["Portfolio context missing — research only, not a personalised allocation"])
        if self.loop > 1:  # the Head has seen loop 1 (via <previous_loop>) and the new margin evidence
            draft = draft.model_copy(update={
                "decision": "wait", "head_score": 50,
                "summary": "Still wait: loop 2 settled the forensic gaps, but the pattern is unconfirmed and the order margin (~11%) is dilutive.",
                "reasons": draft.reasons + [Statement(kind="observation", text="Order margin guided to about 11%.", cites=[self.find("expected to be about 11%")])]})
        if model is HeadDecisionLLM:
            return draft
        objs = self.critique.objections if self.critique else []
        responses = []
        for o in objs:
            if o.severity == "blocker":
                responses.append(ObjectionResponse(objection_id=o.id, action="accept", test_result="unresolved",
                                                   explanation="Pattern is not confirmed in the bundle; cannot be settled now.",
                                                   cites=[self.cite("Chart patterns", 0)], decision_effect="buy → wait"))
            else:
                responses.append(ObjectionResponse(objection_id=o.id, action="accept", test_result="passed",
                                                   explanation="Base case restated on underlying growth; target labelled technical.",
                                                   cites=[self.find("excluding a one-time gain")], decision_effect="assumptions clarified"))
        final = draft if self.loop > 1 else draft.model_copy(update={
            "decision": "wait", "head_score": 52,
            "summary": "Wait for pattern confirmation before buying; order win is real but margin impact is unknown."})
        return HeadFinalLLM(responses=responses, final=final)

    def _critic(self, _m):
        self.critique = CritiqueLLM(
            checklist=["facts", "calculations", "sources", "valuation assumptions", "missing evidence", "dissent", "policy", "portfolio fit"],
            objections=[
                Objection(id="obj-1", severity="blocker", type="logic",
                          text="The draft buys before the double bottom is confirmed; the pattern status is 'forming'.",
                          cites=[self.cite("Chart patterns", 0)], decision_impact="A buy now relies on an unconfirmed signal.",
                          settling_test=SettlingTest(description="Check pattern status in the bundle: is it confirmed?")),
                Objection(id="obj-2", severity="major", type="valuation_assumption",
                          text="Draft does not reflect that underlying growth (6%) is far below headline (14%).",
                          cites=[self.find("excluding a one-time gain")], decision_impact="Valuation support is weaker than implied.",
                          settling_test=SettlingTest(description="Restate the base case using underlying growth.")),
            ])
        if self.loop > 1:  # obj-2 was settled last loop and is not repeated; the blocker is re-tested
            self.critique.objections = [self.critique.objections[0]]
        return self.critique

    async def web_discovery(self, **kw):
        raise RuntimeError("not used in demo")
