"""Citation verification — the accountability core.

Every model statement cites evidence ids, passage ids and a short verbatim quote. Code
checks each citation against the frozen bundle:

  verified          quote found verbatim (whitespace/quote-mark/case-normalised) in the passage
  id_only           evidence/passage exist but no quote was given
  quote_mismatch    quote NOT found in the cited passage/evidence — treated as unsupported
  unknown_passage   passage id not in the bundle
  unknown_evidence  evidence id not in the bundle (fabricated or out of boundary)

A citation's existence is not proof it supports the claim (spec §10.2); a verified quote is
the strongest support code can establish. Interpretive support is left to Head/Critic review.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

from pydantic import BaseModel

from .contracts import Bundle

_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                         "₹": "rs ", " ": " "})


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").translate(_QUOTES).lower()
    s = re.sub(r"rs\.?\s*", "rs ", s)
    s = re.sub(r"[,]", "", s)            # 1,234 == 1234
    return re.sub(r"\s+", " ", s).strip(" .")


class CitationIndex:
    def __init__(self, bundle: Bundle):
        self.evidence = {e.id: e for e in bundle.evidence}
        self.passages: dict[str, tuple[str, str]] = {}
        for e in bundle.evidence:
            for p in e.passages:
                self.passages[p.id] = (e.id, _norm(p.text))

    def check(self, cite: Any) -> str:
        ev_id = getattr(cite, "evidence_id", None) or (cite.get("evidence_id") if isinstance(cite, dict) else None)
        pid = getattr(cite, "passage_id", None) if not isinstance(cite, dict) else cite.get("passage_id")
        quote = getattr(cite, "quote", None) if not isinstance(cite, dict) else cite.get("quote")
        if ev_id not in self.evidence:
            return "unknown_evidence"
        if pid:
            if pid not in self.passages or self.passages[pid][0] != ev_id:
                return "unknown_passage"
        if not quote or not quote.strip():
            return "id_only"
        q = _norm(quote)
        if len(q) < 4:
            return "id_only"
        haystacks = [self.passages[pid][1]] if pid else [t for (e, t) in self.passages.values() if e == ev_id]
        if any(q in h for h in haystacks):
            return "verified"
        # tolerate an ellipsis-joined quote if every fragment is present
        frags = [f.strip() for f in re.split(r"\.\.\.|…", q) if len(f.strip()) >= 4]
        if len(frags) > 1 and all(any(f in h for h in haystacks) for f in frags):
            return "verified"
        return "quote_mismatch"

    def annotate(self, cites: Iterable[Any]) -> list[dict]:
        out = []
        for c in cites:
            d = c.model_dump() if hasattr(c, "model_dump") else dict(c)
            d["status"] = self.check(c)
            ev = self.evidence.get(d.get("evidence_id"))
            if ev:
                d["tier"] = ev.tier
                d["publisher"] = ev.publisher
            out.append(d)
        return out


BAD = {"unknown_evidence", "unknown_passage", "quote_mismatch"}


def iter_cited(obj: Any, path: str = "") -> Iterable[tuple[str, Any, list]]:
    """Yield (path, item, cites) for every object carrying a `cites` list (recursively)."""
    if isinstance(obj, BaseModel):
        if "cites" in type(obj).model_fields:
            yield path, obj, obj.cites
        for name in type(obj).model_fields:
            yield from iter_cited(getattr(obj, name), f"{path}.{name}" if path else name)
    elif isinstance(obj, list):
        for i, x in enumerate(obj):
            yield from iter_cited(x, f"{path}[{i}]")


def audit(obj: Any, idx: CitationIndex, require_cites_for: tuple[str, ...] = ("observation", "conclusion")) -> dict:
    """Score citations in a model output. Returns counts, a 0-1 score and defect strings
    suitable for a bounded repair request."""
    counts = {"verified": 0, "id_only": 0, "quote_mismatch": 0, "unknown_passage": 0, "unknown_evidence": 0}
    defects: list[str] = []
    uncited = 0
    for path, item, cites in iter_cited(obj):
        kind = getattr(item, "kind", None)
        if not cites and (kind in require_cites_for or kind is None and path.startswith(("checks", "red_flags"))):
            # checks with insufficient_evidence legitimately may have no cites
            if getattr(item, "result", None) not in ("insufficient_evidence", "not_applicable"):
                uncited += 1
                defects.append(f"{path}: material statement has no citation")
        for j, c in enumerate(cites):
            st = idx.check(c)
            counts[st] += 1
            if st in BAD:
                defects.append(f"{path}.cites[{j}]: {st} (evidence_id={c.evidence_id!r}, passage_id={c.passage_id!r}"
                               + (f", quote={c.quote[:60]!r}" if c.quote else "") + ")")
    total = sum(counts.values())
    score = (counts["verified"] + 0.5 * counts["id_only"]) / total if total else 0.0
    return {"counts": counts, "total": total, "uncited_material": uncited, "score": round(score, 3),
            "defects": defects}
