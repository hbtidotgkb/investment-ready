"""Newsdesk: per-stock news collection, story merging, claim checking against filings and
ranking. Used by the analysis pipeline (News, Forensic and Quant brains) and usable on its own
from the Newsdesk page and `ca news`.

Adapted from references_news (Newsdesk guide + code) with these deliberate changes for the
platform spec: official filings come from NSE (honest client; BSE refuses one); unknown
publication times stay unknown; repeated coverage is not counted as independent confirmation;
press amounts are compared with filings only when they measure the same thing; a dossier is
snapshotted into the frozen bundle so every brain reads the same news.
"""
