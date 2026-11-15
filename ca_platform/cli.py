"""`ca` command line (spec §12 Interfaces: CLI first)."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys


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
    from .orchestrator import run_analysis
    print(f"Analysing {a.symbol or 'DEMO'} · horizon {a.horizon} · mode {a.mode} …")
    r = asyncio.run(run_analysis(a.symbol, a.horizon, a.action, a.mode))
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
    from .inbox import ingest_batch
    with open(a.file, encoding="utf-8") as fh:
        batch = CaptureBatch.model_validate(json.load(fh))
    print(json.dumps(ingest_batch(batch), indent=1))


def cmd_health(a):
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
    s.add_argument("name", choices=["ANTHROPIC_API_KEY", "KITE_API_KEY", "KITE_API_SECRET"])
    s.set_defaults(fn=cmd_secrets)
    s = sub.add_parser("import-captures", help="Import an exported capture JSON (extension fallback)")
    s.add_argument("file")
    s.set_defaults(fn=cmd_import)
    s = sub.add_parser("health", help="Show configuration and connection status")
    s.set_defaults(fn=cmd_health)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
