# Design notes: implementation status and open concerns

Build 0.2.0 · 15 November 2026. This file records how this build maps to
`Personal_CA_Platform_Final_Design_v2.md`, where it deliberately differs, and what needs a decision.

## Status against the roadmap

| Area | Status |
|---|---|
| Contracts (bundle, brain report, critique, decision record, capture envelope) | Built (Pydantic v2, `schema_version` on records) |
| Point-in-time bundle, canonical hash, timestamps (`published_at` / `first_seen_at` / `ingested_at`) | Built. Unknown publication time stays null; search "page age" is a hint only |
| Prices: Kite → bhavcopy fallback; corporate-action flag from NSE `PREV_CLOSE` | Built. Corporate-action *adjustment* is not built; a flag blocks dependent action instead |
| Technicals, chart patterns (double bottom/top, H&S, inverse H&S) | Built, re-implemented (the original skill scripts were not in this repo) |
| Surveillance (NSE ASM long/short + GSM lists) | Built. The NSE quote endpoint returns Access Denied, so it is not used (no workarounds) |
| NSE announcements, tiered news RSS, Claude web-search discovery (allow-listed) | Built |
| Financial facts | **Manual facts file only** (`data/manual/<SYMBOL>.yaml`). NSE/BSE XBRL ingestion is the next priority |
| Three brains in parallel, role views, anonymisation, Head draft → Critic → Head response | Built |
| Verified-quote citations, bounded repair (≤2), code-side objection resolution | Built |
| Rules: preflight + final enforcement, governance rubric, sizing, tranches, tax, consistency | Built (single-stock scope) |
| Ledger, evaluation contract at issuance, request records, cache by complete-request hash | Built |
| Research inbox: receipts, idempotency, versions, company resolution, claims, research Q&A | Built |
| Chrome extension 0.2.0 (side panel, ET-only optional permission, selection capture, send/export) | Built new (the 0.1.1 ZIP was not in this repo) |
| Local service: loopback, host check, pairing token, origin scoping, size limits | Built |
| Kite read-only adapter + login; Kite MCP for chat with order tools denied | Built |
| Modes `recommend`, `hot`, `replan`, `watch`, `eval`, `backtest` | **Not yet.** Only `analyse` is built |
| Evidence-repair pass (fetch new evidence after the critique, re-version the bundle) | **Not yet.** An open blocker that needs new evidence stays blocked (safe default) |
| Outcome labelling, scorecards, golden-case evals (L2–L4), improvement loop | **Not yet** |
| Lot-level tax, portfolio-wide planned-loss budget, stress view | **Not yet.** The budget is deliberately not invented (spec §9.6) |

## Added in 0.3.0 (user requests, 15 Nov 2026)

| Feature | How it maps to the spec |
|---|---|
| Per-role provider/model (Anthropic + OpenAI), live model lists, UI editor | Spec §12 "config-driven adapters". Vendor separation is now computed per decision, not a static flag. Gemini is not wired yet |
| 1–3 analysis loops | Generalises the spec's single bounded evidence-repair pass (§10.6). Each loop is a full re-run with targeted discovery for open questions and a parent-linked bundle. Only the final loop issues a verdict. **Spec deviation:** the spec allows one repair pass; you chose up to three (`config/user.yaml → analysis.max_loops`) |
| Best sources (5/8/10) | Display only. Ranking formula in `outputs.rank_sources`; nothing is dropped from the bundle |
| ET article queue + "scan page" + manual stock linking + auto claim extraction | The spec's planned "guided exploration" (§7): user-selected pages, visible and sequential, cancellable, stops at access challenges, at most 20 pending. Capture still happens only in the user's own browser |

## Added in 0.4.0 (user requests, 15 Nov 2026)

| Feature | Notes |
|---|---|
| Two editable ET lists (news, stock ideas) | `data/settings/et_lists.yaml`. Saving queues only uncaptured articles; "re-capture" creates new versions, never overwrites |
| Idea extraction + ranking + batch analysis | Model extraction (role `ideas`, prompt `ideas@1.0.0`) with verbatim-quote checks; a heuristic mention-only fallback when no key. A transparent formula ranks the stocks. Each pick goes through the full panel — spec §5: signals never override gates. This is effectively the spec's `recommend` mode, with your ET articles as the screen |
| Topic scans and higher limits | Queue limit 100 (spec default was 20), 15 links per topic scan, 400k chars of evidence per brain. **Spec deviation at your request.** The ET terms concern (below) applies more strongly the more is captured |
| Kite MCP as a platform data source | The hosted server requires MCP OAuth; implemented with PKCE and dynamic registration. **Verified live (0.5.0):** session status, holdings, instrument search, daily candles and LTP. The server exposes mode-based tools (`portfolio`, `market`, `session`, `orders`, `gtt`, `alerts`…); the read-only policy is a (tool, mode) allowlist, so orders, GTT, alerts and logout are refused in code. Kite sessions end daily around 6 AM, so reconnect each morning |
| Extension 0.5.0 | Background worker with a 1-minute heartbeat and opt-in auto-fetch. The platform shows install/connection state. "Fetch now" from the local page goes through `externally_connectable`, restricted to loopback origins. Manifest validated with Chrome's packer |

## Added in 0.6.0: Newsdesk (from references_news, 22 Nov 2026)

Ported into `ca_platform/newsdesk/`, with these changes to the original. Each fixes a spec conflict found in the review.

| Original Newsdesk | In the platform | Why |
|---|---|---|
| BSE API with browser-spoofed headers | NSE announcements + PDFs with an honest UA; BSE not used | Spec §6: identify the client, don't evade access controls (BSE returns Access Denied to honest clients) |
| Missing dates become "now" | Stay `None` | Spec §6 point-in-time |
| Confidence = noisy-OR over all outlets | One story per wording cluster; small capped bonus per independent story | Repeated coverage is not confirmation (spec §6/§7) |
| "Confirmed" if any filing is on the event | Only a same-metric filed figure within 5%; otherwise `filing_on_event` | Spec cross-check rule 6 |
| Any two ₹ amounts compared | Amounts tagged with the nearest metric keyword | Order value ≠ revenue |
| Denial = keyword in any filing | Only a clarification filing | Fewer false denials |
| Separate SQLite app, port and token | Same ledger, service, token and UI | One system, one audit trail |
| Live dossier re-built under the brains | Snapshotted into the frozen bundle | Same news for every brain |
| `[n]` citations + figure check | Our verbatim-quote citations; Newsdesk statuses as computed summary passages | Stronger accountability |
| Separate extension, auto-captures every Prime page, reads JSON-LD | Ours kept; ported density body detection, load retries and an **opt-in** "capture pages I open" | Spec §7: user-selected, no hidden JSON |

Not ported (yet): learned source trust (shown in config only), LLM labelling, alerts, multi-vendor news brains (the spec splits brains by job, not vendor).

## Deliberate refinements (please confirm)

1. **Rejecting an objection requires a verified quote.** If the Head rejects a Critic objection, code downgrades it to `unresolved` unless the rejection cites a passage whose quote is found verbatim. The spec says "a rhetorical reply is not a passed test"; this is the strict, code-checkable version.
2. **Only `blocker` objections block a buy.** Unresolved `major` objections are recorded and shown but do not block. If you want majors to block too, it is a one-line change in `panel.resolve` / `rules.engine.enforce`.
3. **Governance score is computed in code** (rubric `governance@1`) from the Forensic CA's check classifications. The model's own number is ignored. The score is withheld below 60% checklist coverage, so missing evidence never looks clean.
4. **Head score is not used for sizing.** Sizing is purely risk-based: the 2% planned loss at stop, with a 3% minimum stop-distance denominator, the 30% per-stock satellite cap and the 8% floor. The spec calls the score/risk weighting a baseline candidate; it needs a multi-stock `recommend` mode to matter.
5. **The benchmark gate needs a verified tax file.** After-tax figures are refused until `config/tax_rates.yaml` is marked verified, so every live buy is blocked until you check it. Pre-tax numbers are still shown.
6. **`policies_confirmed: false` blocks every actionable buy** until you review horizons, risk base and limits (spec §15 step 7).
7. **Server-side refusal fallback is enabled** on model calls (`fallbacks: "default"`). If a fallback model answers, the substitution is recorded in the decision record and shown in the Audit tab. The spec requires fallbacks to be pre-evaluated, so you may prefer to turn this off until evals exist.

## Concerns to discuss

1. **Vendor separation.** All roles default to Anthropic. Since 0.3.0 you can move any role, e.g. the Critic, to OpenAI from System → Models; the decision record shows `separated`, `partial` or `degraded`. OpenAI models are not hard-coded: the list comes live from your key. Fallbacks across providers are not automatic.
2. **ET Prime rights.** The extension captures only what you can already read and records your personal-research attestation, but ET's terms on extraction, AI processing and retention remain unresolved (spec §7, §16). The attestation records your decision; it does not settle the legal question. Unattested captures are stored but never sent to a model.
3. **Fundamentals: now filed data, with known gaps.** Since 0.7.0, results and shareholding come from the company's NSE XBRL filings (see below). Still missing: insider trades (NSE's feed stopped in Mar-2025), contingent liabilities, slippages/restructuring and CWIP detail (annual-report / investor-presentation items). No sector or own-history multiples yet, so Quant can say what a P/E *requires*, not whether it is cheap versus history.
4. **Tax values are as of the July 2024 changes** (STCG 20%, LTCG 12.5% above ₹1.25 lakh, plus cess). Budgets since then may have changed them. Verify before marking the file verified.
5. **Cost.** A live run makes about 8 Claude Opus 5.5 calls plus up to 9 web searches. All roles default to `claude-opus-5-5`; you can move the brains to `claude-sonnet-5-5` in `config/models.yaml` to cut cost. The monthly cap (`config/user.yaml`) is enforced before each run.
6. **Kite Connect historical data is paid** (₹500/month per app, per the spec's check; verify current terms). Without it, prices come from NSE bhavcopy: end-of-day, unadjusted, with a ~5 minute one-time backfill.
7. **NSE endpoints change.** Announcements and the ASM/GSM lists work today. If NSE starts refusing them, the run says so and surveillance becomes `unknown`, which blocks buys. A source can also be recorded manually in the facts file.
8. **The original skill's scripts and the 0.1.1 extension were not in this repo.** If you have them, we should reconcile the re-implemented features, patterns and allocation against them and port their tests.


## 0.7.0 — Filed financials (NSE XBRL)

Finding: NSE's `results-comparision` and `corporates-financial-results` JSON stop at **Dec-2024**. Results since then are SEBI **Integrated Filing — Financials**, published as XBRL on nsearchives. The pledge endpoint returns empty even for companies with pledges, and the insider (PIT) feed has nothing after Mar-2025. So:

| Data | Source used | Notes |
|---|---|---|
| Quarterly P&L, EPS, balance sheet (half-yearly), YTD cash flow | Integrated Filing XBRL, last 5 quarters | Consolidated for companies, standalone for banks (regulatory ratios) |
| Bank asset quality: GNPA/NNPA %, GNPA amount, CET-1, RoA | Same XBRL (banking format) | PCR is approximate: net NPA amount derived from the rounded %, labelled so |
| Audit opinion, auditors, related-party totals | Same XBRL | Only an **audited** filing's declaration feeds the audit rule; auditor names normalised before change detection |
| Promoter holding, pledge, FPI/DII/MF | Shareholding-pattern XBRL, last 4 quarters | Pledge % of promoter holding computed from share counts |
| Insider trades | — | Reported as unavailable, not shown as current |

- Point in time: only filings broadcast before the decision time; a later revision replaces the original only after it was filed.
- Ratios (`fundamentals@1`, code): TTM EPS, P/E, earnings yield, P/B, book value, ROE, YoY growth, other-income share, effective tax, exceptional-item frequency, OCF/profit, receivable days, D/E, dilution; banks: PCR, deposit/advance growth. Each line names its formula and the filing evidence ids. Missing quarter / negative EPS → "not computed" with a reason.
- A filed value is `verified` only when the filing's own arithmetic reconciles (income = revenue + other income; PBT − tax = profit).
- Rules: pledge now uses the filed series (not applicable without a promoter group; blocks above 10% or when rising 2 quarters, the existing `rising_quarters` setting). Audit passes on a filed unmodified opinion within the lookback and blocks on a modified one; a Forensic "fail" still blocks.
- Manual facts win a tie (same metric and period).
- Prompts: `quant@2.1.0` (use the computed valuation lines; no imported multiples), `forensic@2.1.0` (check → evidence map; insider part unverified).
- UI: run **Numbers** tab + valuation box on Overview. CLI: `ca fundamentals SYMBOL [--price X]`; API `/api/fundamentals/{symbol}`.

### Setup gates vs stock verdict (0.7.1)

`policies_confirmed`, an unverified tax file (when pre-tax already beats the benchmark) and unconfigured capital are **setup gates**. When they are the only blockers of a Head BUY, the verdict stays **BUY — on hold until setup is complete** (not actionable) instead of being relabelled "insufficient data". Any stock-specific block still wins (e.g. surveillance → AVOID). `head@2.2.0` tells the Head not to let setup gates shape its decision. "Insufficient data" is now shown as **"Don't buy yet — not enough evidence"**.

## 0.8.0 — Every analysis ends in a clear verdict (agreed design)

Code decides whether the evidence is enough; the Head decides the direction.

1. **Red flag about the stock** (surveillance list, pledge above limit or rising, modified audit opinion, governance below minimum, liquidity, short-term confirmed bearish pattern) → **AVOID**, whatever the Head proposed.
2. **Critical evidence checklist** (`rules.engine.evidence_gate`), by horizon:
   - all horizons: surveillance status known; shareholding and pledge known
   - short: ≥60 daily bars and technicals computed (candlesticks, chart patterns, support/resistance, RSI/MACD)
   - medium/long: ≥200 daily bars; latest results ≤100 days old (`quarterly_freshness_days`); EPS or book value computable; audit opinion within 15 months
   Missing item → **INSUFFICIENT DATA**, listing each item and how to get it.
3. **Checklist complete** → the Head must choose **BUY, WAIT or AVOID**. An insufficient-data answer is sent back once with the checklist; a second refusal marks the run **incomplete** with the Head's stated reason.
- Every verdict carries its "what next", checked in code and repaired if missing: WAIT → a checkable **trigger** (price level + above/below, a date, or a specific filing/event); AVOID → reason + what would change the view; INSUFFICIENT → the specific evidence. When the rules turn a buy into a wait, code sets the trigger (e.g. the price where reward/risk reaches the minimum).
- **Confidence** (code): High, lowered one step per important gap, never below Low. Gaps: insider trades, news coverage, contingent liabilities, related-party detail, bank slippages. It does not change position size.
- Prompt `head@2.3.0`. UI: verdict card shows trigger / reason / missing items and confidence; Rules tab shows how the verdict was reached.
