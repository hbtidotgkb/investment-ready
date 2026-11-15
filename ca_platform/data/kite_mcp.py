"""Zerodha's hosted Kite MCP server as a platform data source — READ-ONLY by construction.

Login is the standard MCP OAuth flow the server advertises (authorization code + PKCE with
dynamic client registration): the user clicks "Connect Kite (MCP)", logs in on Zerodha's page,
and the callback stores the access token locally (OS keychain, falling back to the git-ignored
data/secrets folder). The token is never sent to the browser or to any model.

Only tools in READ_TOOLS can be called; order/GTT/MF-order tools are refused in code even if
the server offers them. Tool arguments are matched against each tool's live input schema, so a
renamed parameter produces a clear error instead of a silent wrong call.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from ..settings import SECRETS_DIR, get_secret, set_secret

BASE = "https://mcp.kite.trade"
MCP_URL = f"{BASE}/mcp"
READ_TOOLS = frozenset({
    "get_profile", "get_margins", "get_holdings", "get_positions", "get_mf_holdings", "get_quotes", "get_ltp",
    "get_ohlc", "get_historical_data", "search_instruments", "get_trades", "get_orders", "get_order_history",
    "get_order_trades", "get_gtts",
})
WRITE_HINTS = ("place", "modify", "cancel", "delete", "exit", "convert", "sip")

_TOKEN_FILE = SECRETS_DIR / "kite_mcp_token.json"
_CLIENT_FILE = SECRETS_DIR / "kite_mcp_client.json"
_PENDING: dict[str, dict] = {}
_TOOLS_CACHE: dict[str, Any] = {}


class KiteMCPError(RuntimeError):
    pass


# ------------------------------------------------------------------ token store ---

def _save_token(tok: dict) -> None:
    blob = json.dumps(tok)
    try:
        set_secret("KITE_MCP_TOKEN", blob)
        _TOKEN_FILE.unlink(missing_ok=True)
    except Exception:  # Windows credential blobs are size-limited; fall back to the local secrets dir
        _TOKEN_FILE.write_text(blob, encoding="utf-8")


def _load_token() -> dict | None:
    raw = None
    try:
        raw = get_secret("KITE_MCP_TOKEN")
    except Exception:
        pass
    if not raw and _TOKEN_FILE.exists():
        raw = _TOKEN_FILE.read_text(encoding="utf-8")
    if not raw:
        return None
    try:
        tok = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if tok.get("expires_at") and tok["expires_at"] < time.time():
        return None
    return tok


def clear_token() -> None:
    _TOKEN_FILE.unlink(missing_ok=True)
    try:
        set_secret("KITE_MCP_TOKEN", "")
    except Exception:
        pass


def connected() -> bool:
    return _load_token() is not None


# ------------------------------------------------------------------- OAuth flow ---

def _client_id(redirect_uri: str) -> str:
    clients = json.loads(_CLIENT_FILE.read_text(encoding="utf-8")) if _CLIENT_FILE.exists() else {}
    if redirect_uri in clients:
        return clients[redirect_uri]
    r = httpx.post(f"{BASE}/register", timeout=20, json={
        "client_name": "Personal CA (local)", "redirect_uris": [redirect_uri], "grant_types": ["authorization_code"],
        "response_types": ["code"], "token_endpoint_auth_method": "none"})
    if r.status_code not in (200, 201):
        raise KiteMCPError(f"Kite MCP client registration failed: HTTP {r.status_code}")
    clients[redirect_uri] = r.json()["client_id"]
    _CLIENT_FILE.write_text(json.dumps(clients), encoding="utf-8")
    return clients[redirect_uri]


def start_login(redirect_uri: str) -> str:
    """Return the Zerodha authorization URL (PKCE S256 + state)."""
    cid = _client_id(redirect_uri)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    _PENDING[state] = {"verifier": verifier, "redirect_uri": redirect_uri, "client_id": cid, "t": time.time()}
    return f"{BASE}/authorize?" + urlencode({
        "response_type": "code", "client_id": cid, "redirect_uri": redirect_uri, "code_challenge": challenge,
        "code_challenge_method": "S256", "state": state, "scope": "default", "resource": MCP_URL})


def complete_login(code: str, state: str) -> None:
    p = _PENDING.pop(state, None)
    if not p or time.time() - p["t"] > 900:
        raise KiteMCPError("Login state expired or unknown — start the Kite MCP login again")
    r = httpx.post(f"{BASE}/token", timeout=20, data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": p["redirect_uri"],
        "client_id": p["client_id"], "code_verifier": p["verifier"], "resource": MCP_URL})
    if r.status_code != 200:
        raise KiteMCPError(f"Kite MCP token exchange failed: HTTP {r.status_code}")
    t = r.json()
    tok = {"access_token": t["access_token"], "token_type": t.get("token_type", "Bearer"), "obtained_at": time.time()}
    if t.get("expires_in"):
        tok["expires_at"] = time.time() + int(t["expires_in"]) - 60
    _save_token(tok)


# --------------------------------------------------------------------- tool calls ---

def _allowed(name: str) -> bool:
    return name in READ_TOOLS and not any(h in name for h in WRITE_HINTS)


def _parse(result) -> Any:
    if getattr(result, "isError", False):
        txt = " ".join(getattr(c, "text", "") for c in (result.content or []))
        if "auth" in txt.lower() or "login" in txt.lower() or "token" in txt.lower():
            clear_token()
        raise KiteMCPError(f"Kite MCP tool error: {txt[:300]}")
    sc = getattr(result, "structuredContent", None)
    if sc:
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    texts = [getattr(c, "text", "") for c in (result.content or []) if getattr(c, "type", "") == "text"]
    joined = "\n".join(texts).strip()
    try:
        return json.loads(joined)
    except json.JSONDecodeError:
        return joined


async def _session_call(calls: list[tuple[str, dict]]) -> list[Any]:
    tok = _load_token()
    if not tok:
        raise KiteMCPError("Kite MCP not connected — log in from the Portfolio page")
    for name, _ in calls:
        if not _allowed(name):
            raise PermissionError(f"Kite MCP tool '{name}' is not in the read-only allowlist")
    from mcp import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {tok['access_token']}"})
    out = []
    try:
        async with http:
            async with streamable_http_client(MCP_URL, http_client=http) as streams:
                async with ClientSession(streams[0], streams[1]) as s:
                    await s.initialize()
                    if "tools" not in _TOOLS_CACHE or time.time() - _TOOLS_CACHE.get("t", 0) > 3600:
                        tl = await s.list_tools()
                        _TOOLS_CACHE["tools"] = {t.name: (t.inputSchema or {}) for t in tl.tools}
                        _TOOLS_CACHE["t"] = time.time()
                    for name, args in calls:
                        if name not in _TOOLS_CACHE["tools"]:
                            raise KiteMCPError(f"Kite MCP server no longer offers '{name}'")
                        out.append(_parse(await s.call_tool(name, _fit_args(name, args))))
    except BaseExceptionGroup as eg:  # anyio task groups wrap errors
        first = eg.exceptions[0]
        if "401" in str(first):
            clear_token()
            raise KiteMCPError("Kite MCP session expired — log in again") from first
        raise KiteMCPError(f"Kite MCP call failed: {type(first).__name__}: {str(first)[:200]}") from first
    return out


def _fit_args(name: str, args: dict) -> dict:
    """Map our argument names onto the tool's live schema (first matching alias wins)."""
    schema = _TOOLS_CACHE["tools"].get(name, {})
    props = set((schema.get("properties") or {}).keys())
    fitted = {}
    for keys, val in args.items():
        aliases = keys if isinstance(keys, tuple) else (keys,)
        hit = next((a for a in aliases if a in props), None)
        if hit is None:
            raise KiteMCPError(f"Kite MCP tool '{name}' schema changed: none of {aliases} in {sorted(props)}")
        fitted[hit] = val
    missing = [r for r in schema.get("required", []) if r not in fitted]
    if missing:
        raise KiteMCPError(f"Kite MCP tool '{name}' needs {missing}")
    return fitted


async def call(name: str, args: dict | None = None) -> Any:
    """args keys may be a name or a tuple of accepted aliases, e.g. {("from_date", "from"): "..."}."""
    return (await _session_call([(name, args or {})]))[0]


# ----------------------------------------------------------------- data helpers ---

async def status() -> dict:
    if not connected():
        return {"state": "not_connected", "message": "Kite MCP not connected — click Connect (free; uses Zerodha's hosted MCP)."}
    try:
        p = await call("get_profile")
        name = p.get("user_shortname") if isinstance(p, dict) else None
        return {"state": "connected", "message": "Kite MCP connected (read-only)" + (f" — {name}" if name else "")}
    except PermissionError:
        raise
    except Exception as e:
        return {"state": "login_required", "message": f"Kite MCP needs login again ({str(e)[:120]})"}


def _rows(data: Any, key_guess: tuple[str, ...] = ("data", "holdings", "candles", "results")) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in key_guess:
            if isinstance(data.get(k), list):
                return data[k]
    return []


async def holdings() -> list[dict]:
    rows = _rows(await call("get_holdings"))
    out = []
    for h in rows:
        out.append({"symbol": h.get("tradingsymbol"), "exchange": h.get("exchange"), "isin": h.get("isin"),
                    "quantity": (h.get("quantity") or 0) + (h.get("t1_quantity") or 0),
                    "average_price": h.get("average_price"), "last_price": h.get("last_price"), "pnl": h.get("pnl"),
                    "day_change_pct": h.get("day_change_percentage"), "via": "kite_mcp",
                    "note": "Average price is not a tax-lot history (spec §9 Tax)."})
    return out


async def instrument_token(symbol: str) -> int | None:
    res = _rows(await call("search_instruments", {("query", "q"): symbol.upper()}))
    for r in res:
        if str(r.get("tradingsymbol", "")).upper() == symbol.upper() and r.get("exchange") in ("NSE", None):
            return int(r.get("instrument_token"))
    return None


async def candles(symbol: str, days: int = 400) -> list[dict]:
    tok = await instrument_token(symbol)
    if tok is None:
        return []
    to = datetime.now()
    frm = to - timedelta(days=int(days * 1.5))
    data = await call("get_historical_data", {
        ("instrument_token", "token"): tok, ("from_date", "from"): frm.strftime("%Y-%m-%d 09:15:00"),
        ("to_date", "to"): to.strftime("%Y-%m-%d 15:30:00"), ("interval",): "day"})
    out = []
    for d in _rows(data):
        if isinstance(d, dict):
            out.append({"date": str(d.get("date"))[:10], "open": float(d["open"]), "high": float(d["high"]),
                        "low": float(d["low"]), "close": float(d["close"]), "volume": float(d.get("volume") or 0),
                        "source_file": "kite_mcp:get_historical_data"})
        elif isinstance(d, list) and len(d) >= 6:  # [date, o, h, l, c, v]
            out.append({"date": str(d[0])[:10], "open": float(d[1]), "high": float(d[2]), "low": float(d[3]),
                        "close": float(d[4]), "volume": float(d[5]), "source_file": "kite_mcp:get_historical_data"})
    return out


async def ltp(symbols: list[str]) -> dict[str, float]:
    if not symbols:
        return {}
    data = await call("get_ltp", {("instruments",): [f"NSE:{s.upper()}" for s in symbols]})
    out = {}
    if isinstance(data, dict):
        for k, v in data.items():
            price = v.get("last_price") if isinstance(v, dict) else None
            if price is not None:
                out[k.split(":")[-1]] = float(price)
    return out
