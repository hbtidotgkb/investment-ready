import asyncio
import json

from ca_platform.data import docs as D
from ca_platform.evidence import EvidenceFactory
from tests.test_sr_plus import INFY

WHEN = "2026-11-23T10:00:00+05:30"


def make_pdf(pages: list[str]) -> bytes:
    """Minimal PDF with a real text layer, one string per page."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for t in pages:
        lines = " ".join(f"({ln}) Tj T*" for ln in t.replace("(", "").replace(")", "").split("\n"))
        stream = f"BT /F1 10 Tf 14 TL 40 800 Td {lines} ET"
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> /Contents {len(objs)} 0 R >>")
        kids.append(len(objs))
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>"
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offs)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out


class FakeGW:
    available = True

    def __init__(self, meta):
        self.meta, self.calls = meta, []

    async def structured(self, *, role, output_model, documents=None, **kw):
        self.calls.append(role)
        parsed = output_model.model_validate(self.meta if role == "doc_classify" else {"pages": []})
        return type("R", (), {"parsed": parsed, "record": {"model": "fake", "cost_usd": 0.0}})()


def page(n, extra=""):
    return (f"Page {n} of the Infosys annual report FY26 with enough words to count as a real text layer.\n"
            f"Some ordinary narrative about the business and its people.{extra}")


def test_text_layer_pdf_is_read_by_code_and_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "DOCS_DIR", tmp_path)
    (tmp_path / "INFY").mkdir()
    pdf = tmp_path / "INFY" / "ar.pdf"
    pdf.write_bytes(make_pdf([page(1), page(2, "\nContingent liabilities not provided for: Rs 1,234 crore.")]))
    gw = FakeGW({"document_type": "annual_report", "title": "Annual Report FY26", "publisher": "Infosys",
                 "primary_company": "Infosys Limited", "document_date": "2026-06-01"})
    found = asyncio.run(D.collect("INFY", "Infosys Limited", WHEN, gw))
    assert gw.calls == ["doc_classify"]                              # no page transcription: text layer read by code
    d = found["documents"][0]
    assert d["used"] and d["text_method"] == "text_layer" and d["pages_read"] == 2
    fac = EvidenceFactory()
    out = D.add_documents(fac, found, "INFY")
    ev = fac.items[0]
    assert out[0]["evidence_id"] == ev.id and ev.kind == "pdf" and ev.tier == "T4_capture_unverified"
    assert any(p.text.startswith("[p. 2]") and "Rs 1,234 crore" in p.text for p in ev.passages)
    assert json.loads((tmp_path / "INFY" / "ar.pdf.extracted.json").read_text(encoding="utf-8"))["pipeline"] == D.PIPELINE
    again = asyncio.run(D.collect("INFY", "Infosys Limited", WHEN, None))     # cached: no model needed
    assert again["documents"][0]["used"] and not again["errors"]


def test_documents_about_another_company_or_from_the_future_are_not_used():
    base = {"pages": [{"page": 1, "text": "x" * 100}], "meta": {"document_type": "broker_report", "primary_company": "Tata Consultancy Services"}}
    assert D.assess_doc(base, "INFY", "Infosys Limited", WHEN)["status"] == "mismatch"
    fut = dict(base, meta={"document_type": "broker_report", "primary_company": "Infosys", "document_date": "2026-12-01"})
    assert D.assess_doc(fut, "INFY", "Infosys Limited", WHEN)["status"] == "future"
    sector = dict(base, meta={"document_type": "sector_report", "primary_company": None, "companies_mentioned": ["Infosys", "TCS"]})
    assert D.assess_doc(sector, "INFY", "Infosys Limited", WHEN)["used"]


def test_sr_plus_pdfs_are_routed_to_the_structured_reader(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "DOCS_DIR", tmp_path)
    (tmp_path / "INFY").mkdir()
    for name, date_ in (("new.pdf", "2026-11-22"), ("old.pdf", "2026-10-25")):
        p = tmp_path / "INFY" / name
        p.write_bytes(make_pdf([f"Stock Reports Plus {name}"]))
        sha = __import__("hashlib").sha256(p.read_bytes()).hexdigest()
        rec = {"file": name, "sha256": sha, "pipeline": D.PIPELINE, "extracted_at": WHEN, "pages_total": 13,
               "meta": {"document_type": "sr_plus"}, "pages": None, "text_method": None,
               "sr_plus": {"model": "fake", "data": dict(INFY, report_date=date_)}}
        (tmp_path / "INFY" / f"{name}.extracted.json").write_text(json.dumps(rec), encoding="utf-8")
    found = asyncio.run(D.collect("INFY", "Infosys Limited", WHEN, None))
    sr = found["sr_plus"]
    assert sr["used"] and sr["file"] == "new.pdf" and len(sr["history"]) == 2 and found["documents"] == []


def test_long_documents_keep_page_one_and_the_most_relevant_pages():
    pages = [{"page": 1, "text": "cover " * 200}] + [{"page": n, "text": "filler text " * 300} for n in range(2, 30)]
    pages[19] = {"page": 20, "text": "contingent liabilities and related party transactions and auditor " * 40}
    kept, dropped = D.select_pages(pages, 6000)
    nums = [p["page"] for p in kept]
    assert nums[0] == 1 and 20 in nums and dropped == len(pages) - len(kept) and nums == sorted(nums)


def test_empty_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "DOCS_DIR", tmp_path)
    found = asyncio.run(D.collect("INFY", None, WHEN))
    assert found["sr_plus"]["status"] == "missing" and "data/sr_plus/INFY/" in found["sr_plus"]["reason"]
