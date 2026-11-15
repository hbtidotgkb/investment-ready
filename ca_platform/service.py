"""Local service (spec §7 target boundary, §16): FastAPI bound to 127.0.0.1.

Security:
  * Host header must be loopback (blocks DNS-rebinding).
  * Every mutating request needs the pairing token (X-CA-Token). The local UI receives it
    embedded in its own page (unreadable cross-origin); the Chrome extension is paired by
    pasting the token once.
  * /v1/* CORS allows only chrome-extension:// origins. Request bodies are size-bounded.
  * No arbitrary URL-fetch/proxy endpoint. Kite is read-only. Secrets never leave the process.
"""
from __future__ import annotations

import asyncio
import json
import secrets

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from . import inbox as inbox_mod
from .contracts import CaptureBatch
from .data import kite as kite_mod
from .events import PHASES, REGISTRY, STAGES
from .gateway import Budget, Gateway, missing_keys, model_catalog, vendor_separation
from .ledger import get_ledger
from .orchestrator import PANEL_ROLES, max_loops, run_analysis
from .settings import (PROVIDER_KEYS, SECRETS_DIR, WEB, config, config_versions, get_secret, read_override,
                       reload_config, write_override)

MAX_BODY = 2_000_000
TOKEN_FILE = SECRETS_DIR / "pairing_token"


def pairing_token(rotate: bool = False) -> str:
    if rotate or not TOKEN_FILE.exists():
        TOKEN_FILE.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    return TOKEN_FILE.read_text(encoding="utf-8").strip()


app = FastAPI(title="Personal CA Platform", version=__version__, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(WEB)), name="static")
TASKS: set[asyncio.Task] = set()


@app.middleware("http")
async def guard(request: Request, call_next):
    host = (request.headers.get("host") or "").split(":")[0]
    if host not in ("127.0.0.1", "localhost"):
        return JSONResponse({"error": "loopback host required"}, status_code=421)
    origin = request.headers.get("origin")
    is_ext = bool(origin and origin.startswith("chrome-extension://"))
    if request.method == "OPTIONS":
        if is_ext and request.url.path.startswith("/v1/"):
            return JSONResponse({}, headers=_cors(origin))
        return JSONResponse({}, status_code=403)
    if request.method in ("POST", "PUT", "DELETE"):
        if int(request.headers.get("content-length") or 0) > MAX_BODY:
            return JSONResponse({"error": "payload too large"}, status_code=413)
        if not secrets.compare_digest(request.headers.get("x-ca-token", ""), pairing_token()):
            return JSONResponse({"error": "missing or invalid pairing token"}, status_code=401,
                                headers=_cors(origin) if is_ext else None)
        if origin and not is_ext and origin not in _self_origins(request.headers.get("host", "")):
            return JSONResponse({"error": "origin not allowed"}, status_code=403)
    if is_ext and not request.url.path.startswith("/v1/"):
        return JSONResponse({"error": "extension may only use /v1 research endpoints"}, status_code=403)
    resp = await call_next(request)
    if is_ext:
        resp.headers.update(_cors(origin))
    return resp


def _cors(origin: str | None) -> dict:
    return {"Access-Control-Allow-Origin": origin or "", "Access-Control-Allow-Headers": "content-type, x-ca-token",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS", "Vary": "Origin"}


def _self_origins(host: str) -> set[str]:
    """The UI's own origin: same loopback host:port the request was sent to (host already validated)."""
    return {f"http://{host}"} if host else set()


# ------------------------------------------------------------------- UI ---

@app.get("/", response_class=HTMLResponse)
def index():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    return html.replace("__CA_TOKEN__", pairing_token()).replace("__CA_VERSION__", __version__)


@app.get("/api/meta")
def meta():
    return {"stages": STAGES, "phases": PHASES, "version": __version__, "versions": config_versions(),
            "tiers": config("sources")["tiers"], "horizons": config("horizons")["horizons"]}


def _kite_mcp_connected() -> bool:
    from .data import kite_mcp
    return kite_mcp.connected()


@app.get("/api/health")
def health():
    led = get_ledger()
    bh = led.fetchone("SELECT COUNT(DISTINCT date) AS days, MAX(date) AS last FROM bhav_daily")
    tax = config("tax_rates")
    u = config("user")
    return {
        "anthropic": {"configured": bool(get_secret("ANTHROPIC_API_KEY"))},
        "openai": {"configured": bool(get_secret("OPENAI_API_KEY"))},
        "panel_missing_keys": missing_keys(PANEL_ROLES),
        "kite": kite_mod.status(),
        "kite_mcp": {"connected": _kite_mcp_connected()},
        "budget": Budget(led).status(),
        "bhavcopy": {"trading_days": bh["days"], "last": str(bh["last"]) if bh["last"] else None},
        "policies_confirmed": bool(u.get("policies_confirmed")),
        "capital_configured": bool(u["capital"].get("total_investment_capital_inr")),
        "tax_policy": {"review_status": tax.get("review_status"), "version": tax.get("version")},
        "vendor_separation": vendor_separation(),
        "models": {r: f"{v.get('provider', 'anthropic')}:{v['model']}" for r, v in config("models")["roles"].items()},
        "analysis": {"default_loops": (u.get("analysis") or {}).get("default_loops", 1), "max_loops": max_loops()},
        "display": {"top_sources": (u.get("display") or {}).get("top_sources", 8)},
        "queue_pending": led.fetchone("SELECT COUNT(*) AS n FROM capture_queue WHERE status IN ('pending','in_progress')")["n"],
    }


# ---------------------------------------------------------------- models ---

ROLE_HELP = {
    "quant": "Quant brain — valuation, setup, entry conditions",
    "news": "News & Narrative brain — what changed, materiality, priced-in",
    "forensic": "Forensic CA brain — reliability of numbers, governance",
    "head": "Head CA — drafts and finalises the decision",
    "critic": "Critic CA — attacks the draft (best on a different provider than Head)",
    "claims": "Inbox claim extraction",
    "discovery": "Evidence discovery (Claude web search, allow-listed domains)",
    "research_qa": "Research Q&A over captures (Claude Citations API)",
}


@app.get("/api/models")
def models_get():
    m = config("models")
    return {"roles": m["roles"], "providers": m["providers"], "locked": m.get("locked_roles", {}), "help": ROLE_HELP,
            "pricing": m["pricing_usd_per_mtok"], "separation": vendor_separation(), "version": m["version"],
            "keys": {p: bool(get_secret(k)) for p, k in PROVIDER_KEYS.items()}}


@app.get("/api/models/catalog")
async def models_catalog(provider: str, refresh: bool = False):
    if provider not in PROVIDER_KEYS:
        raise HTTPException(400, "unknown provider")
    return await asyncio.to_thread(model_catalog, provider, refresh)


class RoleSetting(BaseModel):
    provider: str
    model: str
    effort: str = "high"


class ModelsUpdate(BaseModel):
    roles: dict[str, RoleSetting]
    pricing: dict[str, dict[str, float]] = {}


@app.post("/api/models")
def models_set(req: ModelsUpdate):
    m = config("models")
    locked = m.get("locked_roles", {})
    ov = read_override("models")
    ov.setdefault("roles", {})
    for role, s in req.roles.items():
        if role not in m["roles"]:
            raise HTTPException(400, f"unknown role {role}")
        if s.provider not in PROVIDER_KEYS:
            raise HTTPException(400, f"unsupported provider {s.provider}")
        if role in locked and s.provider != locked[role]:
            raise HTTPException(400, f"{role} is locked to {locked[role]} (provider-specific tool)")
        if s.effort not in ("low", "medium", "high", "xhigh", "max"):
            raise HTTPException(400, "effort must be low|medium|high|xhigh|max")
        if not s.model.strip() or len(s.model) > 100:
            raise HTTPException(400, "model id required")
        ov["roles"][role] = {"provider": s.provider, "model": s.model.strip(), "effort": s.effort}
    for model_id, p in req.pricing.items():
        if set(p) != {"input", "output"} or min(p.values()) < 0:
            raise HTTPException(400, "pricing needs non-negative input and output USD per million tokens")
        ov.setdefault("pricing_usd_per_mtok", {})[model_id] = p
    write_override("models", ov)
    return models_get()


@app.post("/api/config/reload")
def cfg_reload():
    reload_config()
    return {"ok": True, "versions": config_versions()}


@app.get("/api/symbols")
def symbols(q: str = ""):
    from .data import nse
    q = q.strip().upper()
    if len(q) < 1:
        return []
    out = [r for r in nse.equity_master() if r["symbol"].startswith(q)][:8]
    if len(out) < 8:
        out += [r for r in nse.equity_master() if q in r["name"].upper() and r not in out][:8 - len(out)]
    return out


# ----------------------------------------------------------------- runs ---

class RunRequest(BaseModel):
    symbol: str = ""
    horizon: str = "medium"
    action: str = "new_purchase"
    mode: str = "live"
    loops: int = 1


def _start(req: RunRequest) -> str:
    if req.horizon not in config("horizons")["horizons"]:
        raise HTTPException(400, "unknown horizon")
    if req.mode not in ("live", "demo"):
        raise HTTPException(400, "mode must be live or demo")
    if req.mode == "live" and not req.symbol.strip():
        raise HTTPException(400, "symbol required")
    if not 1 <= req.loops <= max_loops():
        raise HTTPException(400, f"loops must be between 1 and {max_loops()}")
    from .settings import now_ist
    import uuid
    run_id = f"run-{now_ist():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    t = asyncio.create_task(run_analysis(req.symbol, req.horizon, req.action, req.mode, run_id=run_id, loops=req.loops))
    TASKS.add(t)
    t.add_done_callback(TASKS.discard)
    return run_id


@app.post("/api/runs")
async def create_run(req: RunRequest):
    return {"run_id": _start(req)}


@app.get("/api/runs")
def list_runs(limit: int = 50):
    return get_ledger().fetchall(
        "SELECT run_id, symbol, mode, horizon, action, started_at, finished_at, status, final_decision, actionable, cost_usd, summary "
        "FROM runs ORDER BY started_at DESC LIMIT ?", [limit])


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    led = get_ledger()
    run = led.fetchone("SELECT * FROM runs WHERE run_id=?", [run_id])
    if not run:
        raise HTTPException(404, "run not found")
    v = led.fetchone("SELECT body FROM verdicts WHERE run_id=?", [run_id])
    b = led.fetchone("SELECT body FROM bundles WHERE bundle_id=?", [run["bundle_id"]]) if run["bundle_id"] else None
    calls = led.fetchall("SELECT request_id, role, model, prompt_version, input_tokens, output_tokens, web_searches, cost_usd, "
                         "latency_ms, status, created_at, provider_request_id FROM request_records WHERE run_id=? ORDER BY created_at", [run_id])
    loops = [json.loads(r["body"]) for r in led.fetchall("SELECT body FROM loop_records WHERE run_id=? ORDER BY loop", [run_id])]
    bundles = {}
    for lr in loops:
        bb = led.fetchone("SELECT body FROM bundles WHERE bundle_id=?", [lr["bundle_id"]])
        if bb:
            bundles[lr["bundle_id"]] = json.loads(bb["body"])
    return {"run": run, "decision_record": json.loads(v["body"]) if v else None,
            "bundle": json.loads(b["body"]) if b else None, "calls": calls, "loops": loops, "bundles": bundles,
            "events": REGISTRY[run_id].history if run_id in REGISTRY else led.events(run_id)}


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, request: Request):
    async def gen():
        if run_id not in REGISTRY:
            for e in get_ledger().events(run_id):
                yield f"data: {json.dumps(e, default=str)}\n\n"
            yield "event: close\ndata: {}\n\n"
            return
        evs = REGISTRY[run_id]
        q = evs.subscribe()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    e = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(e, default=str)}\n\n"
                if e["type"] == "run_end":
                    yield "event: close\ndata: {}\n\n"
                    break
        finally:
            evs.unsubscribe(q)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


# ----------------------------------------------------------------- kite ---

@app.get("/api/kite/status")
async def kite_status():
    from .data import kite_mcp
    return {"connect": kite_mod.status(), "mcp": await kite_mcp.status()}


@app.get("/kite-mcp/login")
def kite_mcp_login(request: Request):
    from .data import kite_mcp
    host = request.headers.get("host", "127.0.0.1:8765")
    try:
        return RedirectResponse(kite_mcp.start_login(f"http://{host}/kite-mcp/callback"))
    except kite_mcp.KiteMCPError as e:
        raise HTTPException(502, str(e))


@app.get("/kite-mcp/callback")
def kite_mcp_callback(code: str = "", state: str = "", error: str = ""):
    from .data import kite_mcp
    if error or not code:
        return RedirectResponse("/#/portfolio?kite_mcp=failed")
    try:
        kite_mcp.complete_login(code, state)
    except kite_mcp.KiteMCPError:
        return RedirectResponse("/#/portfolio?kite_mcp=failed")
    return RedirectResponse("/#/portfolio?kite_mcp=connected")


@app.post("/api/kite-mcp/disconnect")
def kite_mcp_disconnect():
    from .data import kite_mcp
    kite_mcp.clear_token()
    return {"ok": True}


@app.get("/kite/login")
def kite_login():
    if not kite_mod.configured():
        raise HTTPException(400, "Kite API key/secret not configured")
    return RedirectResponse(kite_mod.login_url())


@app.get("/kite/callback")
def kite_callback(request_token: str = "", status: str = ""):
    if status != "success" or not request_token:
        return RedirectResponse("/#/portfolio?kite=failed")
    kite_mod.complete_login(request_token)
    return RedirectResponse("/#/portfolio?kite=connected")


@app.get("/api/portfolio/holdings")
async def holdings():
    from .data import kite_mcp
    st = {"connect": kite_mod.status(), "mcp": await kite_mcp.status()}
    h = kite_mod.holdings()
    if h is not None:
        return {"status": st, "holdings": h, "via": "Kite Connect"}
    if st["mcp"]["state"] == "connected":
        try:
            return {"status": st, "holdings": await kite_mcp.holdings(), "via": "Kite MCP"}
        except Exception as e:
            st["mcp"] = {"state": "error", "message": str(e)[:200]}
    return {"status": st, "holdings": [], "via": None}


# ------------------------------------------------------------ ET lists & ideas ---

class ETLists(BaseModel):
    news_articles: list[str] = []
    idea_articles: list[str] = []
    recapture: bool = False


@app.get("/api/et/lists")
def et_lists():
    from . import ideas
    return ideas.load_lists() | {"status": ideas.lists_status()}


@app.post("/api/et/lists")
def et_lists_save(req: ETLists):
    from . import ideas
    if len(req.news_articles) > 500:
        raise HTTPException(400, "at most 500 news articles")
    return ideas.save_lists(req.news_articles, req.idea_articles, req.recapture)


class ScanRequest(BaseModel):
    symbol: str


@app.post("/api/et/scan")
def et_scan(req: ScanRequest):
    from . import ideas
    url = ideas.topic_url(req.symbol)
    if not url:
        raise HTTPException(400, f"{req.symbol} not found in the NSE equity list (run `ca equity-master`)")
    return inbox_mod.queue_add([url], symbol=req.symbol, note="topic scan", kind="scan") | {"topic_url": url}


@app.get("/api/ideas")
async def ideas_get(top: int = 8):
    from . import ideas
    return await ideas.rank(top)


@app.post("/api/ideas/extract")
async def ideas_extract(force: bool = False, top: int = 8):
    from . import ideas
    gw = Gateway(get_ledger(), run_id="ideas") if not missing_keys(["ideas"]) else None
    res = await ideas.extract(gw, force=force)
    return res | await ideas.rank(top)


class BatchRequest(BaseModel):
    symbols: list[str]
    horizon: str = "medium"
    loops: int = 1
    mode: str = "live"
    ideas: list[dict] = []


@app.post("/api/batches")
async def batch_start(req: BatchRequest):
    from . import ideas
    syms = [s for s in req.symbols if s.strip()]
    if not 1 <= len(syms) <= 10:
        raise HTTPException(400, "choose 1 to 10 stocks")
    if req.horizon not in config("horizons")["horizons"] or req.mode not in ("live", "demo") or not 1 <= req.loops <= max_loops():
        raise HTTPException(400, "invalid horizon, mode or loops")
    if req.mode == "live":
        miss = missing_keys(PANEL_ROLES)
        if miss:
            raise HTTPException(400, f"Missing API key(s) for the panel: {', '.join(miss)}")
        need = float(config("user")["budget"]["per_run_reservation_usd"]) * req.loops * len(syms)
        rem = Budget(get_ledger()).status()["remaining_usd"]
        if rem < need:
            raise HTTPException(400, f"Budget: this batch reserves about ${need:.2f} but ${rem:.2f} remains this month")
    snap = [{"symbol": i.get("symbol"), "why": i.get("why"), "score": i.get("score")} for i in req.ideas][:10]
    return {"batch_id": ideas.start_batch(syms, req.horizon, req.loops, req.mode, "ideas", snap)}


@app.get("/api/batches")
def batches_list():
    from . import ideas
    return ideas.list_batches()


@app.get("/api/batches/{batch_id}")
def batch_get(batch_id: str):
    from . import ideas
    b = ideas.get_batch(batch_id)
    if not b:
        raise HTTPException(404, "batch not found")
    return b


# ---------------------------------------------------------------- inbox ---

@app.get("/api/inbox")
def inbox_list():
    return inbox_mod.list_sources()


@app.get("/api/inbox/{source_id}")
def inbox_get(source_id: str, version: int | None = None):
    s = inbox_mod.get_source(source_id, version)
    if not s:
        raise HTTPException(404, "source not found")
    return s


@app.post("/api/inbox/{source_id}/claims")
async def inbox_claims(source_id: str):
    gw = Gateway(get_ledger(), run_id=f"inbox-{source_id}")
    if missing_keys(["claims"]):
        raise HTTPException(400, f"API key missing for the claims role: {', '.join(missing_keys(['claims']))}")
    try:
        return await inbox_mod.extract_claims(gw, source_id)
    except PermissionError as e:
        raise HTTPException(403, str(e))


class LinkRequest(BaseModel):
    symbol: str


@app.post("/api/inbox/{source_id}/link")
def inbox_link(source_id: str, req: LinkRequest):
    from .data import nse
    if not nse.lookup(req.symbol) and nse.equity_master():
        raise HTTPException(400, f"{req.symbol} is not in the NSE equity list")
    try:
        return inbox_mod.link_symbol(source_id, req.symbol, method="user")
    except KeyError:
        raise HTTPException(404, "source not found")


class QueueRequest(BaseModel):
    urls: list[str]
    symbol: str | None = None
    note: str | None = None
    kind: str = "article"


@app.get("/api/queue")
def queue_list():
    return inbox_mod.queue_list()


@app.post("/api/queue")
def queue_add(req: QueueRequest):
    if len(req.urls) > 50:
        raise HTTPException(400, "at most 50 URLs per request")
    try:
        return inbox_mod.queue_add(req.urls, req.symbol, req.note, kind=req.kind)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/queue/{queue_id}/cancel")
def queue_cancel(queue_id: str):
    inbox_mod.queue_update(queue_id, "cancelled")
    return {"ok": True}


@app.get("/api/pairing")
def pairing():
    return {"token": pairing_token(), "port": config("user")["service"]["port"]}


@app.post("/api/pairing/rotate")
def pairing_rotate():
    return {"token": pairing_token(rotate=True)}


# ------------------------------------------- /v1 extension-facing API ---

@app.get("/v1/health")
def v1_health():
    return {"ok": True, "service": "personal-ca", "version": __version__}


@app.post("/v1/research/captures")
async def v1_captures(batch: CaptureBatch):
    receipt = await asyncio.to_thread(inbox_mod.ingest_batch, batch)
    auto = (config("user").get("inbox") or {}).get("auto_extract_claims", True)
    if auto and not receipt.get("replayed") and not missing_keys(["claims"]):
        for it in receipt["items"]:
            if it["status"] == "accepted" and it.get("usage_status") == "personal_research_user_attested":
                t = asyncio.create_task(_auto_claims(it["source_id"]))
                TASKS.add(t)
                t.add_done_callback(TASKS.discard)
        receipt["claims_extraction"] = "started in background"
    return receipt


async def _auto_claims(source_id: str):
    try:
        await inbox_mod.extract_claims(Gateway(get_ledger(), run_id=f"inbox-{source_id}"), source_id)
    except Exception:
        pass  # failures are visible as "0 claims" with a manual retry button in the UI


def _require_token(request: Request):
    if not secrets.compare_digest(request.headers.get("x-ca-token", ""), pairing_token()):
        raise HTTPException(401, "missing or invalid pairing token")


@app.get("/v1/research/queue")
def v1_queue(request: Request):
    _require_token(request)
    return [{k: q.get(k) for k in ("queue_id", "canonical_url", "symbol", "note", "status", "kind")}
            for q in inbox_mod.queue_list("pending")]


@app.get("/v1/research/settings")
def v1_settings(request: Request):
    _require_token(request)
    return {"scan_max_links": int((config("user").get("inbox") or {}).get("scan_max_links", 15))}


@app.post("/v1/research/queue")
def v1_queue_add(req: QueueRequest):
    try:
        return inbox_mod.queue_add(req.urls[:50], req.symbol, req.note, kind=req.kind)
    except ValueError as e:
        raise HTTPException(400, str(e))


class QueueStatus(BaseModel):
    status: str
    error: str | None = None


@app.post("/v1/research/queue/{queue_id}/status")
def v1_queue_status(queue_id: str, req: QueueStatus):
    try:
        inbox_mod.queue_update(queue_id, req.status, req.error)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.get("/v1/research/receipts/{receipt_id}")
def v1_receipt(receipt_id: str):
    r = get_ledger().fetchone("SELECT body FROM ingestion_receipts WHERE receipt_id=?", [receipt_id])
    if not r:
        raise HTTPException(404, "receipt not found")
    return json.loads(r["body"])


class Question(BaseModel):
    source_ids: list[str]
    question: str


@app.post("/v1/research/questions")
async def v1_question(q: Question):
    if not q.source_ids or len(q.source_ids) > 10 or len(q.question) > 2000:
        raise HTTPException(400, "1-10 sources and a question up to 2000 chars")
    docs = []
    for sid in q.source_ids:
        s = inbox_mod.get_source(sid)
        if not s:
            raise HTTPException(404, f"{sid} not found")
        if s["usage_status"] != "personal_research_user_attested":
            raise HTTPException(403, f"{sid}: usage not attested — not sent to a model")
        docs.append({"title": s["title"] or s["canonical_url"], "source_id": sid,
                     "passages": [{"id": f"{sid}:v{s['version']}:{p['id']}", "text": p["text"]} for p in s["passages"]]})
    if sum(len(p["text"]) for d in docs for p in d["passages"]) > 120_000:
        raise HTTPException(413, "Selected text exceeds 120,000 characters — select fewer sources (not silently clipped)")
    gw = Gateway(get_ledger(), run_id="research-qa")
    if not gw.available:
        raise HTTPException(400, "ANTHROPIC_API_KEY not configured")
    from .prompting import system_prompt
    sys_p, _ = system_prompt("research_qa")
    ans = await gw.cited_answer(role="research_qa", system=sys_p, documents=docs, question=q.question)
    return ans | {"label": "research_only", "note": "Not a panel verdict: no Quant/Forensic/Critic review was performed."}


@app.post("/v1/analysis/jobs")
async def v1_job(req: RunRequest):
    return {"job_id": _start(req)}


@app.get("/v1/analysis/jobs/{job_id}")
def v1_job_status(job_id: str):
    run = get_ledger().fetchone("SELECT run_id, symbol, status, final_decision, actionable, summary FROM runs WHERE run_id=?", [job_id])
    if not run:
        raise HTTPException(404, "job not found")
    stages = {}
    for e in (REGISTRY[job_id].history if job_id in REGISTRY else get_ledger().events(job_id)):
        if e["type"] in ("stage_start", "stage_end") and e["stage"]:
            stages[e["stage"]] = e["status"]
    return run | {"stages": stages}
