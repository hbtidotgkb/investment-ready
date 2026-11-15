"""NSE full bhavcopy ingestion (spec §6 `ingest_bhav`): incremental, holiday-safe, cached.

Raw files are stored unadjusted under data/raw/bhav/ with the source file recorded on each
row. A PREV_CLOSE that differs from the prior stored close flags a probable corporate action
(split/bonus) — dependent technical conclusions are then blocked until resolved.
"""
from __future__ import annotations

import csv
import io
from datetime import date, timedelta

from ..ledger import get_ledger
from ..settings import RAW
from .sources import PoliteClient

URL = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{d}.csv"
BHAV_DIR = RAW / "bhav"
BHAV_DIR.mkdir(parents=True, exist_ok=True)


def _f(x: str) -> float | None:
    x = (x or "").strip()
    if x in ("", "-"):
        return None
    try:
        return float(x)
    except ValueError:
        return None


def fetch_day(client: PoliteClient, d: date) -> str | None:
    """Return CSV text for a trading day, or None for holidays / not yet published."""
    fname = BHAV_DIR / f"sec_bhavdata_full_{d:%d%m%Y}.csv"
    if fname.exists():
        return fname.read_text(encoding="utf-8", errors="replace")
    marker = BHAV_DIR / f"none_{d:%d%m%Y}"
    if marker.exists() and d < date.today() - timedelta(days=3):
        return None
    r = client.get(URL.format(d=f"{d:%d%m%Y}"))
    if r.status_code == 200 and "SYMBOL" in r.text[:200]:
        fname.write_text(r.text, encoding="utf-8")
        return r.text
    if r.status_code == 404:
        marker.touch()
        return None
    raise RuntimeError(f"bhavcopy {d}: HTTP {r.status_code}")


def ingest(days: int = 260, progress=None) -> dict:
    """Ingest the last `days` calendar-days' worth of trading files (skips cached files)."""
    led = get_ledger()
    client = PoliteClient(min_interval=1.0)
    added_files, missing, errors = 0, 0, []
    d = date.today()
    seen_trading = 0
    try:
        while seen_trading < days and d > date.today() - timedelta(days=int(days * 1.6) + 10):
            if d.weekday() < 5:
                try:
                    text = fetch_day(client, d)
                except Exception as e:  # network/archive problems are recorded, not hidden
                    errors.append(f"{d}: {e}")
                    text = None
                if text:
                    seen_trading += 1
                    already = led.fetchone("SELECT 1 AS x FROM bhav_daily WHERE date=? LIMIT 1", [d.isoformat()])
                    if not already:
                        _load(text, d)
                        added_files += 1
                    if progress:
                        progress(f"{d}: ok ({seen_trading}/{days})")
                else:
                    missing += 1
            d -= timedelta(days=1)
    finally:
        client.close()
    return {"trading_days_available": seen_trading, "files_loaded": added_files,
            "non_trading_or_missing": missing, "errors": errors[:20]}


def _load(text: str, d: date) -> None:
    led = get_ledger()
    rows = []
    reader = csv.DictReader(io.StringIO(text))
    for r in reader:
        r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        if r.get("SERIES") not in ("EQ", "BE", "BZ", "SM", "ST"):
            continue
        rows.append([r["SYMBOL"], r["SERIES"], d.isoformat(), _f(r.get("PREV_CLOSE")), _f(r.get("OPEN_PRICE")),
                     _f(r.get("HIGH_PRICE")), _f(r.get("LOW_PRICE")), _f(r.get("CLOSE_PRICE")),
                     _f(r.get("TTL_TRD_QNTY")), _f(r.get("TURNOVER_LACS")), _f(r.get("DELIV_PER")),
                     f"sec_bhavdata_full_{d:%d%m%Y}.csv"])
    if rows:
        led.con.executemany("INSERT OR REPLACE INTO bhav_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)


def candles(symbol: str, limit: int = 400) -> tuple[list[dict], list[str]]:
    """Return (candles oldest→newest, flags). Flags include probable corporate actions."""
    rows = get_ledger().fetchall(
        "SELECT * FROM bhav_daily WHERE symbol=? AND series='EQ' ORDER BY date DESC LIMIT ?", [symbol.upper(), limit])
    rows.reverse()
    out, flags = [], []
    prev_close = None
    for r in rows:
        if r["close"] is None:
            continue
        if prev_close and r["prev_close"] and abs(r["prev_close"] / prev_close - 1) > 0.02:
            flags.append(f"Probable corporate action on {r['date']}: NSE PREV_CLOSE {r['prev_close']} vs stored close "
                         f"{prev_close} — series is unadjusted; technical levels before this date are unreliable")
        out.append({"date": str(r["date"]), "open": r["open"], "high": r["high"], "low": r["low"],
                    "close": r["close"], "volume": r["volume"] or 0, "deliv_pct": r["deliv_pct"],
                    "source_file": r["source_file"]})
        prev_close = r["close"]
    return out, flags
