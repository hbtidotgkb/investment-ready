"""The user's PDFs for a stock: data/sr_plus/<SYMBOL>/*.pdf — any number, any kind.

Each file is processed once and cached next to it (<file>.extracted.json, keyed by content hash):
  1. Identify it (model, first pages only): SR Plus report, broker report, annual report,
     concall transcript, presentation, results, sector report, other; its company and date.
  2. Read it:
     * SR Plus → the structured transcription in data/sr_plus.py (code then checks the arithmetic).
     * anything else → page text. Pages with a text layer are read by code (verbatim, free); image
       pages are transcribed by the model in small chunks, up to a configured page limit.
  3. Code decides use: a document about another company, or dated after the decision, is excluded.
Large documents are cut down to their most relevant pages (keyword-scored) before the panel sees
them; every passage carries its page number. All files stay local (personal research use).
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

from ..settings import DATA, config, iso
from . import sr_plus as SR

PIPELINE = "docs@1"
DOCS_DIR = DATA / "sr_plus"
DOCS_DIR.mkdir(parents=True, exist_ok=True)
TEXT_PAGE_MIN_CHARS = 80
DocType = Literal["sr_plus", "broker_report", "annual_report", "concall_transcript", "investor_presentation",
                  "results", "sector_report", "news_article", "other"]
TYPE_LABEL = {"sr_plus": "LSEG Stock Reports Plus", "broker_report": "Broker research report", "annual_report": "Annual report",
              "concall_transcript": "Earnings call transcript", "investor_presentation": "Investor presentation",
              "results": "Results document", "sector_report": "Sector / industry report", "news_article": "Article",
              "other": "Document"}


class DocMetaLLM(BaseModel):
    document_type: DocType
    title: Optional[str] = None
    publisher: Optional[str] = None            # broker / company / publisher as printed
    primary_company: Optional[str] = None      # the one company the document is about; null for multi-company documents
    companies_mentioned: list[str] = Field(default_factory=list)
    document_date: Optional[str] = None        # YYYY-MM-DD as printed (report/publication date)
    summary: Optional[str] = None              # one sentence: what the document is


class PageText(BaseModel):
    page: int
    text: str


class PagesLLM(BaseModel):
    pages: list[PageText] = Field(default_factory=list)


# ------------------------------------------------------------------- files ---

def folder(symbol: str) -> Optional[Path]:
    want = symbol.upper()
    for d in DOCS_DIR.iterdir() if DOCS_DIR.exists() else []:
        if d.is_dir() and d.name.upper() == want:
            return d
    return None


def pdfs(symbol: str) -> list[Path]:
    d = folder(symbol)
    if not d:
        return []
    return sorted((p for p in d.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"),
                  key=lambda p: p.stat().st_mtime, reverse=True)


def _sidecar(pdf: Path) -> Path:
    return pdf.with_name(pdf.name + ".extracted.json")


def cached(pdf: Path, sha: str | None = None) -> Optional[dict]:
    sc = _sidecar(pdf)
    if not sc.exists():
        return None
    try:
        rec = json.loads(sc.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if rec.get("pipeline") != PIPELINE or rec.get("sha256") != (sha or hashlib.sha256(pdf.read_bytes()).hexdigest()):
        return None
    return rec


def _save(pdf: Path, rec: dict) -> None:
    _sidecar(pdf).write_text(json.dumps(rec, indent=1, ensure_ascii=False), encoding="utf-8")


def text_layer(raw: bytes) -> list[str]:
    """Per-page text from the PDF's own text layer ('' for image-only pages)."""
    from pypdf import PdfReader
    out = []
    for p in PdfReader(io.BytesIO(raw)).pages:
        try:
            out.append((p.extract_text() or "").strip())
        except Exception:
            out.append("")
    return out


def subset(raw: bytes, pages: list[int]) -> bytes:
    """A new PDF holding only the given 1-based pages."""
    from pypdf import PdfReader, PdfWriter
    r, w = PdfReader(io.BytesIO(raw)), PdfWriter()
    for n in pages:
        w.add_page(r.pages[n - 1])
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


# ------------------------------------------------------------------ reading ---

def _cfg() -> dict:
    return config("sources").get("user_documents", {})


async def _identify(gw, pdf: Path, raw: bytes, sha: str, texts: list[str]) -> tuple[dict, str]:
    from ..prompting import load_prompt
    body, _ = load_prompt(config("models")["roles"]["doc_classify"]["prompt"])
    first = list(range(1, min(len(texts), 3) + 1))
    if all(len(texts[i - 1]) >= TEXT_PAGE_MIN_CHARS for i in first):
        sample = "\n\n".join(f"[page {i}]\n{texts[i - 1][:4000]}" for i in first)
        res = await gw.structured(role="doc_classify", stage="filings", system=body, output_model=DocMetaLLM,
                                  user=f"File name: {pdf.name}\nFirst pages of the document (text layer):\n<document>\n{sample}\n</document>")
    else:
        part = subset(raw, first)
        res = await gw.structured(role="doc_classify", stage="filings", system=body, output_model=DocMetaLLM,
                                  user=f"File name: {pdf.name}. These are the first {len(first)} page(s) of the document.",
                                  documents=[{"title": pdf.name, "sha256": f"{sha}:p1-{len(first)}",
                                              "data": base64.b64encode(part).decode("ascii")}])
    return res.parsed.model_dump(), res.record.get("served_model") or res.record.get("model")


async def _transcribe_pages(gw, pdf: Path, raw: bytes, sha: str, pages: list[int], log) -> tuple[dict[int, str], float]:
    from ..prompting import load_prompt
    body, _ = load_prompt(config("models")["roles"]["doc_pages"]["prompt"])
    chunk = int(_cfg().get("model_pages_per_call", 6))
    got: dict[int, str] = {}
    cost = 0.0
    for i in range(0, len(pages), chunk):
        part_pages = pages[i:i + chunk]
        log(f"Documents: {pdf.name} — model reading image pages {part_pages[0]}–{part_pages[-1]}")
        res = await gw.structured(
            role="doc_pages", stage="filings", system=body, output_model=PagesLLM,
            user=(f"These are pages {', '.join(map(str, part_pages))} of '{pdf.name}', in that order. Return one entry per page with "
                  f"`page` set to these original page numbers."),
            documents=[{"title": pdf.name, "sha256": f"{sha}:p{part_pages[0]}-{part_pages[-1]}",
                        "data": base64.b64encode(subset(raw, part_pages)).decode("ascii")}])
        cost += res.record.get("cost_usd") or 0.0
        for pt in res.parsed.pages:
            if pt.page in part_pages and pt.text.strip():
                got[pt.page] = pt.text.strip()
    return got, cost


async def process(pdf: Path, gw, log=lambda m: None) -> dict:
    """Identify and read one PDF (cached by content hash; a model is needed only for new files)."""
    raw = pdf.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    rec = cached(pdf, sha)
    if rec:
        return rec
    if gw is None or not getattr(gw, "available", False):
        raise RuntimeError("not read yet — needs ANTHROPIC_API_KEY (live mode)")
    texts = text_layer(raw)
    log(f"Documents: reading {pdf.name} ({len(raw) / 1e6:.1f} MB, {len(texts)} pages, "
        f"{sum(len(t) >= TEXT_PAGE_MIN_CHARS for t in texts)} with a text layer)")
    meta, by = await _identify(gw, pdf, raw, sha, texts)
    log(f"Documents: {pdf.name} is a {TYPE_LABEL[meta['document_type']].lower()}"
        + (f" about {meta['primary_company']}" if meta.get("primary_company") else "")
        + (f", dated {meta['document_date']}" if meta.get("document_date") else ""))
    rec = {"file": pdf.name, "sha256": sha, "pipeline": PIPELINE, "extracted_at": iso(), "meta": meta, "identified_by": by,
           "pages_total": len(texts), "sr_plus": None, "pages": None, "text_method": None, "pages_not_read": []}
    if meta["document_type"] == "sr_plus":
        rec["sr_plus"] = await SR.transcribe(gw, pdf.name, sha, base64.b64encode(raw).decode("ascii"))
    else:
        pages = {i + 1: t for i, t in enumerate(texts) if len(t) >= TEXT_PAGE_MIN_CHARS}
        image_pages = [i + 1 for i, t in enumerate(texts) if len(t) < TEXT_PAGE_MIN_CHARS]
        limit = int(_cfg().get("max_model_pages_per_document", 40))
        todo, skipped = image_pages[:limit], image_pages[limit:]
        if todo:
            got, _ = await _transcribe_pages(gw, pdf, raw, sha, todo, log)
            pages.update(got)
        rec["pages"] = [{"page": n, "text": pages[n], "method": "text_layer" if n not in image_pages else "model"}
                        for n in sorted(pages)]
        rec["text_method"] = ("text_layer" if not image_pages else "model" if len(image_pages) == len(texts) else "mixed")
        rec["pages_not_read"] = skipped
    _save(pdf, rec)
    return rec


# ------------------------------------------------------------- use decisions ---

def _relevant(meta: dict, symbol: str, company: str | None) -> tuple[bool, str]:
    pc = meta.get("primary_company")
    if not pc:
        return True, "not specific to one company" if meta.get("companies_mentioned") else "company not stated"
    if SR.names_match(pc, company) or pc.upper().split()[0] == symbol.upper() or symbol.upper() in pc.upper().replace(" ", ""):
        return True, f"about {pc}"
    return False, f"about {pc}, not {company or symbol}"


def assess_doc(rec: dict, symbol: str, company: str | None, decision_at: str) -> dict:
    meta = rec["meta"]
    ok, why = _relevant(meta, symbol, company)
    dd = meta.get("document_date")
    if not ok:
        return {"status": "mismatch", "used": False, "reason": why}
    if dd and dd[:10] > decision_at[:10]:
        return {"status": "future", "used": False, "reason": f"dated {dd}, after the decision date (would be look-ahead)"}
    if not rec.get("pages"):
        return {"status": "unread", "used": False, "reason": "no readable text"}
    return {"status": "available", "used": True, "reason": why}


KEYWORDS = ("contingent liabilit", "related part", "auditor", "qualified opinion", "emphasis of matter", "key audit matter",
            "guidance", "outlook", "margin", "ebitda", "revenue", "order book", "order inflow", "capex", "capacity", "utilisation",
            "debt", "borrowing", "pledge", "target price", "rating", "recommend", "valuation", "eps", "estimate", "risk",
            "litigation", "penalty", "impairment", "write-off", "write off", "cash flow", "working capital", "receivable",
            "inventor", "dividend", "buyback", "demand", "pricing", "raw material", "volume", "market share", "subsidiar")


def select_pages(pages: list[dict], budget: int) -> tuple[list[dict], int]:
    """All pages if they fit; otherwise page 1 plus the highest keyword-scoring pages, back in page order."""
    total = sum(len(p["text"]) for p in pages)
    if total <= budget:
        return pages, 0
    def score(p):
        t = p["text"].lower()
        return sum(t.count(k) for k in KEYWORDS) / (1 + len(t) / 2000)
    chosen, used = [], 0
    for p in sorted(pages, key=lambda p: (p["page"] != 1, -score(p))):
        if used + len(p["text"]) > budget and chosen:
            continue
        chosen.append(p)
        used += len(p["text"])
    return sorted(chosen, key=lambda p: p["page"]), len(pages) - len(chosen)


def _chunks(page: dict, size: int = 1500) -> list[str]:
    t = re.sub(r"[ \t]+", " ", page["text"]).strip()
    parts, cur = [], ""
    for para in re.split(r"\n\s*\n|\n", t):
        if cur and len(cur) + len(para) + 1 > size:
            parts.append(cur)
            cur = para
        else:
            cur = f"{cur}\n{para}" if cur else para
    if cur:
        parts.append(cur)
    return [f"[p. {page['page']}] {x}" for x in parts]


def doc_evidence(fac, rec: dict, symbol: str, budget: int):
    meta = rec["meta"]
    kept, dropped = select_pages(rec["pages"], budget)
    ps = [c for p in kept for c in _chunks(p)]
    label = TYPE_LABEL[meta["document_type"]]
    head = (f"{label}: {meta.get('title') or rec['file']}" + (f" by {meta['publisher']}" if meta.get("publisher") else "")
            + (f", dated {meta['document_date']}" if meta.get("document_date") else ", date not stated")
            + f". {len(rec['pages'])} of {rec['pages_total']} pages read"
            + (f"; {dropped} lower-relevance pages left out for length" if dropped else "")
            + (f"; pages {rec['pages_not_read'][0]}–{rec['pages_not_read'][-1]} not read (page limit)" if rec.get("pages_not_read") else "")
            + ". Opinions in it are the author's, not verified facts.")
    method = {"text_layer": "the PDF's own text layer (verbatim)", "model": "model transcription of image pages",
              "mixed": "the text layer where present, model transcription for image pages"}[rec["text_method"]]
    return fac.add(kind="pdf", title=f"{symbol}: {label} — {meta.get('title') or rec['file']}",
                   publisher=meta.get("publisher") or "Your PDF", tier="T4_capture_unverified", passages=[head] + ps,
                   published_at=(meta["document_date"][:10] + "T00:00:00+05:30") if meta.get("document_date") else None,
                   access_status="user_supplied_pdf", usage_status="personal_research_user_attested",
                   retrieved_via=f"user_documents:{meta['document_type']}",
                   notes=f"Your PDF data/sr_plus/{symbol}/{rec['file']} (sha256 {rec['sha256'][:12]}…), read via {method} ({PIPELINE}).")


async def collect(symbol: str, company: str | None, decision_at: str, gw=None, log=lambda m: None,
                  our_close: float | None = None, our_pe: float | None = None) -> dict:
    """Every PDF in the stock's folder → SR Plus selection + other documents, each with a use decision."""
    files = pdfs(symbol)
    sr_recs, docs, errors = [], [], []
    for f in files:
        try:
            rec = await process(f, gw, log)
        except Exception as e:
            errors.append(f"{f.name}: {str(e)[:200]}" if isinstance(e, RuntimeError) else f"{f.name}: could not be read ({type(e).__name__}: {str(e)[:160]})")
            continue
        if rec["sr_plus"]:
            sr_recs.append({"file": rec["file"], "sha256": rec["sha256"], "extracted_at": rec["extracted_at"],
                            "model": rec["sr_plus"].get("model"), "data": rec["sr_plus"]["data"]})
        else:
            a = assess_doc(rec, symbol, company, decision_at)
            docs.append({"file": rec["file"], "sha256": rec["sha256"], "meta": rec["meta"], "pages_total": rec["pages_total"],
                         "pages_read": len(rec.get("pages") or []), "pages_not_read": rec.get("pages_not_read", []),
                         "text_method": rec["text_method"], "extracted_at": rec["extracted_at"], **a, "_rec": rec})
    sr_errors = [e for e in errors]
    sr = SR.select(sr_recs, symbol, company, decision_at, our_close, our_pe, errors=sr_errors if not sr_recs else [])
    if not files:
        sr["reason"] = (f"no PDFs in data/sr_plus/{symbol.upper()}/ — save the stock's SR Plus report (and any other "
                        "relevant PDFs) there")
    return {"sr_plus": sr, "documents": docs, "errors": errors, "files": [f.name for f in files],
            "folder": f"data/sr_plus/{symbol.upper()}/"}


def add_documents(fac, found: dict, symbol: str) -> list[dict]:
    """Evidence for the usable non-SR-Plus documents within the configured length budget."""
    cfg = _cfg()
    per_doc, total = int(cfg.get("chars_per_document", 30000)), int(cfg.get("chars_total", 90000))
    used_docs = [d for d in found["documents"] if d["used"]]
    share = max(4000, min(per_doc, total // max(1, len(used_docs))))
    out = []
    for d in found["documents"]:
        rec = d.pop("_rec")
        if d["used"]:
            ev = doc_evidence(fac, rec, symbol, share)
            d["evidence_id"] = ev.id
            d["passages"] = len(ev.passages)
        out.append(d)
    return out
