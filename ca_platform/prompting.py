"""Prompt files (prompts/<role>/<semver>.md with front matter) and role-specific views.

Views are where independence (spec N3) and the evidence boundary are enforced: each brain
gets only its role's slice of the frozen bundle; Head/Critic get anonymised reports plus the
passages those reports cite. All source text is wrapped as data, separate from policy.
"""
from __future__ import annotations

import html
import json
from functools import lru_cache

import yaml

from .citations import CitationIndex
from .contracts import Bundle, Evidence
from .settings import PROMPTS, config


SHARED_PROMPT = "_shared@2.1.0"


@lru_cache(maxsize=None)
def load_prompt(ref: str) -> tuple[str, dict]:
    """ref = 'quant@2.0.0' -> (body, front_matter)."""
    role, ver = ref.split("@")
    text = (PROMPTS / role / f"{ver}.md").read_text(encoding="utf-8")
    meta: dict = {}
    if text.startswith("---"):
        _, fm, body = text.split("---", 2)
        meta = yaml.safe_load(fm) or {}
        text = body.strip()
    return text, meta


def system_prompt(role: str) -> tuple[str, str]:
    """Shared policy + role prompt. Returns (system, prompt_version)."""
    ref = config("models")["roles"][role]["prompt"]
    shared, _ = load_prompt(SHARED_PROMPT)
    body, meta = load_prompt(ref)
    return f"{shared}\n\n{body}", meta.get("version", ref)


# -------------------------------------------------------------- rendering ---

ROLE_KINDS = {
    "quant": {"price_series", "computed_feature", "chart_pattern", "manual_fact", "exchange_filing", "company_profile",
              "news_summary", "exchange_announcement", "computed_fundamental", "broker_research", "pdf"},
    "news": {"news_article", "web_search_citation", "exchange_announcement", "browser_capture", "pdf",
             "company_profile", "computed_feature", "news_summary", "broker_research"},
    "forensic": {"exchange_filing", "manual_fact", "exchange_announcement", "company_profile", "web_search_citation",
                 "news_summary", "news_article", "browser_capture", "computed_fundamental", "pdf"},
}
# Newsdesk items carry retrieved_via "newsdesk:<source>:<event_type>"; narrow them per remit.
FORENSIC_PRESS_TYPES = ("governance_flag", "accounting_concern", "regulatory_action", "insider_pledge", "litigation")
QUANT_FILING_TYPES = ("results", "dividend_buyback", "fundraise", "scheduled")


def _role_allows(role: str, e: Evidence) -> bool:
    via = e.retrieved_via or ""
    etype = via.rsplit(":", 1)[-1] if via.startswith("newsdesk:") else ""
    if role == "quant" and e.kind == "exchange_announcement":
        return etype in QUANT_FILING_TYPES          # filed results/catalysts only; no other announcements
    if role == "forensic" and e.kind in ("news_article", "browser_capture"):
        return etype in FORENSIC_PRESS_TYPES        # press governance/accounting/regulatory reports only
    return True
# Within shared kinds, narrow by acquisition path so each role sees its remit.
ROLE_EXCLUDE_VIA = {
    "quant": {"discovery:news"},
    "forensic": {"discovery:news"},
    "news": {"discovery:filings"},
}
MAX_VIEW_CHARS = 180_000


def _esc(s: str | None) -> str:
    return html.escape(s or "", quote=True)


def render_evidence(items: list[Evidence], only_passages: set[str] | None = None) -> str:
    out = []
    for e in items:
        ps = [p for p in e.passages if only_passages is None or p.id in only_passages]
        if not ps:
            continue
        attrs = (f'id="{e.id}" kind="{e.kind}" tier="{e.tier}" publisher="{_esc(e.publisher)}" '
                 f'published_at="{e.published_at or "unknown"}"'
                 + (f' published_hint="{_esc(e.published_hint)}"' if e.published_hint else "")
                 + f' access="{e.access_status}"' + (f' url="{_esc(e.url)}"' if e.url else ""))
        body = "\n".join(f'  <passage id="{p.id}">{_esc(p.text)}</passage>' for p in ps)
        out.append(f"<evidence {attrs}>\n  <title>{_esc(e.title)}</title>\n{body}\n</evidence>")
    return "\n".join(out)


def context_block(b: Bundle, pre: list[dict], prior: list[dict]) -> str:
    hz = config("horizons")["horizons"][b.horizon]
    ctx = {"symbol": b.symbol, "company": b.company_name, "exchange": b.exchange, "sector": b.sector,
           "mode": b.mode, "decision_at": b.decision_at, "horizon": {"name": b.horizon, **hz},
           "intended_action": b.action, "portfolio_context_status": b.portfolio_context_status,
           "holdings_position": b.holdings_position, "quality_flags": b.quality_flags, "coverage": b.coverage}
    parts = [f"<context>\n{json.dumps(ctx, indent=1, default=str)}\n</context>",
             "<preflight>\n" + json.dumps([{k: r[k] for k in ("rule", "result", "detail")} for r in pre], indent=1)
             + "\n</preflight>"]
    if prior:
        parts.append("<prior_decisions>\n" + json.dumps(prior, indent=1, default=str) + "\n</prior_decisions>")
    return "\n".join(parts)


def role_items(role: str, b: Bundle) -> list[Evidence]:
    """The evidence items a specialist is allowed to see (before the length cap)."""
    kinds, excl = ROLE_KINDS[role], ROLE_EXCLUDE_VIA[role]
    items = [e for e in b.evidence if e.kind in kinds and e.retrieved_via not in excl and e.usage_status != "excluded"
             and _role_allows(role, e)]
    if role == "news":  # News sees only the return/price-reaction slice of computed features
        items = [e for e in items if e.kind != "computed_feature" or e.title.startswith("Price reaction")]
    return items


def brain_view(role: str, b: Bundle, pre: list[dict], prior: list[dict]) -> tuple[str, list[str]]:
    """User message for a specialist. Returns (text, omitted_evidence_ids)."""
    items = role_items(role, b)
    rendered, omitted, total = [], [], 0
    cap = int((config("user").get("analysis") or {}).get("max_view_chars", MAX_VIEW_CHARS))
    for e in items:
        r = render_evidence([e])
        if total + len(r) > cap:
            omitted.append(e.id)
            continue
        rendered.append(r)
        total += len(r)
    facts = [f.model_dump(exclude_none=True) for f in b.facts] if role in ("quant", "forensic") else []
    extra = ""
    if role == "quant":
        extra = "\n<patterns>\n" + json.dumps(b.patterns, indent=1) + "\n</patterns>"
    # Specialists judge the current evidence afresh; prior verdicts go only to the Head.
    msg = (f"{context_block(b, pre, [])}\n"
           f"<bundle bundle_id=\"{b.bundle_id}\">\n"
           + ("<facts>\n" + json.dumps(facts, indent=1) + "\n</facts>\n" if facts else "")
           + "\n".join(rendered) + extra + "\n</bundle>\n")
    if omitted:
        msg += (f"\nNOTE: {len(omitted)} evidence item(s) were omitted for length and are NOT visible to you: "
                f"{', '.join(omitted)}. Do not assume their content.\n")
    msg += f"\nProduce your {role} report for the {b.horizon} horizon as the required JSON object."
    return msg, omitted


def annotate_report(rep: dict, idx: CitationIndex) -> dict:
    """Attach code verification status to every citation in a report dict (copy)."""
    def walk(o):
        if isinstance(o, dict):
            o = {k: walk(v) for k, v in o.items()}
            if "cites" in o and isinstance(o["cites"], list):
                o["cites"] = [dict(c, status=idx.check(c)) for c in o["cites"]]
            return o
        if isinstance(o, list):
            return [walk(x) for x in o]
        return o
    return walk(rep)


def cited_passage_ids(obj) -> set[str]:
    ids: set[str] = set()

    def walk(o):
        if isinstance(o, dict):
            if "evidence_id" in o and o.get("passage_id"):
                ids.add(o["passage_id"])
            elif "evidence_id" in o:
                ids.add(o["evidence_id"] + ":*")
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    walk(obj)
    return ids


def panel_bundle_view(b: Bundle, reports_obj) -> str:
    """Passages cited by the reports, plus all computed passages and official filings."""
    want = cited_passage_ids(reports_obj)
    whole = {w[:-2] for w in want if w.endswith(":*")}
    keep: set[str] = set()
    for e in b.evidence:
        core = (e.kind in ("computed_feature", "chart_pattern", "company_profile", "news_summary", "broker_research")
                or e.tier == "T1_official")
        for p in e.passages:
            if core or p.id in want or e.id in whole:
                keep.add(p.id)
    return f"<bundle bundle_id=\"{b.bundle_id}\">\n{render_evidence(b.evidence, keep)}\n</bundle>"
