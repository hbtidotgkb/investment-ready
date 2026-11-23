"""Pydantic v2 contracts (spec §11).

Two families:
  * LLM output schemas (used with structured outputs) — kept to schema features the API
    supports: objects, lists, enums (Literal), nullable fields. No numeric constraints.
  * Platform records — evidence, bundles, decision records — validated in code.

Missing values are null WITH a status/reason. Zero is always a genuine value.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------- evidence ---

Tier = Literal["T1_official", "T2_licensed_data", "T2_reputable_press", "T3_other_press", "T4_capture_unverified",
               "T5_aggregator", "SYS_computed", "DEMO_synthetic"]

EvidenceKind = Literal["price_series", "computed_feature", "chart_pattern", "exchange_filing",
                       "exchange_announcement", "news_article", "web_search_citation",
                       "browser_capture", "pdf", "manual_fact", "aggregator", "company_profile", "news_summary",
                       "computed_fundamental", "broker_research"]


class Passage(BaseModel):
    id: str                      # e.g. "ev-3:P2"
    text: str


class Evidence(BaseModel):
    id: str                      # e.g. "ev-3"
    kind: EvidenceKind
    title: str
    publisher: str
    url: Optional[str] = None
    tier: Tier
    published_at: Optional[str] = None        # public availability time of this version, if KNOWN
    published_hint: Optional[str] = None      # e.g. search "page_age"; never promoted to published_at
    first_seen_at: str
    ingested_at: str
    access_status: str = "public"             # public | capture_unverified | partially_read | inaccessible
    usage_status: str = "personal_research"   # see spec §7 usage dimension
    passages: list[Passage] = Field(default_factory=list)
    content_hash: Optional[str] = None
    derived_from: Optional[str] = None        # syndication / filing-copy relationship
    retrieved_via: str = ""                   # adapter that produced it
    notes: Optional[str] = None


class Calculation(BaseModel):
    id: str
    name: str
    formula: str
    inputs: list[str] = Field(default_factory=list)
    result: Optional[float] = None
    unit: str = ""
    status: Literal["available", "missing", "not_applicable", "conflicted", "stale"] = "available"
    code_version: str = "features@1"


class FinancialFact(BaseModel):
    id: str
    metric: str
    value: Optional[float] = None
    status: Literal["available", "missing", "not_applicable", "conflicted", "stale"]
    reason: Optional[str] = None
    unit: str = ""
    currency: str = "INR"
    scope: Optional[str] = None               # consolidated | standalone
    period: Optional[str] = None              # quarter | ytd | fy | ttm
    period_end: Optional[str] = None
    evidence_id: Optional[str] = None
    verified: bool = False                    # True only for authoritative + reconciled values


class Bundle(BaseModel):
    schema_version: str = "bundle@2.0"
    bundle_id: str = ""
    parent_bundle_id: Optional[str] = None
    mode: Literal["live", "demo"]
    symbol: str
    company_name: str
    exchange: str = "NSE"
    sector: Optional[str] = None
    isin: Optional[str] = None
    decision_at: str
    reconstruction_mode: str = "actual_system"
    horizon: Literal["short", "medium", "long"]
    action: Literal["new_purchase", "additional_purchase", "continue_holding", "reduce_exit"]
    portfolio_context_status: str = "missing"
    price: dict = Field(default_factory=dict)            # last close, date, source
    candles: list[dict] = Field(default_factory=list)    # trimmed OHLCV for charts
    calculations: list[Calculation] = Field(default_factory=list)
    patterns: list[dict] = Field(default_factory=list)
    facts: list[FinancialFact] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    surveillance: dict = Field(default_factory=lambda: {"status": "unknown"})
    holdings_position: Optional[dict] = None
    quality_flags: list[str] = Field(default_factory=list)
    coverage: dict = Field(default_factory=dict)         # required check -> status
    versions: dict = Field(default_factory=dict)


# ---------------------------------------------------- LLM output schemas ---

class Cite(BaseModel):
    """A citation. `quote` must be a short verbatim span copied from the passage —
    code verifies it so citations cannot be invented."""
    evidence_id: str
    passage_id: Optional[str] = None
    quote: Optional[str] = None


class Statement(BaseModel):
    kind: Literal["observation", "interpretation", "assumption", "conclusion"]
    text: str
    cites: list[Cite]


class Check(BaseModel):
    name: str
    result: Literal["pass", "watch", "fail", "not_applicable", "insufficient_evidence"]
    materiality: Literal["minor", "major", "critical"]
    note: str
    cites: list[Cite]


class HorizonStance(BaseModel):
    horizon: Literal["short", "medium", "long"]
    stance: Literal["attractive", "neutral", "unattractive", "insufficient_data"]
    rationale: str


class Scenario(BaseModel):
    name: Literal["bear", "base", "bull"]
    price_assumption: Optional[float] = None      # model-proposed; code computes returns
    rationale: str
    cites: list[Cite]


class BrainReportLLM(BaseModel):
    specialist_conclusion: Literal["supportive", "neutral", "concerning", "insufficient_evidence"]
    headline: str
    statements: list[Statement]
    checks: list[Check]
    red_flags: list[Statement]
    missing_evidence: list[str]
    stances: list[HorizonStance]
    scenarios: list[Scenario]
    entry_low: Optional[float] = None
    entry_high: Optional[float] = None
    stop: Optional[float] = None
    governance_score: Optional[int] = None        # Forensic only, rubric governance@1
    would_change_mind: list[str]


Decision = Literal["buy", "wait", "avoid", "insufficient_data", "hold", "reduce", "exit"]


class Trigger(BaseModel):
    """What must happen before a WAIT becomes a buy decision. Checked by code."""
    kind: Literal["price", "date", "event"]
    description: str                                   # plain words, e.g. "close below Rs 2,200 (about 25x TTM EPS)"
    price_level: Optional[float] = None                # kind=price
    direction: Optional[Literal["above", "below"]] = None   # kind=price: buy when price goes above/below the level
    date: Optional[str] = None                         # kind=date (YYYY-MM-DD); optional expected date for kind=event


class HeadDecisionLLM(BaseModel):
    decision: Decision
    summary: str
    reasons: list[Statement]
    evidence_sufficiency: Literal["sufficient", "partial", "insufficient"]
    entry_low: Optional[float] = None
    entry_high: Optional[float] = None
    stop: Optional[float] = None
    target_low: Optional[float] = None
    target_high: Optional[float] = None
    head_score: Optional[int] = None              # 0-100 rubric head-score@1
    assumptions: list[str]
    entry_conditions: list[str]
    invalidation_conditions: list[str]
    material_dissent: list[str]
    portfolio_limitations: list[str]
    trigger: Optional[Trigger] = None                  # required when decision == "wait"
    avoid_reason: Optional[str] = None                 # required when decision == "avoid"
    would_change_view: list[str] = Field(default_factory=list)   # required for "avoid": what would change it
    insufficiency_reason: Optional[str] = None         # required for "insufficient_data": the specific evidence


class SettlingTest(BaseModel):
    description: str


class Objection(BaseModel):
    id: str
    severity: Literal["blocker", "major", "minor"]
    type: Literal["factual", "numerical", "source_support", "valuation_assumption",
                  "missing_material_evidence", "portfolio_fit", "policy_compliance", "logic"]
    text: str
    cites: list[Cite]
    decision_impact: str
    settling_test: SettlingTest


class CritiqueLLM(BaseModel):
    checklist: list[str]               # what was reviewed (facts, calculations, sources, ...)
    objections: list[Objection]        # zero to seven; never a quota


class ObjectionResponse(BaseModel):
    objection_id: str
    action: Literal["accept", "reject", "defer"]
    test_result: Literal["passed", "failed", "unresolved"]
    explanation: str
    cites: list[Cite]
    decision_effect: str


class HeadFinalLLM(BaseModel):
    responses: list[ObjectionResponse]
    final: HeadDecisionLLM


class ClaimLLM(BaseModel):
    text: str
    claim_type: Literal["recommendation", "target_price", "financial_result", "order_win",
                        "guidance", "management", "regulatory", "opinion", "other"]
    company_mention: str
    direction: Optional[Literal["positive", "negative", "neutral"]] = None
    target_price: Optional[float] = None
    horizon_text: Optional[str] = None
    author_or_broker: Optional[str] = None
    passage_id: str
    quote: str
    verification_task: str


class ClaimsLLM(BaseModel):
    claims: list[ClaimLLM]


class IdeaLLM(BaseModel):
    company_mention: str                       # exactly as written in the article
    stance: Literal["buy", "accumulate", "hold", "reduce", "sell", "avoid", "neutral_mention"]
    target_price: Optional[float] = None
    horizon_text: Optional[str] = None
    recommended_by: Optional[str] = None       # broker / analyst / author, when stated
    rationale: str
    passage_id: str
    quote: str                                 # verbatim 4-25 words from that passage


class IdeasLLM(BaseModel):
    article_type: Literal["stock_recommendations", "sector_or_theme", "single_company", "market_commentary", "other"]
    ideas: list[IdeaLLM]


# ----------------------------------------------------- browser intake ---

class CapturePassage(BaseModel):
    id: str
    text: str = Field(max_length=20000)


class CaptureItem(BaseModel):
    capture_id: str = Field(max_length=100)
    url: str = Field(max_length=2000)
    title: str = Field(default="", max_length=500)
    author: Optional[str] = Field(default=None, max_length=300)
    published_at: Optional[str] = Field(default=None, max_length=60)
    captured_at: str = Field(max_length=60)
    extraction_method: str = Field(default="", max_length=60)
    passages: list[CapturePassage] = Field(max_length=400)
    access_warnings: list[str] = Field(default_factory=list, max_length=20)
    truncated: bool = False
    related_links: list[str] = Field(default_factory=list, max_length=50)
    usage_attested: bool = False               # user ticked "personal research use"
    queue_id: Optional[str] = Field(default=None, max_length=60)   # set when captured from the article queue


class CaptureBatch(BaseModel):
    schema_version: Literal["capture@1"] = "capture@1"
    research_session_id: str = Field(max_length=100)
    extension_version: str = Field(max_length=20)
    items: list[CaptureItem] = Field(max_length=10)   # spec §7: ten sources per session
