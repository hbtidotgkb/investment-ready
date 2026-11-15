"""Zerodha's hosted Kite MCP server as a platform data source — READ-ONLY by construction.

Login is the standard MCP OAuth flow the server advertises (dynamic client registration +
authorization code with PKCE): the user clicks "Connect Kite (MCP)", logs in on Zerodha's page,
and the callback stores the access token locally (OS keychain, falling back to the git-ignored
data/secrets folder). The token is never sent to the browser or to any model.

The server exposes a few tools that switch on a `mode` argument (verified against the live
server, 15 Nov 2026): portfolio, market, session, mutual_funds, orders, gtt, alerts, help.
READ_POLICY whitelists (tool, mode) pairs; everything else — orders, GTT, alerts, session logout —
is refused in code before any request is sent.
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
READ_POLICY: dict[str, frozenset[str]] = {
    "portfolio": frozenset({"profile", "margins", "holdings", "positions"}),
    "market": frozenset({"quote", "ltp", "ohlc", "historical", "search"}),
    "session": frozenset({"status"}),
    "mutual_funds": frozenset({"holdings"}),
}

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

def allowed(tool: str, args: dict) -> bool:
    return args.get("mode") in READ_POLICY.get(tool, frozenset())


def _leaf(e: BaseException) -> BaseException:
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return e


def _parse(result) -> Any:
    if getattr(result, "is_error", False):
        txt = " ".join(getattr(c, "text", "") for c in (result.content or []))
        raise KiteMCPError(f"Kite MCP tool error: {txt[:300]}")
    sc = getattr(result, "structured_content", None)
    if sc is not None:
        return sc
    texts = [getattr(c, "text", "") for c in (result.content or []) if getattr(c, "type", "") == "text"]
    joined = "\n".join(texts).strip()
    try:
        return json.loads(joined)
    except json.JSONDecodeError:
        return joined


def _check_args(tool: str, args: dict) -> None:
    props = set(((_TOOLS_CACHE.get("tools") or {}).get(tool) or {}).get("properties", {}) or {})
    unknown = [k for k in args if props and k not in props]
    if unknown:
        raise KiteMCPError(f"Kite MCP tool '{tool}' no longer accepts {unknown} (schema changed)")


async def _session_call(calls: list[tuple[str, dict]]) -> list[Any]:
    for tool, args in calls:
        if not allowed(tool, args):
            raise PermissionError(f"Kite MCP call {tool}(mode={args.get('mode')!r}) is not in the read-only policy")
    tok = _load_token()
    if not tok:
        raise KiteMCPError("Kite MCP not connected — log in from the Portfolio page")
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
                        _TOOLS_CACHE["tools"] = {t.name: (t.input_schema or {}) for t in tl.tools}
                        _TOOLS_CACHE["t"] = time.time()
                    for tool, args in calls:
                        if tool not in _TOOLS_CACHE["tools"]:
                            raise KiteMCPError(f"Kite MCP server no longer offers '{tool}'")
                        _check_args(tool, args)
                        out.append(_parse(await s.call_tool(tool, args)))
    except BaseExceptionGroup as eg:  # anyio task groups wrap the real error
        leaf = _leaf(eg)
        if isinstance(leaf, (KiteMCPError, PermissionError)):
            raise leaf
        if "401" in str(leaf) or "unauthor" in str(leaf).lower():
            clear_token()
            raise KiteMCPError("Kite MCP login expired — connect again") from leaf
        raise KiteMCPError(f"Kite MCP call failed: {type(leaf).__name__}: {str(leaf)[:200]}") from leaf
    return out


async def call(tool: str, args: dict) -> Any:
    return (await _session_call([(tool, args)]))[0]


# ----------------------------------------------------------------- data helpers ---

def _data(d: Any) -> Any:
    return d.get("data") if isinstance(d, dict) and "data" in d else d


async def status() -> dict:
    if not connected():
        return {"state": "not_connected", "message": "Kite MCP not connected — click Connect (free; uses Zerodha's hosted MCP)."}
    try:
        s = await call("session", {"mode": "status"})
    except Exception as e:
        return {"state": "login_required", "message": f"Kite MCP needs login again ({str(e)[:140]})"}
    if not isinstance(s, dict) or not s.get("authenticated"):
        return {"state": "login_required", "message": "Kite MCP session not authenticated — connect again"}
    if s.get("kite_session_expired"):
        return {"state": "login_required", "message": "Kite session expired (Kite sessions end daily around 6 AM) — connect again"}
    return {"state": "connected", "message": f"Kite MCP connected (read-only) · Kite session valid until {s.get('kite_session_expires_at', '—')}",
            "kite_session_expires_at": s.get("kite_session_expires_at")}


async def holdings() -> list[dict]:
    rows, start = [], 0
    while True:
        d = await call("portfolio", {"mode": "holdings", "type": "full", "limit": 500, "from": start})
        page = _data(d) or []
        rows += page
        pg = d.get("pagination", {}) if isinstance(d, dict) else {}
        if not pg.get("has_more") or not page:
            break
        start += len(page)
    return [{"symbol": h.get("tradingsymbol"), "exchange": h.get("exchange"), "isin": h.get("isin"),
             "quantity": (h.get("quantity") or 0) + (h.get("t1_quantity") or 0),
             "average_price": h.get("average_price"), "last_price": h.get("last_price"), "pnl": h.get("pnl"),
             "day_change_pct": h.get("day_change_percentage"), "via": "kite_mcp",
             "note": "Average price is not a tax-lot history (spec §9 Tax)."} for h in rows]


async def instrument_token(symbol: str) -> int | None:
    d = await call("market", {"mode": "search", "search_mode": "get_by_tradingsymbol", "exchange": "NSE",
                              "tradingsymbol": symbol.upper()})
    for r in _data(d) or []:
        if str(r.get("tradingsymbol", "")).upper() == symbol.upper() and r.get("exchange") == "NSE":
            return int(r["instrument_token"])
    return None


async def candles(symbol: str, days: int = 400) -> list[dict]:
    tok = await instrument_token(symbol)
    if tok is None:
        return []
    to = datetime.now()
    frm = to - timedelta(days=int(days * 1.5))
    d = await call("market", {"mode": "historical", "instrument_token": tok, "interval": "day",
                              "from_date": frm.strftime("%Y-%m-%d"), "to_date": to.strftime("%Y-%m-%d")})
    out = []
    for c in _data(d) or []:
        out.append({"date": str(c.get("date"))[:10], "open": float(c["open"]), "high": float(c["high"]),
                    "low": float(c["low"]), "close": float(c["close"]), "volume": float(c.get("volume") or 0),
                    "source_file": "kite_mcp:market/historical"})
    return out


async def ltp(symbols: list[str]) -> dict[str, float]:
    if not symbols:
        return {}
    d = await call("market", {"mode": "ltp", "instruments": [f"NSE:{s.upper()}" for s in symbols[:500]]})
    out = {}
    for k, v in (_data(d) or {}).items():
        if isinstance(v, dict) and v.get("last_price") is not None:
            out[k.split(":")[-1]] = float(v["last_price"])
    return out
