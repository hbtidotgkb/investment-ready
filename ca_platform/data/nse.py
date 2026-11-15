"""NSE reference data: equity master (company resolution), quote metadata (industry,
surveillance indicator) and corporate announcements.

Only documented public pages/archives are used, politely. If NSE rejects the request we
record the source as unavailable — we never attempt to defeat access controls.
"""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import date

from ..settings import RAW
from .sources import PoliteClient

EQUITY_LIST_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
EQ_FILE = RAW / "EQUITY_L.csv"


def equity_master(refresh: bool = False) -> list[dict]:
    """[{symbol, name, isin}] from NSE's published equity list (cached on disk)."""
    if refresh or not EQ_FILE.exists():
        c = PoliteClient()
        try:
            r = c.get(EQUITY_LIST_URL)
            if r.status_code == 200 and "SYMBOL" in r.text[:100]:
                EQ_FILE.write_text(r.text, encoding="utf-8")
        except Exception:
            pass
        finally:
            c.close()
    if not EQ_FILE.exists():
        return []
    out = []
    for r in csv.DictReader(io.StringIO(EQ_FILE.read_text(encoding="utf-8", errors="replace"))):
        r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        out.append({"symbol": r.get("SYMBOL", ""), "name": r.get("NAME OF COMPANY", ""),
                    "isin": r.get(" ISIN NUMBER", r.get("ISIN NUMBER", ""))})
    return out


_SUFFIX = re.compile(r"\b(limited|ltd\.?|ltd|india|the|and|&|co\.?|company|corporation|corp\.?)\b", re.I)


def lookup(symbol: str) -> dict | None:
    sym = symbol.upper().strip()
    for r in equity_master():
        if r["symbol"] == sym:
            return r
    return None


def resolve_companies(text: str, master: list[dict] | None = None, limit: int = 8) -> dict:
    """Find company mentions in text. Returns resolved (unique) and ambiguous matches.
    Ambiguous matches are never silently attached (spec §7 inbox step 4)."""
    master = master if master is not None else equity_master()
    low = " " + re.sub(r"\s+", " ", text.lower()) + " "
    hits: dict[str, list[dict]] = {}
    for r in master:
        core = re.sub(r"\s+", " ", _SUFFIX.sub(" ", r["name"].lower())).strip(" .,")
        if len(core) < 5:
            # short names (e.g. "DLF") only when written with a company suffix: "DLF Ltd", "DLF Limited"
            if len(core) >= 2 and re.search(rf"\b{re.escape(core)}\s+(ltd\.?|limited)\b", low):
                hits.setdefault(core, []).append(r)
            continue
        if f" {core} " in low or f" {core}'" in low or f" {core}," in low:
            hits.setdefault(core, []).append(r)
        elif len(r["symbol"]) >= 4 and re.search(rf"\b{re.escape(r['symbol'])}\b", text):
            hits.setdefault(r["symbol"].lower(), []).append(r)
    resolved, ambiguous = [], []
    for key, rs in hits.items():
        if len(rs) == 1:
            resolved.append({"symbol": rs[0]["symbol"], "name": rs[0]["name"], "isin": rs[0]["isin"], "matched": key})
        else:
            ambiguous.append({"matched": key, "candidates": [x["symbol"] for x in rs]})
    return {"resolved": resolved[:limit], "ambiguous": ambiguous[:limit]}


class NSESession:
    """Browser-like session for nseindia.com public JSON used by its own website.
    May be refused; callers must treat None as 'unavailable'."""

    def __init__(self):
        self.c = PoliteClient(min_interval=1.2, headers={
            "Accept": "application/json, text/plain, */*", "Accept-Language": "en-IN,en;q=0.9",
            "Referer": "https://www.nseindia.com/"})
        self.warm = False

    def _warm(self):
        if not self.warm:
            try:
                self.c.get("https://www.nseindia.com/")
            except Exception:
                pass
            self.warm = True

    def get_json(self, path: str) -> dict | list | None:
        self._warm()
        try:
            r = self.c.get("https://www.nseindia.com" + path)
            if r.status_code == 200:
                return r.json()
        except (Exception, json.JSONDecodeError):
            return None
        return None

    def quote(self, symbol: str) -> dict | None:
        return self.get_json(f"/api/quote-equity?symbol={symbol.upper()}")

    def announcements(self, symbol: str, days: int = 90) -> list[dict] | None:
        to = date.today()
        frm = date.fromordinal(to.toordinal() - days)
        data = self.get_json(f"/api/corporate-announcements?index=equities&symbol={symbol.upper()}"
                             f"&from_date={frm:%d-%m-%Y}&to_date={to:%d-%m-%Y}")
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return data["data"]
        return None

    def surveillance(self, symbol: str) -> dict | None:
        """Membership in NSE's published ASM (long/short term) and GSM lists. None if unavailable."""
        asm, gsm = self.get_json("/api/reportASM"), self.get_json("/api/reportGSM")
        if not isinstance(asm, dict) or not isinstance(gsm, list):
            return None
        sym = symbol.upper()
        lt = asm.get("longterm", {}).get("data", []) or []
        st = asm.get("shortterm", {}).get("data", []) or []
        hits = [f"{r.get('survDesc')} ({r.get('asmTime')})" for r in lt + st if r.get("symbol") == sym]
        hits += [f"{r.get('survDesc')} — GSM stage {r.get('gsmStage')} ({r.get('gsmTime')})" for r in gsm if r.get("symbol") == sym]
        as_of = (lt[0].get("asmTime") if lt else None) or (gsm[0].get("gsmTime") if gsm else None)
        return {"status": "listed" if hits else "not_listed", "detail": "; ".join(hits), "as_of": as_of,
                "counts": {"asm_long": len(lt), "asm_short": len(st), "gsm": len(gsm)}}

    def close(self):
        self.c.close()
