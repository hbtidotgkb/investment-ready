"""Zerodha Kite Connect adapter — READ-ONLY by construction (spec §16).

The underlying credentials could trade, so this wrapper exposes only an allowlist of read
operations. Order methods are not reachable through it, and a test enforces that.
Access tokens expire daily; the session check returns a one-line "please log in" status
instead of failing silently (spec §12).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from ..settings import RAW, get_secret, set_secret

READ_ALLOWLIST = frozenset({
    "profile", "holdings", "positions", "ltp", "quote", "ohlc", "historical_data", "instruments", "margins",
})

_TOKEN_KEY = "KITE_ACCESS_TOKEN"
_TOKEN_DATE_KEY = "KITE_ACCESS_TOKEN_DATE"


class ReadOnlyKite:
    def __init__(self, kite):
        object.__setattr__(self, "_kite", kite)

    def __getattr__(self, name):
        if name not in READ_ALLOWLIST:
            raise PermissionError(f"Kite operation '{name}' is not in the read-only allowlist")
        return getattr(object.__getattribute__(self, "_kite"), name)

    def __setattr__(self, name, value):
        raise PermissionError("ReadOnlyKite is immutable")


def _token_store_get(key: str) -> str | None:
    try:
        return get_secret(key)
    except Exception:
        return None


def configured() -> bool:
    return bool(get_secret("KITE_API_KEY") and get_secret("KITE_API_SECRET"))


def login_url() -> str:
    from kiteconnect import KiteConnect
    return KiteConnect(api_key=get_secret("KITE_API_KEY")).login_url()


def complete_login(request_token: str) -> dict:
    from kiteconnect import KiteConnect
    kc = KiteConnect(api_key=get_secret("KITE_API_KEY"))
    data = kc.generate_session(request_token, api_secret=get_secret("KITE_API_SECRET"))
    set_secret(_TOKEN_KEY, data["access_token"])
    set_secret(_TOKEN_DATE_KEY, date.today().isoformat())
    return {"user_id": "redacted", "login_time": str(data.get("login_time"))}


def client() -> ReadOnlyKite | None:
    """Return a read-only client when a same-day session exists, else None."""
    if not configured():
        return None
    tok = _token_store_get(_TOKEN_KEY)
    tok_date = _token_store_get(_TOKEN_DATE_KEY)
    if not tok or tok_date != date.today().isoformat():
        return None
    from kiteconnect import KiteConnect
    kc = KiteConnect(api_key=get_secret("KITE_API_KEY"))
    kc.set_access_token(tok)
    return ReadOnlyKite(kc)


def status() -> dict:
    if not configured():
        return {"state": "not_configured", "message": "Add KITE_API_KEY / KITE_API_SECRET (ca secrets set) to enable Kite."}
    k = client()
    if k is None:
        return {"state": "login_required", "message": "Kite session expired — please log in (sessions expire daily)."}
    try:
        p = k.profile()
        return {"state": "connected", "message": "Kite connected (read-only).",
                "broker": p.get("broker"), "products": p.get("products")}
    except Exception as e:
        return {"state": "error", "message": f"Kite error: {type(e).__name__}"}


_INSTR_CACHE = RAW / "kite_instruments_nse.json"


def instrument_token(k: ReadOnlyKite, symbol: str) -> int | None:
    rows = None
    if _INSTR_CACHE.exists():
        cached = json.loads(_INSTR_CACHE.read_text(encoding="utf-8"))
        if cached.get("date") == date.today().isoformat():
            rows = cached["rows"]
    if rows is None:
        rows = [{"tradingsymbol": i["tradingsymbol"], "instrument_token": i["instrument_token"], "name": i.get("name")}
                for i in k.instruments("NSE") if i.get("segment") == "NSE"]
        _INSTR_CACHE.write_text(json.dumps({"date": date.today().isoformat(), "rows": rows}), encoding="utf-8")
    for r in rows:
        if r["tradingsymbol"] == symbol.upper():
            return r["instrument_token"]
    return None


def candles(k: ReadOnlyKite, symbol: str, days: int = 400) -> list[dict]:
    tok = instrument_token(k, symbol)
    if tok is None:
        return []
    to = datetime.now()
    frm = to - timedelta(days=int(days * 1.5))
    data = k.historical_data(tok, frm, to, "day")
    return [{"date": str(d["date"])[:10], "open": float(d["open"]), "high": float(d["high"]),
             "low": float(d["low"]), "close": float(d["close"]), "volume": float(d["volume"]),
             "source_file": "kite:historical_data"} for d in data]


def holdings() -> list[dict] | None:
    k = client()
    if k is None:
        return None
    out = []
    for h in k.holdings():
        out.append({"symbol": h.get("tradingsymbol"), "exchange": h.get("exchange"), "isin": h.get("isin"),
                    "quantity": h.get("quantity", 0) + h.get("t1_quantity", 0),
                    "average_price": h.get("average_price"), "last_price": h.get("last_price"),
                    "pnl": h.get("pnl"), "day_change_pct": h.get("day_change_percentage"),
                    "note": "Average price is not a tax-lot history (spec §9 Tax)."})
    return out
