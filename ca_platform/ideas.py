"""ET Prime lists, stock-idea extraction/ranking and batch analysis.

Two user-editable lists (saved in data/settings/et_lists.yaml, also editable by hand):
  * news_articles  — any ET articles; captured into the research inbox and joined to whichever
                     stock they mention (or are tagged with) as news evidence.
  * idea_articles  — ~10 "stocks to buy"-type pieces; every stock pick is extracted with a verbatim
                     quote, aggregated across articles and ranked. The user picks from the ranking
                     and a batch runs the full panel on each pick.

Spec §5/§6: idea signals guide investigation and never override gates. The ranking says only what
the articles say (attributed opinion); the panel analysis is what decides.
"""
from __future__ import annotations

import asyncio
import json
import re
import statistics
import uuid
from datetime import datetime

import yaml

from . import inbox
from .contracts import IdeasLLM
from .data import nse
from .data.sources import canonical_url, domain_of
from .ledger import get_ledger
from .settings import REPORTS, SETTINGS_DIR, config, iso

LISTS_FILE = SETTINGS_DIR / "et_lists.yaml"
POSITIVE, NEGATIVE = {"buy", "accumulate"}, {"reduce", "sell", "avoid"}


# ------------------------------------------------------------------ the two lists ---

def load_lists() -> dict:
    if not LISTS_FILE.exists():
        return {"news_articles": [], "idea_articles": [], "updated_at": None}
    with open(LISTS_FILE, encoding="utf-8") as fh:
        d = yaml.safe_load(fh) or {}
    return {"news_articles": d.get("news_articles") or [], "idea_articles": d.get("idea_articles") or [],
            "updated_at": d.get("updated_at")}


def _clean(urls: list[str]) -> tuple[list[str], list[dict]]:
    out, bad = [], []
    for u in urls:
        u = u.strip()
        if not u or u.startswith("#"):
            continue
        try:
            cu = canonical_url(u)
        except ValueError as e:
            bad.append({"url": u, "reason": str(e)})
            continue
        if domain_of(cu) != "economictimes.indiatimes.com":
            bad.append({"url": u, "reason": "Not an economictimes.indiatimes.com link"})
            continue
        if cu not in out:
            out.append(cu)
    return out, bad


def save_lists(news: list[str], ideas: list[str], recapture: bool = False) -> dict:
    """Save both lists and queue every article not yet captured (or all, with recapture=True)."""
    n, bad_n = _clean(news)
    i, bad_i = _clean(ideas)
    mx = int((config("user").get("ideas") or {}).get("max_idea_articles", 20))
    if len(i) > mx:
        bad_i += [{"url": u, "reason": f"Idea list limit is {mx} articles"} for u in i[mx:]]
        i = i[:mx]
    LISTS_FILE.write_text(yaml.safe_dump({"news_articles": n, "idea_articles": i, "updated_at": iso()},
                                         sort_keys=False, allow_unicode=True), encoding="utf-8")
    to_queue = [u for u in n + i if recapture or _status(u)["state"] in ("not_captured", "failed", "cancelled")]
    q = inbox.queue_add(to_queue, note="from ET list") if to_queue else {"added": [], "rejected": []}
    return {"saved": {"news_articles": len(n), "idea_articles": len(i)}, "rejected": bad_n + bad_i,
            "queued": len(q["added"]), "queue_rejected": q["rejected"]}


def _status(cu: str) -> dict:
    """Capture state of a listed URL. Matches by canonical URL, or via the queue item that captured
    it (ET may redirect to a different canonical URL than the one you pasted)."""
    led = get_ledger()
    cols = "source_id, version, title, ingested_at, json_array_length(passages) AS n, company_links"
    s = led.fetchone(f"SELECT {cols} FROM research_sources WHERE canonical_url=? ORDER BY version DESC LIMIT 1", [cu])
    if not s:
        via = led.fetchone("SELECT source_id FROM capture_queue WHERE canonical_url=? AND source_id IS NOT NULL "
                           "ORDER BY updated_at DESC LIMIT 1", [cu])
        if via:
            s = led.fetchone(f"SELECT {cols} FROM research_sources WHERE source_id=? ORDER BY version DESC LIMIT 1",
                             [via["source_id"]])
    q = led.fetchone("SELECT status, error, updated_at FROM capture_queue WHERE canonical_url=? ORDER BY created_at DESC LIMIT 1", [cu])
    if s:
        links = json.loads(s["company_links"] or "{}")
        return {"state": "captured", "source_id": s["source_id"], "version": s["version"], "title": s["title"],
                "captured_at": s["ingested_at"], "passages": s["n"],
                "stocks": [c["symbol"] for c in links.get("resolved", [])],
                "requeued": bool(q and q["status"] in ("pending", "in_progress"))}
    if q:
        return {"state": q["status"], "error": q["error"], "updated_at": q["updated_at"]}
    return {"state": "not_captured"}


def lists_status() -> dict:
    lists = load_lists()
    return {"updated_at": lists["updated_at"], "file": str(LISTS_FILE),
            "news_articles": [{"url": u, **_status(u)} for u in lists["news_articles"]],
            "idea_articles": [{"url": u, **_status(u)} for u in lists["idea_articles"]]}


def topic_url(symbol: str) -> str | None:
    """Best-effort ET topic page for a company (the extension scans it for article links)."""
    m = nse.lookup(symbol)
    if not m:
        return None
    name = re.sub(r"\b(limited|ltd\.?)\b", "", m["name"], flags=re.I)
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"https://economictimes.indiatimes.com/topic/{slug}"


# --------------------------------------------------------------- idea extraction ---

def _idea_sources() -> list[dict]:
    out, seen = [], set()
    for u in load_lists()["idea_articles"]:
        st = _status(u)
        if st["state"] == "captured" and st["source_id"] not in seen:
            seen.add(st["source_id"])
            out.append(inbox.get_source(st["source_id"]))
    return out


def _resolve(mention: str, master: list[dict]) -> str | None:
    r = nse.resolve_companies(mention, master)
    if len(r["resolved"]) == 1 and not r["ambiguous"]:
        return r["resolved"][0]["symbol"]
    exact = [m for m in master if m["symbol"] == mention.upper().strip()]
    return exact[0]["symbol"] if exact else None


def _heuristic(src: dict, master: list[dict]) -> list[dict]:
    """No-model fallback: company mentions only (stance 'neutral_mention'), each with a verbatim quote."""
    out = []
    for c in (src.get("company_links") or {}).get("resolved", []):
        key = c["matched"].split(":")[-1] if c.get("matched") else c["name"]
        p = next((p for p in src["passages"] if key.lower() in p["text"].lower() or c["symbol"] in p["text"]), None)
        if not p:
            continue
        words = p["text"].split()
        out.append({"company_mention": c["name"], "symbol": c["symbol"], "stance": "neutral_mention", "target_price": None,
                    "horizon_text": None, "recommended_by": None, "rationale": "Mentioned in the article (no model extraction).",
                    "passage_id": p["id"], "quote": " ".join(words[:20])})
    return out


async def extract(gw=None, force: bool = False) -> dict:
    """Extract ideas from every captured idea article not yet processed (per source version)."""
    from .gateway import missing_keys
    from .prompting import system_prompt
    led = get_ledger()
    master = nse.equity_master()
    use_model = gw is not None and not missing_keys(["ideas"])
    done, skipped, errors = 0, 0, []
    for src in _idea_sources():
        have = led.fetchone("SELECT COUNT(*) AS n FROM idea_mentions WHERE source_id=? AND version=?",
                            [src["source_id"], src["version"]])["n"]
        if have and not force:
            skipped += 1
            continue
        led.execute("DELETE FROM idea_mentions WHERE source_id=? AND version=?", [src["source_id"], src["version"]])
        attested = src["usage_status"] == "personal_research_user_attested"
        rows, method = [], "heuristic"
        if use_model and attested:
            pmap = {p["id"]: " ".join(p["text"].lower().split()) for p in src["passages"]}

            def check(c: IdeasLLM) -> list[str]:
                errs = []
                for k, it in enumerate(c.ideas):
                    if it.passage_id not in pmap:
                        errs.append(f"ideas[{k}]: unknown passage_id {it.passage_id}")
                    elif " ".join(it.quote.lower().split()) not in pmap[it.passage_id]:
                        errs.append(f"ideas[{k}]: quote not found verbatim in {it.passage_id}")
                return errs
            body = "\n".join(f'<passage id="{p["id"]}">{p["text"]}</passage>' for p in src["passages"])
            sys_p, _ = system_prompt("ideas")
            try:
                res = await gw.structured(role="ideas", stage="ideas", system=sys_p, output_model=IdeasLLM, semantic_check=check,
                                          user=f'<article title="{src["title"]}" publisher="{src["publisher"]}">\n{body}\n</article>\nList the stock ideas.')
                rows = [dict(i.model_dump(), symbol=_resolve(i.company_mention, master)) for i in res.parsed.ideas]
                method = f"model:{gw.role_cfg('ideas')['model']}"
            except Exception as e:
                errors.append(f"{src['title'][:60]}: {type(e).__name__}: {str(e)[:120]}")
                rows = _heuristic(src, master)
        else:
            rows = _heuristic(src, master)
        for r in rows:
            led.execute("INSERT INTO idea_mentions VALUES (?,?,?,?,?,?,?)",
                        [f"idm-{uuid.uuid4().hex[:10]}", src["source_id"], src["version"], r.get("symbol"), iso(), method,
                         json.dumps(r | {"source_title": src["title"], "source_url": src["canonical_url"],
                                         "published_at": src["published_at"], "captured_at": src["captured_at"]})])
        done += 1
    return {"processed": done, "skipped_already_done": skipped, "errors": errors,
            "method": "model" if use_model else "heuristic (no model key for the ideas role)"}


async def rank(top: int = 8) -> dict:
    """Aggregate idea mentions per stock across the CURRENT idea articles and rank them."""
    led = get_ledger()
    current = {(s["source_id"], s["version"]) for s in _idea_sources()}
    mentions = [json.loads(r["body"]) | {"_src": r["source_id"], "_v": r["version"], "_method": r["method"]}
                for r in led.fetchall("SELECT * FROM idea_mentions")]
    mentions = [m for m in mentions if (m["_src"], m["_v"]) in current]
    by: dict[str, dict] = {}
    unresolved = []
    for m in mentions:
        if not m.get("symbol"):
            unresolved.append({"mention": m["company_mention"], "source": m["source_title"]})
            continue
        g = by.setdefault(m["symbol"], {"symbol": m["symbol"], "mentions": [], "articles": set(), "pos": set(), "neg": set(),
                                        "recommenders": set(), "targets": []})
        g["mentions"].append(m)
        g["articles"].add(m["_src"])
        if m["stance"] in POSITIVE:
            g["pos"].add(m["_src"])
        if m["stance"] in NEGATIVE:
            g["neg"].add(m["_src"])
        if m.get("recommended_by"):
            g["recommenders"].add(m["recommended_by"].strip().lower())
        if m.get("target_price"):
            g["targets"].append(float(m["target_price"]))
    prices, price_src = await _prices(list(by))
    ranked = []
    for sym, g in by.items():
        px = prices.get(sym)
        upside = round((statistics.median(g["targets"]) / px - 1) * 100, 1) if g["targets"] and px else None
        score = 3 * len(g["pos"]) - 3 * len(g["neg"]) + 1.0 * len(g["articles"] - g["pos"] - g["neg"]) \
            + 0.5 * len(g["recommenders"]) + (max(min(upside, 50), -20) / 10 if upside is not None else 0)
        why = [f"{len(g['articles'])} article(s)", f"{len(g['pos'])} positive", f"{len(g['neg'])} negative"]
        if g["recommenders"]:
            why.append(f"{len(g['recommenders'])} named recommender(s)")
        if upside is not None:
            why.append(f"median target implies {upside:+.1f}% vs {price_src} price {px:,.2f}")
        name = (nse.lookup(sym) or {}).get("name", sym)
        ranked.append({"symbol": sym, "name": name, "score": round(score, 2), "why": " · ".join(why),
                       "positive_articles": len(g["pos"]), "negative_articles": len(g["neg"]), "articles": len(g["articles"]),
                       "median_target": statistics.median(g["targets"]) if g["targets"] else None, "price": px,
                       "upside_pct": upside,
                       "mentions": [{k: m.get(k) for k in ("stance", "target_price", "horizon_text", "recommended_by", "rationale",
                                                           "quote", "passage_id", "source_title", "source_url", "_src", "_method")}
                                    for m in g["mentions"]]})
    ranked.sort(key=lambda r: (-r["score"], -r["positive_articles"], r["symbol"]))
    n = max(5, min(10, int(top)))
    return {"top": ranked[:n], "total_candidates": len(ranked), "unresolved_mentions": unresolved[:30],
            "idea_articles_captured": len(current), "idea_articles_listed": len(load_lists()["idea_articles"]),
            "price_source": price_src,
            "note": "Attributed opinions from your articles, not our valuation. Run the panel to decide (spec: signals never override gates)."}


async def _prices(symbols: list[str]) -> tuple[dict[str, float], str]:
    from .data import kite_mcp
    if symbols and kite_mcp.connected():
        try:
            return await kite_mcp.ltp(symbols[:50]), "Kite MCP last"
        except Exception:
            pass
    out = {}
    for s in symbols:
        r = get_ledger().fetchone("SELECT close FROM bhav_daily WHERE symbol=? AND series='EQ' ORDER BY date DESC LIMIT 1", [s])
        if r:
            out[s] = r["close"]
    return out, "NSE bhavcopy close"


# ------------------------------------------------------------------------ batches ---

TASKS: set = set()


def start_batch(symbols: list[str], horizon: str, loops: int, mode: str, source: str = "ideas", ideas_snapshot=None,
                batch_api: bool | None = None) -> str:
    from .orchestrator import run_analysis
    from .settings import now_ist
    syms = [s.upper().strip() for s in symbols if s.strip()][:10]
    bid = f"batch-{now_ist():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
    items = [{"symbol": s, "run_id": f"run-{now_ist():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}", "status": "queued"} for s in syms]
    icfg = config("user").get("ideas") or {}
    # Batch API: every model call at half price, answers within hours instead of minutes.
    batch_api = bool(icfg.get("batch_api", True) if batch_api is None else batch_api) and mode == "live"
    params = {"horizon": horizon, "loops": loops, "mode": mode, "source": source, "ideas": ideas_snapshot or [],
              "batch_api": batch_api}
    get_ledger().execute("INSERT INTO batches VALUES (?,?,?,?,?,?)", [bid, iso(), "running", json.dumps(params), json.dumps(items), None])
    conc = max(1, int(icfg.get("batch_concurrency", 1)))
    if batch_api:  # queued calls don't compete for rate limits, so stocks wait on the queue side by side
        conc = max(conc, int(icfg.get("batch_api_concurrency", 5)))

    async def runner():
        sem = asyncio.Semaphore(conc)

        async def one(it):
            async with sem:
                _set_item(bid, it["run_id"], "running")
                r = await run_analysis(it["symbol"], horizon, "new_purchase", mode, run_id=it["run_id"], loops=loops,
                                       batch_api=batch_api)
                _set_item(bid, it["run_id"], r.get("status", "failed"))
        await asyncio.gather(*(one(it) for it in items))
        path = write_batch_report(bid)
        get_ledger().execute("UPDATE batches SET status='completed', report_path=? WHERE batch_id=?", [path, bid])

    t = asyncio.get_running_loop().create_task(runner())
    TASKS.add(t)
    t.add_done_callback(TASKS.discard)
    return bid


def _set_item(bid: str, run_id: str, status: str) -> None:
    led = get_ledger()
    row = led.fetchone("SELECT items FROM batches WHERE batch_id=?", [bid])
    items = json.loads(row["items"])
    for it in items:
        if it["run_id"] == run_id:
            it["status"] = status
    led.execute("UPDATE batches SET items=? WHERE batch_id=?", [json.dumps(items), bid])


ORDER = {"buy": 0, "wait": 1, "hold": 2, "insufficient_data": 3, "reduce": 4, "avoid": 5, "exit": 6}


def get_batch(bid: str) -> dict | None:
    from .events import REGISTRY
    led = get_ledger()
    b = led.fetchone("SELECT * FROM batches WHERE batch_id=?", [bid])
    if not b:
        return None
    items = json.loads(b["items"])
    for it in items:
        run = led.fetchone("SELECT status, final_decision, actionable, cost_usd, summary FROM runs WHERE run_id=?", [it["run_id"]])
        v = led.fetchone("SELECT body FROM verdicts WHERE run_id=?", [it["run_id"]])
        it["run"] = run
        if run and run["status"] == "running" and it["run_id"] in REGISTRY:
            evs = REGISTRY[it["run_id"]].history
            ends = [e for e in evs if e["type"] == "stage_end"]
            it["progress"] = {"stages_done": len(ends), "loop": evs[-1].get("loop", 1) if evs else 1,
                              "current": next((e["stage"] for e in reversed(evs) if e["type"] == "stage_start"), None)}
        if v:
            dr = json.loads(v["body"])
            it["verdict"] = {"decision": dr["final_action"]["decision"], "actionable": dr["final_action"]["actionable"],
                             "head_decision": dr["head_proposal"]["decision"], "summary": dr["head_proposal"]["summary"],
                             "blockers": [r["rule"] for r in dr["rule_results"] if r["result"] in ("blocked", "unknown")],
                             "governance": dr["governance"]["score"], "top_sources": dr.get("top_sources", [])[:3],
                             "report_path": dr.get("report_path"), "cost_usd": dr.get("cost_usd")}
    items.sort(key=lambda it: (ORDER.get((it.get("verdict") or {}).get("decision"), 9),
                               not (it.get("verdict") or {}).get("actionable", False)))
    return {"batch_id": bid, "created_at": b["created_at"], "status": b["status"], "params": json.loads(b["params"]),
            "items": items, "report_path": b["report_path"]}


def list_batches(limit: int = 30) -> list[dict]:
    rows = get_ledger().fetchall("SELECT batch_id, created_at, status, params, items FROM batches ORDER BY created_at DESC LIMIT ?", [limit])
    return [{"batch_id": r["batch_id"], "created_at": r["created_at"], "status": r["status"],
             "symbols": [i["symbol"] for i in json.loads(r["items"])], "params": json.loads(r["params"])} for r in rows]


def write_batch_report(bid: str) -> str:
    b = get_batch(bid)
    p = b["params"]
    lines = [f"# Batch report {bid}", "", f"*{b['created_at']} · horizon {p['horizon']} · {p['loops']} loop(s) · mode {p['mode']}*", "",
             "> Analysis, not investment advice. Ideas came from your ET articles (attributed opinions); each stock below "
             "went through the full panel and rules.", "",
             "| Stock | Permitted action | Actionable | Head proposal | Governance | Blockers | Summary |", "|---|---|---|---|---|---|---|"]
    for it in b["items"]:
        v = it.get("verdict") or {}
        lines.append(f"| {it['symbol']} | {v.get('decision', it['status'])} | {'yes' if v.get('actionable') else 'no'} | "
                     f"{v.get('head_decision', '—')} | {v.get('governance') if v.get('governance') is not None else '—'} | "
                     f"{', '.join(v.get('blockers', [])) or '—'} | {(v.get('summary') or '').replace('|', '/')} |")
    lines += ["", "## Idea signals that led here", ""]
    for i in p.get("ideas", []):
        lines.append(f"- **{i['symbol']}** — {i.get('why', '')}")
    lines += ["", "## Full reports", ""]
    for it in b["items"]:
        rp = (it.get("verdict") or {}).get("report_path")
        lines.append(f"- {it['symbol']}: {rp or 'no report (run ' + it['status'] + ')'}")
    path = REPORTS / f"{bid}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)
