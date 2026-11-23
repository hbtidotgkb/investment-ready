"""Evidence factory: turns adapter outputs into Evidence items with stable ids, passages
written by code (for computed values) or copied verbatim from sources (for documents)."""
from __future__ import annotations

from typing import Optional

from .contracts import Evidence, Passage
from .data.sources import domain_of, tier_for
from .settings import iso, sha256_text


def fmt(x: Optional[float], nd: int = 2) -> str:
    if x is None:
        return "n/a"
    return f"{x:,.{nd}f}"


class EvidenceFactory:
    def __init__(self):
        self.items: list[Evidence] = []
        self.n = 0

    def add(self, *, kind: str, title: str, publisher: str, tier: str, passages: list[str],
            url: str | None = None, published_at: str | None = None, published_hint: str | None = None,
            first_seen_at: str | None = None, access_status: str = "public", usage_status: str = "personal_research",
            derived_from: str | None = None, retrieved_via: str = "", notes: str | None = None) -> Evidence:
        self.n += 1
        eid = f"ev-{self.n}"
        ps = [Passage(id=f"{eid}:P{i + 1}", text=t.strip()) for i, t in enumerate(passages) if t and t.strip()]
        ev = Evidence(id=eid, kind=kind, title=title[:300], publisher=publisher[:120], url=url, tier=tier,
                      published_at=published_at, published_hint=published_hint,
                      first_seen_at=first_seen_at or iso(), ingested_at=iso(), access_status=access_status,
                      usage_status=usage_status, passages=ps,
                      content_hash=sha256_text("\n".join(p.text for p in ps)), derived_from=derived_from,
                      retrieved_via=retrieved_via, notes=notes)
        self.items.append(ev)
        return ev


def technical_passages(f: dict, source: str) -> list[str]:
    above = "above" if f["sma200"] and f["close"] > f["sma200"] else ("below" if f["sma200"] else "n/a vs")
    sr = f.get("sr") or {}
    sup, res = sr.get("support"), sr.get("resistance")
    p = [
        f"Last close on {f['last_date']}: Rs {fmt(f['close'])} (source: {source}).",
        f"Daily bars available: {f['bars']}; last bar dated {f['last_date']}.",
        f"SMA20 Rs {fmt(f['sma20'])}, SMA50 Rs {fmt(f['sma50'])}, SMA200 Rs {fmt(f['sma200'])}; close is {above} SMA200.",
        f"RSI(14) is {fmt(f['rsi14'], 1)}; MACD {fmt(f['macd'])} versus signal {fmt(f['macd_signal'])}.",
        f"ATR(14) is Rs {fmt(f['atr14'])}, which is {fmt(f['atr_pct'], 1)}% of the last close.",
        (f"52-week high Rs {fmt(f['high_52w'])} and low Rs {fmt(f['low_52w'])}" if f.get("range_bars", 252) >= 250 else
         f"High Rs {fmt(f['high_52w'])} and low Rs {fmt(f['low_52w'])} over only {f.get('range_bars')} bars (less than 52 weeks of data)")
        + f"; close is {fmt(f['pct_from_52w_high'], 1)}% from the high and {fmt(f.get('pct_above_52w_low'), 1)}% above the low"
        + (f" ({fmt(f['pos_in_52w_range_pct'], 0)}% of the way from low to high)." if f.get("pos_in_52w_range_pct") is not None else "."),
        f"20-day average traded value is Rs {fmt(f['adv20_crore'])} crore.",
        f"Price returns: 1 month {fmt(f['ret_1m_pct'], 1)}%, 3 months {fmt(f['ret_3m_pct'], 1)}%, 6 months {fmt(f['ret_6m_pct'], 1)}%.",
        (f"Nearest support zone Rs {fmt(sup['level'])} ({sup['touches']} touches)" if sup else "No support zone below the last close in 180 bars")
        + "; " + (f"nearest resistance zone Rs {fmt(res['level'])} ({res['touches']} touches)." if res else "no resistance zone above the last close in 180 bars."),
    ]
    if f.get("up_volume_share_20d") is not None:
        p.append(f"Up-day share of 20-day volume is {fmt(f['up_volume_share_20d'] * 100, 0)}%.")
    for c in f.get("candles", []):
        p.append(f"Last-bar candlestick: {c['name']} ({c['bias']}) in a {c['trend_context'] or 'unclear'} short-term trend.")
    return p


def pattern_passage(pt: dict) -> str:
    vol = {True: "confirmed by breakout volume", False: "not confirmed by breakout volume",
           None: "breakout volume not applicable"}[pt.get("breakout_volume_confirmed")]
    return (f"{pt['name'].replace('_', ' ').title()} ({pt['bias']}) from {pt['start_date']} to {pt['end_date']}: "
            f"status {pt['status']}, neckline Rs {fmt(pt['neckline'])}, measured-move target Rs {fmt(pt['measured_target'])}, "
            f"pattern stop Rs {fmt(pt['pattern_stop'])}, {vol}, quality {pt['quality']}.")


def from_discovery(fac: EvidenceFactory, out: dict, purpose: str, decision_at: str,
                   first_seen: dict[str, str] | None = None) -> list[Evidence]:
    """Group API citations by URL; passages are the verbatim cited_text from the source.
    `first_seen` (url -> YYYY-MM-DD) marks sources carried forward from an earlier day's search."""
    first_seen = first_seen or {}
    by_url: dict[str, dict] = {}
    ages = {r["url"]: r.get("page_age") for r in out.get("results", [])}
    for c in out.get("citations", []):
        d = by_url.setdefault(c["url"], {"title": c["title"], "texts": [], "claims": []})
        if c["cited_text"] not in d["texts"]:
            d["texts"].append(c["cited_text"])
        d["claims"].append(c["claim"])
    made = []
    for url, d in by_url.items():
        tier = tier_for(url)
        kind = "exchange_filing" if tier == "T1_official" else "web_search_citation"
        day = first_seen.get(url)
        earlier = day is not None and day < decision_at[:10]
        made.append(fac.add(kind=kind, title=d["title"] or url, publisher=domain_of(url), tier=tier, url=url,
                            passages=d["texts"], published_hint=ages.get(url),
                            first_seen_at=day if earlier else decision_at,
                            retrieved_via=f"discovery:{purpose}",
                            notes="Passages are verbatim cited_text returned by the search API."
                            + (f" Found by the search on {day}; carried forward." if earlier else "")))
    return made
