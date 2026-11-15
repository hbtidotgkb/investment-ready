"""DEMO dataset — a FICTIONAL company with synthetic data, for showing the full pipeline
without API keys. Every evidence item is tier DEMO_synthetic and the rules engine refuses
any actionable decision in demo mode."""
from __future__ import annotations

import math
import random
from datetime import date, timedelta

DEMO_SYMBOL = "DEMOCO"
DEMO_NAME = "Demo Precision Industries Ltd (fictional)"
DEMO_SECTOR = "Industrial Manufacturing (fictional)"


def _trading_days(end: date, n: int) -> list[date]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return list(reversed(out))


def candles(end: date | None = None, n: int = 320) -> list[dict]:
    rng = random.Random(42)
    end = end or date.today()
    if end.weekday() >= 5:
        end -= timedelta(days=end.weekday() - 4)
    days = _trading_days(end - timedelta(days=1), n)
    # waypoints: trend up, pull back to A, bounce to neckline, retest B, recover toward neckline
    way = [(0, 980), (190, 1310), (235, 1150), (262, 1242), (292, 1156), (n - 1, 1231)]
    out, prev = [], way[0][1]
    for i, d in enumerate(days):
        for (i0, p0), (i1, p1) in zip(way, way[1:]):
            if i0 <= i <= i1:
                base = p0 + (p1 - p0) * (i - i0) / max(i1 - i0, 1)
                break
        close = base * (1 + 0.009 * math.sin(i / 3.1) + rng.uniform(-0.006, 0.006))
        o = prev * (1 + rng.uniform(-0.004, 0.004))
        hi = max(o, close) * (1 + rng.uniform(0.001, 0.012))
        lo = min(o, close) * (1 - rng.uniform(0.001, 0.012))
        vol = 450000 * (1 + rng.uniform(-0.35, 0.45)) * (1.8 if i == n - 1 else 1)
        out.append({"date": d.isoformat(), "open": round(o, 2), "high": round(hi, 2), "low": round(lo, 2),
                    "close": round(close, 2), "volume": round(vol), "source_file": "demo:synthetic"})
        prev = close
    return out


def manual_doc(decision_day: date) -> dict:
    q_end = date(decision_day.year, ((decision_day.month - 1) // 3) * 3 + 1, 1) - timedelta(days=1)
    pub = (q_end + timedelta(days=38)).isoformat() + "T18:30:00+05:30"
    return {
        "company_name": DEMO_NAME, "sector": DEMO_SECTOR,
        "sources": [
            {"id": "results", "title": "DEMO quarterly results filing (synthetic)", "publisher": "Demo Exchange",
             "tier": "DEMO_synthetic", "url": None, "published_at": pub},
            {"id": "ar", "title": "DEMO annual report extract (synthetic)", "publisher": "Demo Precision Industries",
             "tier": "DEMO_synthetic", "url": None, "published_at": f"{q_end.year}-07-15T10:00:00+05:30"},
            {"id": "shp", "title": "DEMO shareholding pattern (synthetic)", "publisher": "Demo Exchange",
             "tier": "DEMO_synthetic", "url": None, "published_at": pub},
        ],
        "facts": [
            {"metric": "revenue", "value": 1842.0, "unit": "crore", "period": "quarter", "period_end": q_end.isoformat(), "scope": "consolidated", "source": "results"},
            {"metric": "net_profit_attributable", "value": 214.0, "unit": "crore", "period": "quarter", "period_end": q_end.isoformat(), "scope": "consolidated", "source": "results"},
            {"metric": "net_profit_underlying", "value": 198.0, "unit": "crore", "period": "quarter", "period_end": q_end.isoformat(), "scope": "consolidated", "source": "results"},
            {"metric": "operating_cash_flow", "value": 610.0, "unit": "crore", "period": "fy", "period_end": f"{q_end.year}-03-31", "scope": "consolidated", "source": "ar"},
            {"metric": "pe_ttm", "value": 31.5, "unit": "x", "period": "ttm", "period_end": q_end.isoformat(), "source": "results"},
            {"metric": "roe_pct", "value": 17.2, "unit": "%", "period": "fy", "period_end": f"{q_end.year}-03-31", "source": "ar"},
            {"metric": "debt_to_equity", "value": 0.22, "unit": "x", "period": "fy", "period_end": f"{q_end.year}-03-31", "source": "ar"},
            {"metric": "promoter_holding_pct", "value": 58.4, "unit": "%", "period": "quarter", "period_end": q_end.isoformat(), "source": "shp"},
            {"metric": "promoter_pledge_pct", "value": 0.0, "unit": "%", "period": "quarter", "period_end": q_end.isoformat(), "source": "shp"},
        ],
        "notes": [
            {"source": "results", "text": "Net profit growth was 14% year on year, but 6% excluding a one-time gain on sale of land in the current quarter."},
            {"source": "results", "text": "Exceptional item: gain of Rs 16 crore on sale of non-core land."},
            {"source": "ar", "text": "The statutory auditor issued an unmodified opinion with no emphasis of matter for the last financial year."},
            {"source": "ar", "text": "Operating cash flow was 0.92 times net profit over the last three financial years."},
            {"source": "ar", "text": "Related-party transactions were 2.1% of revenue, mainly purchases from a promoter-group logistics company."},
            {"source": "shp", "text": "Promoter holding was unchanged at 58.4% with no shares pledged over the last four quarters."},
        ],
        "surveillance": {"status": "not_listed", "source": "DEMO synthetic surveillance list"},
    }


def news_items(decision_day: date) -> list[dict]:
    d = lambda k: (decision_day - timedelta(days=k)).isoformat() + "T09:00:00+05:30"  # noqa: E731
    return [
        {"title": "DEMO: Demo Precision wins Rs 420 crore order from a state rail operator", "publisher": "Demo Financial Daily",
         "published_at": d(12), "text": "Demo Precision Industries said it won an order worth Rs 420 crore from a state rail operator, to be executed over 18 months. The company did not disclose margins on the contract."},
        {"title": "DEMO: Demo Exchange announcement — order win disclosure", "publisher": "Demo Exchange",
         "published_at": d(12), "text": "Disclosure under Regulation 30: the company has received a letter of award for Rs 420 crore, subject to execution of a definitive contract."},
        {"title": "DEMO: Brokerage starts coverage on Demo Precision with an add rating", "publisher": "Demo Financial Daily",
         "published_at": d(30), "text": "A brokerage initiated coverage with an add rating and a 12-month target of Rs 1,450, citing rail and defence orders."},
    ]


def capture(decision_day: date) -> dict:
    return {"title": "DEMO Prime: Is the order book enough to rerate Demo Precision?",
            "publisher": "Demo Times Prime (fictional capture)", "published_at": (decision_day - timedelta(days=5)).isoformat() + "T07:00:00+05:30",
            "passages": [
                "The order book now stands at about 2.6 times trailing revenue, the highest in five years, according to the company.",
                "Margins on rail orders have historically been lower than on export orders, which could dilute operating margin next year.",
                "The stock trades at about 31 times trailing earnings, above its five-year average of 26 times.",
                "Two analysts we spoke to expect working capital to rise as execution accelerates."],
            "warnings": ["capture_unverified", "Fallback body selector used — check the article end for completeness"]}
