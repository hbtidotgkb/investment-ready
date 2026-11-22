"""DuckDB ledger (spec §14). Append-only records of runs, events, bundles, brain reports,
critiques, verdicts, every model request, research intake and receipts."""
from __future__ import annotations

import json
import threading
from typing import Any

import duckdb

from .settings import DATA

DB_PATH = DATA / "market.duckdb"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id VARCHAR PRIMARY KEY, symbol VARCHAR, mode VARCHAR, horizon VARCHAR, action VARCHAR,
  started_at VARCHAR, finished_at VARCHAR, status VARCHAR, final_decision VARCHAR,
  actionable BOOLEAN, bundle_id VARCHAR, cost_usd DOUBLE, versions JSON, summary VARCHAR);
CREATE TABLE IF NOT EXISTS run_events (run_id VARCHAR, seq INTEGER, ts VARCHAR, event JSON);
CREATE TABLE IF NOT EXISTS bundles (bundle_id VARCHAR PRIMARY KEY, symbol VARCHAR, decision_at VARCHAR, body JSON);
CREATE TABLE IF NOT EXISTS brain_reports (run_id VARCHAR, role VARCHAR, label VARCHAR, model VARCHAR,
  prompt_version VARCHAR, valid BOOLEAN, citation_score DOUBLE, body JSON);
CREATE TABLE IF NOT EXISTS critiques (run_id VARCHAR, body JSON);
CREATE TABLE IF NOT EXISTS verdicts (verdict_id VARCHAR PRIMARY KEY, run_id VARCHAR, symbol VARCHAR,
  horizon VARCHAR, decision VARCHAR, actionable BOOLEAN, issued_at VARCHAR, body JSON);
CREATE TABLE IF NOT EXISTS request_records (request_id VARCHAR, run_id VARCHAR, role VARCHAR, model VARCHAR,
  prompt_version VARCHAR, request_hash VARCHAR, provider_request_id VARCHAR, input_tokens INTEGER,
  output_tokens INTEGER, web_searches INTEGER, cost_usd DOUBLE, latency_ms INTEGER, status VARCHAR,
  created_at VARCHAR, request JSON, response JSON);
CREATE TABLE IF NOT EXISTS bhav_daily (symbol VARCHAR, series VARCHAR, date DATE, prev_close DOUBLE,
  open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE, turnover_lacs DOUBLE,
  deliv_pct DOUBLE, source_file VARCHAR, PRIMARY KEY (symbol, series, date));
CREATE TABLE IF NOT EXISTS research_sources (source_id VARCHAR, version INTEGER, canonical_url VARCHAR,
  title VARCHAR, publisher VARCHAR, author VARCHAR, published_at VARCHAR, captured_at VARCHAR,
  first_seen_at VARCHAR, ingested_at VARCHAR, fingerprint VARCHAR, access_status VARCHAR,
  usage_status VARCHAR, truncated BOOLEAN, warnings JSON, passages JSON, company_links JSON,
  research_session_id VARCHAR, PRIMARY KEY (source_id, version));
CREATE TABLE IF NOT EXISTS ingestion_receipts (receipt_id VARCHAR PRIMARY KEY, created_at VARCHAR, body JSON);
CREATE TABLE IF NOT EXISTS research_claims (claim_id VARCHAR PRIMARY KEY, source_id VARCHAR, version INTEGER,
  symbol VARCHAR, created_at VARCHAR, status VARCHAR, body JSON);
CREATE TABLE IF NOT EXISTS loop_records (run_id VARCHAR, loop INTEGER, created_at VARCHAR, body JSON,
  PRIMARY KEY (run_id, loop));
CREATE TABLE IF NOT EXISTS capture_queue (queue_id VARCHAR PRIMARY KEY, url VARCHAR, canonical_url VARCHAR,
  symbol VARCHAR, note VARCHAR, status VARCHAR, created_at VARCHAR, updated_at VARCHAR, source_id VARCHAR,
  version INTEGER, error VARCHAR);
ALTER TABLE capture_queue ADD COLUMN IF NOT EXISTS kind VARCHAR DEFAULT 'article';
CREATE TABLE IF NOT EXISTS idea_mentions (mention_id VARCHAR PRIMARY KEY, source_id VARCHAR, version INTEGER,
  symbol VARCHAR, created_at VARCHAR, method VARCHAR, body JSON);
CREATE TABLE IF NOT EXISTS batches (batch_id VARCHAR PRIMARY KEY, created_at VARCHAR, status VARCHAR,
  params JSON, items JSON, report_path VARCHAR);
CREATE SEQUENCE IF NOT EXISTS nd_seq START 1;
CREATE TABLE IF NOT EXISTS nd_articles (id BIGINT PRIMARY KEY, url VARCHAR UNIQUE, source VARCHAR, publisher VARCHAR,
  tier VARCHAR, title VARCHAR, body VARCHAR, published_at VARCHAR, first_seen_at VARCHAR, fetched_at VARCHAR,
  category VARCHAR, is_stub BOOLEAN, content_hash VARCHAR, dropped_reason VARCHAR, via VARCHAR);
CREATE TABLE IF NOT EXISTS nd_links (article_id BIGINT, symbol VARCHAR, salience DOUBLE, PRIMARY KEY (article_id, symbol));
CREATE TABLE IF NOT EXISTS nd_events (id BIGINT PRIMARY KEY, symbol VARCHAR, event_type VARCHAR, title VARCHAR,
  direction VARCHAR, fact_type VARCHAR, sig JSON, first_seen VARCHAR, last_updated VARCHAR);
CREATE TABLE IF NOT EXISTS nd_event_articles (event_id BIGINT, article_id BIGINT, PRIMARY KEY (event_id, article_id));
CREATE TABLE IF NOT EXISTS nd_claims (id BIGINT PRIMARY KEY, article_id BIGINT, symbol VARCHAR, text VARCHAR,
  amount_cr DOUBLE, metric VARCHAR);
CREATE TABLE IF NOT EXISTS nd_state (symbol VARCHAR PRIMARY KEY, last_refresh VARCHAR, last_counts JSON);
CREATE TABLE IF NOT EXISTS alerts (alert_id VARCHAR PRIMARY KEY, created_at VARCHAR, symbol VARCHAR,
  type VARCHAR, message VARCHAR, acknowledged BOOLEAN);
"""


class Ledger:
    def __init__(self, path=DB_PATH):
        self._lock = threading.RLock()
        self.con = duckdb.connect(str(path))
        self.con.execute(SCHEMA)

    def execute(self, sql: str, params: list | tuple = ()) -> Any:
        with self._lock:
            return self.con.execute(sql, params)

    def fetchall(self, sql: str, params: list | tuple = ()) -> list[dict]:
        with self._lock:
            cur = self.con.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def fetchone(self, sql: str, params: list | tuple = ()) -> dict | None:
        rows = self.fetchall(sql, params)
        return rows[0] if rows else None

    # ---- helpers
    def add_event(self, ev: dict) -> None:
        self.execute("INSERT INTO run_events VALUES (?, ?, ?, ?)",
                     [ev["run_id"], ev["seq"], ev["ts"], json.dumps(ev, default=str)])

    def events(self, run_id: str) -> list[dict]:
        rows = self.fetchall("SELECT event FROM run_events WHERE run_id=? ORDER BY seq", [run_id])
        return [json.loads(r["event"]) for r in rows]

    def month_spend_usd(self, month_prefix: str) -> float:
        row = self.fetchone("SELECT COALESCE(SUM(cost_usd),0) AS s FROM request_records WHERE created_at LIKE ?",
                            [month_prefix + "%"])
        return float(row["s"]) if row else 0.0


_LEDGER: Ledger | None = None


def get_ledger() -> Ledger:
    global _LEDGER
    if _LEDGER is None:
        _LEDGER = Ledger()
    return _LEDGER


def dumps(obj: Any) -> str:
    if hasattr(obj, "model_dump"):
        obj = obj.model_dump()
    return json.dumps(obj, default=str, ensure_ascii=False)
