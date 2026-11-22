"""Official financial data from NSE's XBRL filings (SEBI Integrated Filing — Financials, and
the quarterly shareholding pattern).

Why XBRL: since early 2025 quarterly results are filed as "Integrated Filing", and NSE's older
results-comparison / financial-results JSON feeds stopped updating (they end at Dec-2024).
The XBRL files on nsearchives are the company's own machine-readable filing: every value is
tagged with its period, unit and precision. We read them; we never estimate a missing value.

Point in time: only filings broadcast on or before the decision time are used.
Caching: XBRL files are immutable (a revision gets a new file), so they are cached on disk.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from xml.etree import ElementTree as ET

from ..settings import IST, RAW
from .sources import PoliteClient

XBRL_DIR = RAW / "xbrl"
ARCHIVE_HOSTS = ("nsearchives.nseindia.in", "nsearchives.nseindia.com", "archives.nseindia.com")
RESULTS_API = "/api/integrated-filing-results?index=equities&symbol={sym}&type=Integrated%20Filing-%20Financials"
SHP_API = "/api/corporate-share-holdings-master?index=equities&symbol={sym}"

# Text (non-numeric) tags we keep from a results filing.
TEXT_TAGS = {
    "NameOfTheCompany", "Symbol", "LevelOfRounding", "ReportingQuarter", "TypeOfReportingPeriod",
    "WhetherResultsAreAuditedOrUnaudited", "NatureOfReportStandaloneConsolidated",
    "DateOfBoardMeetingWhenFinancialResultsWereApproved", "DateOfStartOfFinancialYear", "DateOfEndOfFinancialYear",
    "DeclarationOfUnmodifiedOpinionOrStatementOnImpactOfAuditQualification",
    "WhetherTheCompanyHasEnteredIntoAnyRelatedPartyTransactionDuringThePeriod",
    "WhetherTheDisclosureForTheDefaultOnLoansAndDebtSecuritiesIsApplicableToTheEntity",
    "WhetherStatementOnDeviationOrVariationForProceedsOfPublicIssueOrRightIssueOrPreferentialIssueOrQIPEtcIsApplicableToTheCompanyForTheCurrentQuarter",
}


# ------------------------------------------------------------------ parsing ---

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _num(text: str | None) -> Optional[float]:
    try:
        return float((text or "").strip())
    except ValueError:
        return None


def _contexts(root) -> dict[str, dict]:
    out = {}
    for c in root:
        if _local(c.tag) != "context":
            continue
        per = c.find("{*}period")
        get = (lambda n: (per.find("{*}" + n).text if per is not None and per.find("{*}" + n) is not None else None))
        explicit = tuple(m.text.split(":")[-1] for m in c.iter() if _local(m.tag) == "explicitMember")
        typed = any(_local(m.tag) == "typedMember" for m in c.iter())
        out[c.get("id")] = {"start": get("startDate"), "end": get("endDate"), "instant": get("instant"),
                            "dims": explicit, "typed": typed}
    return out


def parse_results(content: bytes) -> dict:
    """One Integrated Filing (Financials) XBRL → {meta, q, ytd, bs, auditors, rpt}.

    q   = values for the quarter (shortest duration ending on the period end, no dimensions)
    ytd = values for the year-to-date duration (longest duration ending on the period end)
    bs  = balance-sheet values at the period end (instant)
    All money values are in INR (absolute), exactly as tagged.
    """
    root = ET.fromstring(content)
    ctx = _contexts(root)
    plain = {k: v for k, v in ctx.items() if not v["dims"] and not v["typed"]
             and not k.startswith(("D_", "I_"))}
    durs = {k: v for k, v in plain.items() if v["start"] and v["end"]}
    if not durs:
        raise ValueError("no plain duration contexts — not a results filing")
    end = max(v["end"] for v in durs.values())
    at_end = [(k, v) for k, v in durs.items() if v["end"] == end]
    at_end.sort(key=lambda kv: kv[1]["start"], reverse=True)          # latest start = shortest span
    q_start, ytd_start = at_end[0][1]["start"], at_end[-1][1]["start"]
    q_ids = {k for k, v in at_end if v["start"] == q_start}          # Q1: quarter and year-to-date are the same span
    ytd_ids = {k for k, v in at_end if v["start"] == ytd_start}
    q_ctx = at_end[0][0]
    insts = sorted({v["instant"] for v in plain.values() if v["instant"]}, reverse=True)
    bs_ctx = {k for k, v in plain.items() if v["instant"] and v["instant"] == (insts[0] if insts else None)}

    meta: dict = {"period_start": q_start, "period_end": end, "ytd_start": ytd_start,
                  "bs_date": insts[0] if insts else None}
    q: dict[str, float] = {}
    ytd: dict[str, float] = {}
    bs: dict[str, float] = {}
    auditors: list[str] = []
    rpt = {"rows": 0, "amount_in_period": 0.0}
    for el in root:
        ref = el.get("contextRef")
        if not ref or el.text is None:
            continue
        tag = _local(el.tag)
        txt = el.text.strip()
        if tag == "AuditorsFirmName":
            if txt and txt not in auditors:
                auditors.append(txt)
            continue
        if tag == "AmountOfRelatedPartyTransactionDuringTheReportingPeriod":
            v = _num(txt)
            if v is not None:
                rpt["rows"] += 1
                rpt["amount_in_period"] += abs(v)
            continue
        if tag in TEXT_TAGS:
            if ref in q_ids or tag not in meta:
                meta[tag] = txt
            continue
        v = _num(txt)
        if v is None:
            continue
        if ref in q_ids:
            q.setdefault(tag, v)
        if ref in ytd_ids:
            ytd.setdefault(tag, v)
        if ref in bs_ctx:
            bs.setdefault(tag, v)
    meta["is_bank"] = "InterestEarned" in q or "Advances" in bs
    return {"meta": meta, "q": q, "ytd": ytd, "bs": bs, "auditors": auditors, "rpt": rpt}


SHP_MEMBERS = {
    "ShareholdingPatternMember": "total",
    "ShareholdingOfPromoterAndPromoterGroupMember": "promoter",
    "PublicShareholdingMember": "public",
    "InstitutionsForeignMember": "fpi",
    "InstitutionsDomesticMember": "dii",
    "MutualFundsOrUTIMember": "mf",
}
SHP_TAGS = {"NumberOfShares": "shares", "ShareholdingAsAPercentageOfTotalNumberOfShares": "pct",
            "NumberOfSharesEncumberedUnderPledged": "pledged", "NumberOfSharesEncumbered": "encumbered"}


def parse_shareholding(content: bytes) -> dict:
    """Shareholding-pattern XBRL → {date, total/promoter/public/fpi/dii/mf: {shares, pct, pledged, encumbered}}.
    Uses category-level contexts only (exactly one category dimension, no per-holder typed member)."""
    root = ET.fromstring(content)
    ctx = _contexts(root)
    cat_ctx = {k: SHP_MEMBERS[v["dims"][0]] for k, v in ctx.items()
               if len(v["dims"]) == 1 and not v["typed"] and v["dims"][0] in SHP_MEMBERS}
    out: dict = {"date": next((v["instant"] for k, v in ctx.items() if k in cat_ctx and v["instant"]), None)}
    for el in root:
        ref = el.get("contextRef")
        tag = _local(el.tag)
        if ref in cat_ctx and tag in SHP_TAGS:
            v = _num(el.text)
            if v is not None:
                out.setdefault(cat_ctx[ref], {}).setdefault(SHP_TAGS[tag], v)
    return out


# ----------------------------------------------------------------- fetching ---

def _when(s: str | None) -> Optional[datetime]:
    """NSE timestamps like '08-Oct-2026 22:07:33' or '06-OCT-2026 17:00:45' (IST)."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y"):
        try:
            return datetime.strptime(s.title(), fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def _day(s: str | None) -> Optional[str]:
    d = _when(s)
    return d.date().isoformat() if d else None


def fetch_xbrl(client: PoliteClient, url: str) -> Optional[bytes]:
    """Cached download of an NSE archive XBRL file. Only the official archive hosts are allowed."""
    m = re.match(r"https://([^/]+)/.+/([A-Za-z0-9_.\-]+\.xml)$", url or "")
    if not m or m.group(1) not in ARCHIVE_HOSTS:
        return None
    path = XBRL_DIR / m.group(2)
    if path.exists() and path.stat().st_size > 0:
        return path.read_bytes()
    r = client.get(url)
    if r.status_code != 200 or b"xbrl" not in r.content[:4000].lower():
        return None
    XBRL_DIR.mkdir(parents=True, exist_ok=True)
    path.write_bytes(r.content)
    return r.content


def choose_results(rows: list[dict], decision_at: str, quarters: int) -> tuple[list[dict], str, list[str]]:
    """Pick one filing per quarter, same scope throughout, broadcast on/before decision time.
    Banks: standalone (regulatory ratios are reported standalone). Others: consolidated when filed."""
    cutoff = datetime.fromisoformat(decision_at)
    notes: list[str] = []
    ok = []
    for r in rows:
        b = _when(r.get("broadcast_Date"))
        if b is None or b > cutoff or not r.get("xbrl") or not r.get("qe_Date"):
            continue
        ok.append(dict(r, _b=b, _qe=_when(r["qe_Date"]).date().isoformat(),
                       _scope="consolidated" if (r.get("consolidated") or "").lower().startswith("consol") else "standalone"))
    if not ok:
        return [], "", notes
    is_bank = any("BANKING" in (r.get("xbrl") or "") for r in ok)
    latest_q = max(r["_qe"] for r in ok)
    have = {r["_scope"] for r in ok if r["_qe"] == latest_q}
    pref = "standalone" if is_bank else "consolidated"
    scope = pref if pref in have else next(iter(have))
    if scope != pref:
        notes.append(f"Latest quarter has no {pref} filing; using {scope} figures throughout")
    by_q: dict[str, dict] = {}
    for r in ok:
        if r["_scope"] != scope:
            continue
        cur = by_q.get(r["_qe"])
        if cur is None or r["_b"] > cur["_b"]:          # a revised filing replaces the original
            by_q[r["_qe"]] = r
    picked = [by_q[k] for k in sorted(by_q, reverse=True)[:quarters]]
    return picked, scope, notes


def collect(symbol: str, decision_at: str, log: Callable[[str], None] = lambda m: None,
            quarters: int = 5, shp_quarters: int = 4, session=None) -> dict:
    """Fetch and parse the latest results and shareholding filings (point in time).
    Returns a snapshot dict; any source that fails is listed in `failed` (never guessed)."""
    from .nse import NSESession
    sym = symbol.upper()
    snap: dict = {"symbol": sym, "as_of": decision_at, "scope": None, "quarters": [], "shareholding": [],
                  "failed": [], "notes": [], "source": "NSE XBRL filings (Integrated Filing — Financials; shareholding pattern)"}
    s = session or NSESession()
    c = PoliteClient(min_interval=0.8, timeout=40)
    try:
        listing = s.get_json(RESULTS_API.format(sym=sym))
        rows = (listing or {}).get("data") if isinstance(listing, dict) else None
        if not isinstance(rows, list):
            snap["failed"].append("NSE integrated-filing results list unavailable")
            log("Financial results list: unavailable from NSE")
        else:
            picked, scope, notes = choose_results(rows, decision_at, quarters)
            snap["scope"], snap["notes"] = scope, notes
            log(f"Financial results: {len(rows)} filings listed; using {len(picked)} quarter(s), {scope or 'n/a'}")
            for i, r in enumerate(picked, 1):
                raw = fetch_xbrl(c, r["xbrl"])
                if raw is None:
                    snap["failed"].append(f"XBRL for quarter ended {r['_qe']} could not be downloaded")
                    log(f"Results XBRL {i} of {len(picked)} (quarter ended {r['_qe']}): download failed")
                    continue
                try:
                    p = parse_results(raw)
                except Exception as e:     # malformed or unexpected format: report, do not guess
                    snap["failed"].append(f"XBRL for quarter ended {r['_qe']} could not be parsed ({type(e).__name__})")
                    log(f"Results XBRL {i} of {len(picked)}: parse failed ({type(e).__name__})")
                    continue
                p["filing"] = {"broadcast_at": r["_b"].isoformat(), "quarter_end": r["_qe"], "scope": scope,
                               "audited": r.get("audited") or p["meta"].get("WhetherResultsAreAuditedOrUnaudited"),
                               "xbrl": r["xbrl"], "ixbrl": r.get("ixbrl"), "revision": r.get("type_Sub")}
                snap["quarters"].append(p)
                log(f"Results XBRL {i} of {len(picked)}: quarter ended {r['_qe']} ({p['filing']['audited']}) — "
                    f"{len(p['q'])} quarter values, {len(p['bs'])} balance-sheet values")
        shp = s.get_json(SHP_API.format(sym=sym))
        if not isinstance(shp, list):
            snap["failed"].append("NSE shareholding-pattern list unavailable")
            log("Shareholding pattern list: unavailable from NSE")
        else:
            cutoff = datetime.fromisoformat(decision_at)
            elig = [x for x in shp if x.get("xbrl") and (_when(x.get("broadcastDate") or x.get("submissionDate")) or cutoff) <= cutoff]
            elig.sort(key=lambda x: _when(x.get("date")) or datetime.min.replace(tzinfo=IST), reverse=True)
            seen = set()
            for x in elig:
                if x.get("date") in seen:
                    continue
                seen.add(x.get("date"))
                if len(seen) > shp_quarters:
                    break
                raw = fetch_xbrl(c, x["xbrl"])
                if raw is None:
                    snap["failed"].append(f"Shareholding XBRL for {x.get('date')} could not be downloaded")
                    continue
                try:
                    p = parse_shareholding(raw)
                except Exception as e:
                    snap["failed"].append(f"Shareholding XBRL for {x.get('date')} could not be parsed ({type(e).__name__})")
                    continue
                p["filing"] = {"broadcast_at": (_when(x.get("broadcastDate")) or cutoff).isoformat(),
                               "date": _day(x.get("date")), "xbrl": x["xbrl"]}
                snap["shareholding"].append(p)
            log(f"Shareholding pattern: {len(snap['shareholding'])} quarter(s) read from XBRL")
    finally:
        c.close()
        if session is None:
            s.close()
    snap["insider_trades"] = {"status": "unavailable",
                              "reason": "NSE's insider-trading (PIT) feed returns no disclosures after Mar-2025; "
                                        "not used rather than shown as current."}
    return snap


def cache_dir() -> Path:
    return XBRL_DIR
