"""`ca` command line (spec §12 Interfaces: CLI first)."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys


def _ledger_locked() -> bool:
    """DuckDB allows one process; when `ca serve` holds the ledger, CLI commands go through it."""
    import duckdb
    from .ledger import DB_PATH
    try:
        duckdb.connect(str(DB_PATH)).close()
        return False
    except duckdb.IOException as e:
        return "used by another process" in str(e) or "lock" in str(e).lower()


def _server():
    import httpx
    from .settings import SECRETS_DIR, config
    base = f"http://127.0.0.1:{config('user')['service']['port']}"
    tok = (SECRETS_DIR / "pairing_token").read_text(encoding="utf-8").strip()
    return httpx.Client(base_url=base, timeout=60, headers={"x-ca-token": tok, "origin": base})


def _analyse_via_server(a):
    print("(ca serve is running — sending the run to it; watch it live in the UI too)")
    with _server() as c:
        r = c.post("/api/runs", json={"symbol": a.symbol, "horizon": a.horizon, "action": a.action, "mode": a.mode, "loops": a.loops})
        r.raise_for_status()
        run_id = r.json()["run_id"]
        with c.stream("GET", f"/api/runs/{run_id}/events", timeout=None) as s:
            for line in s.iter_lines():
                if not line.startswith("data: "):
                    continue
                e = json.loads(line[6:])
                if e["type"] == "stage_end":
                    mark = {"completed": "✔", "failed": "✖", "skipped": "↷", "degraded": "!"}.get(e["status"], "•")
                    print(f"  {mark} L{e.get('loop', 1)} {e['stage']:<12} {e['message'][:105]}")
                if e["type"] == "run_end":
                    break
        d = c.get(f"/api/runs/{run_id}").json()
    dr = d.get("decision_record")
    if not dr:
        print(f"\nRun did not complete: {d['run']['summary']}")
        sys.exit(1)
    print("\n" + dr["whatsapp"])
    print(f"\nFull report: {dr['report_path']}\nUI: http://127.0.0.1:{_server().base_url.port}/#/run/{run_id}")


def _print_events(run_id: str):
    from .events import REGISTRY
    ev = REGISTRY.get(run_id)
    if not ev:
        return
    for e in ev.history:
        if e["type"] == "stage_end":
            mark = {"completed": "✔", "failed": "✖", "skipped": "↷", "degraded": "!"}.get(e["status"], "•")
            print(f"  {mark} {e['stage']:<12} {e['message'][:110]}")


def cmd_analyse(a):
    if _ledger_locked():
        return _analyse_via_server(a)
    from .orchestrator import run_analysis
    print(f"Analysing {a.symbol or 'DEMO'} · horizon {a.horizon} · mode {a.mode} …")
    r = asyncio.run(run_analysis(a.symbol, a.horizon, a.action, a.mode, loops=a.loops))
    _print_events(r["run_id"])
    if r.get("status") == "failed":
        print(f"\nRun failed: {r['error']}")
        sys.exit(1)
    dr = r["decision_record"]
    print("\n" + dr["whatsapp"])
    print(f"\nFull report: {dr['report_path']}\nOpen the UI (ca serve) → Runs → {r['run_id']}")


def cmd_serve(a):
    import uvicorn
    from .settings import config
    from .service import pairing_token
    port = a.port or config("user")["service"]["port"]
    print(f"Personal CA running at http://127.0.0.1:{port}  (loopback only)")
    print(f"Extension pairing token: {pairing_token()}")
    uvicorn.run("ca_platform.service:app", host="127.0.0.1", port=port, log_level="warning")


def cmd_ingest_bhav(a):
    if _ledger_locked():
        with _server() as c:
            r = c.post("/api/ingest/bhav", json={"days": a.days})
            r.raise_for_status()
        print("ca serve is running — bhavcopy ingestion started inside it (≈1 request/second). "
              "Progress: System page → NSE bhavcopy store.")
        return
    from .data import bhav
    print(f"Ingesting up to {a.days} trading days of NSE bhavcopy (≈1 request/second, cached) …")
    r = bhav.ingest(a.days, progress=lambda m: print("  " + m) if a.verbose else None)
    print(json.dumps(r, indent=1))


def cmd_equity(a):
    from .data import nse
    rows = nse.equity_master(refresh=True)
    print(f"NSE equity master: {len(rows)} symbols cached")


def cmd_secrets(a):
    from .settings import set_secret
    val = getpass.getpass(f"Value for {a.name} (input hidden): ")
    if not val.strip():
        print("Empty value — nothing stored")
        return
    set_secret(a.name, val.strip())
    print(f"Stored {a.name} in the OS keychain (service 'personal-ca-platform').")


def cmd_import(a):
    from .contracts import CaptureBatch
    with open(a.file, encoding="utf-8") as fh:
        batch = CaptureBatch.model_validate(json.load(fh))
    if _ledger_locked():
        with _server() as c:
            print(json.dumps(c.post("/v1/research/captures", json=batch.model_dump()).json(), indent=1))
        return
    from .inbox import ingest_batch
    print(json.dumps(ingest_batch(batch), indent=1))


def cmd_news(a):
    """Newsdesk on its own: check sources, print a dossier, show quality."""
    sym = a.symbol.upper()
    if _ledger_locked():
        with _server() as c:
            d = c.get(f"/api/newsdesk/dossier/{sym}", params={"days": a.days, "refresh": "force" if a.action == "check" else "auto"},
                      timeout=300).json()
    else:
        from .newsdesk import dossier, pipeline
        if a.action == "check" or not pipeline.is_fresh(sym):
            r = pipeline.refresh(sym, lambda m: print("  " + m), a.days)
            if a.action == "check":
                print(json.dumps(r, indent=1))
        d = dossier.build(sym, a.days)
    if "detail" in d:
        print(d["detail"])
        sys.exit(1)
    if a.action == "check" and "refresh_report" in d and d["refresh_report"]:
        print(json.dumps(d["refresh_report"], indent=1))
    q = d["quality"]
    print(f"\n{d['symbol']} · {d['name']} · as of {d['as_of'][:16]} · {d['window_days']} days")
    print(f"Quality: {q['events']} events · {q['filed_or_confirmed']} filed/confirmed · {q['filing_on_event']} filing on event · "
          f"{q['press_only']} press-only · {q['against']} contradicted/denied · {q['headline_only_events']} headline-only · dropped {q['dropped']}")
    if a.action == "quality":
        return
    for i, e in enumerate(d["events"], 1):
        print(f"\n{i}. {e['title'][:110]}  {e['citations']}")
        print(f"   {e['type']} · {e['status_label']} · conf {e['confidence']} · score {e['score']} · "
              f"{e['filings']} filing(s), {e['independent_press_stories']} independent press stor{'y' if e['independent_press_stories'] == 1 else 'ies'}")
    for c in d["coming_up"]:
        print(f"\nComing up {c['date']}: {c['what'][:100]} [{c['citation']}]")
    for x in d["check_first"]:
        print(f"Check first: {x}")
    for x in d["not_known"]:
        print(f"Not yet known: {x}")


def cmd_health(a):
    if _ledger_locked():
        with _server() as c:
            print(json.dumps(c.get("/api/health").json(), indent=1))
        return
    from .service import health
    print(json.dumps(health(), indent=1, default=str))


def main(argv=None):
    p = argparse.ArgumentParser(prog="ca", description="Personal CA Platform — analysis, not investment advice.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("analyse", help="Run the full panel on one stock")
    s.add_argument("symbol", nargs="?", default="")
    s.add_argument("--horizon", choices=["short", "medium", "long"], default="medium")
    s.add_argument("--action", choices=["new_purchase", "additional_purchase", "continue_holding", "reduce_exit"], default="new_purchase")
    s.add_argument("--mode", choices=["live", "demo"], default="live")
    s.add_argument("--loops", type=int, default=1, help="Analysis loops (each re-fetches evidence with open questions)")
    s.set_defaults(fn=cmd_analyse)
    s = sub.add_parser("serve", help="Start the local web UI and extension endpoint")
    s.add_argument("--port", type=int)
    s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("ingest-bhav", help="Backfill NSE bhavcopy end-of-day data")
    s.add_argument("--days", type=int, default=260)
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_ingest_bhav)
    s = sub.add_parser("equity-master", help="Refresh the NSE equity list (company resolution)")
    s.set_defaults(fn=cmd_equity)
    s = sub.add_parser("secrets", help="Store a secret in the OS keychain")
    s.add_argument("action", choices=["set"])
    s.add_argument("name", choices=["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "KITE_API_KEY", "KITE_API_SECRET"])
    s.set_defaults(fn=cmd_secrets)
    s = sub.add_parser("import-captures", help="Import an exported capture JSON (extension fallback)")
    s.add_argument("file")
    s.set_defaults(fn=cmd_import)
    s = sub.add_parser("news", help="Newsdesk on its own: check | dossier | quality for one stock")
    s.add_argument("action", choices=["check", "dossier", "quality"])
    s.add_argument("symbol")
    s.add_argument("--days", type=int, default=90)
    s.set_defaults(fn=cmd_news)
    s = sub.add_parser("health", help="Show configuration and connection status")
    s.set_defaults(fn=cmd_health)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
