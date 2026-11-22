"""Fundamentals computed in code from official XBRL filings (rubric-free arithmetic, fundamentals@1).

Inputs come only from the company's own filings (see data/nse_xbrl.py). Each ratio carries its
formula and the evidence ids of the filings it used; a value that cannot be computed honestly
(missing quarter, negative earnings, non-consecutive periods) is reported as missing with a reason —
never estimated. Filed values are marked `verified` only when the filing's own arithmetic reconciles.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

from ..contracts import Calculation, FinancialFact
from ..evidence import EvidenceFactory, fmt

CODE_VERSION = "fundamentals@1"
CR = 1e7

T = {   # concept → XBRL tags in preference order (Ind AS non-bank, then banking format)
    "revenue": ("RevenueFromOperations",),
    "total_income": ("Income",),
    "other_income": ("OtherIncome",),
    "pbt": ("ProfitBeforeTax", "ProfitLossFromOrdinaryActivitiesBeforeTax"),
    "tax": ("TaxExpense",),
    "pat": ("ProfitLossForPeriod", "ProfitLossForThePeriod"),
    "pat_cont": ("ProfitLossForPeriodFromContinuingOperations", "ProfitLossFromOrdinaryActivitiesAfterTax"),
    "pat_owners": ("ProfitOrLossAttributableToOwnersOfParent", "ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates",
                   "ProfitLossForPeriod", "ProfitLossForThePeriod"),
    "eps": ("DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations", "DilutedEarningsPerShareAfterExtraordinaryItems",
            "DilutedEarningsLossPerShareFromContinuingOperations", "DilutedEarningsPerShareBeforeExtraordinaryItems"),
    "exceptional": ("ExceptionalItemsBeforeTax", "ExceptionalItems"),
    "paid_up": ("PaidUpValueOfEquityShareCapital",),
    "face": ("FaceValueOfEquityShareCapital",),
    "equity": ("EquityAttributableToOwnersOfParent", "Equity"),
    "ocf": ("CashFlowsFromUsedInOperatingActivities",),
    "interest_earned": ("InterestEarned",),
    "interest_expended": ("InterestExpended",),
    "provisions": ("ProvisionsOtherThanTaxAndContingencies",),
    "gnpa_amt": ("GrossNonPerformingAssets",),
    "gnpa_pct": ("PercentageOfGrossNpa",),
    "nnpa_pct": ("PercentageOfNpa",),
    "cet1": ("CET1Ratio",),
    "at1": ("AdditionalTier1Ratio",),
    "roa": ("ReturnOnAssets",),
}
LABEL = {"revenue": "Revenue from operations", "total_income": "Total income", "other_income": "Other income",
         "pbt": "Profit before tax", "tax": "Tax expense", "pat_owners": "Net profit attributable to owners",
         "eps": "Diluted EPS", "exceptional": "Exceptional items", "ocf": "Operating cash flow (year to date)",
         "interest_earned": "Interest earned", "interest_expended": "Interest expended",
         "provisions": "Provisions (other than tax) and contingencies", "gnpa_amt": "Gross NPA",
         "gnpa_pct": "Gross NPA ratio", "nnpa_pct": "Net NPA ratio", "cet1": "CET-1 ratio", "at1": "Additional Tier-1 ratio",
         "roa": "Return on assets (quarter, not annualised)"}
MONEY = {"revenue", "total_income", "other_income", "pbt", "tax", "pat_owners", "exceptional", "ocf", "interest_earned",
         "interest_expended", "provisions", "gnpa_amt"}
RATIO = {"gnpa_pct", "nnpa_pct", "cet1", "at1", "roa"}


def g(d: dict, concept: str) -> Optional[float]:
    for t in T[concept]:
        if d.get(t) is not None:
            return d[t]
    return None


def pct(v: Optional[float]) -> Optional[float]:
    """XBRL 'pure' ratios are fractions (0.0111 = 1.11%)."""
    if v is None:
        return None
    return v * 100 if abs(v) <= 1 else v


def _d(s: str) -> date:
    return date.fromisoformat(s[:10])


def consecutive(qs: list[dict], start: int, n: int) -> bool:
    if len(qs) < start + n:
        return False
    for a, b in zip(qs[start:start + n - 1], qs[start + 1:start + n]):
        gap = (_d(a["meta"]["period_end"]) - _d(b["meta"]["period_end"])).days
        if not 80 <= gap <= 100:
            return False
    return True


def year_ago(qs: list[dict], i: int = 0) -> Optional[int]:
    for j in range(i + 1, len(qs)):
        gap = (_d(qs[i]["meta"]["period_end"]) - _d(qs[j]["meta"]["period_end"])).days
        if 355 <= gap <= 375:
            return j
    return None


def reconcile(p: dict) -> dict:
    """The filing's own arithmetic: income = revenue + other income; PBT − tax = profit (continuing)."""
    q, notes, ok = p["q"], [], True
    checked = 0
    inc, rev, oi = q.get("Income"), (g(q, "revenue") or g(q, "interest_earned")), g(q, "other_income")
    if None not in (inc, rev, oi):
        checked += 1
        if abs(inc - (rev + oi)) > max(abs(inc) * 0.005, 1e7):
            ok = False
            notes.append(f"Total income {fmt(inc / CR)} cr ≠ revenue/interest {fmt(rev / CR)} + other income {fmt(oi / CR)}")
    pbt, tax, pc = g(q, "pbt"), g(q, "tax"), g(q, "pat_cont")
    if None not in (pbt, tax, pc):
        checked += 1
        if abs((pbt - tax) - pc) > max(abs(pc) * 0.01, 1e7):
            ok = False
            notes.append(f"PBT {fmt(pbt / CR)} − tax {fmt(tax / CR)} ≠ profit {fmt(pc / CR)} cr")
    return {"ok": ok if checked else None, "checks": checked, "notes": notes}


def auditor_key(name: str) -> str:
    """'B S R & Co. LLP' and 'BSR and Co LLP' are the same firm."""
    n = re.sub(r"\band\b", "&", name.lower())
    return re.sub(r"[^a-z0-9&]", "", n)


def shares(p: dict) -> Optional[float]:
    pu, fv = g(p["q"], "paid_up"), g(p["q"], "face")
    return pu / fv if pu and fv else None


def book_equity(p: dict) -> Optional[float]:
    bs = p["bs"]
    if p["meta"].get("is_bank"):
        c, r = bs.get("Capital"), bs.get("ReservesAndSurplus")
        return c + r if c is not None and r is not None else None
    return g(bs, "equity")


def build(snap: dict, close: Optional[float]) -> dict:
    """All metrics with formula, inputs (quarter/shareholding indexes) and status."""
    qs: list[dict] = snap.get("quarters") or []
    shp: list[dict] = snap.get("shareholding") or []
    bank = bool(qs and qs[0]["meta"].get("is_bank"))
    out: dict = {"code_version": CODE_VERSION, "is_bank": bank, "scope": snap.get("scope"), "metrics": [],
                 "flags": [], "reconciliation": [reconcile(p) for p in qs]}
    M = out["metrics"]

    def add(name, label, value, unit, formula, q_idx=(), s_idx=(), status=None, note=None):
        M.append({"name": name, "label": label, "value": value, "unit": unit, "formula": formula,
                  "q": list(q_idx), "s": list(s_idx), "status": status or ("available" if value is not None else "missing"),
                  "note": note})

    if not qs:
        out["flags"].append("No results filings available — valuation and fundamentals cannot be computed")
    for i, r in enumerate(out["reconciliation"]):
        if r["ok"] is False:
            out["flags"].append(f"Quarter ended {qs[i]['meta']['period_end']}: filing arithmetic does not reconcile — "
                                + "; ".join(r["notes"]))

    ttm_ok = consecutive(qs, 0, 4)
    q4 = list(range(4))

    def ttm(concept, start=0):
        if not consecutive(qs, start, 4):
            return None
        vals = [g(qs[k]["q"], concept) for k in range(start, start + 4)]
        return None if any(v is None for v in vals) else sum(vals)

    eps_ttm = ttm("eps")
    pat_ttm = ttm("pat_owners")
    top_concept = "total_income" if bank else "revenue"
    rev_ttm = ttm(top_concept)
    why_ttm = None if ttm_ok else f"needs 4 consecutive quarterly filings (have {len(qs)})"
    add("eps_ttm", "EPS, trailing 12 months (diluted)", eps_ttm, "INR",
        "sum of diluted EPS of the last 4 quarters", q4 if ttm_ok else (), note=why_ttm)
    sh = shares(qs[0]) if qs else None
    mcap = close * sh / CR if close and sh else None
    add("market_cap_cr", "Market capitalisation", mcap, "INR crore", "last close × shares (paid-up capital ÷ face value)", [0] if qs else ())
    if eps_ttm is not None and close:
        if eps_ttm > 0:
            add("pe_ttm", "P/E (trailing)", close / eps_ttm, "x", f"last close {fmt(close)} ÷ TTM EPS {fmt(eps_ttm)}", q4)
            add("earnings_yield_pct", "Earnings yield", eps_ttm / close * 100, "%", "TTM EPS ÷ last close", q4)
        else:
            add("pe_ttm", "P/E (trailing)", None, "x", "last close ÷ TTM EPS", q4, status="not_applicable",
                note="TTM EPS is zero or negative — P/E is not meaningful")
    else:
        add("pe_ttm", "P/E (trailing)", None, "x", "last close ÷ TTM EPS", note=why_ttm or "no price")

    bs_i = next((i for i, p in enumerate(qs) if book_equity(p) is not None), None)
    if bs_i is not None:
        eq = book_equity(qs[bs_i])
        sh_b = shares(qs[bs_i])
        bvps = eq / sh_b if sh_b else None
        bs_date = qs[bs_i]["meta"]["bs_date"]
        add("book_value_cr", f"Book value (equity) at {bs_date}", eq / CR, "INR crore",
            "Capital + Reserves and surplus" if bank else "Equity attributable to owners", [bs_i])
        add("bvps", "Book value per share", bvps, "INR", "book value ÷ shares", [bs_i])
        if bvps and close:
            add("pb", "P/B", close / bvps, "x", f"last close {fmt(close)} ÷ book value per share {fmt(bvps)}", [bs_i])
        if pat_ttm is not None:
            add("roe_pct", "Return on equity (TTM profit ÷ period-end equity)", pat_ttm / eq * 100, "%",
                "TTM profit attributable to owners ÷ latest reported equity", q4 + ([bs_i] if bs_i > 3 else []))
    else:
        add("pb", "P/B", None, "x", "last close ÷ book value per share", note="no balance sheet in the filings read")

    yi = year_ago(qs, 0)
    if yi is not None:
        for concept, name, label in ((top_concept, "revenue_yoy_pct", f"{LABEL[top_concept]} growth vs same quarter last year"),
                                     ("pat_owners", "profit_yoy_pct", "Net profit growth vs same quarter last year")):
            a, b = g(qs[0]["q"], concept), g(qs[yi]["q"], concept)
            v = (a / b - 1) * 100 if a is not None and b and b > 0 else None
            add(name, label, v, "%", f"quarter ended {qs[0]['meta']['period_end']} ÷ quarter ended {qs[yi]['meta']['period_end']} − 1",
                [0, yi], note=None if v is not None else "base quarter missing or not positive")
    else:
        add("revenue_yoy_pct", "Revenue growth vs same quarter last year", None, "%", "", note="year-ago quarter not in the filings read")

    oi, pbt_t, tax_t = ttm("other_income"), ttm("pbt"), ttm("tax")
    if oi is not None and pbt_t and not bank:          # for banks, fee income is core business, not "other"
        add("other_income_share_pct", "Other income as a share of profit before tax (TTM)", oi / pbt_t * 100, "%",
            "TTM other income ÷ TTM profit before tax", q4)
    if tax_t is not None and pbt_t and pbt_t > 0:
        add("effective_tax_rate_pct", "Effective tax rate (TTM)", tax_t / pbt_t * 100, "%", "TTM tax expense ÷ TTM profit before tax", q4)
    exc = [g(qs[k]["q"], "exceptional") for k in range(min(4, len(qs)))]
    if qs and any(e is not None for e in exc):
        n_nz = sum(1 for e in exc if e)
        add("exceptional_quarters", "Quarters with exceptional items (last 4)", float(n_nz), "count",
            "count of quarters with non-zero exceptional items", list(range(len(exc))),
            note=f"total {fmt(sum(e or 0 for e in exc) / CR)} crore")

    if not bank:
        ocf_i = next((i for i, p in enumerate(qs) if g(p["ytd"], "ocf") is not None), None)
        if ocf_i is not None:
            p = qs[ocf_i]
            ocf, pat_y = g(p["ytd"], "ocf"), g(p["ytd"], "pat")
            add("ocf_to_profit", f"Operating cash flow ÷ net profit ({p['meta']['ytd_start']} to {p['meta']['period_end']})",
                ocf / pat_y if pat_y and pat_y > 0 else None, "x", "year-to-date operating cash flow ÷ year-to-date net profit", [ocf_i])
        if bs_i is not None:
            bs = qs[bs_i]["bs"]
            tr = (bs.get("TradeReceivablesCurrent") or 0) + (bs.get("TradeReceivablesNoncurrent") or 0)
            rev_b = ttm("revenue", bs_i)
            if tr and rev_b:
                add("receivable_days", "Receivable days", tr / rev_b * 365, "days",
                    "trade receivables ÷ TTM revenue (ending at the balance-sheet date) × 365", list(range(bs_i, bs_i + 4)))
            debt = (bs.get("BorrowingsCurrent") or 0) + (bs.get("BorrowingsNoncurrent") or 0)
            eq_all = bs.get("Equity") or g(bs, "equity")          # total equity incl. non-controlling interests
            if eq_all and ("BorrowingsCurrent" in bs or "BorrowingsNoncurrent" in bs):
                add("debt_to_equity", "Debt to equity", debt / eq_all, "x", "current + non-current borrowings ÷ equity", [bs_i])
    else:
        p0 = qs[0]
        for c in ("gnpa_pct", "nnpa_pct"):
            v = pct(g(p0["q"], c))
            prev = pct(g(qs[yi]["q"], c)) if yi is not None else None
            add(c, LABEL[c], v, "%", "as filed", [0] + ([yi] if yi is not None else []),
                note=f"year ago {fmt(prev)}%" if prev is not None else None)
        gn_amt, nn_pct = g(p0["q"], "gnpa_amt"), pct(g(p0["q"], "nnpa_pct"))
        adv = p0["bs"].get("Advances")
        if gn_amt and nn_pct is not None and adv:
            nn_amt = nn_pct / 100 * adv
            add("pcr_pct_approx", "Provision coverage (approx., excl. technical write-offs)", (1 - nn_amt / gn_amt) * 100, "%",
                "1 − (net NPA % × net advances) ÷ gross NPA amount", [0],
                note="net NPA amount derived from the rounded net NPA % — approximate")
        else:
            add("pcr_pct_approx", "Provision coverage", None, "%", "", note="needs gross NPA amount, net NPA % and advances in one filing")
        add("cet1_pct", LABEL["cet1"], pct(g(p0["q"], "cet1")), "%", "as filed", [0])
        add("roa_quarter_pct", LABEL["roa"], pct(g(p0["q"], "roa")), "%", "as filed", [0])
        if yi is not None:
            for k, label in (("Deposits", "Deposit growth (year on year)"), ("Advances", "Advances growth (year on year)")):
                a, b = p0["bs"].get(k), qs[yi]["bs"].get(k)
                if a and b:
                    add(k.lower() + "_yoy_pct", label, (a / b - 1) * 100, "%", f"{k} at {p0['meta']['bs_date']} ÷ year-ago − 1", [0, yi])

    if yi is not None and sh and shares(qs[yi]):
        add("share_count_change_pct", "Change in share count vs a year ago (dilution)", (sh / shares(qs[yi]) - 1) * 100, "%",
            "shares now ÷ shares a year ago − 1", [0, yi])

    # ---- shareholding ----
    srows = []
    for p in shp:
        prom, tot = p.get("promoter") or {}, p.get("total") or {}
        ps = prom.get("shares") or 0.0
        srows.append({"date": p.get("date") or p["filing"]["date"], "promoter_pct": pct(prom.get("pct")) if prom else 0.0,
                      "pledge_pct_of_promoter": (prom.get("pledged", 0.0) / ps * 100) if ps else None,
                      "encumbered_pct_of_promoter": (prom.get("encumbered", 0.0) / ps * 100) if ps else None,
                      "fpi_pct": pct((p.get("fpi") or {}).get("pct")), "dii_pct": pct((p.get("dii") or {}).get("pct")),
                      "mf_pct": pct((p.get("mf") or {}).get("pct")), "public_pct": pct((p.get("public") or {}).get("pct")),
                      "total_shares": tot.get("shares"), "has_promoter": ps > 0})
    out["shareholding"] = srows

    # ---- audit opinion and auditors ----
    audit = {"status": "unknown", "detail": "No audited results filing in the filings read", "q": None}
    for i, p in enumerate(qs):
        aud = (p["filing"].get("audited") or "").lower()
        if aud.startswith("audited"):
            decl = (p["meta"].get("DeclarationOfUnmodifiedOpinionOrStatementOnImpactOfAuditQualification") or "").strip()
            if "unmodified" in decl.lower():
                audit = {"status": "unmodified", "detail": f"'{decl}' in audited results for quarter ended {p['meta']['period_end']}", "q": i}
            elif decl:
                audit = {"status": "modified", "detail": f"'{decl}' in audited results for quarter ended {p['meta']['period_end']}", "q": i}
            else:
                audit = {"status": "unknown", "detail": f"Audited results for {p['meta']['period_end']} carry no opinion declaration", "q": i}
            break
    out["audit"] = audit
    now_aud = qs[0]["auditors"] if qs else []
    out["auditors"] = now_aud
    if yi is not None and now_aud and qs[yi]["auditors"] and             {auditor_key(a) for a in now_aud} != {auditor_key(a) for a in qs[yi]["auditors"]}:
        out["flags"].append(f"Auditor change: now {', '.join(now_aud)}; a year ago {', '.join(qs[yi]['auditors'])}")
    if qs:
        r = qs[0]["rpt"]
        out["rpt"] = {"rows": r["rows"], "amount_cr": r["amount_in_period"] / CR}
    return out


# ------------------------------------------------------------- evidence/facts ---

def _qline(p: dict) -> list[str]:
    m, f = p["meta"], p["filing"]
    head = (f"Quarter {m['period_start']} to {m['period_end']} ({f['scope']}, {f.get('audited') or 'audit status not stated'}); "
            f"filed with NSE on {f['broadcast_at'][:10]}. Values read from the company's XBRL filing, shown in INR crore.")
    lines = [head]
    for c in ("revenue", "interest_earned", "total_income", "other_income", "interest_expended", "provisions", "exceptional",
              "pbt", "tax", "pat_owners"):
        v = g(p["q"], c)
        if v is not None and not (c == "total_income" and g(p["q"], "revenue") is not None):
            lines.append(f"{LABEL[c]} for the quarter: Rs {fmt(v / CR)} crore.")
    e = g(p["q"], "eps")
    if e is not None:
        lines.append(f"Diluted EPS for the quarter: Rs {fmt(e)}.")
    for c in ("gnpa_amt",):
        v = g(p["q"], c)
        if v is not None:
            lines.append(f"{LABEL[c]}: Rs {fmt(v / CR)} crore.")
    for c in ("gnpa_pct", "nnpa_pct", "cet1", "roa"):
        v = pct(g(p["q"], c))
        if v is not None:
            lines.append(f"{LABEL[c]}: {fmt(v)}%.")
    o = g(p["ytd"], "ocf")
    if o is not None:
        lines.append(f"Operating cash flow {m['ytd_start']} to {m['period_end']}: Rs {fmt(o / CR)} crore.")
    eq = book_equity(p)
    if eq is not None:
        lines.append(f"Equity at {m['bs_date']}: Rs {fmt(eq / CR)} crore.")
    for k, label in (("TradeReceivablesCurrent", "Trade receivables (current)"), ("BorrowingsCurrent", "Borrowings (current)"),
                     ("BorrowingsNoncurrent", "Borrowings (non-current)"), ("Deposits", "Deposits"), ("Advances", "Advances")):
        if p["bs"].get(k) is not None:
            lines.append(f"{label} at {m['bs_date']}: Rs {fmt(p['bs'][k] / CR)} crore.")
    sh = shares(p)
    if sh:
        lines.append(f"Paid-up equity capital Rs {fmt(g(p['q'], 'paid_up') / CR)} crore at face value Rs {fmt(g(p['q'], 'face'))} "
                     f"({fmt(sh / 1e7)} crore shares).")
    decl = p["meta"].get("DeclarationOfUnmodifiedOpinionOrStatementOnImpactOfAuditQualification")
    if decl:
        lines.append(f"Audit opinion statement in this filing: {decl}.")
    if p["auditors"]:
        lines.append(f"Statutory auditor(s) named in this filing: {', '.join(p['auditors'])}.")
    if p["rpt"]["rows"]:
        lines.append(f"Related-party transactions disclosed for the period: {p['rpt']['rows']} line items totalling "
                     f"Rs {fmt(p['rpt']['amount_in_period'] / CR)} crore (gross; includes all transaction types).")
    return lines


def to_evidence(fac: EvidenceFactory, snap: dict, fund: dict, company: str) -> tuple[list[FinancialFact], list[Calculation], dict]:
    qs, shp = snap.get("quarters") or [], snap.get("shareholding") or []
    q_ev, s_ev = [], []
    for p in qs:
        f = p["filing"]
        q_ev.append(fac.add(
            kind="exchange_filing", title=f"{company}: results for quarter ended {f['quarter_end']} ({f['scope']}, {f.get('audited')}) — XBRL",
            publisher="NSE · company XBRL filing", tier="T1_official", url=f.get("ixbrl") or f["xbrl"],
            published_at=f["broadcast_at"], passages=_qline(p), retrieved_via="filings:nse_xbrl",
            notes=f"Machine-readable filing {f['xbrl']}. Passages are rendered by code from tagged values ({CODE_VERSION})."))
    for p, row in zip(shp, fund.get("shareholding", [])):
        lines = [f"Shareholding pattern as of {row['date']} (filed {p['filing']['broadcast_at'][:10]})."]
        lines.append(f"Promoter and promoter group: {fmt(row['promoter_pct'])}% of shares." if row["has_promoter"]
                     else "No promoter or promoter group shareholding is reported.")
        if row["has_promoter"]:
            lines.append(f"Promoter shares pledged: {fmt(row['pledge_pct_of_promoter'])}% of promoter holding; "
                         f"encumbered in any form: {fmt(row['encumbered_pct_of_promoter'])}%.")
        lines.append(f"Foreign portfolio investors {fmt(row['fpi_pct'])}%, domestic institutions {fmt(row['dii_pct'])}% "
                     f"(of which mutual funds {fmt(row['mf_pct'])}%), public total {fmt(row['public_pct'])}%.")
        s_ev.append(fac.add(kind="exchange_filing", title=f"{company}: shareholding pattern as of {row['date']} — XBRL",
                            publisher="NSE · company XBRL filing", tier="T1_official", url=p["filing"]["xbrl"],
                            published_at=p["filing"]["broadcast_at"], passages=lines, retrieved_via="filings:nse_xbrl",
                            notes="Category-level totals read from the shareholding XBRL by code."))

    def refs(m) -> list[str]:
        return [q_ev[i].id for i in m["q"] if i < len(q_ev)] + [s_ev[i].id for i in m["s"] if i < len(s_ev)]

    passages, calcs = [], []
    for m in fund["metrics"]:
        r = refs(m)
        if m["value"] is not None:
            if m["unit"] == "count":
                val = str(int(m["value"]))
            else:
                unit = "" if m["unit"] == "x" else ("%" if m["unit"] == "%" else " " + m["unit"])
                val = f"{fmt(m['value'])}{'x' if m['unit'] == 'x' else ''}{unit}"
            passages.append(f"{m['label']}: {val} — {m['formula']}" + (f" ({m['note']})" if m.get("note") else "")
                            + (f"; inputs {', '.join(r)}." if r else "."))
        else:
            passages.append(f"{m['label']}: not computed — {m.get('note') or 'inputs missing'}.")
        calcs.append(Calculation(id=f"calc-{m['name']}", name=m["name"], formula=f"{CODE_VERSION}: {m['formula']}",
                                 inputs=r, result=m["value"], unit=m["unit"], status=m["status"], code_version=CODE_VERSION))
    a = fund["audit"]
    passages.append(f"Audit opinion: {a['status']} — {a['detail']}.")
    for fl in fund["flags"]:
        passages.append(f"Data flag: {fl}.")
    ins = snap.get("insider_trades") or {}
    if ins.get("status") == "unavailable":
        passages.append(f"Insider trades: not available — {ins['reason']}")
    comp = fac.add(kind="computed_fundamental", title="Valuation & fundamentals (computed from filings)",
                   publisher="ca_platform fundamentals", tier="SYS_computed", passages=passages or ["No fundamentals could be computed."],
                   retrieved_via="filings:nse_xbrl", notes=f"Computed by {CODE_VERSION} only from the XBRL filings in this bundle.")

    facts: list[FinancialFact] = []

    def fact(metric, value, unit, ev_id, period, period_end, verified, status=None, reason=None, scope=None):
        facts.append(FinancialFact(id=f"xf-{len(facts) + 1}", metric=metric, value=value,
                                   status=status or ("available" if value is not None else "missing"), reason=reason,
                                   unit=unit, scope=scope or snap.get("scope"), period=period, period_end=period_end,
                                   evidence_id=ev_id, verified=verified))
    if qs:
        p, ev0 = qs[0], q_ev[0]
        ok = fund["reconciliation"][0]["ok"] is True
        pe = p["meta"]["period_end"]
        top = "total_income" if fund["is_bank"] else "revenue"
        for c, metric in ((top, "revenue" if top == "revenue" else "total_income"), ("pat_owners", "net_profit_attributable"),
                          ("other_income", "other_income"), ("exceptional", "exceptional_items")):
            v = g(p["q"], c)
            if v is not None:
                fact(metric, round(v / CR, 2), "INR crore", ev0.id, "quarter", pe, ok)
        if g(p["q"], "eps") is not None:
            fact("eps", g(p["q"], "eps"), "INR", ev0.id, "quarter", pe, ok)
        for c, metric in (("gnpa_pct", "gnpa_pct"), ("nnpa_pct", "nnpa_pct"), ("cet1", "cet1_pct"), ("roa", "roa_quarter_pct")):
            v = pct(g(p["q"], c))
            if v is not None:
                fact(metric, round(v, 4), "%", ev0.id, "quarter", pe, True)
        oi = next((i for i, x in enumerate(qs) if g(x["ytd"], "ocf") is not None), None)
        if oi is not None and not fund["is_bank"]:     # a bank's operating cash flow mostly reflects deposits/loans
            fact("operating_cash_flow", round(g(qs[oi]["ytd"], "ocf") / CR, 2), "INR crore", q_ev[oi].id, "ytd",
                 qs[oi]["meta"]["period_end"], True)
        a = fund["audit"]
        if a["q"] is not None:
            fact("audit_opinion_unmodified", 1.0 if a["status"] == "unmodified" else (0.0 if a["status"] == "modified" else None),
                 "flag", q_ev[a["q"]].id, "quarter", qs[a["q"]]["meta"]["period_end"], a["status"] != "unknown",
                 reason=a["detail"])
    for i, row in enumerate(fund.get("shareholding", [])):
        ev = s_ev[i].id
        if row["has_promoter"]:
            fact("promoter_pledge_pct", round(row["pledge_pct_of_promoter"], 4), "%", ev, "quarter", row["date"], True, scope="company")
        else:
            fact("promoter_pledge_pct", None, "%", ev, "quarter", row["date"], True, status="not_applicable",
                 reason="No promoter or promoter group shareholding reported", scope="company")
        fact("promoter_holding_pct", round(row["promoter_pct"] or 0.0, 4), "%", ev, "quarter", row["date"], True, scope="company")
        if i == 0:
            for k, metric in (("fpi_pct", "fii_holding_pct"), ("dii_pct", "dii_holding_pct"), ("mf_pct", "mf_holding_pct")):
                if row[k] is not None:
                    fact(metric, round(row[k], 4), "%", ev, "quarter", row["date"], True, scope="company")

    info = {"code_version": CODE_VERSION, "as_of": snap.get("as_of"), "scope": snap.get("scope"), "is_bank": fund["is_bank"],
            "failed": snap.get("failed", []), "notes": snap.get("notes", []), "flags": fund["flags"], "audit": fund["audit"],
            "auditors": fund.get("auditors", []), "rpt": fund.get("rpt"), "insider_trades": snap.get("insider_trades"),
            "metrics": [dict(m, evidence_ids=refs(m)) for m in fund["metrics"]], "shareholding": fund.get("shareholding", []),
            "quarters": [{"period_start": p["meta"]["period_start"], "period_end": p["meta"]["period_end"],
                          "audited": p["filing"].get("audited"), "filed": p["filing"]["broadcast_at"][:10],
                          "url": p["filing"].get("ixbrl") or p["filing"]["xbrl"], "evidence_id": q_ev[i].id,
                          "reconciled": fund["reconciliation"][i]["ok"],
                          "revenue_cr": (g(p["q"], "total_income" if fund["is_bank"] else "revenue") or 0) / CR or None,
                          "pat_cr": (g(p["q"], "pat_owners") or 0) / CR or None, "eps": g(p["q"], "eps"),
                          "gnpa_pct": pct(g(p["q"], "gnpa_pct")), "nnpa_pct": pct(g(p["q"], "nnpa_pct"))}
                         for i, p in enumerate(qs)],
            "computed_evidence_id": comp.id}
    return facts, calcs, info


def report(symbol: str, price: Optional[float] = None, log=lambda m: None) -> dict:
    """Stand-alone view (CLI/API): fetch filings now, compute, and return the same record a run stores."""
    from ..data import nse_xbrl
    from ..settings import iso
    sym = symbol.upper()
    price_note = None
    if price is None:
        try:
            from ..data import bhav
            c, _ = bhav.candles(sym, limit=5)
            if c:
                price, price_note = c[-1]["close"], f"last close {c[-1]['date']} (NSE bhavcopy)"
        except Exception:
            price = None
    if price is None:
        try:
            import asyncio

            from ..data import kite_mcp
            if kite_mcp.connected():
                lt = asyncio.run(kite_mcp.ltp([sym]))
                if lt.get(sym):
                    price, price_note = lt[sym], "last traded price (Kite MCP)"
        except Exception as e:
            log(f"Price unavailable from Kite MCP ({type(e).__name__})")
    snap = nse_xbrl.collect(sym, iso(), log)
    fund = build(snap, price)
    fac = EvidenceFactory()
    facts, _, info = to_evidence(fac, snap, fund, sym)
    info["price"] = {"close": price, "note": price_note or ("given" if price else "no price — price-based ratios not computed")}
    info["facts"] = [f.model_dump() for f in facts]
    info["evidence"] = [{"id": e.id, "title": e.title, "url": e.url, "passages": [p.text for p in e.passages]} for e in fac.items]
    return info
