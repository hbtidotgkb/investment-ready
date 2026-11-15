"""L0 code evals: deterministic feature builders."""
from ca_platform.data import demo
from ca_platform.features import patterns, technicals


def test_sma_and_rsi_bounds():
    closes = [float(x) for x in range(1, 61)]
    s = technicals.sma(closes, 20)
    assert s[18] is None and s[19] == sum(range(1, 21)) / 20
    r = technicals.rsi(closes)
    assert r[-1] == 100.0  # monotonic rise => no losses
    down = list(reversed(closes))
    assert technicals.rsi(down)[-1] == 0.0


def test_compute_on_demo_series_is_complete_and_deterministic():
    c = demo.candles()
    f1, f2 = technicals.compute(c), technicals.compute(c)
    assert f1["bars"] == 320 and f1["sma200"] is not None and f1["adv20_crore"] > 0
    assert f1["close"] == f2["close"] and f1["rsi14"] == f2["rsi14"]
    assert f1["low_52w"] <= f1["close"] <= f1["high_52w"]


def _w_shape():
    """Synthetic W: down to 100, up to 120, down to 101, up to 125 (breakout)."""
    pts = []
    path = [140 - i for i in range(40)] + [100 + i * 0.8 for i in range(25)] + [120 - i * 0.8 for i in range(24)] + \
           [101 + i for i in range(30)]
    for i, p in enumerate(path):
        pts.append({"date": f"2026-01-{1 + i % 28:02d}", "open": p, "high": p + 0.5, "low": p - 0.5, "close": p,
                    "volume": 1000 * (3 if i == len(path) - 6 else 1)})
    return pts


def test_double_bottom_detected_with_neckline_and_target():
    found = patterns.detect(_w_shape())
    db = [p for p in found if p["name"] == "double_bottom"]
    assert db, found
    p = db[0]
    assert p["status"] == "confirmed"
    assert p["measured_target"] > p["neckline"] > p["pattern_stop"]
