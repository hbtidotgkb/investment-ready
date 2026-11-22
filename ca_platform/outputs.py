"""Rendered outputs: WhatsApp-ready text, Learn concepts and a Markdown report.
All text is rendered from validated FINAL fields — never a persuasive buy narrative on a
blocked plan (spec §9 'final consistency')."""
from __future__ import annotations

from datetime import datetime

from .settings import REPORTS, config

EXCLUDE_FROM_RANKING = {"price_series", "computed_feature", "chart_pattern", "company_profile", "news_summary"}


def rank_sources(bundle, dr: dict, idx, limit: int = 10) -> list[dict]:
    """Rank external sources by quality × how much the panel relied on them × recency.

    score = tier weight × (1 + 0.6·verified citations + 0.2·id-only citations) × recency
    Recency: ≤30 days 1.0, ≤90 days 0.85, older 0.7, unknown date 0.75. Computed sources
    (prices, technicals) are excluded — they are our own calculations, shown elsewhere.
    """
    weights = {k: v.get("weight", 0.5) for k, v in config("sources")["tiers"].items()}
    used: dict[str, dict] = {}

    def walk(o, by: str):
        if isinstance(o, dict):
            if "evidence_id" in o and "quote" in o:
                st = idx.check(o)
                if st in ("verified", "id_only"):
                    u = used.setdefault(o["evidence_id"], {"verified": 0, "id_only": 0, "by": set()})
                    u[st] += 1
                    u["by"].add(by)
            for v in o.values():
                walk(v, by)
        elif isinstance(o, list):
            for x in o:
                walk(x, by)

    for role, rep in (dr.get("reports") or {}).items():
        walk(rep, role)
    walk(dr.get("head_proposal"), "head")
    walk(dr.get("critique"), "critic")
    try:
        decided = datetime.fromisoformat(bundle.decision_at)
    except ValueError:
        decided = None
    out = []
    for e in bundle.evidence:
        if e.kind in EXCLUDE_FROM_RANKING:
            continue
        u = used.get(e.id, {"verified": 0, "id_only": 0, "by": set()})
        age_days, rec = None, 0.75
        if e.published_at and decided:
            try:
                age_days = (decided - datetime.fromisoformat(e.published_at.replace("Z", "+00:00"))).days
                rec = 1.0 if age_days <= 30 else 0.85 if age_days <= 90 else 0.7
            except ValueError:
                pass
        w = weights.get(e.tier, 0.5) if e.tier != "DEMO_synthetic" else 0.6  # demo: rank by usage for display only
        score = w * (1 + 0.6 * u["verified"] + 0.2 * u["id_only"]) * rec
        why = [config("sources")["tiers"].get(e.tier, {}).get("label", e.tier)]
        if u["verified"] or u["id_only"]:
            why.append(f"cited {u['verified'] + u['id_only']}× ({u['verified']} verified)"
                       + (f" by {', '.join(sorted(u['by']))}" if u["by"] else ""))
        else:
            why.append("not cited by the panel")
        why.append(f"{age_days} days old" if age_days is not None else "publication date unknown")
        out.append({"evidence_id": e.id, "title": e.title, "publisher": e.publisher, "tier": e.tier, "url": e.url,
                    "published_at": e.published_at, "score": round(score, 3), "why": " · ".join(why),
                    "verified_citations": u["verified"]})
    out.sort(key=lambda x: (-x["score"], x["evidence_id"]))
    return out[:limit]


DISCLAIMER = "Analysis, not investment advice. Personal research only."

CONCEPTS = {
    "evidence-coverage": ("Evidence coverage", "A check that could not be done is not a check that passed. We list what was missing so a 'clean' result is never assumed."),
    "measured-move": ("Measured move", "A chart-pattern target: the pattern's height added to the breakout line. It is a technical estimate, not a valuation."),
    "double-bottom": ("Double bottom", "Two similar lows with a peak between. It only counts once price closes above that peak (the neckline), ideally on high volume."),
    "underlying-growth": ("Underlying vs headline growth", "Headline profit can include one-off gains. Underlying growth strips them out and is the better guide to the business."),
    "cash-conversion": ("Cash conversion", "Operating cash flow divided by profit. Persistently below 1 can mean profits are not turning into cash."),
    "reward-risk": ("Reward/risk", "Distance to target divided by distance to stop. Below 2 for short trades means you risk too much for the expected gain."),
    "priced-in": ("Priced in", "News is 'priced in' when the price already reflects it. A price move alone does not prove it; you need the prior expectation."),
    "planned-loss": ("Planned loss at stop", "What you lose if the stop executes at its price. Gaps and slippage can make the real loss bigger."),
    "governance-score": ("Governance score", "A 0-100 score computed from forensic findings by a fixed rubric. Low coverage means no score, not a good score."),
    "point-in-time": ("Point-in-time", "A decision may only use information public at that moment. Using later data in a backtest is look-ahead."),
    "attributed-opinion": ("Attributed opinion", "A broker target is someone's view. We record who said it and when; it is not our valuation."),
    "stcg-ltcg": ("STCG vs LTCG", "Gains on listed shares are taxed differently by holding period. The exact rates and boundary come from the verified tax policy file."),
}


def pick_concepts(decision_record: dict) -> list[str]:
    picks: list[str] = []
    text = str(decision_record).lower()
    for key, trig in [("double-bottom", "double_bottom"), ("underlying-growth", "underlying"), ("measured-move", "measured"),
                      ("cash-conversion", "ocf_vs_profit"), ("priced-in", "priced in"), ("evidence-coverage", "insufficient"),
                      ("reward-risk", "reward_risk"), ("governance-score", "governance"), ("attributed-opinion", "broker")]:
        if trig in text and key not in picks:
            picks.append(key)
        if len(picks) == 2:
            break
    for k in ("evidence-coverage", "point-in-time"):
        if len(picks) < 2 and k not in picks:
            picks.append(k)
    return picks[:2]


def decision_text(fa: dict) -> str:
    """'insufficient_data' reads as a non-answer; say plainly what it means for the reader."""
    d = fa["decision"]
    return {"insufficient_data": "DON'T BUY YET — NOT ENOUGH EVIDENCE"}.get(d, d.upper().replace("_", " "))


def whatsapp(dr: dict) -> str:
    fa = dr["final_action"]
    head = dr["head_proposal"]
    plan = fa.get("plan") or {}
    lines = [f"*{dr['symbol']}* — {dr['horizon'].title()} horizon" + (" (DEMO)" if dr["mode"] == "demo" else ""),
             f"Decision: *{decision_text(fa)}*" + (" ✅ actionable" if fa["actionable"] else
                                                   " · on hold until setup is complete" if fa.get("on_hold") else " · research only")]
    if head["decision"] != fa["decision"]:
        lines.append(f"Head proposed {head['decision'].upper()}; adjusted by rules.")
    lines.append(f"Why: {head['summary']}")
    if plan and plan.get("entry_low"):
        lines.append(f"Plan: entry {plan['entry_low']:,.0f}–{plan['entry_high']:,.0f} · stop {plan['stop']:,.0f} · "
                     f"target {plan['target_low']:,.0f}" + (f"–{plan['target_high']:,.0f}" if plan.get("target_high") else ""))
    sz = fa.get("sizing") or {}
    if sz.get("status") == "ok":
        lines.append(f"Size: {sz['qty']} sh (~Rs {sz['value_inr']:,.0f}); tranches " +
                     ", ".join(f"{t['tranche']} {t['qty']}@{t['price']:,.0f}" for t in sz["tranches"]))
    blocks = [r for r in dr["rule_results"] if r["result"] in ("blocked", "unknown")]
    if blocks:
        lines.append("Needs: " + "; ".join(f"{r['rule'].replace('_', ' ')}" for r in blocks[:5]))
    if head.get("invalidation_conditions"):
        lines.append("Invalid if: " + head["invalidation_conditions"][0])
    lines.append(f"Learn: {', '.join(CONCEPTS[c][0] for c in dr['learn'])}")
    lines.append(f"_{DISCLAIMER}_")
    return "\n".join(lines)


def markdown_report(dr: dict, bundle: dict) -> str:
    ev = {e["id"]: e for e in bundle["evidence"]}
    fa, head = dr["final_action"], dr["head_proposal"]
    out = [f"# {dr['symbol']} — {dr['company_name']}", "",
           f"*Run {dr['run_id']} · decision time {dr['decision_at']} · mode {dr['mode']} · bundle `{dr['bundle_id'][:16]}`*", "",
           f"> {DISCLAIMER}", "",
           f"## Final action: **{decision_text(fa)}** ({'actionable' if fa['actionable'] else 'on hold until setup is complete' if fa.get('on_hold') else 'not actionable'})", "",
           f"Head proposal: **{head['decision'].upper()}** — {head['summary']}", ""]
    for a in dr.get("adjustments", []):
        out.append(f"- Adjusted by rules: {a}")
    out += ["", "## Reasons", ""]
    for s in head.get("reasons", []):
        refs = ", ".join(f"[{c['evidence_id']}]" for c in s.get("cites", []))
        out.append(f"- ({s['kind']}) {s['text']} {refs}")
    out += ["", "## Rule results", "", "| Rule | Result | Detail |", "|---|---|---|"]
    for r in dr["rule_results"]:
        out.append(f"| {r['rule']} | {r['result']} | {r['detail'].replace('|', '/')} |")
    out += ["", "## Critique and resolution", ""]
    for row in dr.get("resolution", {}).get("rows", []):
        o, r = row["objection"], row.get("response") or {}
        out.append(f"- **{o['id']}** ({o['severity']}, {o['type']}): {o['text']} → {r.get('action', 'no response')} / "
                   f"effective **{row['effective_result']}**" + (f" — {row['code_note']}" if row.get("code_note") else ""))
    out += ["", "## Sources", ""]
    for eid, e in ev.items():
        out.append(f"- **[{eid}]** {e['title']} — {e['publisher']} · tier {e['tier']} · published {e.get('published_at') or e.get('published_hint') or 'unknown'}"
                   + (f" · <{e['url']}>" if e.get("url") else ""))
    out += ["", "## Learn", ""]
    for c in dr["learn"]:
        t, d = CONCEPTS[c]
        out.append(f"- **{t}** — {d}")
    return "\n".join(out)


def save_report(dr: dict, bundle: dict) -> str:
    path = REPORTS / f"{dr['run_id']}.md"
    path.write_text(markdown_report(dr, bundle), encoding="utf-8")
    return str(path)
