"""Checkpointed single-stock pipeline (spec §5 'End-to-end run for one stock', §12).

Plain asyncio, no agent framework: every hand-off between stages is an explicit, typed,
stored record. Progress for every stage and sub-step is emitted as events for the UI.
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
from .contracts import Bundle, Calculation
from .data import demo
from .data import manual as manual_mod
from .events import REGISTRY, RunEvents
from .evidence import EvidenceFactory, from_discovery, pattern_passage, technical_passages
from .features import patterns as patterns_mod
from .features import technicals
from .gateway import Budget, BudgetExceeded, Gateway, LLMUnavailable
from .ledger import dumps, get_ledger
from .mock import MockGateway
from .outputs import pick_concepts, save_report, whatsapp
from .prompting import panel_bundle_view, system_prompt
from .rules import engine as R
from .settings import canonical_hash, config, config_versions, iso, now_ist


class RunFailed(RuntimeError):
    pass


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


async def run_analysis(symbol: str, horizon: str = "medium", action: str = "new_purchase",
                       mode: str = "live", run_id: str | None = None) -> dict:
    led = get_ledger()
    run_id = run_id or f"run-{now_ist():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
    ev = REGISTRY.get(run_id) or RunEvents(run_id, persist=led.add_event)
    REGISTRY[run_id] = ev
    symbol = (demo.DEMO_SYMBOL if mode == "demo" else symbol).upper().strip()
    decision_at = iso()
    led.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [run_id, symbol, mode, horizon, action, decision_at, None, "running", None, None, None, 0.0,
                 json.dumps(config_versions()), None])
    ev.emit("run_start", None, f"Analysing {symbol} ({horizon} horizon, {mode} mode)", "running",
            {"symbol": symbol, "horizon": horizon, "action": action, "mode": mode, "decision_at": decision_at})
    state: dict[str, Any] = {"run_id": run_id, "symbol": symbol, "mode": mode}
    try:
        result = await _pipeline(ev, led, state, symbol, horizon, action, mode, decision_at)
        ev.emit("run_end", None, "Run complete", "completed", {"verdict_id": result.get("verdict_id")})
        return result
    except Exception as e:  # never render a partial run as a completed verdict
        msg = str(e) if isinstance(e, (RunFailed, BudgetExceeded, LLMUnavailable)) else f"{type(e).__name__}: {e}"
        led.execute("UPDATE runs SET status=?, finished_at=?, summary=? WHERE run_id=?", ["failed", iso(), msg, run_id])
        ev.emit("run_end", None, msg, "failed", {"trace": traceback.format_exc()[-2000:]})
        return {"run_id": run_id, "status": "failed", "error": msg}


async def _pipeline(ev: RunEvents, led, state, symbol, horizon, action, mode, decision_at) -> dict:
    fac = EvidenceFactory()
    flags: list[str] = []
    today = date.fromisoformat(decision_at[:10])

    # ------------------------------------------------------------ context ---
    ev.start("context", "Establishing decision timestamp, horizon, mode and portfolio context")
    ev.log("context", f"Decision timestamp {decision_at} (Asia/Kolkata)")
    hz = config("horizons")["horizons"][horizon]
    ev.log("context", f"Horizon '{horizon}': {hz['label']} ({hz['trading_days']} trading days)")
    if mode == "live":
        gw: Any = Gateway(led, state["run_id"], log=ev.log)
        if not gw.available:
            raise RunFailed("ANTHROPIC_API_KEY not configured. Store it with `ca secrets set ANTHROPIC_API_KEY` or run DEMO mode.")
        budget = Budget(led)
        reserve = float(config("user")["budget"]["per_run_reservation_usd"])
        budget.reserve(reserve)
        bs = budget.status()
        ev.log("context", f"Budget: ${bs['spent_usd']:.2f} spent of ${bs['cap_usd']:.2f} this month; reserved ${reserve:.2f} for this run")
    from .data import kite as kite_mod
    ks = kite_mod.status() if mode == "live" else {"state": "demo", "message": "Kite not used in DEMO"}
    ev.log("context", f"Kite: {ks['message']}")
    holdings_position, pf_status = None, "missing"
    if mode == "live" and ks["state"] == "connected":
        try:
            hs = kite_mod.holdings() or []
            pf_status = "available"
            holdings_position = next((h for h in hs if h["symbol"] == symbol), None)
            ev.log("context", f"Portfolio synced: {len(hs)} holdings; {'position found' if holdings_position else 'no position'} in {symbol}")
        except Exception as e:
            ev.log("context", f"Holdings unavailable ({type(e).__name__}) — portfolio context missing")
    if mode == "demo":
        company, sector, isin = demo.DEMO_NAME, demo.DEMO_SECTOR, None
    else:
        from .data import nse
        m = nse.lookup(symbol)
        if not m:
            ev.log("context", "Symbol not found in cached NSE equity master — will try exchange quote")
        company, sector, isin = (m["name"] if m else symbol), None, (m["isin"] if m else None)
    ev.log("context", f"Company: {company}")
    ev.end("context", "completed", f"{company} · {hz['label']} · portfolio context {pf_status}",
           {"company": company, "portfolio_context_status": pf_status, "kite": ks["state"]})

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
                ev.log("prices", f"Kite candles failed ({type(e).__name__}); falling back to bhavcopy")
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
        ev.log("features", f"SMA/RSI/MACD/ATR/52w computed ({technicals.CODE_VERSION}); {len(pats)} chart pattern(s) ({patterns_mod.CODE_VERSION})")
        fac.add(kind="computed_feature", title="Price & technicals (computed)", publisher="ca_platform features", tier=tier_c,
                passages=technical_passages(feats, src), retrieved_via="features",
                notes=f"Computed by {technicals.CODE_VERSION} from the price series above.")
        fac.add(kind="computed_feature", title="Price reaction summary (computed)", publisher="ca_platform features", tier=tier_c,
                passages=[technical_passages(feats, src)[0], technical_passages(feats, src)[7]], retrieved_via="features")
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
               {"close": feats["close"], "patterns": [{k: p[k] for k in ("name", "status")} for p in pats]})
    else:
        ev.end("features", "degraded", "Not enough bars for features")

    # ------------------------------------------------------------ filings ---
    ev.start("filings", "Exchange data, surveillance status, announcements and financial facts")
    facts = []
    surveillance: dict = {"status": "unknown"}
    if mode == "demo":
        doc = demo.manual_doc(today)
        facts, mflags, surveillance = manual_mod.to_evidence(doc, fac, decision_at)
        flags += mflags
        fac.add(kind="company_profile", title="DEMO company profile (synthetic)", publisher="Demo Exchange", tier="DEMO_synthetic",
                passages=[f"{demo.DEMO_NAME} operates in {demo.DEMO_SECTOR}."], retrieved_via="filings")
        n = demo.news_items(today)[1]
        fac.add(kind="exchange_announcement", title=n["title"], publisher=n["publisher"], tier="DEMO_synthetic",
                passages=[n["text"]], published_at=n["published_at"], retrieved_via="filings")
        ev.log("filings", f"DEMO facts: {len(facts)} typed facts, 1 exchange announcement, surveillance {surveillance['status']}")
    else:
        from .data import nse
        s = nse.NSESession()
        try:
            q = await asyncio.to_thread(s.quote, symbol)
            if q:
                info, meta = q.get("info", {}), q.get("metadata", {})
                ind = q.get("industryInfo", {})
                company = info.get("companyName") or company
                sector = ind.get("sector") or meta.get("industry") or sector
                isin = info.get("isin") or isin
                sec = q.get("securityInfo", {}) or {}
                surv = sec.get("surveillance") or {}
                stage_txt = surv.get("surv") if isinstance(surv, dict) else None
                pe = fac.add(kind="company_profile", title=f"NSE quote metadata — {symbol}", publisher="NSE", tier="T1_official",
                             url=f"https://www.nseindia.com/get-quotes/equity?symbol={symbol}",
                             passages=[f"{company} (ISIN {isin}); sector {sector}; industry {ind.get('basicIndustry') or meta.get('industry')}.",
                                       f"NSE surveillance indicator: {stage_txt or 'none shown'}."],
                             retrieved_via="filings")
                surveillance = ({"status": "listed", "stage": stage_txt, "source": "NSE quote", "refs": [pe.id]} if stage_txt and stage_txt not in ("-", "")
                                else {"status": "not_listed", "source": "NSE quote surveillance indicator", "refs": [pe.id]})
                ev.log("filings", f"NSE quote: sector {sector}; surveillance {surveillance['status']}")
            else:
                ev.log("filings", "NSE quote metadata refused by NSE — not retried (no access-control workarounds)")
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
            elif surveillance["status"] == "unknown":
                ev.log("filings", "NSE ASM/GSM lists unavailable — surveillance status unknown")
            anns = await asyncio.to_thread(s.announcements, symbol)
            if anns:
                for a in anns[:25]:
                    when = a.get("an_dt") or a.get("sort_date") or a.get("dt")
                    body = " — ".join(x for x in [a.get("desc"), a.get("attchmntText")] if x)
                    if not body:
                        continue
                    fac.add(kind="exchange_announcement", title=(a.get("desc") or "NSE announcement")[:200], publisher="NSE",
                            tier="T1_official", url=a.get("attchmntFile"), passages=[body[:4000]], published_hint=when,
                            retrieved_via="filings")
                ev.log("filings", f"NSE announcements: {min(len(anns), 25)} in the last 90 days")
            else:
                ev.log("filings", "NSE announcements unavailable")
        finally:
            s.close()
        doc = manual_mod.load(symbol)
        if doc:
            facts, mflags, msurv = manual_mod.to_evidence(doc, fac, decision_at)
            flags += mflags
            if surveillance["status"] == "unknown" and msurv.get("status") != "unknown":
                surveillance = msurv
            ev.log("filings", f"Manual facts file: {len(facts)} typed facts ({sum(f.verified for f in facts)} user-verified from T1)")
        else:
            ev.log("filings", f"No data/manual/{symbol}.yaml — financial facts missing (Forensic will report coverage gaps)")
        if gw.available:
            try:
                sys_p, _ = system_prompt("discovery")
                out = await gw.web_discovery(
                    role="discovery", stage="filings", system=sys_p,
                    allowed_domains=config("sources")["filings_search_domains"],
                    user=(f"Company: {company} (NSE: {symbol}). Decision date {decision_at[:10]}. Find the latest quarterly and annual "
                          f"results (revenue, net profit attributable to owners, exceptional items, operating cash flow), auditor "
                          f"opinion or auditor changes, promoter pledge and shareholding changes, and any regulatory actions. "
                          f"Only information published on or before {decision_at[:10]}."), max_uses=4)
                made = from_discovery(fac, out, "filings", decision_at)
                ev.log("filings", f"Filings discovery: {len(made)} sources with verbatim cited passages")
            except Exception as e:
                ev.log("filings", f"Filings discovery failed ({type(e).__name__}: {str(e)[:120]})")
    ev.end("filings", "completed", f"{len(facts)} facts · surveillance {surveillance['status']}",
           {"facts": len(facts), "surveillance": surveillance["status"]})

    # --------------------------------------------------------------- news ---
    ev.start("news", "Discovering news from tiered, allow-listed publishers")
    if mode == "demo":
        for n in demo.news_items(today):
            if n["publisher"] == "Demo Exchange":
                continue
            fac.add(kind="news_article", title=n["title"], publisher=n["publisher"], tier="DEMO_synthetic", passages=[n["text"]],
                    published_at=n["published_at"], retrieved_via="news")
        ev.log("news", "DEMO: 2 synthetic press items")
        ev.end("news", "completed", "2 items (DEMO)")
    else:
        from .data import news as news_mod
        items, nlog = await asyncio.to_thread(news_mod.search_headlines, company, symbol)
        for line in nlog:
            ev.log("news", line)
        for it in items:
            if it["published_at"] and it["published_at"] > decision_at:
                continue
            fac.add(kind="news_article", title=it["title"], publisher=it["publisher"], tier=it["tier"], url=it["url"],
                    passages=[f"Headline: {it['title']}"], published_at=it["published_at"], retrieved_via="news:rss",
                    notes=f"Headline only via {it['feed']}; body not retrieved.")
        try:
            sys_p, _ = system_prompt("discovery")
            out = await gw.web_discovery(
                role="discovery", stage="news", system=sys_p, allowed_domains=config("sources")["news_search_domains"],
                user=(f"Company: {company} (NSE: {symbol}). Decision date {decision_at[:10]}. Find material news and exchange "
                      f"announcements from the last 90 days: results, orders, guidance, management changes, regulatory or legal "
                      f"matters, block deals, rating changes, and broker views. Only items published on or before {decision_at[:10]}."),
                max_uses=5)
            made = from_discovery(fac, out, "news", decision_at)
            ev.log("news", f"Search discovery: {len(made)} sources with verbatim cited passages")
        except Exception as e:
            ev.log("news", f"Search discovery failed ({type(e).__name__}: {str(e)[:120]})")
        tiers: dict[str, int] = {}
        for e in fac.items:
            if e.retrieved_via.startswith(("news", "discovery:news")):
                tiers[e.tier] = tiers.get(e.tier, 0) + 1
        ev.end("news", "completed", f"{sum(tiers.values())} news sources · " + ", ".join(f"{k}: {v}" for k, v in sorted(tiers.items())),
               {"by_tier": tiers})

    # -------------------------------------------------------------- inbox ---
    ev.start("inbox", "Joining eligible browser captures from the research inbox")
    n_caps = 0
    if mode == "demo":
        c = demo.capture(today)
        fac.add(kind="browser_capture", title=c["title"], publisher=c["publisher"], tier="DEMO_synthetic", passages=c["passages"],
                published_at=c["published_at"], access_status="capture_unverified", retrieved_via="inbox",
                notes="; ".join(c["warnings"]))
        n_caps = 1
    else:
        rows = led.fetchall("SELECT * FROM research_sources WHERE first_seen_at <= ? ORDER BY ingested_at DESC", [decision_at])
        seen = set()
        for r in rows:
            links = json.loads(r["company_links"] or "{}")
            if symbol not in [x["symbol"] for x in links.get("resolved", [])] or r["source_id"] in seen:
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
    }
    if mode == "demo":
        flags.append("DEMO mode: fictional company and synthetic data — never actionable")
    if not facts:
        flags.append("Financial facts missing — valuation and forensic coverage limited")
    b = Bundle(mode=mode, symbol=symbol, company_name=company, sector=sector, isin=isin, decision_at=decision_at,
               horizon=horizon, action=action, portfolio_context_status=pf_status,
               price={"close": feats.get("close"), "date": feats.get("last_date"), "source": src},
               candles=[{k: c[k] for k in ("date", "open", "high", "low", "close", "volume")} for c in candles[-260:]],
               calculations=calcs, patterns=pats, facts=facts, evidence=fac.items, surveillance=surveillance,
               holdings_position=holdings_position, quality_flags=flags, coverage=coverage,
               versions=config_versions() | {"features": technicals.CODE_VERSION, "patterns": patterns_mod.CODE_VERSION})
    if feats:
        b.price.update({"sma20": feats["series"]["sma20"][-260:], "sma50": feats["series"]["sma50"][-260:],
                        "sma200": feats["series"]["sma200"][-260:]})
    b.bundle_id = canonical_hash(b.model_dump(exclude={"bundle_id"}))
    led.execute("INSERT OR REPLACE INTO bundles VALUES (?,?,?,?)", [b.bundle_id, symbol, decision_at, b.model_dump_json()])
    led.execute("UPDATE runs SET bundle_id=? WHERE run_id=?", [b.bundle_id, state["run_id"]])
    idx = CitationIndex(b)
    pre = R.preflight(b)
    nb = sum(r["result"] == R.BLOCKED for r in pre)
    nu = sum(r["result"] == R.UNKNOWN for r in pre)
    ev.log("bundle", f"{len(b.evidence)} evidence items, {sum(len(e.passages) for e in b.evidence)} passages; hash {b.bundle_id[:12]}…")
    ev.log("bundle", f"Preflight rules: {nb} blocked, {nu} unknown, {len(pre) - nb - nu} pass")
    ev.end("bundle", "completed", f"Bundle {b.bundle_id[:12]} · {len(b.evidence)} sources · preflight {nb} blocked / {nu} unknown",
           {"bundle_id": b.bundle_id, "evidence": len(b.evidence), "preflight": pre, "coverage": coverage,
            "quality_flags": flags})
    if mode == "demo":
        gw = MockGateway(led, state["run_id"], b, log=ev.log)

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
                        [state["run_id"], role, None, rec.get("served_model", rec.get("model")), ver, True, au["score"], rep.model_dump_json()])
            ev.end(stg, "completed", rep.headline, {"conclusion": rep.specialist_conclusion, "citation_score": au["score"],
                                                    "report": P.annotate_report(rep.model_dump(), idx), "audit": au,
                                                    "model": rec.get("served_model"), "prompt_version": ver,
                                                    "cost_usd": rec.get("cost_usd", 0), "cache_hit": rec.get("cache_hit")})
            return role, rep, au
        except Exception as e:
            led.execute("INSERT INTO brain_reports VALUES (?,?,?,?,?,?,?,?)",
                        [state["run_id"], role, None, None, None, False, 0.0, json.dumps({"error": str(e)})])
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
        led.execute("UPDATE brain_reports SET label=? WHERE run_id=? AND role=?", [lbl, state["run_id"], r])
    ev.end("anonymise", "completed", f"{len(labels)} report(s) labelled", {"labels": {v: P.REMIT[k].split(' — ')[0] for k, v in labels.items()}})

    head_draft = critique = final = None
    resolution = {"rows": [], "open_blockers": [], "open_major": []}
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
        if failed_roles:
            ev.log("head_draft", f"Missing report(s): {', '.join(failed_roles)} — a buy cannot be actionable")
        try:
            head_draft, hver, hrec = await P.run_head_draft(gw, b, pre, prior, calc_view, rblock, pview, idx, "head_draft")
            dau = audit(head_draft, idx)
            ev.end("head_draft", "completed", f"Draft: {head_draft.decision.upper()} — {head_draft.summary}",
                   {"draft": P.annotate_report(head_draft.model_dump(), idx), "audit": dau, "calculations": calc_view,
                    "cost_usd": hrec.get("cost_usd", 0)})
        except Exception as e:
            ev.end("head_draft", "failed", f"{type(e).__name__}: {str(e)[:300]}")

        if head_draft is not None:
            ev.start("critic", "Critic CA reviews the draft against the checklist")
            try:
                critique, cver, crec = await P.run_critic(gw, b, pre, calc_view, rblock, pview, head_draft, dau, idx, "critic")
                sev = {}
                for o in critique.objections:
                    sev[o.severity] = sev.get(o.severity, 0) + 1
                    ev.log("critic", f"{o.id} [{o.severity}/{o.type}] {o.text[:140]}")
                led.execute("INSERT INTO critiques VALUES (?, ?)", [state["run_id"], critique.model_dump_json()])
                ev.end("critic", "completed", f"{len(critique.objections)} objection(s)" +
                       (" — " + ", ".join(f"{v} {k}" for k, v in sev.items()) if sev else " — draft judged sound"),
                       {"critique": P.annotate_report(critique.model_dump(), idx), "cost_usd": crec.get("cost_usd", 0)})
            except Exception as e:
                ev.end("critic", "failed", f"{type(e).__name__}: {str(e)[:300]} — draft stays unreviewed (research only)")

            ev.start("resolution", "Head responds to each objection; code checks the settling tests")
            if critique is not None:
                try:
                    if critique.objections:
                        final, fver, frec = await P.run_head_final(gw, b, pre, prior, calc_view, rblock, pview, head_draft, critique, idx, "resolution")
                    else:
                        from .contracts import HeadFinalLLM
                        final = HeadFinalLLM(responses=[], final=head_draft)
                        ev.log("resolution", "No objections — draft becomes the final proposal without another model call")
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
           f"({'actionable' if enf['final_action']['actionable'] else 'not actionable'})", enf)

    ev.start("consistency", "Validating the whole final plan")
    cons = R.consistency(enf, head_dict)
    for i in cons["issues"]:
        ev.log("consistency", i)
    ev.end("consistency", "completed" if cons["result"] == "passed" else "failed", f"Consistency {cons['result']}", cons)

    # ------------------------------------------------------------ outputs ---
    ev.start("outputs", "Writing the decision record, report and WhatsApp text")
    fa = enf["final_action"]
    if cons["result"] != "passed":
        fa = dict(fa, actionable=False)
    verdict_id = f"verdict-{uuid.uuid4().hex[:10]}"
    roles_models = {r["role"]: r for r in led.fetchall(
        "SELECT role, label, model, prompt_version, citation_score FROM brain_reports WHERE run_id=?", [state["run_id"]])}
    dr = {
        "schema_version": "decision-record@2.0", "verdict_id": verdict_id, "run_id": state["run_id"], "symbol": symbol,
        "company_name": company, "mode": mode, "horizon": horizon, "action": action, "decision_at": decision_at,
        "issued_at": iso(), "bundle_id": b.bundle_id, "price_at_decision": b.price.get("close"),
        "head_draft": head_draft.model_dump() if head_draft else None, "head_proposal": head_dict,
        "critique": critique.model_dump() if critique else None, "resolution": resolution,
        "rule_results": enf["rule_results"], "adjustments": enf["adjustments"], "governance": enf["governance"],
        "calculations": enf["calculations"], "final_action": fa, "consistency_check": cons,
        "failed_roles": failed_roles, "labels": labels, "brains": roles_models,
        "vendor_separation": config("models").get("vendor_separation"),
        "fallback_events": getattr(gw, "fallback_events", []),
        "evaluation_contract": {"id": f"eval-{verdict_id}", "horizon": horizon, **config("horizons")["evaluation"],
                                "benchmark_assumption": config("rules")["benchmark"], "frozen_at": iso()},
        "versions": b.versions | {"prompts": {r: config("models")["roles"][r]["prompt"] for r in ("quant", "news", "forensic", "head", "critic")}},
        "cost_usd": round(getattr(gw, "total_cost", 0.0), 4),
    }
    dr["learn"] = pick_concepts(dr)
    dr["whatsapp"] = whatsapp(dr)
    dr["report_path"] = save_report(dr, b.model_dump())
    led.execute("INSERT INTO verdicts VALUES (?,?,?,?,?,?,?,?)",
                [verdict_id, state["run_id"], symbol, horizon, fa["decision"], fa["actionable"], dr["issued_at"], dumps(dr)])
    led.execute("UPDATE runs SET status=?, finished_at=?, final_decision=?, actionable=?, cost_usd=?, summary=? WHERE run_id=?",
                ["completed" if not failed_roles and critique is not None else "partial", iso(), fa["decision"], fa["actionable"],
                 dr["cost_usd"], head_dict.get("summary"), state["run_id"]])
    ev.log("outputs", f"Ledger: verdict {verdict_id} with evaluation contract {dr['evaluation_contract']['id']}")
    ev.log("outputs", f"Report saved: {dr['report_path']}")
    ev.end("outputs", "completed", f"{fa['decision'].upper()} · cost ${dr['cost_usd']:.4f}", {"verdict_id": verdict_id})
    return {"run_id": state["run_id"], "status": "completed", "verdict_id": verdict_id, "decision_record": dr}
