"""Deterministic technical features (spec §6 feature store; ports the role of the skill's
technicals.py). Pure Python so results are reproducible and testable.

Input candles: list of dicts with date (YYYY-MM-DD), open, high, low, close, volume.
"""
from __future__ import annotations

from statistics import mean
from typing import Optional

CODE_VERSION = "technicals@1"


def sma(values: list[float], n: int) -> list[Optional[float]]:
    out: list[Optional[float]] = []
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def ema(values: list[float], n: int) -> list[Optional[float]]:
    out: list[Optional[float]] = []
    k = 2 / (n + 1)
    prev: Optional[float] = None
    for i, v in enumerate(values):
        if i < n - 1:
            out.append(None)
            continue
        if prev is None:
            prev = mean(values[i - n + 1:i + 1])
        else:
            prev = v * k + prev * (1 - k)
        out.append(prev)
    return out


def rsi(closes: list[float], n: int = 14) -> list[Optional[float]]:
    """Wilder's RSI."""
    out: list[Optional[float]] = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    avg_g = mean(gains[:n])
    avg_l = mean(losses[:n])
    for i in range(n, len(closes)):
        if i > n:
            avg_g = (avg_g * (n - 1) + gains[i - 1]) / n
            avg_l = (avg_l * (n - 1) + losses[i - 1]) / n
        out[i] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return out


def macd(closes: list[float]) -> tuple[list[Optional[float]], list[Optional[float]]]:
    e12, e26 = ema(closes, 12), ema(closes, 26)
    line = [a - b if a is not None and b is not None else None for a, b in zip(e12, e26)]
    valid = [x for x in line if x is not None]
    sig_valid = ema(valid, 9) if len(valid) >= 9 else [None] * len(valid)
    signal: list[Optional[float]] = [None] * (len(line) - len(valid)) + sig_valid
    return line, signal


def atr(candles: list[dict], n: int = 14) -> list[Optional[float]]:
    trs = []
    for i, c in enumerate(candles):
        if i == 0:
            trs.append(c["high"] - c["low"])
        else:
            pc = candles[i - 1]["close"]
            trs.append(max(c["high"] - c["low"], abs(c["high"] - pc), abs(c["low"] - pc)))
    out: list[Optional[float]] = [None] * len(candles)
    if len(trs) < n:
        return out
    a = mean(trs[:n])
    out[n - 1] = a
    for i in range(n, len(trs)):
        a = (a * (n - 1) + trs[i]) / n
        out[i] = a
    return out


def swing_points(candles: list[dict], k: int = 5) -> tuple[list[int], list[int]]:
    """Indices of swing highs/lows (local extremum over +-k bars)."""
    highs, lows = [], []
    for i in range(k, len(candles) - k):
        win = candles[i - k:i + k + 1]
        if candles[i]["high"] == max(c["high"] for c in win):
            highs.append(i)
        if candles[i]["low"] == min(c["low"] for c in win):
            lows.append(i)
    return highs, lows


def support_resistance(candles: list[dict], last_close: float, tol: float = 0.015) -> dict:
    """Cluster swing points into zones; return nearest support below / resistance above."""
    hi_idx, lo_idx = swing_points(candles)
    levels = sorted([candles[i]["high"] for i in hi_idx] + [candles[i]["low"] for i in lo_idx])
    zones: list[list[float]] = []
    for lv in levels:
        if zones and abs(lv - mean(zones[-1])) / mean(zones[-1]) <= tol:
            zones[-1].append(lv)
        else:
            zones.append([lv])
    zone_list = [{"level": round(mean(z), 2), "touches": len(z)} for z in zones]
    below = [z for z in zone_list if z["level"] < last_close]
    above = [z for z in zone_list if z["level"] > last_close]
    return {
        "support": max(below, key=lambda z: z["level"]) if below else None,
        "resistance": min(above, key=lambda z: z["level"]) if above else None,
        "zones": zone_list[-12:],
    }


def candlestick_patterns(candles: list[dict], sma20: list[Optional[float]]) -> list[dict]:
    """Last-bar candlestick patterns with trend context (only meaningful in context)."""
    out = []
    if len(candles) < 3:
        return out
    c, p = candles[-1], candles[-2]
    body = abs(c["close"] - c["open"])
    rng = max(c["high"] - c["low"], 1e-9)
    lower = min(c["open"], c["close"]) - c["low"]
    upper = c["high"] - max(c["open"], c["close"])
    trend = None
    if sma20[-1] is not None and sma20[-6] is not None:
        trend = "down" if sma20[-1] < sma20[-6] else "up"
    if body / rng < 0.1:
        out.append({"name": "doji", "bias": "indecision", "trend_context": trend})
    if lower > 2 * body and upper < body and trend == "down":
        out.append({"name": "hammer", "bias": "bullish_reversal_candidate", "trend_context": trend})
    if upper > 2 * body and lower < body and trend == "up":
        out.append({"name": "shooting_star", "bias": "bearish_reversal_candidate", "trend_context": trend})
    if (p["close"] < p["open"] and c["close"] > c["open"] and c["close"] >= p["open"] and c["open"] <= p["close"]):
        out.append({"name": "bullish_engulfing", "bias": "bullish", "trend_context": trend})
    if (p["close"] > p["open"] and c["close"] < c["open"] and c["open"] >= p["close"] and c["close"] <= p["open"]):
        out.append({"name": "bearish_engulfing", "bias": "bearish", "trend_context": trend})
    return out


def compute(candles: list[dict]) -> dict:
    """All features for the latest bar. Missing => None with explicit status by caller."""
    closes = [c["close"] for c in candles]
    n = len(closes)
    s20, s50, s200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)
    r = rsi(closes)
    m_line, m_sig = macd(closes)
    a = atr(candles)
    last = candles[-1]
    last_252 = candles[-252:]
    hi52 = max(c["high"] for c in last_252)
    lo52 = min(c["low"] for c in last_252)
    adv20 = None
    if n >= 20:
        adv20 = mean(c["close"] * c["volume"] for c in candles[-20:]) / 1e7  # ₹ crore
    vol_bias = None
    if n >= 20:
        up = sum(c["volume"] for c in candles[-20:] if c["close"] >= c["open"])
        tot = sum(c["volume"] for c in candles[-20:]) or 1
        vol_bias = up / tot
    ret = lambda d: (closes[-1] / closes[-1 - d] - 1) * 100 if n > d else None  # noqa: E731
    return {
        "code_version": CODE_VERSION,
        "bars": n,
        "last_date": last["date"],
        "close": closes[-1],
        "sma20": s20[-1], "sma50": s50[-1], "sma200": s200[-1],
        "rsi14": r[-1],
        "macd": m_line[-1], "macd_signal": m_sig[-1],
        "atr14": a[-1],
        "atr_pct": (a[-1] / closes[-1] * 100) if a[-1] else None,
        "high_52w": hi52, "low_52w": lo52,
        "pct_from_52w_high": (closes[-1] / hi52 - 1) * 100,
        "pct_above_52w_low": (closes[-1] / lo52 - 1) * 100,
        "pos_in_52w_range_pct": ((closes[-1] - lo52) / (hi52 - lo52) * 100) if hi52 > lo52 else None,
        "range_bars": len(last_252),
        "adv20_crore": adv20,
        "up_volume_share_20d": vol_bias,
        "ret_1m_pct": ret(21), "ret_3m_pct": ret(63), "ret_6m_pct": ret(126),
        "sr": support_resistance(candles[-180:], closes[-1]),
        "candles": candlestick_patterns(candles, s20),
        "series": {"sma20": s20, "sma50": s50, "sma200": s200},
    }
