"""Checkpointed single-stock pipeline (spec §5 'End-to-end run for one stock', §12).

Plain asyncio, no agent framework: every hand-off between stages is an explicit, typed,
stored record. Progress for every stage and sub-step is emitted as events for the UI.

Multi-loop runs: loop k+1 re-fetches ALL evidence, adds targeted discovery for the open
questions loop k left behind (missing evidence, unsettled objections, unknown rules), builds a
new bundle (parent = previous bundle), re-runs the brains blind, and gives the Head and Critic
the previous loop's outcome as context. Only the final loop issues a verdict; earlier loops are
stored as loop records (a retry never creates another independent recommendation, spec §12).

Failure is role-aware (spec §12 failure table): partial research is preserved, but a
missing required assessment can never produce a normal buy.
"""
from __future__ import annotations

import asyncio
import json
import traceback
import uuid
from datetime import date
from typing import Any

from . import panel as P
from .citations import CitationIndex, audit
from .contracts import Bundle, Calculation, HeadFinalLLM
from .data import demo
from .data import manual as manual_mod
from .events import REGISTRY, RunEvents
from .evidence import EvidenceFactory, from_discovery, pattern_passage, technical_passages
from .features import patterns as patterns_mod
from .features import technicals
from .gateway import Budget, BudgetExceeded, Gateway, LLMUnavailable, missing_keys, vendor_separation
from .ledger import dumps, get_ledger
from .mock import MockGateway
from .outputs import pick_concepts, rank_sources, save_report, whatsapp
from .prompting import panel_bundle_view, system_prompt
from .rules import engine as R
from .settings import canonical_hash, config, config_versions, iso, now_ist

PANEL_ROLES = ["quant", "news", "forensic", "head", "critic"]


class RunFailed(RuntimeError):
    pass


def max_loops() -> int:
    return int((config("user").get("analysis") or {}).get("max_loops", 3))


def prior_decisions(symbol: str, limit: int = 3) -> list[dict]:
    rows = get_ledger().fetchall(
        "SELECT body FROM verdicts WHERE symbol=? ORDER BY issued_at DESC LIMIT ?", [symbol.upper(), limit])
    out = []
    for r in rows:
        b = json.loads(r["body"])
        out.append({"issued_at": b["issued_at"], "horizon": b["horizon"], "final_decision": b["final_action"]["decision"],
                    "actionable": b["final_action"]["actionable"], "head_summary": b["head_proposal"]["summary"],
                    "entry_conditions": b["head_proposal"].get("entry_conditions", []),
                    "invalidation_conditions": b["head_proposal"].get("invalidation_conditions", []),
                    "open_blockers": b.get("resolution", {}).get("open_blockers", []),
                    "price_at_decision": b.get("price_at_decision")})
    return out


def open_questions(rec: dict) -> list[str]:
    """What the previous loop could not settle — becomes targeted discovery for the next loop."""
    qs: list[str] = []
    for row in (rec.get("resolution") or {}).get("rows", []):
        if row["effective_result"] != "passed":
            qs.append(f"{row['objection']['text']} (test: {row['objection']['settling_test']['description']})")
    for role, rep in (rec.get("reports") or {}).items():
        for m in rep.get("missing_evidence", []):
            qs.append(f"{m} ({role})")
    for r in rec.get("rule_results", []):
        if r["result"] == "unknown" and r["rule"] not in ("benchmark",):
            qs.append(f"{r['rule'].replace('_', ' ')}: {r['detail']}")
    seen, out = set(), []
    for q in qs:
        k = q.lower()[:80]
        if k not in seen:
            seen.add(k)
            out.append(q)
    return out[:8]


def previous_loop_block(rec: dict) -> dict:
    return {"loop": rec["loop"], "final_decision": rec["head_proposal"]["decision"], "permitted_action": rec["final_action"]["decision"],
            "head_summary": rec["head_proposal"]["summary"],
            "objections": [{"id": r["objection"]["id"], "severity": r["objection"]["severity"], "text": r["objection"]["text"],
                            "settling_test": r["objection"]["settling_test"]["description"], "effective_result": r["effective_result"]}
                           for r in (rec.get("resolution") or {}).get("rows", [])],
            "rule_blockers": [f"{r['rule']} ({r['result']}): {r['detail']}" for r in rec["rule_results"] if r["result"] in ("blocked", "unknown")],
            "open_questions": rec.get("open_questions", [])}


async def run_analysis(symbol: str, horizon: str = "medium", action: str = "new_purchase",
                       mode: str = "live", run_id: str | None = None, loops: int = 1, batch_api: bool = False) -> dict:
    led = get_ledger()
    run_id = run_id or f"run-{now_ist():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    ev = REGISTRY.get(run_id) or RunEvents(run_id, persist=led.add_event)
    REGISTRY[run_id] = ev
    symbol = (demo.DEMO_SYMBOL if mode == "demo" else symbol).upper().strip()
    loops = max(1, min(int(loops or 1), max_loops()))
    decision_at = iso()
    led.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [run_id, symbol, mode, horizon, action, decision_at, None, "running", None, None, None, 0.0,
                 json.dumps(config_versions() | {"loops": loops, "batch_api": bool(batch_api and mode == "live")}), None])
    ev.emit("run_start", None, f"Analysing {symbol} ({horizon} horizon, {mode} mode, {loops} loop{'s' if loops > 1 else ''})",
            "running", {"symbol": symbol, "horizon": horizon, "action": action, "mode": mode, "decision_at": decision_at,
                        "loops": loops})
    try:
        gw = None
        if mode == "live":
            miss = missing_keys(PANEL_ROLES)
            if miss:
                raise RunFailed(f"Missing API key(s) for the configured panel: {', '.join(miss)}. "
                                f"Store with `ca secrets set <NAME>` or change models in System → Models, or run DEMO mode.")
            budget = Budget(led)
            reserve = float(config("user")["budget"]["per_run_reservation_usd"]) * loops
            budget.reserve(reserve)
            gw = Gateway(led, run_id, log=ev.log, batch_api=batch_api)
            if batch_api:
                ev.log("context", "Batch API mode: model calls are queued at half price; this run can take a few hours")
        carry: dict | None = None
        records: list[dict] = []
        total_cost, unknown_calls = 0.0, 0
        for k in range(1, loops + 1):
            ev.loop = k
            ev.emit("loop_start", None, f"Loop {k} of {loops}" + (" — re-fetching all evidence with the previous loop's open questions"
                                                                    if carry else ""), "running",
                    {"loop": k, "open_questions": (carry or {}).get("open_questions", [])})
            rec, _ = await _loop(ev, led, run_id, symbol, horizon, action, mode, decision_at, k, loops, carry, gw)
            total_cost += rec["cost_usd"]                 # per-loop deltas (the live gateway is shared)
            unknown_calls += rec["calls_with_unknown_price"]
            records.append(rec)
            ev.emit("loop_end", None, f"Loop {k}: {rec['final_action']['decision'].upper()}", "completed",
                    {"loop": k, "summary": loop_summary(rec)})
            carry = rec
        final = records[-1]
        final["loops"] = [loop_summary(r) for r in records]
        final["cost_usd"] = round(total_cost, 4)
        final["calls_with_unknown_price"] = unknown_calls
        _issue(led, run_id, final)
        ev.emit("run_end", None, "Run complete", "completed", {"verdict_id": final["verdict_id"], "loops": loops})
        return {"run_id": run_id, "status": "completed", "verdict_id": final["verdict_id"], "decision_record": final}
    except Exception as e:  # never render a partial run as a completed verdict
        msg = str(e) if isinstance(e, (RunFailed, BudgetExceeded, LLMUnavailable)) else f"{type(e).__name__}: {e}"
        led.execute("UPDATE runs SET status=?, finished_at=?, summary=? WHERE run_id=?", ["failed", iso(), msg, run_id])
        ev.emit("run_end", None, msg, "failed", {"trace": traceback.format_exc()[-2000:]})
        return {"run_id": run_id, "status": "failed", "error": msg}


def loop_summary(rec: dict) -> dict:
    rep_scores = {r: a["score"] for r, a in (rec.get("audits") or {}).items()}
    return {"loop": rec["loop"], "bundle_id": rec["bundle_id"], "parent_bundle_id": rec.get("parent_bundle_id"),
            "evidence_count": rec["evidence_count"], "new_evidence": rec.get("new_evidence", 0),
            "draft_decision": (rec.get("head_draft") or {}).get("decision"), "head_decision": rec["head_proposal"]["decision"],
            "permitted": rec["final_action"]["decision"], "actionable": rec["final_action"]["actionable"],
            "objections": len((rec.get("critique") or {}).get("objections", [])),
            "open_blockers": (rec.get("resolution") or {}).get("open_blockers", []),
            "rule_blockers": [r["rule"] for r in rec["rule_results"] if r["result"] in ("blocked", "unknown")],
            "governance": rec["governance"]["score"], "governance_coverage": rec["governance"]["coverage"],
            "citation_scores": rep_scores, "open_questions": rec.get("open_questions", []),
            "asked": rec.get("asked_questions", []), "summary": rec["head_proposal"]["summary"]}


def _issue(led, run_id: str, dr: dict) -> None:
    fa = dr["final_action"]
    dr["learn"] = pick_concepts(dr)
    dr["whatsapp"] = whatsapp(dr)
    bundle = json.loads(led.fetchone("SELECT body FROM bundles WHERE bundle_id=?", [dr["bundle_id"]])["body"])
    dr["report_path"] = save_report(dr, bundle)
    led.execute("INSERT INTO verdicts VALUES (?,?,?,?,?,?,?,?)",
                [dr["verdict_id"], run_id, dr["symbol"], dr["horizon"], fa["decision"], fa["actionable"], dr["issued_at"], dumps(dr)])
    complete = (not dr["failed_roles"] and dr.get("critique") is not None and dr.get("panel_complete", True)
                and not fa.get("incomplete"))
    led.execute("UPDATE runs SET status=?, finished_at=?, final_decision=?, actionable=?, cost_usd=?, summary=? WHERE run_id=?",
                ["completed" if complete else "partial", iso(), fa["decision"], fa["actionable"], dr["cost_usd"],
                 dr["head_proposal"].get("summary"), run_id])


async def _discover(ev, led, gw_live, fac, stage, purpose, symbol, decision_at, loop, targeted, domains, max_uses,
                    prompt) -> list:
    """Search-grounded discovery; incremental when an earlier day's search for this stock can be extended."""
    from . import discovery_log as DL
    p = DL.plan(led, symbol, purpose, decision_at, loop)
    user = prompt + targeted
    if p["mode"] == "incremental":
        user = prompt + DL.incremental_note(p, decision_at[:10])
        ev.log(stage, f"Searching only for what is new since {p['since']} (full search on {p['base']}; "
               f"{len(DL.known_sources(p))} earlier source(s) carried forward)")
    sys_p, _ = system_prompt("discovery")
    out = await gw_live.web_discovery(role="discovery", stage=stage, system=sys_p, allowed_domains=domains,
                                      user=user, max_uses=max_uses)
    if loop == 1:
        DL.record(led, symbol, purpose, decision_at, p["mode"], out)
    if p["mode"] == "incremental":
        new = len({c["url"] for c in out.get("citations") or []})
        out, first_seen = DL.merged(p, out, decision_at[:10])
        ev.log(stage, f"Incremental search found {new} new source(s)")
        return from_discovery(fac, out, purpose, decision_at, first_seen)
    return from_discovery(fac, out, purpose, decision_at)


async def _loop(ev: RunEvents, led, run_id, symbol, horizon, action, mode, decision_at, loop, loops, carry, gw_live):
    # Spend is counted from the start of the loop so document reads (SR Plus, PDFs) and discovery are included.
    live_cost_start = getattr(gw_live, "total_cost", 0.0)
    live_unknown_start = getattr(gw_live, "unknown_cost_calls", 0)
    fac = EvidenceFactory()
    nd_info: dict = {}
    fin_info: dict = {}
    sr_info: dict = {"status": "missing", "used": False, "reason": "DEMO runs do not read SR Plus reports"}
    user_docs: list[dict] = []
    flags: list[str] = []
    today = date.fromisoformat(decision_at[:10])
    asked = carry.get("open_questions", []) if carry else []
    targeted = ("\nAlso look specifically for evidence on these open questions from the previous analysis loop:\n- "
                + "\n- ".join(asked)) if asked else ""

    # ------------------------------------------------------------ context ---
    ev.start("context", f"Loop {loop}/{loops}: decision timestamp, horizon, mode and portfolio context")
    ev.log("context", f"Decision timestamp {decision_at} (Asia/Kolkata)")
    hz = config("horizons")["horizons"][horizon]
    ev.log("context", f"Horizon '{horizon}': {hz['label']} ({hz['trading_days']} trading days)")
    vs = vendor_separation()
    ev.log("context", "Panel models: " + ", ".join(f"{r}={vs['providers'][r]}:{vs['models'][r]}" for r in vs["providers"])
           + f" · vendor separation {vs['status']}")
    if asked:
        ev.log("context", f"Carrying {len(asked)} open question(s) from loop {loop - 1} into targeted discovery")
    if mode == "live":
        bs = Budget(led).status()
        ev.log("context", f"Budget: ${bs['spent_usd']:.2f} spent of ${bs['cap_usd']:.2f} this month")
    from .data import kite as kite_mod
    from .data import kite_mcp
    ks = kite_mod.status() if mode == "live" else {"state": "demo", "message": "Kite not used in DEMO"}
    ev.log("context", f"Kite Connect: {ks['message']}")
    mcp_on = mode == "live" and kite_mcp.connected()
    if mode == "live":
        ev.log("context", "Kite MCP: " + ("connected (read-only)" if mcp_on else "not connected"))
    holdings_position, pf_status = None, "missing"
    if mode == "live" and (ks["state"] == "connected" or mcp_on):
        try:
            hs = (kite_mod.holdings() if ks["state"] == "connected" else await kite_mcp.holdings()) or []
            pf_status = "available"
            holdings_position = next((h for h in hs if h["symbol"] == symbol), None)
            ev.log("context", f"Portfolio synced via {'Kite Connect' if ks['state'] == 'connected' else 'Kite MCP'}: "
                   f"{len(hs)} holdings; {'position found' if holdings_position else 'no position'} in {symbol}")
        except Exception as e:
            ev.log("context", f"Holdings unavailable ({type(e).__name__}: {str(e)[:100]}) — portfolio context missing")
    if mode == "demo":
        company, sector, isin = demo.DEMO_NAME, demo.DEMO_SECTOR, None
    else:
        from .data import nse
        m = nse.lookup(symbol)
        if not m:
            ev.log("context", "Symbol not found in cached NSE equity master (run `ca equity-master`)")
        company, sector, isin = (m["name"] if m else symbol), None, (m["isin"] if m else None)
    ev.log("context", f"Company: {company}")
    ev.end("context", "completed", f"{company} · {hz['label']} · portfolio context {pf_status}",
           {"company": company, "portfolio_context_status": pf_status, "kite": ks["state"], "vendor_separation": vs})

    # ------------------------------------------------------------- prices ---
    ev.start("prices", "Loading daily candles")
    candles: list[dict] = []
    src = None
    if mode == "demo":
        candles, src = demo.candles(today), "DEMO synthetic candles"
        ev.log("prices", f"Generated {len(candles)} synthetic bars (fictional company)")
    else:
        k = kite_mod.client()
        if k is not None:
            try:
                candles, src = kite_mod.candles(k, symbol), "Zerodha Kite historical_data"
                ev.log("prices", f"Kite: {len(candles)} daily candles")
            except Exception as e:
                ev.log("prices", f"Kite candles failed ({type(e).__name__}); falling back")
        if not candles and mcp_on:
            try:
                candles, src = await kite_mcp.candles(symbol), "Zerodha Kite MCP get_historical_data"
                ev.log("prices", f"Kite MCP: {len(candles)} daily candles")
            except Exception as e:
                ev.log("prices", f"Kite MCP candles failed ({str(e)[:120]}); falling back to bhavcopy")
                candles = []
        if not candles:
            from .data import bhav
            candles, ca_flags = bhav.candles(symbol)
            src = "NSE bhavcopy (sec_bhavdata_full, unadjusted)"
            flags += ca_flags
            ev.log("prices", f"Bhavcopy store: {len(candles)} bars" + (f"; {len(ca_flags)} corporate-action flag(s)" if ca_flags else ""))
            if len(candles) < 200:
                ev.log("prices", "Fewer than 200 bars — run `ca ingest-bhav --days 260` to backfill")
    if candles:
        tier = "DEMO_synthetic" if mode == "demo" else "T1_official"
        fac.add(kind="price_series", title=f"Daily OHLCV {candles[0]['date']} to {candles[-1]['date']}", publisher=src,
                tier=tier, passages=[f"{len(candles)} daily bars from {candles[0]['date']} to {candles[-1]['date']} ({src})."],
                retrieved_via="prices", published_at=candles[-1]["date"] + "T15:30:00+05:30")
        ev.end("prices", "completed", f"{len(candles)} bars from {src}", {"bars": len(candles), "source": src})
    else:
        flags.append("No price data available")
        ev.end("prices", "degraded", "No price data — Quant will report insufficient data")

    # ----------------------------------------------------------- features ---
    ev.start("features", "Computing technicals and chart patterns in code")
    feats: dict = {}
    pats: list[dict] = []
    calcs: list[Calculation] = []
    tier_c = "DEMO_synthetic" if mode == "demo" else "SYS_computed"
    if len(candles) >= 30:
        feats = technicals.compute(candles)
        pats = patterns_mod.detect(candles)
        tp = technical_passages(feats, src)
        ev.log("features", f"SMA/RSI/MACD/ATR/52w computed ({technicals.CODE_VERSION}); {len(pats)} chart pattern(s) ({patterns_mod.CODE_VERSION})")
        fac.add(kind="computed_feature", title="Price & technicals (computed)", publisher="ca_platform features", tier=tier_c,
                passages=tp, retrieved_via="features", notes=f"Computed by {technicals.CODE_VERSION} from the price series above.")
        fac.add(kind="computed_feature", title="Price reaction summary (computed)", publisher="ca_platform features", tier=tier_c,
                passages=[tp[0], tp[7]], retrieved_via="features")
        if pats:
            fac.add(kind="chart_pattern", title="Chart patterns (computed)", publisher="ca_platform patterns", tier=tier_c,
                    passages=[pattern_passage(p) for p in pats], retrieved_via="features")
            for p in pats:
                ev.log("features", f"Pattern: {p['name']} — {p['status']}, neckline {p['neckline']}, target {p['measured_target']}")
        for name, unit in [("close", "INR"), ("sma200", "INR"), ("rsi14", ""), ("atr14", "INR"), ("adv20_crore", "INR crore"),
                           ("high_52w", "INR"), ("low_52w", "INR"), ("ret_3m_pct", "%")]:
            calcs.append(Calculation(id=f"calc-{name}", name=name, formula=f"{technicals.CODE_VERSION}:{name}",
                                     inputs=["price_series"], result=feats.get(name), unit=unit,
                                     status="available" if feats.get(name) is not None else "missing"))
        ev.end("features", "completed", f"Close {feats['close']:,.2f} · RSI {feats['rsi14'] or 0:.1f} · {len(pats)} pattern(s)",
               {"close": feats["close"], "patterns": [{k2: p[k2] for k2 in ("name", "status")} for p in pats]})
    else:
        ev.end("features", "degraded", "Not enough bars for features")

    # ------------------------------------------------------------ filings ---
    ev.start("filings", "Exchange data, surveillance status, announcements and financial facts")
    facts = []
    surveillance: dict = {"status": "unknown"}
    if mode == "demo":
        doc = demo.manual_doc(today, loop=loop)
        facts, mflags, surveillance = manual_mod.to_evidence(doc, fac, decision_at)
        flags += mflags
        fac.add(kind="company_profile", title="DEMO company profile (synthetic)", publisher="Demo Exchange", tier="DEMO_synthetic",
                passages=[f"{demo.DEMO_NAME} operates in {demo.DEMO_SECTOR}."], retrieved_via="filings")
        n = demo.news_items(today)[1]
        fac.add(kind="exchange_announcement", title=n["title"], publisher=n["publisher"], tier="DEMO_synthetic",
                passages=[n["text"]], published_at=n["published_at"], retrieved_via="filings")
        ev.log("filings", f"DEMO facts: {len(facts)} typed facts, 1 exchange announcement, surveillance {surveillance['status']}")
        if loop > 1:
            ev.log("filings", "DEMO targeted discovery: found receivable-days and contingent-liability notes for open forensic questions")
    else:
        from .data import nse
        s = nse.NSESession()
        try:
            sv = await asyncio.to_thread(s.surveillance, symbol)
            if sv is not None:
                c = sv["counts"]
                se = fac.add(kind="exchange_filing", title=f"NSE surveillance lists (ASM/GSM) as of {sv['as_of']}", publisher="NSE",
                             tier="T1_official", url="https://www.nseindia.com/reports/asm",
                             passages=[f"{symbol} is {'ON' if sv['status'] == 'listed' else 'not on'} the NSE surveillance lists "
                                       f"(long-term ASM {c['asm_long']} stocks, short-term ASM {c['asm_short']} stocks, GSM {c['gsm']} stocks)"
                                       + (f": {sv['detail']}." if sv["detail"] else ".")],
                             retrieved_via="filings")
                surveillance = {"status": sv["status"], "stage": sv["detail"], "source": f"NSE ASM/GSM lists ({sv['as_of']})",
                                "refs": [se.id]}
                ev.log("filings", f"Surveillance (NSE ASM/GSM lists): {sv['status']}")
            else:
                ev.log("filings", "NSE ASM/GSM lists unavailable — surveillance status unknown")
        finally:
            s.close()
        # Official financials: results + shareholding XBRL filings (point in time), ratios computed in code.
        from .data import nse_xbrl
        from .features import fundamentals as fund_mod
        x_facts: list = []
        try:
            snap = await asyncio.to_thread(nse_xbrl.collect, symbol, decision_at,
                                           lambda m: ev.log("filings", f"Filed financials · {m}"))
            fund = fund_mod.build(snap, feats.get("close"))
            x_facts, x_calcs, fin_info = fund_mod.to_evidence(fac, snap, fund, company)
            calcs += x_calcs
            for f_ in snap["failed"]:
                ev.log("filings", f"Filed financials: {f_}")
                flags.append(f"Filed financials: {f_}")
            for f_ in fund["flags"]:
                ev.log("filings", f"Filed financials flag: {f_}")
                flags.append(f_)
            got = {m["name"]: m["value"] for m in fund["metrics"] if m["value"] is not None}
            ev.log("filings", "Filed financials: " + (", ".join(
                f"{k} {got[k]:,.2f}" for k in ("pe_ttm", "pb", "roe_pct", "gnpa_pct", "cet1_pct") if k in got) or "no ratios computed")
                   + f" · audit opinion {fund['audit']['status']} · {len(x_facts)} typed facts")
        except Exception as e:
            ev.log("filings", f"Filed financials unavailable ({type(e).__name__}: {str(e)[:120]}) — Forensic/Quant will see the gap")
            flags.append("Filed financials (NSE XBRL) unavailable for this run")
        # The user's PDFs for this stock (data/sr_plus/<SYMBOL>/): SR Plus reports + any other documents.
        from .data import docs as docs_mod
        from .data import sr_plus as sr_mod
        our_pe = next((m["value"] for m in fin_info.get("metrics", []) if m["name"] == "pe_ttm"), None)
        found = await docs_mod.collect(symbol, company, decision_at, gw_live, lambda m: ev.log("filings", m),
                                       our_close=feats.get("close"), our_pe=our_pe)
        sr_info = found["sr_plus"]
        user_docs = docs_mod.add_documents(fac, found, symbol)
        ev.log("filings", f"Your PDFs in {found['folder']}: {len(found['files'])} file(s)"
               + (f" — {sum(d['used'] for d in user_docs)} other document(s) used" if user_docs else ""))
        for d in user_docs:
            m_ = d["meta"]
            ev.log("filings", f"Document {d['file']}: {docs_mod.TYPE_LABEL[m_['document_type']].lower()}"
                   + (f" dated {m_['document_date']}" if m_.get("document_date") else "")
                   + f" — {'used' if d['used'] else 'not used'} ({d['reason']}); {d['pages_read']}/{d['pages_total']} pages read")
        for e_ in found["errors"]:
            ev.log("filings", f"Document not read: {e_}")
            flags.append(f"Your PDF not read: {e_}")
        if sr_info["used"]:
            sr_ev = sr_mod.to_evidence(fac, sr_info, symbol)
            sr_info["evidence_id"] = sr_ev.id
            ok = sum(c["ok"] for c in sr_info["checks"])
            ev.log("filings", f"SR Plus report dated {sr_info['report_date']} ({sr_info['file']}): {sr_info['status']} · "
                   f"{ok}/{len(sr_info['checks'])} report figures recompute · consensus "
                   f"{(sr_info['data'].get('ibes_mean') or 'n/a').replace('_', ' ')}, mean target {sr_info['data'].get('target_mean') or 'n/a'}")
        else:
            ev.log("filings", f"SR Plus: {sr_info['status']} — {sr_info['reason']}")
        for w in sr_info.get("warnings", []):
            ev.log("filings", f"SR Plus warning: {w}")
            if sr_info["used"]:
                flags.append(f"SR Plus: {w}")
        if sr_info["status"] == "stale":
            flags.append(f"SR Plus: {sr_info['reason']}")
        from .newsdesk import pipeline as nd_pipeline
        if nd_pipeline.is_fresh(symbol):
            ev.log("filings", "Newsdesk: data is fresh (kept current by the background worker) — no refetch")
            nd_info["refreshed_in_run"] = False
        else:
            nd_info["refreshed_in_run"] = True
            ev.log("filings", "Newsdesk: refreshing NSE filings (with PDFs), press search, feeds and captures…")
            nd_report = await asyncio.to_thread(nd_pipeline.refresh, symbol, lambda m: ev.log("filings", f"Newsdesk · {m}"))
            nd_info["refresh"] = nd_report
            for f in nd_report["failed"]:
                ev.log("filings", f"Newsdesk source failed: {f}")
        doc = manual_mod.load(symbol)
        if doc:
            facts, mflags, msurv = manual_mod.to_evidence(doc, fac, decision_at)
            flags += mflags
            if surveillance["status"] == "unknown" and msurv.get("status") != "unknown":
                surveillance = msurv
            ev.log("filings", f"Manual facts file: {len(facts)} typed facts ({sum(f.verified for f in facts)} user-verified from T1)")
        else:
            ev.log("filings", f"No data/manual/{symbol}.yaml (optional — filed figures come from NSE XBRL)")
        if x_facts:
            manual_keys = {(f.metric, f.period_end) for f in facts}
            overridden = [f for f in x_facts if (f.metric, f.period_end) in manual_keys]
            if overridden:
                ev.log("filings", f"Manual facts override {len(overridden)} filed value(s) for the same metric and period")
            facts = facts + [f for f in x_facts if (f.metric, f.period_end) not in manual_keys]
        if gw_live.available:
            try:
                made = await _discover(
                    ev, led, gw_live, fac, "filings", "filings", symbol, decision_at, loop, targeted,
                    config("sources")["filings_search_domains"], 4,
                    f"Company: {company} (NSE: {symbol}). Decision date {decision_at[:10]}. Find the latest quarterly and annual "
                    f"results (revenue, net profit attributable to owners, exceptional items, operating cash flow), auditor "
                    f"opinion or auditor changes, promoter pledge and shareholding changes, and any regulatory actions. "
                    f"Only information published on or before {decision_at[:10]}.")
                ev.log("filings", f"Filings discovery: {len(made)} sources with verbatim cited passages")
            except Exception as e:
                ev.log("filings", f"Filings discovery failed ({type(e).__name__}: {str(e)[:120]})")
        else:
            ev.log("filings", "Filings discovery skipped — needs ANTHROPIC_API_KEY (web search)")
    ev.end("filings", "completed", f"{len(facts)} facts · surveillance {surveillance['status']}",
           {"facts": len(facts), "surveillance": surveillance["status"]})

    # --------------------------------------------------------------- news ---
    ev.start("news", "Discovering news from tiered, allow-listed publishers")
    if mode == "demo":
        for n in demo.news_items(today, loop=loop):
            if n["publisher"] == "Demo Exchange":
                continue
            fac.add(kind="news_article", title=n["title"], publisher=n["publisher"], tier="DEMO_synthetic", passages=[n["text"]],
                    published_at=n["published_at"], retrieved_via=n.get("via", "news"))
        ev.log("news", "DEMO: synthetic press items" + (" (+1 found by targeted discovery)" if loop > 1 else ""))
        ev.end("news", "completed", f"{3 if loop > 1 else 2} items (DEMO)")
    else:
        from .newsdesk import dossier as nd_dossier
        dossier = await asyncio.to_thread(nd_dossier.build, symbol, None, iso())
        nd_ev = nd_dossier.to_evidence(fac, dossier, candles)
        # The exact news snapshot this loop used, kept with the decision (shown in the run's News tab).
        nd_info["dossier"] = dossier
        nd_info["evidence_ids"] = nd_ev["evidence_ids"]
        nd_info["summary_evidence_id"] = nd_ev["summary_evidence_id"]
        q = dossier["quality"]
        ev.log("news", f"Newsdesk dossier: {q['events']} events ({q['filed_or_confirmed']} filed/confirmed, "
               f"{q['press_only']} press-only, {q['against']} contradicted/denied), {len(dossier['coming_up'])} coming up, "
               f"{nd_ev['evidence_items']} sources frozen into the bundle")
        for x in dossier["check_first"]:
            ev.log("news", f"Check first: {x}")
        nd_info["quality"] = q
        if gw_live.available:
            try:
                made = await _discover(
                    ev, led, gw_live, fac, "news", "news", symbol, decision_at, loop, targeted,
                    config("sources")["news_search_domains"], 5,
                    f"Company: {company} (NSE: {symbol}). Decision date {decision_at[:10]}. Find material news and exchange "
                    f"announcements from the last 90 days: results, orders, guidance, management changes, regulatory or legal "
                    f"matters, block deals, rating changes, and broker views. Only items published on or before {decision_at[:10]}.")
                ev.log("news", f"Search discovery: {len(made)} sources with verbatim cited passages")
            except Exception as e:
                ev.log("news", f"Search discovery failed ({type(e).__name__}: {str(e)[:120]})")
        tiers: dict[str, int] = {}
        for e in fac.items:
            if e.retrieved_via.startswith(("news", "discovery:news")) and e.kind != "news_summary":
                tiers[e.tier] = tiers.get(e.tier, 0) + 1
        ev.end("news", "completed", f"{sum(tiers.values())} news sources · " + ", ".join(f"{k}: {v}" for k, v in sorted(tiers.items())),
               {"by_tier": tiers})

    # -------------------------------------------------------------- inbox ---
    ev.start("inbox", "Joining eligible browser captures (ET Prime etc.) from the research inbox")
    n_caps = 0
    if mode == "demo":
        c = demo.capture(today)
        fac.add(kind="browser_capture", title=c["title"], publisher=c["publisher"], tier="DEMO_synthetic", passages=c["passages"],
                published_at=c["published_at"], access_status="capture_unverified", retrieved_via="inbox",
                notes="; ".join(c["warnings"]))
        n_caps = 1
    else:
        rows = led.fetchall("SELECT * FROM research_sources WHERE first_seen_at <= ? ORDER BY version DESC", [iso()])
        seen = set()
        already = {e.url for e in fac.items if e.url}
        for r in rows:
            if r["source_id"] in seen:
                continue
            if r["canonical_url"] in already:
                seen.add(r["source_id"])
                n_caps += 1
                ev.log("inbox", f"Capture already in the bundle via Newsdesk: {r['title'][:80]}")
                continue
            links = json.loads(r["company_links"] or "{}")
            if symbol not in [x["symbol"] for x in links.get("resolved", [])]:
                continue
            seen.add(r["source_id"])  # latest version only
            if r["usage_status"] != "personal_research_user_attested":
                ev.log("inbox", f"Skipped capture '{r['title'][:60]}' — usage not attested (not sent to models)")
                continue
            ps = json.loads(r["passages"])
            fac.add(kind="browser_capture", title=r["title"], publisher=r["publisher"], tier="T4_capture_unverified",
                    url=r["canonical_url"], passages=[p["text"] for p in ps], published_at=r["published_at"],
                    first_seen_at=r["first_seen_at"], access_status=r["access_status"], retrieved_via="inbox",
                    notes=f"Capture {r['source_id']} v{r['version']}; warnings: {', '.join(json.loads(r['warnings'] or '[]')) or 'none'}")
            ev.log("inbox", f"Joined capture: {r['title'][:80]} ({len(ps)} passages)")
            n_caps += 1
    ev.end("inbox", "completed", f"{n_caps} capture(s) linked to {symbol}", {"captures": n_caps})

    # ------------------------------------------------------------- bundle ---
    ev.start("bundle", "Freezing the point-in-time evidence bundle")
    coverage = {
        "price_series": "available" if len(candles) >= 200 else ("partial" if candles else "missing"),
        "financial_facts": "available" if facts else "missing",
        "surveillance": surveillance["status"],
        "news": "available" if any(e.kind in ("news_article", "web_search_citation") for e in fac.items) else "missing",
        "exchange_filings": "available" if any(e.tier == "T1_official" and e.kind in ("exchange_filing", "exchange_announcement")
                                               for e in fac.items) else "missing",
        "browser_captures": n_caps,
        "sr_plus": sr_info["status"],
        "user_documents": sum(d["used"] for d in user_docs),
        "insider_trades": ((fin_info.get("insider_trades") or {}).get("status") or "unknown") if fin_info else "unknown",
        "related_party": "available" if (fin_info.get("rpt") or {}).get("rows") else "missing",
        "is_bank": bool(fin_info.get("is_bank")),
        "news_quality": ("press_only" if (nd_info.get("quality") or {}).get("events") and
                         not (nd_info.get("quality") or {}).get("filed_or_confirmed") else "ok"),
    }
    if mode == "demo":
        flags.append("DEMO mode: fictional company and synthetic data — never actionable")
    if not facts:
        flags.append("Financial facts missing — valuation and forensic coverage limited")
    b = Bundle(mode=mode, symbol=symbol, company_name=company, sector=sector, isin=isin, decision_at=decision_at,
               horizon=horizon, action=action, portfolio_context_status=pf_status,
               parent_bundle_id=carry["bundle_id"] if carry else None,
               price={"close": feats.get("close"), "date": feats.get("last_date"), "source": src},
               candles=[{k2: c[k2] for k2 in ("date", "open", "high", "low", "close", "volume")} for c in candles[-260:]],
               calculations=calcs, patterns=pats, facts=facts, evidence=fac.items, surveillance=surveillance,
               holdings_position=holdings_position, quality_flags=flags, coverage=coverage,
               versions=config_versions() | {"features": technicals.CODE_VERSION, "patterns": patterns_mod.CODE_VERSION})
    if feats:
        b.price.update({"sma20": feats["series"]["sma20"][-260:], "sma50": feats["series"]["sma50"][-260:],
                        "sma200": feats["series"]["sma200"][-260:]})
    b.bundle_id = canonical_hash(b.model_dump(exclude={"bundle_id"}))
    led.execute("INSERT OR REPLACE INTO bundles VALUES (?,?,?,?)", [b.bundle_id, symbol, decision_at, b.model_dump_json()])
    led.execute("UPDATE runs SET bundle_id=? WHERE run_id=?", [b.bundle_id, run_id])
    new_ev = 0
    if carry:
        prev = json.loads(led.fetchone("SELECT body FROM bundles WHERE bundle_id=?", [carry["bundle_id"]])["body"])
        prev_hashes = {e["content_hash"] for e in prev["evidence"]}
        new_ev = sum(e.content_hash not in prev_hashes for e in b.evidence)
        ev.log("bundle", f"Versus loop {loop - 1}: {new_ev} new or changed evidence item(s); parent bundle {carry['bundle_id'][:12]}…")
    idx = CitationIndex(b)
    pre = R.preflight(b)
    gate = R.evidence_gate(b)
    new_money = action in ("new_purchase", "additional_purchase")
    for it in gate["items"]:
        ev.log("bundle", f"Evidence checklist ({horizon}): {'OK' if it['ok'] else 'MISSING'} — {it['label']}: {it['detail']}")
    nb = sum(r["result"] == R.BLOCKED for r in pre)
    nu = sum(r["result"] == R.UNKNOWN for r in pre)
    ev.log("bundle", f"{len(b.evidence)} evidence items, {sum(len(e.passages) for e in b.evidence)} passages; hash {b.bundle_id[:12]}…")
    ev.log("bundle", f"Preflight rules: {nb} blocked, {nu} unknown, {len(pre) - nb - nu} pass")
    ev.end("bundle", "completed", f"Bundle {b.bundle_id[:12]} · {len(b.evidence)} sources · preflight {nb} blocked / {nu} unknown",
           {"bundle_id": b.bundle_id, "evidence": len(b.evidence), "new_evidence": new_ev, "preflight": pre,
            "coverage": coverage, "quality_flags": flags})
    gw: Any = MockGateway(led, run_id, b, log=ev.log, loop=loop) if mode == "demo" else gw_live
    cost_before = live_cost_start if gw is gw_live else getattr(gw, "total_cost", 0.0)
    unknown_before = live_unknown_start if gw is gw_live else getattr(gw, "unknown_cost_calls", 0)

    # ------------------------------------------------------------- brains ---
    roles = [("quant", "quant"), ("news", "news_brain"), ("forensic", "forensic")]
    for _, stg in roles:
        ev.start(stg, "Independent analysis on the frozen bundle (no access to other reports)")

    async def one(role, stg):
        try:
            rep, ver, rec = await P.run_brain(gw, role, b, pre, idx, stg)
            au = audit(rep, idx)
            ev.log(stg, f"Citations: {au['counts']['verified']} verified, {au['counts']['id_only']} id-only, "
                   f"{au['total'] - au['counts']['verified'] - au['counts']['id_only']} defective · score {au['score']:.0%}")
            led.execute("INSERT INTO brain_reports VALUES (?,?,?,?,?,?,?,?)",
                        [run_id, role, None, f"{rec.get('provider', 'anthropic')}:{rec.get('served_model', rec.get('model'))}",
                         ver, True, au["score"], rep.model_dump_json()])
            ev.end(stg, "completed", rep.headline, {"conclusion": rep.specialist_conclusion, "citation_score": au["score"],
                                                    "report": P.annotate_report(rep.model_dump(), idx), "audit": au,
                                                    "model": f"{rec.get('provider', 'anthropic')}:{rec.get('served_model')}",
                                                    "prompt_version": ver, "cost_usd": rec.get("cost_usd", 0),
                                                    "price_known": rec.get("price_known", True), "cache_hit": rec.get("cache_hit")})
            return role, rep, au
        except Exception as e:
            led.execute("INSERT INTO brain_reports VALUES (?,?,?,?,?,?,?,?)",
                        [run_id, role, None, None, None, False, 0.0, json.dumps({"error": str(e)})])
            ev.end(stg, "failed", f"{type(e).__name__}: {str(e)[:300]}")
            return role, None, None

    results = await asyncio.gather(*(one(r, s) for r, s in roles))
    reports = {r: rep.model_dump() for r, rep, _ in results if rep is not None}
    audits = {r: au for r, rep, au in results if rep is not None}
    failed_roles = [r for r, rep, _ in results if rep is None]
    citation_ok = all(a["score"] >= P.CITATION_MIN_SCORE for a in audits.values())

    # ---------------------------------------------------------- anonymise ---
    ev.start("anonymise", "Hiding model identity behind random A/B/C labels (remit kept)")
    labels = P.anonymise(list(reports))
    for r, lbl in sorted(labels.items(), key=lambda x: x[1]):
        ev.log("anonymise", f"Analyst {lbl} = {P.REMIT[r].split(' — ')[0]} (mapping stored in ledger only)")
    for r, lbl in labels.items():
        led.execute("UPDATE brain_reports SET label=? WHERE run_id=? AND role=? AND label IS NULL", [lbl, run_id, r])
    ev.end("anonymise", "completed", f"{len(labels)} report(s) labelled", {"labels": {v: P.REMIT[k].split(' — ')[0] for k, v in labels.items()}})

    head_draft = critique = final = None
    dau = None
    refusals = 0
    resolution = {"rows": [], "open_blockers": [], "open_major": []}
    prev_block = previous_loop_block(carry) if carry else None
    if len(reports) < 2:
        for s in ("head_draft", "critic", "resolution"):
            ev.start(s)
            ev.end(s, "skipped", "Fewer than two specialist reports — no panel verdict (spec §12)")
    else:
        prior = prior_decisions(symbol)
        calc_view = P.panel_calculations(b, reports)
        rblock = P.reports_block(reports, labels, idx, audits)
        pview = panel_bundle_view(b, reports)

        ev.start("head_draft", "Head CA weighs the anonymised reports")
        if prior:
            ev.log("head_draft", f"Memory: {len(prior)} prior verdict(s) on {symbol} from the ledger supplied as context")
        if prev_block:
            ev.log("head_draft", f"Loop memory: loop {loop - 1} ended {prev_block['permitted_action'].upper()} with "
                   f"{len(prev_block['objections'])} objection(s) and {len(prev_block['open_questions'])} open question(s)")
        if failed_roles:
            ev.log("head_draft", f"Missing report(s): {', '.join(failed_roles)} — a buy cannot be actionable")
        try:
            head_draft, hver, hrec = await P.run_head_draft(gw, b, pre, prior, calc_view, rblock, pview, idx, "head_draft", prev_block)
            if new_money and gate["complete"] and head_draft.decision == "insufficient_data":
                refusals = 1
                ev.log("head_draft", "Evidence checklist is complete but the Head answered INSUFFICIENT DATA "
                       f"({head_draft.insufficiency_reason or 'no reason'}) — sending it back once to decide")
                try:
                    head_draft, hver, hrec = await P.run_head_draft(
                        gw, b, pre, prior, calc_view, rblock, pview, idx, "head_draft", prev_block,
                        extra=P.decide_instruction(gate, head_draft.insufficiency_reason))
                    ev.log("head_draft", f"After send-back the Head decided: {head_draft.decision.upper()}")
                    if head_draft.decision == "insufficient_data":
                        refusals = 2
                except Exception as e:
                    ev.log("head_draft", f"Send-back call failed ({type(e).__name__}) — keeping the first draft")
            dau = audit(head_draft, idx)
            ev.end("head_draft", "completed", f"Draft: {head_draft.decision.upper()} — {head_draft.summary}",
                   {"draft": P.annotate_report(head_draft.model_dump(), idx), "audit": dau, "calculations": calc_view,
                    "cost_usd": hrec.get("cost_usd", 0), "model": f"{hrec.get('provider', 'anthropic')}:{hrec.get('served_model')}"})
        except Exception as e:
            ev.end("head_draft", "failed", f"{type(e).__name__}: {str(e)[:300]}")

        if head_draft is not None:
            ev.start("critic", "Critic CA reviews the draft against the checklist")
            try:
                critique, cver, crec = await P.run_critic(gw, b, pre, calc_view, rblock, pview, head_draft, dau, idx, "critic", prev_block)
                sev: dict[str, int] = {}
                for o in critique.objections:
                    sev[o.severity] = sev.get(o.severity, 0) + 1
                    ev.log("critic", f"{o.id} [{o.severity}/{o.type}] {o.text[:140]}")
                led.execute("INSERT INTO critiques VALUES (?, ?)", [run_id, critique.model_dump_json()])
                ev.end("critic", "completed", f"{len(critique.objections)} objection(s)" +
                       (" — " + ", ".join(f"{v} {k}" for k, v in sev.items()) if sev else " — draft judged sound"),
                       {"critique": P.annotate_report(critique.model_dump(), idx), "cost_usd": crec.get("cost_usd", 0),
                        "model": f"{crec.get('provider', 'anthropic')}:{crec.get('served_model')}"})
            except Exception as e:
                ev.end("critic", "failed", f"{type(e).__name__}: {str(e)[:300]} — draft stays unreviewed (research only)")

            ev.start("resolution", "Head responds to each objection; code checks the settling tests")
            if critique is not None:
                try:
                    if critique.objections:
                        final, fver, frec = await P.run_head_final(gw, b, pre, prior, calc_view, rblock, pview, head_draft,
                                                                   critique, idx, "resolution", prev_block)
                    else:
                        final = HeadFinalLLM(responses=[], final=head_draft)
                        ev.log("resolution", "No objections — draft becomes the final proposal without another model call")
                    if new_money and gate["complete"] and final.final.decision == "insufficient_data":
                        if refusals == 0 and critique.objections:
                            refusals = 1
                            ev.log("resolution", "Evidence checklist is complete but the final answer is INSUFFICIENT DATA — "
                                   "sending it back once to decide")
                            try:
                                final, fver, frec = await P.run_head_final(
                                    gw, b, pre, prior, calc_view, rblock, pview, head_draft, critique, idx, "resolution", prev_block,
                                    extra=P.decide_instruction(gate, final.final.insufficiency_reason))
                                ev.log("resolution", f"After send-back the Head decided: {final.final.decision.upper()}")
                            except Exception as e:
                                ev.log("resolution", f"Send-back call failed ({type(e).__name__})")
                        if final.final.decision == "insufficient_data":
                            refusals = 2
                            ev.log("resolution", "The Head declined to decide twice although the evidence was complete — "
                                   "the run will be marked INCOMPLETE with its reason")
                    resolution = P.resolve(critique, final, idx)
                    for row in resolution["rows"]:
                        r = row["response"] or {}
                        ev.log("resolution", f"{row['objection']['id']}: {r.get('action', 'no response')} → test {row['effective_result']}"
                               + (f" ({row['code_note']})" if row["code_note"] else ""))
                    ev.end("resolution", "completed",
                           f"Final proposal: {final.final.decision.upper()} · open blockers: {', '.join(resolution['open_blockers']) or 'none'}",
                           {"final": P.annotate_report(final.final.model_dump(), idx), "resolution": resolution})
                except Exception as e:
                    ev.end("resolution", "failed", f"{type(e).__name__}: {str(e)[:300]}")
            else:
                ev.end("resolution", "skipped", "No critique — unreviewed draft is research only")

    # -------------------------------------------------------------- rules ---
    ev.start("rules", "Applying hard vetoes, sizing and tax policy in code")
    proposal = (final.final if final else head_draft)
    head_dict = proposal.model_dump() if proposal else {
        "decision": "insufficient_data", "summary": "No panel proposal could be formed.", "reasons": [],
        "entry_low": None, "entry_high": None, "stop": None, "target_low": None, "target_high": None,
        "invalidation_conditions": [], "entry_conditions": [], "material_dissent": [], "assumptions": [],
        "portfolio_limitations": [], "evidence_sufficiency": "insufficient", "head_score": None}
    forensic_checks = reports.get("forensic", {}).get("checks", [])
    enf = R.enforce(b, pre, head_dict, {
        "forensic_checks": forensic_checks, "brains_ok": not failed_roles, "critic_ok": critique is not None and final is not None,
        "open_blockers": resolution["open_blockers"], "citation_ok": citation_ok, "patterns": pats})
    for r in enf["rule_results"]:
        if r["result"] != R.PASS:
            ev.log("rules", f"{r['rule']}: {r['result'].upper()} — {r['detail']}")
    for a in enf["adjustments"]:
        ev.log("rules", f"Adjusted by rules: {a}")
    g = enf["governance"]
    ev.log("rules", f"Governance (code rubric {g['rubric']}): {g['score'] if g['score'] is not None else 'withheld'} · coverage {g['coverage']:.0%}")
    ev.end("rules", "completed", f"Permitted action: {enf['final_action']['decision'].upper()} "
           f"({'actionable' if enf['final_action']['actionable'] else 'on hold until setup is complete' if enf['final_action'].get('on_hold') else 'not actionable'})", enf)

    ev.start("consistency", "Validating the whole final plan")
    cons = R.consistency(enf, head_dict)
    for i in cons["issues"]:
        ev.log("consistency", i)
    ev.end("consistency", "completed" if cons["result"] == "passed" else "failed", f"Consistency {cons['result']}", cons)

    # ------------------------------------------------------------ outputs ---
    last = loop == loops
    ev.start("outputs", "Writing the decision record, report and WhatsApp text" if last else f"Storing loop {loop} record")
    fa = enf["final_action"]
    if cons["result"] != "passed":
        fa = dict(fa, actionable=False)
    roles_models = {r["role"]: r for r in led.fetchall(
        "SELECT role, label, model, prompt_version, citation_score FROM brain_reports WHERE run_id=? ORDER BY rowid", [run_id])}
    dr = {
        "schema_version": "decision-record@2.1", "verdict_id": f"verdict-{uuid.uuid4().hex[:10]}", "run_id": run_id,
        "loop": loop, "loops_total": loops, "symbol": symbol, "company_name": company, "mode": mode, "horizon": horizon,
        "action": action, "decision_at": decision_at, "issued_at": iso(), "bundle_id": b.bundle_id,
        "parent_bundle_id": b.parent_bundle_id, "evidence_count": len(b.evidence), "new_evidence": new_ev,
        "price_at_decision": b.price.get("close"), "reports": reports, "audits": audits,
        "head_draft": head_draft.model_dump() if head_draft else None, "head_proposal": head_dict,
        "critique": critique.model_dump() if critique else None, "resolution": resolution,
        "rule_results": enf["rule_results"], "adjustments": enf["adjustments"], "governance": enf["governance"],
        "calculations": enf["calculations"], "final_action": fa, "consistency_check": cons,
        "failed_roles": failed_roles, "labels": labels, "brains": roles_models,
        "panel_complete": head_draft is not None and critique is not None and final is not None,
        "vendor_separation": vendor_separation(),
        "fallback_events": getattr(gw, "fallback_events", []),
        "asked_questions": asked,
        "newsdesk": nd_info,
        "fundamentals": fin_info,
        "sr_plus": sr_info,
        "user_documents": user_docs,
        "evidence_gate": enf.get("evidence_gate"),
        "head_refusals": refusals,
        "evaluation_contract": {"id": None, "horizon": horizon, **config("horizons")["evaluation"],
                                "benchmark_assumption": config("rules")["benchmark"], "frozen_at": iso()},
        "versions": b.versions | {"prompts": {r: config("models")["roles"][r]["prompt"] for r in PANEL_ROLES},
                                  "role_models": {r: f"{config('models')['roles'][r].get('provider', 'anthropic')}:"
                                                     f"{config('models')['roles'][r]['model']}" for r in PANEL_ROLES}},
        "cost_usd": round(getattr(gw, "total_cost", 0.0) - cost_before, 4),
        "calls_with_unknown_price": getattr(gw, "unknown_cost_calls", 0) - unknown_before,
    }
    dr["evaluation_contract"]["id"] = f"eval-{dr['verdict_id']}"
    dr["top_sources"] = rank_sources(b, dr, idx)
    dr["open_questions"] = open_questions(dr)
    led.execute("INSERT INTO loop_records VALUES (?,?,?,?)", [run_id, loop, iso(), dumps(dr)])
    ev.log("outputs", f"Loop {loop} record stored: {len(dr['top_sources'])} ranked sources, {len(dr['open_questions'])} open question(s)")
    if last:
        ev.log("outputs", f"Final loop — verdict {dr['verdict_id']} with evaluation contract {dr['evaluation_contract']['id']}")
    else:
        ev.log("outputs", f"Open questions passed to loop {loop + 1}: " + ("; ".join(q[:70] for q in dr["open_questions"][:3]) or "none"))
    ev.end("outputs", "completed", f"{fa['decision'].upper()} · loop cost ${dr['cost_usd']:.4f}", {"verdict_id": dr["verdict_id"] if last else None})
    return dr, gw
