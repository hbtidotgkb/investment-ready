"""Research inbox (spec §7): validate selected browser captures, return idempotent receipts,
version sources (never overwrite), resolve company identity (ambiguous stays unresolved),
and extract attributed claims with verification tasks."""
from __future__ import annotations

import json
import uuid

from .contracts import CaptureBatch, ClaimsLLM
from .data import nse
from .data.sources import canonical_url, domain_of, tier_for
from .ledger import get_ledger
from .settings import canonical_hash, iso, sha256_text

MAX_SOURCE_CHARS = 80_000


def _fingerprint(passages: list[dict]) -> str:
    return sha256_text("\n".join(" ".join(p["text"].split()) for p in passages))


def ingest_batch(batch: CaptureBatch) -> dict:
    led = get_ledger()
    rid = "rcpt-" + canonical_hash({"s": batch.research_session_id,
                                    "i": [[i.capture_id, _fingerprint([p.model_dump() for p in i.passages])] for i in batch.items]})[:16]
    prior = led.fetchone("SELECT body FROM ingestion_receipts WHERE receipt_id=?", [rid])
    if prior:
        body = json.loads(prior["body"])
        body["replayed"] = True
        return body
    master = nse.equity_master()
    out_items = []
    for it in batch.items:
        res = {"capture_id": it.capture_id, "status": "rejected", "reason": None, "source_id": None, "version": None}
        try:
            url = canonical_url(it.url)
        except ValueError as e:
            res["reason"] = str(e)
            out_items.append(res)
            continue
        passages = [{"id": f"P{i + 1}", "text": " ".join(p.text.split())} for i, p in enumerate(it.passages) if p.text.strip()]
        total = sum(len(p["text"]) for p in passages)
        if not passages:
            res["reason"] = "No text captured"
            out_items.append(res)
            continue
        if total > MAX_SOURCE_CHARS:
            res["reason"] = f"Capture is {total:,} chars; limit is {MAX_SOURCE_CHARS:,}. Use 'Capture selection'."
            out_items.append(res)
            continue
        fp = _fingerprint(passages)
        sid = "src-" + sha256_text(url)[:12]
        q = led.fetchone("SELECT * FROM capture_queue WHERE queue_id=?", [it.queue_id]) if it.queue_id else None
        existing = led.fetchall("SELECT version, fingerprint FROM research_sources WHERE source_id=? ORDER BY version", [sid])
        if any(r["fingerprint"] == fp for r in existing):
            v = next(r["version"] for r in existing if r["fingerprint"] == fp)
            res.update(status="duplicate", source_id=sid, version=v, reason="Identical content already ingested")
            if q:
                _queue_set(it.queue_id, "captured", source_id=sid, version=v)
                if q["symbol"]:
                    link_symbol(sid, q["symbol"], method="queue")
            out_items.append(res)
            continue
        version = (existing[-1]["version"] + 1) if existing else 1
        first_seen = led.fetchone("SELECT MIN(first_seen_at) AS f FROM research_sources WHERE source_id=?", [sid])
        companies = nse.resolve_companies(it.title + " " + " ".join(p["text"] for p in passages[:40]), master)
        if q and q["symbol"]:
            companies = _with_link(companies, q["symbol"], master, "queue")
        warnings = list(it.access_warnings) + (["truncated_by_extension"] if it.truncated else [])
        access = "partially_read" if it.truncated else "capture_unverified"
        usage = "personal_research_user_attested" if it.usage_attested else "unattested_not_for_model"
        now = iso()
        led.execute("INSERT INTO research_sources VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [sid, version, url, it.title[:500], domain_of(url), it.author, it.published_at, it.captured_at,
                     (first_seen or {}).get("f") or now, now, fp, access, usage, it.truncated, json.dumps(warnings),
                     json.dumps(passages), json.dumps(companies), batch.research_session_id])
        if q:
            _queue_set(it.queue_id, "captured", source_id=sid, version=version)
        res.update(status="accepted", source_id=sid, version=version,
                   reason="New version of an existing source" if existing else None,
                   tier=tier_for(url, "T4_capture_unverified"), usage_status=usage, access_status=access,
                   companies=companies, passages=len(passages), chars=total)
        out_items.append(res)
    body = {"receipt_id": rid, "created_at": iso(), "research_session_id": batch.research_session_id,
            "extension_version": batch.extension_version, "items": out_items,
            "note": "Captures are attributed, unverified research inputs. Import does not verify any claim."}
    led.execute("INSERT INTO ingestion_receipts VALUES (?,?,?)", [rid, body["created_at"], json.dumps(body)])
    return body


def _with_link(companies: dict, symbol: str, master: list[dict], method: str) -> dict:
    """Attach an explicit user-chosen company link. Explicit user links win over ambiguity."""
    sym = symbol.upper().strip()
    if any(c["symbol"] == sym for c in companies.get("resolved", [])):
        return companies
    m = next((r for r in master if r["symbol"] == sym), None)
    entry = {"symbol": sym, "name": m["name"] if m else sym, "isin": m["isin"] if m else None, "matched": f"user:{method}"}
    return {"resolved": [entry] + companies.get("resolved", []), "ambiguous": companies.get("ambiguous", [])}


def link_symbol(source_id: str, symbol: str, method: str = "user") -> dict:
    """Link the latest version of a source to a symbol (e.g. when name matching was ambiguous)."""
    led = get_ledger()
    r = led.fetchone("SELECT version, company_links FROM research_sources WHERE source_id=? ORDER BY version DESC LIMIT 1", [source_id])
    if not r:
        raise KeyError(source_id)
    links = _with_link(json.loads(r["company_links"] or "{}"), symbol, nse.equity_master(), method)
    led.execute("UPDATE research_sources SET company_links=? WHERE source_id=? AND version=?",
                [json.dumps(links), source_id, r["version"]])
    return links


# ------------------------------------------------------------------ article queue ---

def _queue_set(queue_id: str, status: str, **kw) -> None:
    sets = ["status=?", "updated_at=?"] + [f"{k}=?" for k in kw]
    get_ledger().execute(f"UPDATE capture_queue SET {', '.join(sets)} WHERE queue_id=?",
                         [status, iso(), *kw.values(), queue_id])


def queue_add(urls: list[str], symbol: str | None = None, note: str | None = None) -> dict:
    """Queue article URLs for the extension to capture in the user's own signed-in browser.
    ET hosts only (the extension's optional permission); duplicates of pending items are skipped."""
    from .settings import config
    led = get_ledger()
    limit = int((config("user").get("inbox") or {}).get("queue_max_pending", 20))
    pending = led.fetchone("SELECT COUNT(*) AS n FROM capture_queue WHERE status IN ('pending','in_progress')")["n"]
    added, rejected = [], []
    for raw in urls:
        raw = raw.strip()
        if not raw:
            continue
        try:
            cu = canonical_url(raw)
        except ValueError as e:
            rejected.append({"url": raw, "reason": str(e)})
            continue
        if domain_of(cu) != "economictimes.indiatimes.com":
            rejected.append({"url": raw, "reason": "Only economictimes.indiatimes.com articles can be queued (extension permission)"})
            continue
        if led.fetchone("SELECT 1 AS x FROM capture_queue WHERE canonical_url=? AND status IN ('pending','in_progress')", [cu]):
            rejected.append({"url": raw, "reason": "Already queued"})
            continue
        if pending >= limit:
            rejected.append({"url": raw, "reason": f"Queue limit {limit} reached"})
            continue
        qid = "q-" + uuid.uuid4().hex[:10]
        led.execute("INSERT INTO capture_queue VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [qid, raw, cu, (symbol or "").upper().strip() or None, (note or "")[:300] or None, "pending",
                     iso(), iso(), None, None, None])
        pending += 1
        added.append({"queue_id": qid, "url": cu})
    return {"added": added, "rejected": rejected}


def queue_list(status: str | None = None) -> list[dict]:
    led = get_ledger()
    if status:
        return led.fetchall("SELECT * FROM capture_queue WHERE status=? ORDER BY created_at", [status])
    return led.fetchall("SELECT * FROM capture_queue ORDER BY created_at DESC LIMIT 200")


def queue_update(queue_id: str, status: str, error: str | None = None) -> None:
    if status not in ("pending", "in_progress", "failed", "cancelled"):
        raise ValueError("invalid status")
    _queue_set(queue_id, status, error=(error or "")[:500] or None)


def list_sources(limit: int = 200) -> list[dict]:
    rows = get_ledger().fetchall(
        "SELECT source_id, version, canonical_url, title, publisher, published_at, captured_at, first_seen_at, ingested_at, "
        "access_status, usage_status, truncated, warnings, company_links, json_array_length(passages) AS n_passages "
        "FROM research_sources ORDER BY ingested_at DESC LIMIT ?", [limit])
    for r in rows:
        r["warnings"] = json.loads(r["warnings"] or "[]")
        r["company_links"] = json.loads(r["company_links"] or "{}")
        r["claims"] = get_ledger().fetchone("SELECT COUNT(*) AS n FROM research_claims WHERE source_id=? AND version=?",
                                            [r["source_id"], r["version"]])["n"]
    return rows


def get_source(source_id: str, version: int | None = None) -> dict | None:
    led = get_ledger()
    q = "SELECT * FROM research_sources WHERE source_id=? " + ("AND version=? " if version else "") + "ORDER BY version DESC LIMIT 1"
    r = led.fetchone(q, [source_id, version] if version else [source_id])
    if not r:
        return None
    for k in ("warnings", "passages", "company_links"):
        r[k] = json.loads(r[k] or "null")
    r["claims"] = [json.loads(c["body"]) for c in led.fetchall(
        "SELECT body FROM research_claims WHERE source_id=? AND version=? ORDER BY claim_id", [source_id, r["version"]])]
    return r


async def extract_claims(gw, source_id: str) -> list[dict]:
    from .prompting import system_prompt
    src = get_source(source_id)
    if not src:
        raise KeyError(source_id)
    if src["usage_status"] != "personal_research_user_attested":
        raise PermissionError("Usage for personal research was not attested for this capture; it will not be sent to a model.")
    sys_p, _ = system_prompt("claims")
    body = "\n".join(f'<passage id="{p["id"]}">{p["text"]}</passage>' for p in src["passages"])
    user = f"<article title=\"{src['title']}\" publisher=\"{src['publisher']}\">\n{body}\n</article>\nExtract the claims."
    pmap = {p["id"]: " ".join(p["text"].lower().split()) for p in src["passages"]}

    def check(c: ClaimsLLM) -> list[str]:
        errs = []
        for i, cl in enumerate(c.claims):
            if cl.passage_id not in pmap:
                errs.append(f"claims[{i}]: unknown passage_id {cl.passage_id}")
            elif " ".join(cl.quote.lower().split()) not in pmap[cl.passage_id]:
                errs.append(f"claims[{i}]: quote not found verbatim in {cl.passage_id}")
        return errs

    res = await gw.structured(role="claims", stage="inbox", system=sys_p, user=user, output_model=ClaimsLLM, semantic_check=check)
    master = nse.equity_master()
    led = get_ledger()
    out = []
    for cl in res.parsed.claims:
        comp = nse.resolve_companies(cl.company_mention, master)
        sym = comp["resolved"][0]["symbol"] if len(comp["resolved"]) == 1 and not comp["ambiguous"] else None
        body = cl.model_dump() | {"claim_id": f"clm-{uuid.uuid4().hex[:10]}", "source_id": source_id, "version": src["version"],
                                  "symbol": sym, "company_resolution": comp, "status": "unverified",
                                  "evidence_class": "third_party_opinion" if cl.claim_type in ("recommendation", "target_price", "opinion") else "publisher_observation"}
        led.execute("INSERT INTO research_claims VALUES (?,?,?,?,?,?,?)",
                    [body["claim_id"], source_id, src["version"], sym, iso(), "unverified", json.dumps(body)])
        out.append(body)
    return out
