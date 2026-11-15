"""Specialist brains, anonymisation, Head draft, Critic, Head response and code-side
resolution of settling tests (spec §8, §10).

Communication contract between roles (all explicit, all stored):
  brains     <- role view of frozen bundle                      -> BrainReportLLM
  head draft <- context, preflight, calculations, prior decisions,
                anonymised reports WITH citation statuses, cited passages -> HeadDecisionLLM
  critic     <- same + draft + draft citation audit              -> CritiqueLLM (0-7 objections)
  head final <- same + draft + critique                          -> HeadFinalLLM
Code then decides which objections are actually resolved: a 'reject' without a verified
citation, or a missing response, is treated as unresolved. Open blockers prevent a buy.
"""
from __future__ import annotations

import json
import secrets
from typing import Any

from .citations import BAD, CitationIndex, audit
from .contracts import BrainReportLLM, Bundle, CritiqueLLM, HeadDecisionLLM, HeadFinalLLM
from .prompting import annotate_report, brain_view, context_block, panel_bundle_view, system_prompt
from .rules.engine import plan_valid, reward_risk, scenario_returns
from .settings import config

REMIT = {"quant": "Quant — valuation, setup and entry conditions",
         "news": "News & Narrative — what changed, materiality, catalysts, priced-in",
         "forensic": "Forensic CA — reliability of numbers and governance"}
FORENSIC_NAMES = {"ocf_vs_profit", "receivable_inventory_days", "other_income_exceptional", "related_party_transactions",
                  "contingent_liabilities", "auditor", "promoter_pledge_insider", "capitalised_costs_cwip", "dilution",
                  "tax_rate"}
BANK_NAMES = {"asset_quality_gnpa_nnpa", "provision_coverage", "capital_adequacy", "slippages_restructured"}
NONBANK_FORENSIC = {"ocf_vs_profit", "receivable_inventory_days", "other_income_exceptional", "capitalised_costs_cwip"}
COMMON_FORENSIC = FORENSIC_NAMES - NONBANK_FORENSIC
CITATION_MIN_SCORE = 0.5


# ------------------------------------------------------------- validators ---

def brain_checker(role: str, idx: CitationIndex):
    def check(rep: BrainReportLLM) -> list[str]:
        errs = list(audit(rep, idx)["defects"])
        if not rep.statements and rep.specialist_conclusion != "insufficient_evidence":
            errs.append("No statements: give observations with citations, or conclude insufficient_evidence")
        if role == "forensic":
            names = {c.name for c in rep.checks}
            missing_common = COMMON_FORENSIC - names
            missing_kind = min(NONBANK_FORENSIC - names, BANK_NAMES - names, key=len)
            if missing_common or missing_kind:
                errs.append("Forensic checklist incomplete; missing check names: "
                            + ", ".join(sorted(missing_common | missing_kind))
                            + " (use the bank/NBFC set only for lenders)")
        if role == "quant" and rep.stop is not None and rep.entry_low is not None:
            if rep.stop >= rep.entry_low:
                errs.append("Stop must be below entry_low")
            if rep.entry_high is not None and rep.entry_low > rep.entry_high:
                errs.append("entry_low is above entry_high")
        if role != "quant" and (rep.entry_low or rep.entry_high or rep.stop):
            errs.append(f"{role} must leave entry_low/entry_high/stop null (Quant owns trade levels)")
        names = [s.name for s in rep.scenarios]
        if len(names) != len(set(names)):
            errs.append("Duplicate scenario names")
        return errs
    return check


def head_checker(idx: CitationIndex):
    def check(d: HeadDecisionLLM) -> list[str]:
        errs = list(audit(d, idx)["defects"])
        if d.decision == "buy":
            errs += plan_valid(d.model_dump())
        if d.decision in ("avoid", "insufficient_data") and any(
                v is not None for v in (d.entry_low, d.entry_high, d.stop, d.target_low, d.target_high)):
            errs.append(f"A '{d.decision}' decision must not carry trade levels — set them to null")
        if not d.reasons:
            errs.append("Give at least one cited reason")
        return errs
    return check


def critic_checker(idx: CitationIndex):
    def check(c: CritiqueLLM) -> list[str]:
        errs = list(audit(c, idx)["defects"])
        if len(c.objections) > 7:
            errs.append(f"{len(c.objections)} objections — return at most seven material objections")
        ids = [o.id for o in c.objections]
        if len(ids) != len(set(ids)):
            errs.append("Objection ids must be unique")
        if not c.checklist:
            errs.append("Record the checklist items you reviewed")
        return errs
    return check


def final_checker(idx: CitationIndex, objection_ids: list[str]):
    head_check = head_checker(idx)

    def check(f: HeadFinalLLM) -> list[str]:
        errs = head_check(f.final)
        errs += [x for x in audit(f.responses, idx)["defects"]]
        got = [r.objection_id for r in f.responses]
        missing = [o for o in objection_ids if o not in got]
        extra = [o for o in got if o not in objection_ids]
        dup = {o for o in got if got.count(o) > 1}
        if missing:
            errs.append(f"Respond to every objection; missing: {', '.join(missing)}")
        if extra:
            errs.append(f"Responses reference unknown objection ids: {', '.join(extra)}")
        if dup:
            errs.append(f"Respond to each objection exactly once; duplicated: {', '.join(sorted(dup))}")
        return errs
    return check


# ----------------------------------------------------------- calculations ---

def panel_calculations(b: Bundle, reports: dict[str, dict]) -> dict:
    hz = config("horizons")["horizons"][b.horizon]
    close = b.price.get("close")
    out: dict[str, Any] = {"close": close, "horizon_months": hz["calendar_months"],
                           "note": "Computed in code from analyst-proposed assumptions. Gross of tax, net of assumed costs."}
    q = reports.get("quant")
    if q:
        out["quant_scenarios"] = scenario_returns(close, q.get("scenarios", []), hz["calendar_months"])
        base = next((s for s in q.get("scenarios", []) if s["name"] == "base"), None)
        if base and base.get("price_assumption"):
            out["quant_reward_risk_to_base"] = reward_risk(q.get("entry_low"), q.get("entry_high"), q.get("stop"),
                                                           base["price_assumption"])
        out["quant_levels"] = {k: q.get(k) for k in ("entry_low", "entry_high", "stop")}
    return out


# ------------------------------------------------------------- anonymise ---

def anonymise(roles: list[str]) -> dict[str, str]:
    labels = ["A", "B", "C"][:len(roles)]
    rng = secrets.SystemRandom()
    rng.shuffle(labels)
    return dict(zip(roles, labels))


def reports_block(reports: dict[str, dict], labels: dict[str, str], idx: CitationIndex, audits: dict[str, dict]) -> str:
    blocks = []
    for role in sorted(reports, key=lambda r: labels[r]):
        rep = annotate_report(reports[role], idx)
        rep.pop("governance_score", None)
        blocks.append({"analyst": f"Analyst {labels[role]}", "remit": REMIT[role],
                       "citation_audit": {k: audits[role][k] for k in ("counts", "score", "uncited_material")},
                       "report": rep})
    return "<reports>\n" + json.dumps(blocks, indent=1, ensure_ascii=False) + "\n</reports>"


# ---------------------------------------------------------------- calls ---

async def run_brain(gw, role: str, b: Bundle, pre: list[dict], idx: CitationIndex, stage: str):
    sys_p, ver = system_prompt(role)
    user, omitted = brain_view(role, b, pre, [])
    gw.log(stage, f"Prepared role view: {user.count('<evidence ')} evidence items, {len(user):,} chars"
           + (f", {len(omitted)} omitted for length" if omitted else ""))
    res = await gw.structured(role=role, stage=stage, system=sys_p, user=user, output_model=BrainReportLLM,
                              semantic_check=brain_checker(role, idx))
    return res.parsed, ver, res.record


def head_inputs(b, pre, prior, calcs, rblock, panel_view) -> str:
    return (f"{context_block(b, pre, prior)}\n<calculations>\n{json.dumps(calcs, indent=1, default=str)}\n</calculations>\n"
            f"{rblock}\n{panel_view}\n")


async def run_head_draft(gw, b, pre, prior, calcs, rblock, panel_view, idx, stage):
    sys_p, ver = system_prompt("head")
    user = head_inputs(b, pre, prior, calcs, rblock, panel_view) + (
        f"\nWrite your DRAFT decision for the {b.horizon} horizon as the required JSON object.")
    res = await gw.structured(role="head", stage=stage, system=sys_p, user=user, output_model=HeadDecisionLLM,
                              semantic_check=head_checker(idx))
    return res.parsed, ver, res.record


async def run_critic(gw, b, pre, calcs, rblock, panel_view, draft: HeadDecisionLLM, draft_audit, idx, stage):
    sys_p, ver = system_prompt("critic")
    dd = annotate_report(draft.model_dump(), idx)
    user = (head_inputs(b, pre, [], calcs, rblock, panel_view)
            + f"<draft>\n{json.dumps(dd, indent=1, ensure_ascii=False)}\n</draft>\n"
            + f"<draft_citation_audit>\n{json.dumps({k: draft_audit[k] for k in ('counts', 'score', 'defects')}, indent=1)}\n</draft_citation_audit>\n"
            + "\nReview the draft and return your critique as the required JSON object.")
    res = await gw.structured(role="critic", stage=stage, system=sys_p, user=user, output_model=CritiqueLLM,
                              semantic_check=critic_checker(idx))
    return res.parsed, ver, res.record


async def run_head_final(gw, b, pre, prior, calcs, rblock, panel_view, draft, critique: CritiqueLLM, idx, stage):
    sys_p, ver = system_prompt("head")
    user = (head_inputs(b, pre, prior, calcs, rblock, panel_view)
            + f"<draft>\n{draft.model_dump_json(indent=1)}\n</draft>\n"
            + f"<critique>\n{json.dumps(annotate_report(critique.model_dump(), idx), indent=1, ensure_ascii=False)}\n</critique>\n"
            + "\nRespond to every objection and give your FINAL decision as the required JSON object.")
    ids = [o.id for o in critique.objections]
    res = await gw.structured(role="head", stage=stage, system=sys_p, user=user, output_model=HeadFinalLLM,
                              semantic_check=final_checker(idx, ids))
    return res.parsed, ver, res.record


# ------------------------------------------------------------ resolution ---

def resolve(critique: CritiqueLLM, final: HeadFinalLLM, idx: CitationIndex) -> dict:
    """Code-side resolution: decides the EFFECTIVE status of every objection."""
    by_id = {r.objection_id: r for r in final.responses}
    rows, open_blockers, open_major = [], [], []
    for o in critique.objections:
        r = by_id.get(o.id)
        if r is None:
            eff, why = "unresolved", "No Head response recorded"
        else:
            statuses = [idx.check(c) for c in r.cites]
            eff, why = r.test_result, ""
            if r.action == "reject" and "verified" not in statuses:
                eff, why = "unresolved", "Rejected without a verified citation — treated as unresolved by code"
            if r.test_result == "passed" and any(s in BAD for s in statuses):
                eff, why = "unresolved", "Settling-test evidence has invalid citations"
            if r.action == "accept" and r.test_result == "passed":
                why = "Accepted; head reports the test passed after the change"
        row = {"objection": o.model_dump(), "response": r.model_dump() if r else None,
               "effective_result": eff, "code_note": why,
               "citation_status": [idx.check(c) for c in (r.cites if r else [])]}
        rows.append(row)
        if o.severity == "blocker" and eff != "passed":
            open_blockers.append(o.id)
        if o.severity == "major" and eff == "unresolved":
            open_major.append(o.id)
    return {"rows": rows, "open_blockers": open_blockers, "open_major": open_major}
