import asyncio
import json

import pytest

from ca_platform.data import sr_plus as S
from ca_platform.evidence import EvidenceFactory

# Figures as printed in the Infosys report dated 2026-11-22.
INFY = {
    "company_line": "INFOSYS (INFY-IN)", "ticker": "INFY-IN", "report_date": "2026-11-22", "last_close": 1098.0,
    "last_close_date": "2026-11-20", "trailing_pe": 14.5, "forward_pe": 14.2, "score_average": 8, "score_earnings": 1,
    "score_fundamental": 9, "score_relative_valuation": 7, "score_risk": 10, "score_price_momentum": 7,
    "ibes_mean": "hold", "ibes_analysts": 42, "ibes_strong_buy": 6, "ibes_buy": 11, "ibes_hold": 21, "ibes_sell": 4,
    "ibes_strong_sell": 0, "target_mean": 1197.65, "target_high": 1627.0, "target_low": 908.0, "target_vs_current_pct": 9.1,
    "target_analysts": 40, "surprises_positive_last4": 1, "surprises_negative_last4": 1, "surprises_inline_last4": 2,
    "surprise_avg_last4_pct": 0.25, "revisions_up_4w": 0, "revisions_down_4w": 8, "revisions_avg_up_pct": 0.0,
    "revisions_avg_down_pct": -19.0, "broker_upgrades_120d": 0, "broker_downgrades_120d": 15,
    "surprise_detail": [
        {"type": "In-Line", "announce_date": "2026-10-23", "period_end": "2026-09-30", "actual_eps": 19.30, "mean_eps": 19.52, "surprise_pct": -1.1},
        {"type": "Positive", "announce_date": "2026-04-23", "period_end": "2026-03-31", "actual_eps": 20.98, "mean_eps": 18.49, "surprise_pct": 13.5},
        {"type": "Negative", "announce_date": "2026-01-14", "period_end": "2025-12-31", "actual_eps": 16.14, "mean_eps": 18.12, "surprise_pct": -10.9}],
    "estimate_trend": [
        {"period": "Q 26-12", "current": 18.70, "days_30_ago": 19.11, "days_90_ago": 19.14, "change_90d_pct": -2.3},
        {"period": "Y 2027", "current": 76.20, "days_30_ago": 76.49, "days_90_ago": 76.41, "change_90d_pct": -0.30},
        {"period": "Price Target", "current": 1197.65, "days_30_ago": 1185.13, "days_90_ago": 1201.03, "change_90d_pct": -0.30}],
    "metrics": [{"name": "Valuation: Trailing PE 5-Yr Average", "value": 25.1, "unit": "x"},
                {"name": "Risk: Beta vs. BSE SENSEX", "value": 0.64}],
    "peers": [{"ticker": "TCS", "average_score": 8, "trailing_pe": 16.7, "ibes_mean": "Buy"}],
    "highlights": [{"section": "Earnings", "text": "Infosys currently has an Earnings Rating of 1.", "page": 3}],
}


def rec(d=INFY):
    return {"file": "infy.pdf", "sha256": "ab" * 32, "extractor": S.EXTRACTOR, "extracted_at": "2026-11-22T19:30:00+05:30",
            "model": "claude-opus-5-5", "data": S.SRPlusLLM.model_validate(d).model_dump()}


def test_report_arithmetic_recomputes_for_the_infosys_report():
    checks = S.arithmetic_checks(rec()["data"])
    assert checks and all(c["ok"] for c in checks), [c for c in checks if not c["ok"]]
    names = {c["check"] for c in checks}
    assert {"analyst_count", "target_upside", "target_range", "surprise_counts", "surprise_2026-03-31"} <= names


def test_transcription_errors_are_caught():
    bad = dict(INFY, ibes_hold=12, target_vs_current_pct=19.0)
    a = S.assess(rec(bad), "INFY", "Infosys Limited", "2026-11-23T10:00:00+05:30")
    assert a["status"] == "unreliable" and not a["used"] and a["failed_checks"] == 2


def test_company_must_match_folder():
    a = S.assess(rec(), "TCS", "Tata Consultancy Services Limited", "2026-11-23T10:00:00+05:30")
    assert a["status"] == "mismatch" and not a["used"]
    ok, _ = S.company_matches({"company_line": "INFOSYS (INFY-IN)", "ticker": None}, "INFY", None)
    assert ok
    ok, _ = S.company_matches({"company_line": "MAHINDRA & MAHINDRA (MAHM-IN)", "ticker": "MAHM-IN"}, "M&M", "Mahindra & Mahindra Limited")
    assert ok


@pytest.mark.parametrize("when,status", [("2026-11-23", "available"), ("2026-12-12", "stale"), ("2026-12-30", "expired"),
                                         ("2026-11-20", "unreliable")])
def test_freshness(when, status):
    assert S.assess(rec(), "INFY", "Infosys Limited", when + "T10:00:00+05:30")["status"] == status


def test_cross_checks_against_our_numbers_warn_only():
    a = S.assess(rec(), "INFY", "Infosys Limited", "2026-11-23T10:00:00+05:30", our_close=1180.0, our_pe=19.0)
    assert a["status"] == "available"
    assert any("from our last close" in w for w in a["warnings"]) and any("trailing P/E" in w for w in a["warnings"])


def test_passages_are_written_by_code_and_attributed():
    info = {**rec(), **S.assess(rec(), "INFY", "Infosys Limited", "2026-11-23T10:00:00+05:30")}
    fac = EvidenceFactory()
    ev = S.to_evidence(fac, info, "INFY")
    text = "\n".join(p.text for p in ev.passages)
    assert ev.tier == "T2_licensed_data" and ev.kind == "broker_research"
    assert "0 upgrades, 15 downgrades" in text and "8 down (average -19.00%)" in text
    assert "not our valuation" in text and "hold from 42 analysts" in text
    assert "Code checks on this transcription" in ev.passages[-1].text


def test_newest_usable_report_is_current_and_older_ones_become_history():
    old = rec(dict(INFY, report_date="2026-10-20", target_mean=1300.0, target_vs_current_pct=None, broker_downgrades_120d=4)) | {"file": "old.pdf"}
    info = S.select([old, rec()], "INFY", "Infosys Limited", "2026-11-23T10:00:00+05:30")
    assert info["report_date"] == "2026-11-22" and info["status"] == "available"
    assert [h["report_date"] for h in info["history"]] == ["2026-10-20", "2026-11-22"]
    assert info["others"][0]["status"] == "expired"
    text = "\n".join(S.passages(info))
    assert "Earlier SR Plus reports you saved" in text and "mean target Rs 1,300.00" in text and "broker changes 0 up / 4 down" in text


def test_no_sr_plus_report():
    info = S.select([], "INFY", None, "2026-11-23T10:00:00+05:30")
    assert info["status"] == "missing" and not info["used"]


def test_printed_text_is_converted_by_code():
    tx = {"values": [{"key": "company_line", "text": "INFOSYS (INFY-IN)", "page": 1},
                     {"key": "report_date", "text": "2026-Nov-22", "page": 1},
                     {"key": "last_close_date", "text": "2026 November 20", "page": 1},
                     {"key": "last_close", "text": "1,098.00", "page": 1},
                     {"key": "score_earnings", "text": "1", "page": 3},
                     {"key": "ibes_mean", "text": "Hold", "page": 1},
                     {"key": "revisions_avg_down_pct", "text": "-19.0%", "page": 3},
                     {"key": "target_vs_current_pct", "text": "9.1%", "page": 3},
                     {"key": "score_risk", "text": "NR", "page": 8},
                     {"key": "made_up", "text": "1", "page": 2}],
          "rows": [{"table": "surprise_detail", "page": 4, "cells": ["In-Line", "2026-10-23", "2026-09-30", "19.300", "19.520", "\u22121.1%"]},
                   {"table": "metrics", "page": 6, "cells": ["Valuation: Price to Sales Rel. to 5-Yr Avg.", "44", "% discount", ""]},
                   {"table": "peers", "page": 2, "cells": ["TCS", "8", "2,376.20", "-3.4%", "3.4%", "-24.4%", "8.6T", "16.7", "15.3", "2.8%", "18.2%", "5.4%", "Buy", "40"]}],
          "highlights": [{"section": "Earnings", "text": "Infosys currently has an Earnings Rating of 1.", "page": 3}],
          "unreadable": []}
    d, notes = S.normalize(tx)
    assert d["report_date"] == "2026-11-22" and d["last_close_date"] == "2026-11-20" and d["last_close"] == 1098.0
    assert d["score_earnings"] == 1 and d["score_risk"] is None and d["ibes_mean"] == "hold"
    assert d["revisions_avg_down_pct"] == -19.0 and d["surprise_detail"][0]["surprise_pct"] == -1.1
    assert d["metrics"][0]["value"] == 44.0 and d["metrics"][0]["unit"] == "% discount" and d["metrics"][0]["page"] == 6
    assert d["peers"][0]["price"] == 2376.2 and d["peers"][0]["analysts"] == 40 and d["peers"][0]["ibes_mean"] == "Buy"
    assert any("made_up" in n for n in notes) and not any("score_risk" in n for n in notes)   # "NR" = printed no-rating


def test_transcript_schema_has_no_optional_fields():
    def walk(schema, defs):
        for name, sub in (schema.get("properties") or {}).items():
            assert name in schema.get("required", []), name
            assert "anyOf" not in sub, name
        for d in defs.values():
            for name in (d.get("properties") or {}):
                assert name in d.get("required", []), name
    s = S.SRTranscriptLLM.model_json_schema()
    walk(s, s.get("$defs", {}))
