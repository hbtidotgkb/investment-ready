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
from .gateway import Budget, Gateway
from .ledger import get_ledger
from .orchestrator import run_analysis
from .settings import SECRETS_DIR, WEB, config, config_versions, get_secret, reload_config

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


@app.get("/api/health")
def health():
    led = get_ledger()
    bh = led.fetchone("SELECT COUNT(DISTINCT date) AS days, MAX(date) AS last FROM bhav_daily")
    tax = config("tax_rates")
    return {
        "anthropic": {"configured": bool(get_secret("ANTHROPIC_API_KEY"))},
        "kite": kite_mod.status(),
        "budget": Budget(led).status(),
        "bhavcopy": {"trading_days": bh["days"], "last": str(bh["last"]) if bh["last"] else None},
        "policies_confirmed": bool(config("user").get("policies_confirmed")),
        "capital_configured": bool(config("user")["capital"].get("total_investment_capital_inr")),
        "tax_policy": {"review_status": tax.get("review_status"), "version": tax.get("version")},
        "vendor_separation": config("models").get("vendor_separation"),
        "models": {r: v["model"] for r, v in config("models")["roles"].items()},
    }


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


def _start(req: RunRequest) -> str:
    if req.horizon not in config("horizons")["horizons"]:
        raise HTTPException(400, "unknown horizon")
    if req.mode not in ("live", "demo"):
        raise HTTPException(400, "mode must be live or demo")
    if req.mode == "live" and not req.symbol.strip():
        raise HTTPException(400, "symbol required")
    from .settings import now_ist
    import uuid
    run_id = f"run-{now_ist():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    t = asyncio.create_task(run_analysis(req.symbol, req.horizon, req.action, req.mode, run_id=run_id))
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
    return {"run": run, "decision_record": json.loads(v["body"]) if v else None,
            "bundle": json.loads(b["body"]) if b else None, "calls": calls,
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
def kite_status():
    return kite_mod.status()


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
def holdings():
    h = kite_mod.holdings()
    if h is None:
        return {"status": kite_mod.status(), "holdings": []}
    return {"status": kite_mod.status(), "holdings": h}


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
    if not gw.available:
        raise HTTPException(400, "ANTHROPIC_API_KEY not configured")
    try:
        return await inbox_mod.extract_claims(gw, source_id)
    except PermissionError as e:
        raise HTTPException(403, str(e))


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
def v1_captures(batch: CaptureBatch):
    return inbox_mod.ingest_batch(batch)


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
