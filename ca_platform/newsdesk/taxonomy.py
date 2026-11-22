"""Rule-based labelling: event type, direction, fact/speculation/opinion, rupee amounts tagged
with the metric they measure, and short checkable claims. Deterministic and testable.

Ported from Newsdesk's enrich.py with fixes from the review: a "business_development" type
(approvals, launches, partnerships) ranked high; reported figures are facts even in price-move
stories; every amount carries a metric so a press figure is only compared with a filing figure
of the same kind (spec §6 cross-check rule 6).
"""
from __future__ import annotations

import re

# type: (base materiality, half-life hours, pattern)
EVENT_TYPES: dict[str, tuple[float, int, str]] = {
    "scheduled": (0.15, 336, r"board meeting intimation|intimation (of|for|regarding) (the )?board meeting|prior intimation|"
                             r"meeting of the board.{0,80}\bwill be held\b|(intimation|schedule) of (an? )?(earnings|conference|analyst|investor) (call|meet)|"
                             r"analysts?/institutional investor meet"),
    "regulatory_action": (0.75, 1440, r"\bsebi\b.*\b(order|penalt|show.cause|ban|probe)|\brbi\b.*\b(penalt|restrict|curb)|show.cause notice|\bpenalty\b|\bbarred\b|adjudication order"),
    "governance_flag": (0.80, 2160, r"related.party|auditor.{0,20}(resign|quit|qualif)|corporate governance|forensic audit|whistle.?blower|siphon|fund diversion"),
    "accounting_concern": (0.80, 2160, r"restat|accounting (irregular|lapse)|qualified opinion|emphasis of matter"),
    "clarification": (0.40, 168, r"clarification|clarifies|news item|media report|market rumou?r|spurt in (price|volume)"),
    "results": (0.70, 336, r"\bq[1-4]\b|quarterly results|financial results|net profit|\bpat\b|\bebitda\b|results for the (quarter|period|year)"),
    "guidance": (0.65, 336, r"guidance|outlook|forecast|expects? (revenue|growth|margin)"),
    "m_and_a": (0.75, 720, r"acquisi|acquire|merger|amalgamat|demerg|takeover|stake (sale|purchase)|open offer|scheme of arrangement"),
    "order_win": (0.55, 336, r"\border(s)?\b.*\b(win|won|bag|secur|receiv)|\b(won|wins|bags?|bagged|secures?|secured|receives?|received|receipt of)\b[^.]{0,60}?\b(orders?|contracts?)\b|order inflow|letter of (award|intent)|\bLoA\b|contract (win|award)"),
    "business_development": (0.55, 504, r"approv(al|ed) (by|from)|\blaunch(es|ed)?\b|partnership|collaborat|\bmou\b|tie.?up|joint venture|new product|licen[cs]e (granted|received)|regulatory approval|\bcftc\b|\busfda\b"),
    "rating_action": (0.55, 720, r"\b(crisil|icra|care ratings|india ratings|moody|fitch|s&p)\b|credit rating|rating (upgrade|downgrade|reaffirm)"),
    "insider_pledge": (0.55, 720, r"pledge|insider trad|\bsast\b|promoter (sell|buy|stake|holding)|block deal|bulk deal|encumbrance"),
    "management_change": (0.50, 336, r"resign|appoint|\bceo\b|\bcfo\b|managing director|steps down|key managerial|cessation"),
    "litigation": (0.55, 720, r"\bnclt\b|court|tribunal|arbitration|lawsuit|insolvency|\bed\b raid|income tax (raid|search)"),
    "fundraise": (0.50, 336, r"\bqip\b|rights issue|preferential (issue|allot)|fund.?rais|\bncds?\b|bond issue|\bipo\b"),
    "capex": (0.45, 336, r"capex|capacity expansion|new plant|commission(ed|ing)|greenfield"),
    "dividend_buyback": (0.35, 168, r"dividend|buy.?back|bonus issue|stock split|record date"),
    "analyst_view": (0.20, 72, r"target price|\b(buy|sell|hold|accumulate|reduce) (rating|call)\b|brokerage|stocks? to buy|should you"),
    "price_move": (0.10, 48, r"shares? (jump|surge|soar|rise|fall|slump|plunge|tank|rally|climb|drop)|stock (jumps|surges|soars|rises|falls|slumps|plunges|tanks|rallies)|hits? (52|record)"),
    "sector_macro": (0.30, 120, r"\bsector\b|industry|\bpolicy\b|tariff|\bgdp\b|inflation|repo rate"),
    "other": (0.15, 72, r""),
}
# Checked in this order (specific before generic); results beats price_move so a headline that
# reports a figure while mentioning a price move is still a results event.
ORDER = ["scheduled", "regulatory_action", "governance_flag", "accounting_concern", "clarification", "results",
         "guidance", "m_and_a", "order_win", "rating_action", "insider_pledge", "management_change", "litigation",
         "fundraise", "capex", "dividend_buyback", "business_development", "analyst_view", "price_move", "sector_macro"]

NSE_CATEGORY = {
    "financial result updates": "results", "outcome of board meeting": "results", "board meeting intimation": "scheduled",
    "analysts/institutional investor meet/con. call updates": "scheduled", "credit rating": "rating_action",
    "acquisition": "m_and_a", "amalgamation/ merger": "m_and_a", "change in director(s)": "management_change",
    "resignation": "management_change", "appointment": "management_change", "dividend": "dividend_buyback",
    "record date": "dividend_buyback", "buy back": "dividend_buyback", "press release": "other", "updates": "other",
    "clarification - financial results": "clarification", "spurt in volume": "clarification", "clarification": "clarification",
    "litigation / dispute / regulatory action(s)": "litigation", "award of order / receipt of order": "order_win",
    "bagging/receiving of orders/contracts": "order_win", "pledge": "insider_pledge", "fund raising": "fundraise",
}

POSITIVE = r"\b(surge|jump|soar|rise|rises|gain|beat|record|upgrade|wins?|bags?|secures?|strong|growth|profit up|expands?|approv\w*|order|launch\w*)\b"
NEGATIVE = r"\b(fall|falls|slump|plunge|drop|decline|miss|downgrade|loss|penalt\w*|probe|resign\w*|default|pledge|cut|weak|concern|fraud|raid|ban|denies|show.cause)\b"
SPECULATION = r"sources (said|say)|people (familiar|aware)|according to people|reportedly|likely to|in talks|is said to|considering|mulls|could\b|may\b"
OPINION = (r"target price|price target|price forecast|stock forecast|should you|stocks? to buy|our view|analysts? (say|believe|see|expect)|"
           r"brokerage|recommend|rated (buy|sell|hold)|\b(buy|sell|hold) rating|looks (cheap|discounted|expensive|undervalued)|"
           r"^\s*(buy|sell|hold|accumulate|reduce|add)\b[^.]{0,80}\btarget\b|target of rs")
REPORTED_FIGURE = r"(profit|revenue|pat|ebitda|net income|sales|order|margin)\D{0,30}\d"

AMOUNT = re.compile(r"(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d+)?)\s*(lakh crore|lakh cr|crore|cr\b|billion|bn\b|million|mn\b|lakh)?"
                    r"|([\d,]+(?:\.\d+)?)\s*(lakh crore|crore|cr\b)", re.IGNORECASE)
UNIT_TO_CR = {"lakh crore": 1e5, "lakh cr": 1e5, "crore": 1, "cr": 1, "billion": 100, "bn": 100, "million": 0.1, "mn": 0.1, "lakh": 0.01}
METRICS = [("order_value", r"order|contract|award|deal|loa\b"), ("revenue", r"revenue|sales|turnover|income from operations|top.?line"),
           ("profit", r"profit|\bpat\b|net income|earnings|bottom.?line"), ("ebitda", r"ebitda|operating profit"),
           ("dividend", r"dividend"), ("fundraise", r"qip|raise|rights|preferential|ncd|bond"),
           ("capex", r"capex|invest|plant|capacity"), ("penalty", r"penalt|fine\b|demand notice")]


def classify(title: str, body: str = "", category: str | None = None, official: bool = False) -> str:
    if category:
        cat = NSE_CATEGORY.get(category.strip().lower())
        if cat and cat != "other":
            return cat
    text = f"{title}. {(body or '')[:1500]}".lower()
    if not official and re.search(OPINION, title, re.I):
        return "analyst_view"          # an opinion headline is a view, even if it mentions Q2 or results
    for etype in ORDER:
        if re.search(EVENT_TYPES[etype][2], text, re.IGNORECASE):
            if etype == "clarification" and not official:
                continue  # only a filing is a clarification; press "denies" is ordinary news
            return etype
    return "other"


def direction(text: str) -> str:
    p = len(re.findall(POSITIVE, text, re.I))
    n = len(re.findall(NEGATIVE, text, re.I))
    return "+" if p > n else "-" if n > p else "0"


def fact_type(title: str, body: str = "") -> str:
    head = f"{title}. {(body or '')[:600]}"
    if re.search(REPORTED_FIGURE, head, re.I) and not re.search(OPINION, title, re.I):
        return "fact"              # reported figures are facts even inside price-move stories
    if re.search(OPINION, head, re.I):
        return "opinion"
    if re.search(SPECULATION, head, re.I):
        return "speculation"
    return "fact"


def amounts(text: str) -> list[dict]:
    """[{amount_cr, metric, context}] — metric from the words around the figure."""
    out = []
    for m in AMOUNT.finditer(text or ""):
        num, unit = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        if not unit:
            continue
        try:
            val = float(num.replace(",", "")) * UNIT_TO_CR[unit.lower().strip()]
        except (ValueError, KeyError):
            continue
        # the metric is the keyword NEAREST the figure ("revenue seen at Rs 9,000 crore on new order" -> revenue)
        lo = max(0, m.start() - 70)
        ctx = text[lo: m.end() + 30].lower()
        pos = m.start() - lo
        best, best_d = "amount", 10**9
        for name, pat in METRICS:
            for k in re.finditer(pat, ctx):
                dist = pos - k.end() if k.end() <= pos else k.start() - (pos + len(m.group(0)))
                dist = abs(dist) + (15 if k.start() > pos else 0)    # prefer words before the figure
                if dist < best_d:
                    best, best_d = name, dist
        metric = best
        out.append({"amount_cr": round(val, 2), "metric": metric, "context": " ".join(text[max(0, m.start() - 70): m.end() + 30].split())})
    return out


def claims(title: str, body: str = "", limit: int = 3) -> list[dict]:
    """Up to `limit` checkable claims: sentences carrying an amount, else the headline."""
    out = []
    sentences = re.split(r"(?<=[.!?])\s+", f"{title}. {(body or '')[:4000]}")
    for s in sentences:
        am = amounts(s)
        if am:
            out.append({"text": " ".join(s.split())[:300], "amount_cr": am[0]["amount_cr"], "metric": am[0]["metric"]})
        if len(out) >= limit:
            break
    if not out:
        out.append({"text": " ".join(title.split())[:300], "amount_cr": None, "metric": None})
    return out


def label(title: str, body: str = "", category: str | None = None, official: bool = False) -> dict:
    text = f"{title}. {(body or '')[:1500]}"
    etype = classify(title, body, category, official)
    am = amounts(text)
    return {"event_type": etype, "direction": direction(text), "fact_type": "fact" if official else fact_type(title, body),
            "amount_cr": am[0]["amount_cr"] if am else None, "metric": am[0]["metric"] if am else None,
            "claims": claims(title, body)}
