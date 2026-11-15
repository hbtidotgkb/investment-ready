"""Chart-pattern detection (role of the skill's chart_patterns.py).

Detects double bottom/top and (inverse) head & shoulders on swing points. Each pattern
reports status (forming | confirmed | failed), neckline, measured-move target, pattern stop,
breakout-volume confirmation and a quality score. Heuristic: patterns are evidence for the
Quant brain to interpret, never a trade by themselves.
"""
from __future__ import annotations

from statistics import mean

from .technicals import swing_points

CODE_VERSION = "patterns@1"


def _status(candles: list[dict], start: int, neckline: float, bullish: bool, invalid: float) -> tuple[str, int | None]:
    for i in range(start, len(candles)):
        c = candles[i]["close"]
        if bullish and c < invalid:
            return "failed", i
        if not bullish and c > invalid:
            return "failed", i
        if bullish and c > neckline:
            return "confirmed", i
        if not bullish and c < neckline:
            return "confirmed", i
    return "forming", None


def _vol_confirm(candles: list[dict], i: int | None) -> bool | None:
    if i is None or i < 20:
        return None
    avg = mean(c["volume"] for c in candles[i - 20:i])
    return candles[i]["volume"] >= 1.5 * avg


def detect(candles: list[dict], lookback: int = 160, tol: float = 0.03) -> list[dict]:
    cs = candles[-lookback:]
    off = len(candles) - len(cs)
    hi, lo = swing_points(cs, k=4)
    found: list[dict] = []

    # Double bottom: two comparable lows separated by a swing high
    for a_i, b_i in zip(lo, lo[1:]):
        la, lb = cs[a_i]["low"], cs[b_i]["low"]
        if b_i - a_i < 10 or abs(la - lb) / la > tol:
            continue
        mids = [h for h in hi if a_i < h < b_i]
        if not mids:
            continue
        neck = max(cs[h]["high"] for h in mids)
        height = neck - min(la, lb)
        if height / neck < 0.04:
            continue
        status, bi = _status(cs, b_i + 1, neck, True, min(la, lb) * 0.99)
        found.append({
            "name": "double_bottom", "bias": "bullish", "status": status,
            "start_date": cs[a_i]["date"], "end_date": cs[b_i]["date"],
            "neckline": round(neck, 2), "measured_target": round(neck + height, 2),
            "pattern_stop": round(min(la, lb) * 0.99, 2),
            "breakout_date": cs[bi]["date"] if bi is not None else None,
            "breakout_volume_confirmed": _vol_confirm(cs, bi),
            "quality": round(1 - abs(la - lb) / la / tol, 2), "code_version": CODE_VERSION,
            "_idx": off + b_i,
        })

    # Double top
    for a_i, b_i in zip(hi, hi[1:]):
        ha, hb = cs[a_i]["high"], cs[b_i]["high"]
        if b_i - a_i < 10 or abs(ha - hb) / ha > tol:
            continue
        mids = [l for l in lo if a_i < l < b_i]
        if not mids:
            continue
        neck = min(cs[l]["low"] for l in mids)
        height = max(ha, hb) - neck
        if height / neck < 0.04:
            continue
        status, bi = _status(cs, b_i + 1, neck, False, max(ha, hb) * 1.01)
        found.append({
            "name": "double_top", "bias": "bearish", "status": status,
            "start_date": cs[a_i]["date"], "end_date": cs[b_i]["date"],
            "neckline": round(neck, 2), "measured_target": round(neck - height, 2),
            "pattern_stop": round(max(ha, hb) * 1.01, 2),
            "breakout_date": cs[bi]["date"] if bi is not None else None,
            "breakout_volume_confirmed": _vol_confirm(cs, bi),
            "quality": round(1 - abs(ha - hb) / ha / tol, 2), "code_version": CODE_VERSION,
            "_idx": off + b_i,
        })

    # Head & shoulders (bearish) on three consecutive swing highs
    for l_i, h_i, r_i in zip(hi, hi[1:], hi[2:]):
        L, H, R = cs[l_i]["high"], cs[h_i]["high"], cs[r_i]["high"]
        if not (H > L * 1.03 and H > R * 1.03 and abs(L - R) / L <= 0.05):
            continue
        troughs = [l for l in lo if l_i < l < r_i]
        if len(troughs) < 2:
            continue
        neck = mean(cs[t]["low"] for t in troughs[:2])
        status, bi = _status(cs, r_i + 1, neck, False, H)
        found.append({
            "name": "head_and_shoulders", "bias": "bearish", "status": status,
            "start_date": cs[l_i]["date"], "end_date": cs[r_i]["date"],
            "neckline": round(neck, 2), "measured_target": round(neck - (H - neck), 2),
            "pattern_stop": round(R * 1.01, 2), "breakout_date": cs[bi]["date"] if bi is not None else None,
            "breakout_volume_confirmed": _vol_confirm(cs, bi), "quality": 0.6, "code_version": CODE_VERSION,
            "_idx": off + r_i,
        })

    # Inverse head & shoulders (bullish)
    for l_i, h_i, r_i in zip(lo, lo[1:], lo[2:]):
        L, H, R = cs[l_i]["low"], cs[h_i]["low"], cs[r_i]["low"]
        if not (H < L * 0.97 and H < R * 0.97 and abs(L - R) / L <= 0.05):
            continue
        peaks = [h for h in hi if l_i < h < r_i]
        if len(peaks) < 2:
            continue
        neck = mean(cs[p]["high"] for p in peaks[:2])
        status, bi = _status(cs, r_i + 1, neck, True, H)
        found.append({
            "name": "inverse_head_and_shoulders", "bias": "bullish", "status": status,
            "start_date": cs[l_i]["date"], "end_date": cs[r_i]["date"],
            "neckline": round(neck, 2), "measured_target": round(neck + (neck - H), 2),
            "pattern_stop": round(R * 0.99, 2), "breakout_date": cs[bi]["date"] if bi is not None else None,
            "breakout_volume_confirmed": _vol_confirm(cs, bi), "quality": 0.6, "code_version": CODE_VERSION,
            "_idx": off + r_i,
        })

    # keep the most recent, non-overlapping few
    found.sort(key=lambda p: p["_idx"], reverse=True)
    out, seen = [], set()
    for p in found:
        if p["name"] in seen:
            continue
        seen.add(p["name"])
        p.pop("_idx", None)
        out.append(p)
    return out[:4]
