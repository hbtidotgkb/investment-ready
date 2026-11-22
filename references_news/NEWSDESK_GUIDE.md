# Newsdesk and the News Brain: setup and usage guide

Newsdesk collects stock news from exchange filings and the financial press, checks
press claims against the filings, scores each event, and builds a cited **dossier**
per stock. The **news brain** reads that dossier and writes a structured, cited news
view for the Head CA.

This guide covers everything needed to run both on your own machine.

**Contents**
1. How it fits together
2. Install and run
3. Running on your cloud VM
4. Sources: what's used and why
5. How Newsdesk works
6. Configuration
7. Adding stocks
8. The UI
9. ET Prime capture extension
10. API reference
11. The news brain
12. Worked example: BSE Ltd
13. TradingView MCP and the nse-bse-mcp server
14. Known gaps and next steps
15. Troubleshooting
16. Usage and legal notes
17. Operations: logs, backups, costs
18. Roadmap: getting quality news

---

## 1. How it fits together

```
 Exchange filings (BSE, NSE, SEBI, RBI) ─┐
 Company news search (by publisher)      ├─► Newsdesk ─► dossier per stock ─► News brain ─► news view
 Market news feeds (ET, BS, Mint, MC)    │   (stores,        (ranked events,     (Claude /     (JSON, cited)
 ET Prime articles you open (extension) ─┘    checks,        citations,          Gemini /          │
                                              scores)        coming up)          ChatGPT)          ▼
                                                                                              Critic (script)
                                                                                                   │
                     Fundamentals brain ──┐                                                        ▼
                     Technicals brain   ──┼──────────────────────────────────────────────────► Head CA ─► you decide
                     (Kite, filings)    ──┘
```

- **Newsdesk** does the slow, careful work in the background: fetching, matching
  news to stocks, merging duplicates, checking claims against filings, and ranking.
- **The news brain** is an LLM call. It reads one prepared dossier, follows fixed
  rules, and answers in a fixed format. It doesn't browse or remember anything.
- **The critic** is plain code. It checks that every point the brain makes is
  backed by a real citation.
- **The Head CA** weighs the news view against the fundamentals and technicals views.

### Terms used in this guide

| Term | Meaning |
|---|---|
| **Head CA** | The final analyst step (an LLM prompt or a person) that combines the news, fundamentals and technicals views into one verdict. It is outside Newsdesk. |
| **Fundamentals brain** | A separate step that reads financial statements and ratios (Screener, filings). Not covered in this guide. |
| **Technicals brain** | A separate step that reads price and volume data (Kite). Not covered in this guide. |
| **Kite** | Zerodha's trading and market-data API, used for prices. |
| **Dossier** | Newsdesk's ranked, cited summary of one stock's recent events. |
| **News view** | The news brain's JSON answer for one stock (section 11). |
| **Tier** | How authoritative a source is: 0 for exchange and regulator filings, 1 for major press, 2 for everything else. |
| **Trust** | A 0–1 score per source, learned from how often its claims match later filings. |
| **Salience** | How much an article is about a given stock (section 5, step 2). |

---

## 2. Install and run

You need Python 3.10 or newer.

```
unzip newsdesk.zip
cd newsdesk
python3 -m venv .venv
. .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -r requirements.txt   # requests, pyyaml, pypdf
python3 -c "import secrets; print(secrets.token_urlsafe(24))"   # copy the output
export NEWSDESK_INGEST_TOKEN=<the token you just generated>      # Windows: set NEWSDESK_INGEST_TOKEN=...
python run.py serve
```

Open **http://127.0.0.1:8765**. The first visit to a stock fetches from every
source, which can take 20–30 seconds.

### First run checklist

1. **Edit `config/watchlist.yaml` before the first serve.** Replace the sample stocks
   with your own, each with its `bse_scrip` code (section 6). Without the scrip code a
   stock gets no BSE filings.
2. **Set the ingest token** (above) and keep a copy. You'll paste the same value into
   the extension (section 9). The token has to be exported in every new shell, so put
   it in your shell profile or the systemd unit (section 3).
3. **Run `python run.py check <TICKER>`** for one stock and confirm that at least BSE
   and the company news search return rows.
4. **Open the Sources tab** and fix any feed that shows an error (feed URLs change).
5. **Optionally set `ANTHROPIC_API_KEY`** for better labels. Read section 17 on costs
   first.

### Commands

| Command | What it does |
|---|---|
| `python run.py serve` | UI and API, with the background worker |
| `python run.py serve --no-worker` | UI and API only; fetching on demand still works |
| `python run.py fetch` | Poll every source once and exit |
| `python run.py check RELIANCE` | Test every source for one stock: what came back, what matched, what failed |
| `python run.py dossier LT --md` | Print a dossier |
| `python run.py nightly` | Run the nightly re-score and trust update now |
| `python news_brain.py RELIANCE` | Run the news brain on a stock (section 11) |
| `python -m unittest discover tests` | Offline tests (22 at the time of writing; the count grows as tests are added) |

### Settings (environment variables)

| Variable | Purpose |
|---|---|
| `NEWSDESK_INGEST_TOKEN` | Secret the extension must send. **Set this** (section 9). |
| `ANTHROPIC_API_KEY` | Better event labels and summaries in Newsdesk; also the Claude news brain |
| `NEWSDESK_DB` | Database location (default `data/newsdesk.db`) |
| `NEWSDESK_URL` | Where `news_brain.py` finds Newsdesk (default `http://127.0.0.1:8765`) |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | ChatGPT as a news brain |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | Gemini as a news brain |

Generate the ingest token with:

```
python3 -c "import secrets; print(secrets.token_urlsafe(24))"
```

---

## 3. Running on your cloud VM

Run it on the VM so it keeps collecting while your laptop sleeps. Otherwise you
miss the after-market results, which are filed in the evening.

```
tmux new -s newsdesk
cd newsdesk && . .venv/bin/activate
export NEWSDESK_INGEST_TOKEN=<your token>
python run.py serve
# Ctrl+B, then D, to detach
```

Newsdesk only listens on the VM itself. To reach it from your laptop, open an SSH tunnel:

```
ssh -L 8765:127.0.0.1:8765 user@your-vm
```

Then http://127.0.0.1:8765 on your laptop reaches the VM's Newsdesk. The extension
uses the same address, so keep the tunnel open while you read ET Prime.

### Keeping it running after a reboot (systemd)

tmux stops when the VM reboots, and after-market filings are missed until you notice.
A systemd service restarts Newsdesk on boot and after a crash. Adjust the user and paths:

```ini
# /etc/systemd/system/newsdesk.service
[Unit]
Description=Newsdesk
After=network-online.target
Wants=network-online.target

[Service]
User=youruser
WorkingDirectory=/home/youruser/newsdesk
EnvironmentFile=/home/youruser/newsdesk/.env
ExecStart=/home/youruser/newsdesk/.venv/bin/python run.py serve
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Put the settings in `.env`, one per line, and make it readable only by you
(`chmod 600 .env`):

```
NEWSDESK_INGEST_TOKEN=...
ANTHROPIC_API_KEY=...
TZ=Asia/Kolkata
```

Then:

```
sudo systemctl daemon-reload
sudo systemctl enable --now newsdesk
journalctl -u newsdesk -f          # follow the logs
```

**Time zone.** Polling phases and the 07:00, 08:45 and 23:00 jobs are in IST. If the VM
runs in UTC, confirm that Newsdesk converts to IST, or set `TZ=Asia/Kolkata` as above.
Check by watching the logs around 07:00 IST for the morning sweep.

---

## 4. Sources: what's used and why

### In Newsdesk

| Source | What it gives | Tier | Starting trust |
|---|---|---|---|
| BSE corporate announcements | Every filing; the attached PDF is read for its figures | 0 | 0.98 |
| NSE announcements (RSS) | NSE filings, headlines only | 0 | 0.98 |
| SEBI | Orders, notices, circulars | 0 | 0.98 |
| RBI | Press releases, penalties on banks | 0 | 0.97 |
| Company news search (Google News) | Recent press about one stock, credited to the real publisher | per publisher | — |
| Economic Times Markets | Market-wide headlines | 1 | 0.80 |
| ET Prime | Headlines from the feed; full text through the extension | 1 | 0.85 |
| Business Standard | Market-wide headlines | 1 | 0.80 |
| Mint | Market-wide headlines | 1 | 0.78 |
| Moneycontrol | Market-wide headlines | 1 | 0.70 |
| Reuters, CNBC-TV18, BusinessLine, Financial Express, NDTV Profit | Through the company search | 1 | 0.72–0.90 |
| Other outlets | Anything else the search finds | 2 | 0.55 |

**Tier 0 is ground truth.** Listed companies must file material events with the
exchanges under SEBI's listing rules. Press claims are checked against these filings.

**NSE's 0.98 is provisional.** The NSE RSS carries headlines only and its URL is
unverified (section 14, gap 1). Until a full NSE filings source replaces it, treat an
NSE headline as weaker evidence than a BSE filing whose PDF was read, even though both
start at the same trust. Consider lowering its starting trust in `sources.yaml` until
then.

**Trust is learned.** Every night, each press source's trust moves toward its record:
claims later confirmed by a filing raise it, mismatches lower it. You can see each
source's record in the Sources tab.

Trust only moves when a press claim is later matched or contradicted by a filing. That
is rare for most publishers, especially while most press items are headline-only
(gap 5). Expect the starting values to dominate for months, and check a source's
record count in the Sources tab before reading much into its trust.

**Why the RSS feeds aren't enough on their own.** They only carry the latest
market-wide headlines, so they rarely have anything about one particular stock. The
company search is what finds a stock's news.

### Where the filing PDFs come from

From BSE's own servers. Each announcement links to the document the company filed:

```
https://www.bseindia.com/xml-data/corpfiling/AttachLive/<file>.pdf
```

For watchlist stocks, Newsdesk downloads it and reads up to 6 pages (20,000 characters).
That's how "Disclosure under Regulation 30" becomes "received an order worth ₹2,100 crore".

Limits:
- **Scanned PDFs** have no text layer and yield nothing.
- **Long reports** are cut off after the limit.
- **NSE filings** arrive without their PDF.

### Deliberately not used in Newsdesk

| Source | Why not |
|---|---|
| TradingView | Its terms don't allow storing its data in a database. Use its MCP in chat instead (section 13). |
| Bloomberg, Reuters/LSEG feeds | Enterprise pricing; Indian press relays their India stories quickly |
| Social media, Telegram, forums | Too noisy; a rumour-signal layer could be added later, but never feeding the dossier directly |
| Screener, CMIE | Fundamentals, not news; they belong to the fundamentals brain |

---

## 5. How Newsdesk works

### Two ways of running

**In the background (the worker).** Every 30 seconds it checks which sources are due.
Polling frequency depends on the market phase (IST):

| Phase | Hours | Filings | Press |
|---|---|---|---|
| Overnight | 22:00–07:00, weekends | every 30 min | 30–60 min |
| Pre-market | 07:00–09:15 | every 10 min | 10–20 min |
| Market | 09:15–15:30 | every 2 min | 5–15 min |
| Post-market | 15:30–22:00 | every 2 min | 5–15 min |

It also runs three fixed jobs:
- **07:00:** sweep every source.
- **08:45:** sweep again, then pre-build every watchlist dossier, ready for the open.
- **23:00:** nightly maintenance: decay scores, re-check unconfirmed claims against
  the day's filings, and update source trust.

Whenever a poll changes a stock's events, that stock's dossier is rebuilt straight away.

**On demand.** When you open a stock or request its dossier, Newsdesk checks how fresh
its data is. If it's older than 2 minutes during market hours (30 minutes otherwise),
it first:
- pulls that stock's BSE filings
- runs the company news search
- makes one pass over the feeds

Then it builds the dossier. **Refresh now** skips the freshness check.

### What happens to each article

1. **Drop paid and routine items.** Sponsored content ("Brand Connect", "partner
   content") and paperwork filings (trading-window closures, newspaper copies,
   share-certificate notices) are kept on record but not used.
2. **Match to stocks.** This uses the stock's names (section 6), its BSE scrip code,
   or a symbol tag. Each match gets a salience score:
   - 1.0: the article is about the company
   - 0.4: it only names a subsidiary or asset
   - below 0.3: a passing mention, which is dropped
3. **Read the filing PDF** for tier-0 matches.
4. **Label it.** The event type (results, order win, regulatory action, governance
   concern…), direction, and fact versus speculation versus opinion. It also extracts
   rupee amounts and up to 3 checkable claims. This step is rule-based, or uses Claude
   if `ANTHROPIC_API_KEY` is set.
5. **Merge into an event.** Articles about the same thing within a few days become
   one event with several citations.
6. **Check against filings.** Each press claim becomes:
   - **filed:** from the filing itself
   - **confirmed:** a filing matches it, amounts within 5%
   - **contradicted:** a filing gives a different figure
   - **denied:** the company filed a clarification denying it
   - **unconfirmed:** no filing yet
   - **investigative:** a governance or accounting report from the press, such as ET Prime
7. **Score it:**
   ```
   score = materiality × confidence × recency
   ```
   - **Materiality:** set by event type, raised when the rupee amount is large
     relative to market cap.
   - **Confidence:** grows with independent sources and filings; cut for denied,
     contradicted, speculation, opinion, or headline-only events.
   - **Recency:** halves on a schedule that depends on event type. Analyst views
     fade in 3 days; regulatory actions last about 2 months; governance concerns about 3 months.
     These figures are approximate half-lives: after one half-life the recency weight
     is 0.5, after two it is 0.25. The exact values per event type, and the ranges
     of materiality and confidence, are set in `scoring.py`; read them there before
     tuning, and record any change you make in that file's comments.
8. **Alert** on new filings, high-impact events, and press reports a filing contradicts.

### What's in a dossier

| Part | Contents |
|---|---|
| Events | Up to 15, ranked by score, each with status, type, claims and citations |
| Coming up | Results dates, record dates and earnings calls from the company's own notices |
| Check these first | Contradicted or denied reports, and events where sources disagree on direction |
| Not known yet | Unconfirmed material events, headline-only articles, missing filings |
| Citations | Numbered sources: publisher, tier, link, times, a quote, and the SHA-256 hash of the saved text |

In the text form a brain reads, the "Not known yet" part appears under the heading
`## Not yet known` (see the example in section 12). They are the same part.

### Claim statuses and how the UI shows them

The pipeline and the UI use different names for the same statuses:

| Pipeline status | UI label | Strength as evidence |
|---|---|---|
| filed | Company filing | Strong |
| confirmed | Confirmed by filing | Strong |
| unconfirmed | Press only | Weak |
| contradicted | Doesn't match the filing | Counts against the report |
| denied | Denied by the company | Counts against the report |
| investigative | Investigative report | Weigh case by case; not a filing, but often the first sign of a governance issue |

### Fact, speculation and opinion

- **Fact:** reports something that happened or a reported figure ("profit rose 45%",
  "received an order"), whether or not a filing has confirmed it yet.
- **Speculation:** something that may happen ("in talks", "likely to", "sources say").
- **Opinion:** a judgement or recommendation (ratings, targets, "a good buy").

A headline that states a reported figure should be labelled fact, even when it comes
from a price-move story. If you see one labelled opinion, that is a labelling bug
(section 14, gap 4).

---

## 6. Configuration

### `config/sources.yaml`

| Key | Purpose |
|---|---|
| `sources` | Each source's id, tier, starting trust, feed URL and polling cadence per market phase |
| `gnews.publishers` | Maps publisher names in search results to sources, which sets their tier and trust |
| `routine_filings` | Regex patterns for paperwork filings to skip |
| `paid_markers` | Phrases that mark sponsored content |
| `provider_names` | Publisher mapping for items posted through the batch API |
| `llm` | Model used for labelling when `ANTHROPIC_API_KEY` is set |
| `freshness` | How stale data can be before an on-demand request fetches first |

Feed URLs change from time to time. After the first run, check the Sources tab and
fix any feed that shows an error.

### `config/watchlist.yaml`

```yaml
- ticker: RELIANCE                # NSE symbol
  name: Reliance Industries
  bse_scrip: "500325"             # from the company's bseindia.com page
  aliases: [Reliance Industries, RIL]          # identify the company; used for news search
  link_only: [Reliance]                        # press shorthand, too broad to search with
  related: [Jio Platforms, Reliance Jio, Jio, Reliance Retail, Jamnagar refinery]
  exclude: [Reliance Power, Reliance Infrastructure, Reliance Capital, Anil Ambani, ...]
  market_cap_cr: 1900000          # rough, in ₹ crore; sizes how material an amount is
```

- **`aliases`:** names that identify the company. They're also used for the news search.
- **`link_only`:** shorthand like "Reliance" or "HDFC". These catch most headlines,
  but only work safely with a good `exclude` list.
- **`related`:** subsidiaries, brands and assets. A headline naming only these is
  kept, ranked below news about the company itself.
- **`exclude`:** look-alikes, removed before matching, so "Reliance Power" never
  matches "Reliance".

---

## 7. Adding stocks

In the UI, use "Add a stock", or edit `watchlist.yaml` and restart. Recent articles
that matched nothing are re-checked for the new stock, so news from the last 30 days
links straight away.

Always give the **BSE scrip code** where one exists. Without it, the stock gets no
BSE filings, which means no tier-0 checks and no "Coming up". Find it on the
company's page at bseindia.com.

A few companies list only on NSE, such as BSE Ltd itself. They depend on NSE
filings, which are weak today (section 14).

---

## 8. The UI

- **Left:** watchlist (a red count means unseen alerts), "Add a stock", and the views:
  Dossier, Alerts, Sources.
- **Centre:** the dossier.
  - **Time window:** 7, 30, 90 or 180 days.
  - **Copy for brain:** copies the text form that the news brain reads.
  - **Refresh now:** fetches regardless of freshness.
  - The status line says how many items came from how many sources, and names any
    source that couldn't be reached.
- **Each event:**
  - Its status in colour: Company filing, Confirmed by filing, Press only, Doesn't
    match the filing, Denied by the company, or Investigative report.
  - Claims with numbered citations.
  - A strip of marks, one per source: solid for a filing, outlined for press,
    dashed for headline only.
  - Its weight and confidence.
- **Right:** the sources cited. Clicking a citation number highlights its source.
  "View saved copy" shows the exact text stored, with its hash.

---

## 9. ET Prime capture extension

The feed only gives ET Prime headlines. The extension adds the full text of the
articles you open. It never fetches, scrolls or logs in by itself.

### Install

1. Open `chrome://extensions` and turn on Developer mode.
2. Click "Load unpacked" and pick the `extension` folder. For an update, click the
   reload icon on "Newsdesk capture" instead.
3. Click the extension icon. Enter the Newsdesk address if it's not
   `http://127.0.0.1:8765`, paste your ingest token, and click "Save settings".

### Use

Open an ET Prime article while logged in. Within about 15 seconds the icon shows:
- **✓** sent
- **!** Newsdesk not reachable, or wrong token
- **?** too little text found

The popup lists the last 25 captures, each with a line saying which stocks it linked
to, or "Saved; no watchlist stock mentioned". **Capture this page** reruns the capture
on the current tab and reports how many characters it found, and how.

### Where captured articles go

Into Newsdesk's database. A captured article:
- replaces its headline-only record
- links to any watchlist stocks it names
- re-scores the event

If it names no watchlist stock, it's stored unlinked, and adding that stock later links it.

To list recent captures:

```
sqlite3 data/newsdesk.db "SELECT id, title, dropped_reason FROM articles WHERE source_id='et_prime' ORDER BY id DESC LIMIT 10"
```

To read one: `http://127.0.0.1:8765/api/article/<id>`.

### Why set the token

Any website open in your browser can send requests to `127.0.0.1`. Without a token,
a malicious page could post fake articles into your dossiers, and the brains would
treat them as real. The token only guards the capture endpoints.

**The other write endpoints are not guarded.** `POST /api/watchlist` and
`POST /api/alerts/seen` change state too. A malicious page that reached
`POST /api/watchlist` could add the real company name to a stock's `exclude` list, or
fill `related` with junk, and quietly corrupt its dossier without posting a single
article. Browsers may block such a request when the server only accepts
`Content-Type: application/json`, because that forces a preflight check, but don't
rely on it. Until the token covers every POST endpoint (section 14, gap 9):
- keep Newsdesk bound to `127.0.0.1` only
- don't leave the SSH tunnel open while browsing untrusted sites
- check `watchlist.yaml` if a stock's events suddenly disappear

---

## 10. API reference

| Method and path | Purpose |
|---|---|
| `GET /api/dossier/{TICKER}` | Dossier as JSON. `?refresh=auto` (default), `force` or `no`. `?days=7…365` |
| `GET /api/dossier/{TICKER}?format=md` | Dossier as text for a brain's prompt |
| `GET /api/article/{id}` | One stored article: full text, source, times, hash |
| `GET /api/watchlist` | Stocks with last fetch time and unseen alerts |
| `POST /api/watchlist` | Add or update a stock: `{ticker, name, bse_scrip, aliases, link_only, related, exclude}` |
| `GET /api/sources` | Each source's health, item count and track record |
| `GET /api/alerts` / `POST /api/alerts/seen` | Alerts |
| `POST /api/ingest` | One article: `{url, title, body, published_at?, source? or provider?, symbols?}` |
| `POST /api/ingest/batch` | Headlines from another tool of yours: `{items: [{url, title, provider, symbols, published_at}]}` |

The two ingest endpoints require the `X-Ingest-Token` header when a token is set.
The other POST endpoints do not yet (section 9, "Why set the token").

**Never send TradingView data to either ingest endpoint.** TradingView's terms don't
allow storing its data (section 13). `/api/ingest/batch` is for your own tools and
sources whose terms allow storage.

---

## 11. The news brain

### Its job

Turn one dossier into a **news view**: stance, drivers, risks, catalysts, whether it's
priced in, and what's unknown, with every point cited. It does not decide trades.
The Head CA weighs its view with the fundamentals and technicals views, and you decide.

### The flow for one stock

1. **Fetch the dossier.** Text for the prompt, JSON for the critic.
2. **Pass 1, scan.** The brain reads the dossier and names up to 5 citations it
   wants in full, preferring filings and ET Prime.
3. **Pass 2, read and write.** Those articles' saved text (up to 6,000 characters
   each) is added, and the brain writes its view.
4. **The critic checks it:**
   - every citation exists
   - no driver rests only on opinion
   - no driver uses a denied or contradicted report
   - every figure appears in the cited text
   - the stance is one of the allowed values

   **What "cited text" means.** For the up to 5 articles read in pass 2, it's the full
   saved text. For every other citation it's only what the dossier holds: the headline
   and a short quote. Most press items are headline-only today (gap 5), so the figure
   check on those citations is weak; it confirms the number appears in a headline,
   not that the article supports it.
5. **Save the view** to `data/brain_views/<TICKER>-<brain>-<time>.json` for the Head CA.

Two passes give depth where it matters without pasting a month of articles into one
prompt. That would cost more, and most of it would be repetition.

### The rules every brain follows

- Use only the dossier and attached articles, and cite every point.
- Filed and confirmed are strong evidence; unconfirmed, speculation and opinion are
  weak; denied and contradicted count against a report.
- Analyst targets and ratings are opinion, never drivers.
- Price moves are context for "already priced in", not drivers.
- "Coming up" items haven't happened yet.
- Say what's unknown rather than filling gaps.

### The output format

```json
{
  "stance": "positive | negative | neutral | mixed",
  "strength": 0.6,
  "horizon": "days | weeks | months",
  "drivers": [{"point": "...", "direction": "+", "citations": [1, 2]}],
  "risks": [{"point": "...", "citations": [5]}],
  "catalysts": [{"date": "2027-01-15", "what": "Q3 results", "citations": [9]}],
  "already_priced_in": "partly: stock up ~20% in a month [8]",
  "would_change_my_view": ["..."],
  "gaps": ["..."]
}
```

- **`strength`** runs from 0 to 1: how firmly the evidence supports the stance. A
  suggested reading, worth writing into the brain's prompt so every brain uses the
  same scale: around 0.3 for weak or mostly press-only evidence, around 0.6 for a mix
  of filed and press evidence, and 0.8 or more only with filed or confirmed drivers.
- **`horizon`** is how long the brain expects the news to matter. It is the brain's
  judgement, separate from the recency half-lives Newsdesk uses for ranking.

### Running it

```
export ANTHROPIC_API_KEY=...
python news_brain.py RELIANCE                  # Claude
python news_brain.py RELIANCE --brain openai   # needs OPENAI_API_KEY and OPENAI_MODEL
python news_brain.py RELIANCE --brain gemini   # needs GEMINI_API_KEY and GEMINI_MODEL
python news_brain.py BSE --days 90 --no-refresh
python news_brain.py RELIANCE --dry-run        # print the prompts; call no model
```

It prints the saved record. The exit code is 0 if the critic passed and 2 if it
found problems, which are listed in the record under `critic.problems`.

`--dry-run` is the best way to see exactly what a brain receives.

### Running several brains fairly

Let the first brain refresh the dossier, and run the others with `--no-refresh`, so
all of them read the same dossier:

```
python news_brain.py RELIANCE --brain claude
python news_brain.py RELIANCE --brain openai --no-refresh
python news_brain.py RELIANCE --brain gemini --no-refresh
```

Differences in their views then come from reasoning, not from different news. Each
record stores `dossier_as_of`, so the Head CA can confirm they match.

**`--no-refresh` doesn't freeze the dossier.** It stops the brain forcing a fetch,
but the background worker still rebuilds a dossier whenever a poll changes that
stock's events. If a filing arrives between runs, the brains read different news.
To be sure:
- compare `dossier_as_of` across the saved records, and re-run any brain whose value
  differs; or
- run the brains with the worker stopped (`python run.py serve --no-worker`), or
  outside market and post-market hours.

A planned fix is to save one dossier snapshot and pass it to every brain (section 18).

**Data sent to model providers.** Pass 2 sends the saved text of up to 5 articles,
which can include ET Prime text captured through your subscription, to whichever
provider runs the brain (Anthropic, OpenAI or Google). Check that your ET Prime terms
and each provider's data settings are acceptable to you before using that brain.

### What the Head CA does with it

- It reads each brain's news view that passed the critic, along with the
  fundamentals and technicals views.
- Where the news brains disagree, it looks at the citations each one used.
- It discounts views whose drivers are all press-only.
- It gives the verdict with the citations carried through.

A view that failed the critic should be re-run, or set aside, never used silently.

---

## 12. Worked example: BSE Ltd

**What the brain receives** (shortened):

```
1. BSE shares jump nearly 4% after Q2 profit rises 45% [3][4]
   type=results status=unconfirmed kind=opinion confidence=0.45 sources=2
2. BANKEX becomes first Indian sectoral index futures approved by US CFTC [12][13][14][15]
   status=unconfirmed kind=fact confidence=0.75
3. SEBI chief says considering limit for MTF, no decision yet [5][6]
4. BSE shares jump 20% in a month [8][9]
5. BSE Ltd is Rated Buy by MarketsMOJO [18][19]  kind=opinion
## Not yet known
- No exchange filings for BSE in the last 90 days.
```

**How it reasons:**
- MarketsMOJO and broker targets are opinion, so they're ignored.
- The CFTC approval has 4 citations, so it's the strongest point.
- No filing confirms the 45% profit figure.
- The stock is already up 20%, so the good news is partly priced in.

**What it writes:**

```json
{
  "stance": "positive", "strength": 0.45, "horizon": "weeks",
  "drivers": [
    {"point": "Q2 profit up about 45%, press-only, not yet checked against a filing", "direction": "+", "citations": [3]},
    {"point": "US CFTC approved BANKEX futures, a new revenue line; reported by 4 outlets", "direction": "+", "citations": [12, 13, 14, 15]}
  ],
  "risks": [{"point": "SEBI may cap margin-trading funding; no decision yet", "citations": [5, 17]}],
  "catalysts": [],
  "already_priced_in": "partly: stock up ~20% in a month [8]",
  "would_change_my_view": ["SEBI announcing MTF limits", "NSE results filing with weaker numbers"],
  "gaps": ["No exchange filings", "No revenue or volume figures", "No next results date"]
}
```

**The critic:** all citations exist, no driver rests on opinion, and "45" is in
citation 3. The view passes.

**Note on event 1.** The dossier tags the 45% profit story `kind=opinion`, yet the
brain uses it as a driver and the critic passes it. That looks like a contradiction
of the rule "no driver rests only on opinion". The tag is the error: a headline
reporting a profit figure is a fact claim (section 5, "Fact, speculation and
opinion"), and Newsdesk mislabelled it, probably because it came in as a price-move
story. With the label fixed (gap 4) the example is consistent. If the tag were
correct, the critic should have failed this view. Citation [17] in the risks also
falls in the part of the dossier left out of this shortened excerpt.

**The Head CA:**

> News mildly positive but unconfirmed. Strong quarter and a new US product, with a
> regulatory overhang, and much of it already priced in. Low confidence until the NSE
> results filing is checked.

**What this example shows about the dossier** (see section 14):
- no filings, because BSE Ltd lists only on NSE
- the same story split into several events
- junk such as a live price page and robo-written ratings
- the CFTC approval ranked too low
- a results headline labelled opinion
- every driver press-only, so the claim checks had nothing to check against

---

## 13. TradingView MCP and the nse-bse-mcp server

### TradingView MCP: on demand, in chat

TradingView's official MCP gives:
- per-symbol news (up to 200 headlines, with provider and related symbols)
- full story text
- fundamentals, price history and technicals
- filings and transcripts
- an economic calendar

It's a useful cross-check, used **on demand in chat only**. Its terms say:
- **Personal use only:** not for work, and not shared.
- **No storing:** its data can't feed a database or a data feed, so it can't be a
  Newsdesk source.
- **No automated trading decisions:** answers to your questions are fine.

Daily calls depend on your plan: 10 on Essential, 50 on Premium, and 500 on Ultimate
or with the AI add-on. A single stock analysis can take 6–10 calls.

Connect it with your **personal** account, not your work one:

- **claude.ai:** Settings → Connectors → "TradingView MCP" → Connect → log in and Allow.
- **Claude Code on the VM:**
  ```
  claude mcp add --transport http mcp-tradingview https://mcp.tradingview.com/mcp
  ```
  Then run `/mcp` and log in.

Allow only the read tools, so it can't change your watchlists or alerts.

Don't feed what TradingView returns into Newsdesk, whether through
`/api/ingest/batch`, `/api/ingest` or by copying headlines in. Comparing in chat is
fine; storing is not.

Example use:

> Fetch the RELIANCE dossier from Newsdesk, then get TradingView's latest RELIANCE
> headlines, and tell me what TradingView has that the dossier is missing.

### nse-bse-mcp: NSE filings and the results calendar

An open-source (MIT) MCP server wrapping NSE and BSE's own endpoints. It has 60
tools, including:
- NSE corporate announcements, board meetings and corporate actions
- the BSE result calendar
- block and bulk deals
- market holidays
- a document downloader

It's early-stage and calls the same unofficial endpoints, so test it before relying on it.

```
git clone https://github.com/bshada/nse-bse-mcp && cd nse-bse-mcp
bun install && bun run build && bun start        # serves http://localhost:3000/mcp
claude mcp add --transport http nse-bse http://localhost:3000/mcp
```

Keep it on localhost. Clone and read the code rather than running `npx`, which
fetches whatever is newest on npm.

Test, in Claude Code:
- "Use nse-bse to get RELIANCE corporate announcements from the last 30 days"
- "Use nse-bse to get the BSE result calendar for this month"
- "Use nse-bse to get NSE market holidays"

If the NSE calls return data, it can become Newsdesk's NSE filings source (section 14).

### Also worth checking: TapeTide

If you use the TapeTide MCP, it offers stock events, corporate actions, declared
results, and tools to list and read company documents. Check whether it covers NSE
filings, and whether its terms allow storing its data in Newsdesk, before building on
nse-bse-mcp. If both answers are yes, it may be the simpler route to closing gaps 1
and 8.

---

## 14. Known gaps and next steps

| # | Gap | Effect | Fix |
|---|---|---|---|
| 1 | **NSE filings are weak.** The RSS has headlines only, and its URL is unverified. | NSE-only stocks (like BSE Ltd) have no tier-0 checks | NSE announcements through nse-bse-mcp, as a tier-0 source |
| 2 | **Headline-only stories don't merge well.** | The same story appears as several events | Match on distinctive words (BANKEX, CFTC, Q2) within a few days; merge results stories across labels |
| 3 | **Junk gets in.** | Live price pages, robo-written ratings and previews | Low-quality publisher list and title patterns in config |
| 4 | **Some events are mislabelled.** | Approvals and launches ranked as "Other"; price moves treated as news | New "business development" type ranked high; price moves weighted lowest; weekly recaps merged |
| 5 | **Search results have no article text.** | Most press events have headlines only | Resolve publisher links and fetch article text for the top events |
| 6 | **No price check.** | "Already priced in" is left to the brain | Use Kite to measure the move since each event (hook in `scoring.py`) |
| 7 | **Exchange holidays aren't modelled.** | Polling on holidays (harmless) | Holiday list from nse-bse-mcp |
| 8 | **Scanned filing PDFs** | No figures from them | OCR for image-only PDFs |
| 9 | **The token guards only the ingest endpoints.** | A malicious page could change a stock's names through `POST /api/watchlist` (section 9) | Require `X-Ingest-Token` on every POST endpoint, and have the UI send it |
| 10 | **Multi-brain runs can read different dossiers.** | The worker can rebuild a dossier between brain runs (section 11) | Save one dossier snapshot and pass it to every brain |
| 11 | **No measure of news quality.** | No way to tell whether a change made dossiers better | A per-stock quality report (section 18) |
| 12 | **No record of whether views were right.** | No way to tell whether the news brain adds anything | Log each view and compare its stance with the stock's later move |

Gaps 2–4 are self-contained. Gaps 1, 5 and 7 depend on live sites, so test them on your machine.
Gaps 9–12 are self-contained. Section 18 sets out the order to tackle them in.

---

## 15. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| A stock shows no events | Run `python run.py check TICKER`. **FAILED** rows mean a source isn't reachable from your network. **ok** with 0 matches means its names need work: add `link_only` names, or check `exclude`. |
| Status line names sources as not reachable | Open the Sources tab for the error. Feed URLs change; update `sources.yaml`. |
| "No BSE scrip code for this stock" | Add `bse_scrip` from bseindia.com |
| Extension shows **?** | Wait until the article is fully on screen, then click "Capture this page". If it still finds only a few hundred characters, the extraction needs tuning: copy the article container's outerHTML (right-click a paragraph, Inspect, right-click the container, Copy outerHTML). |
| Extension shows **!** | Newsdesk isn't running, the SSH tunnel is closed, or the token differs (re-paste it, no spaces) |
| "Sent" but not in any dossier | It names no watchlist stock. Add the stock, and it links automatically. |
| A wrong company's news appears | Add the look-alike name to that stock's `exclude` |
| Critic fails a view | Read `critic.problems` in the saved record; re-run, or set the view aside |
| `news_brain.py` HTTP 401/403 | API key missing or wrong for that brain |
| A stock's events suddenly disappear | Check its entry in `watchlist.yaml` (or `GET /api/watchlist`) for an unexpected `exclude` name (section 9) |
| Brains disagree on what happened | Compare `dossier_as_of` in their saved records; if it differs, re-run them on the same dossier (section 11) |
| The 07:00 sweep runs at the wrong hour | The VM is in UTC; set `TZ=Asia/Kolkata` (section 3) |
| Newsdesk stopped after the VM rebooted | It was running in tmux; use the systemd service (section 3) |

---

## 16. Usage and legal notes

- **Personal use.** This setup is for your own research. ET Prime content captured
  through your subscription, and anything from TradingView, can't be shared with others.
- **Before going public under SEBI registration:**
  - switch to **licensed** news feeds and an authorised exchange data vendor
  - drop the unofficial NSE/BSE endpoints
  - drop the ET Prime capture
- **Analysis, not advice.** The brains describe evidence. Decisions are yours. Check
  the cited filing before acting on any figure.
- **Keep work and personal separate.** Use your personal Claude and TradingView
  accounts for this project, not your employer's.
- **Model providers see what you send them.** Pass 2 sends saved article text,
  including ET Prime text, to Anthropic, OpenAI or Google (section 11).
- **Nothing from TradingView goes into Newsdesk's database** (sections 10 and 13).

---

## 17. Operations: logs, backups, costs

### Logs

- **Under systemd:** `journalctl -u newsdesk -f` to follow, `journalctl -u newsdesk --since today` for today.
- **Under tmux:** the output is in the tmux window (`tmux attach -t newsdesk`).
- **Source health:** the Sources tab, or `GET /api/sources`.

### Backups

Everything Newsdesk knows (articles, saved text, events, trust records, alerts) is in
one SQLite file, `data/newsdesk.db` (or `NEWSDESK_DB`). The news brain's views are in
`data/brain_views/`. Back up both.

Copy the database safely while Newsdesk is running with SQLite's backup command,
not a plain `cp`:

```
sqlite3 data/newsdesk.db ".backup data/backup-$(date +%F).db"
```

A daily cron entry for that, plus copying the file off the VM now and then, is enough.

### Database size

Every article is stored with its text, including dropped and unlinked ones, so the
file grows steadily. Check its size every month or so (`ls -lh data/newsdesk.db`).
There is no automatic clean-up yet. If it gets large, ask before deleting anything:
saved text is what citations and hashes point to, so old articles cited by saved
brain views should be kept.

### Costs

- **Newsdesk without `ANTHROPIC_API_KEY`:** no model costs; labelling is rule-based.
- **Newsdesk with `ANTHROPIC_API_KEY`:** one labelling call per new matched article,
  so cost grows with the number of stocks and how newsy they are. Run a day with the
  key set, then check usage in the Anthropic Console before leaving it on. The model
  is set under `llm` in `sources.yaml`; a small model is usually enough for labelling.
- **News brain:** two calls per run (scan, then read and write). Pass 2 is the larger
  one, with up to 5 articles of up to 6,000 characters each plus the dossier. Use
  `--dry-run` to see the exact prompt sizes before choosing a model.
- **Several brains:** cost multiplies by the number of brains. Run one until dossiers
  are rich enough for disagreement between brains to be informative (section 18).
- **TradingView:** counts against your plan's daily call limit (section 13).

---

## 18. Roadmap: getting quality news

The design is sound: filings as ground truth, claim checks, saved and hashed sources,
and a code-based critic. What limits it today is input quality. Most press events are
headline-only, NSE filings are thin, and junk and duplicates crowd the ranking, so the
claim checks often have nothing to check. This roadmap focuses on fixing the inputs.
Every step adds to what exists; nothing is removed.

| Step | Work | Gaps closed | Needs live sites? |
|---|---|---|---|
| 1 | **Fetch article text for top events.** Resolve the publisher URL behind each search result and extract the body for the highest-scoring press events. Store it like any other article: saved text, hash, "View saved copy". | 5 | Yes |
| 2 | **Filter junk.** A list of low-quality publishers and title patterns in `sources.yaml` (live price pages, robo-written ratings, "stocks to watch" previews, price-move-only stories). Kept on record with a dropped reason, like paid content, but not used. | 3 | No |
| 3 | **Merge duplicates.** Group events on distinctive words (BANKEX, CFTC, Q2) within a few days; merge results stories across labels. | 2 | No |
| 4 | **Fix labels and ranking.** A "business development" type ranked high; price moves ranked lowest; reported figures labelled fact, not opinion. | 4 | No |
| 5 | **NSE filings as a real tier-0 source,** through nse-bse-mcp, TapeTide or NSE's own endpoints. Build against saved sample responses, then test live. | 1, 7 | Yes |
| 6 | **Quality report.** `python run.py quality TICKER` showing the share of events that are filed, confirmed, press-only and headline-only, and how many items were dropped as junk. Run it before and after each step. | 11 | No |
| 7 | **Guard every write endpoint** with the token. | 9 | No |
| 8 | **Dossier snapshots** for fair multi-brain runs. | 10 | No |
| 9 | **Price reaction** from Kite, so "already priced in" is measured, not guessed. | 6 | Yes |
| 10 | **Track whether views were right.** Log each view and compare its stance with the stock's move over the following weeks. | 12 | No |

**Suggested order:** 6 first (so you can measure), then 1–4 (the biggest lift in
quality), then 5, then the rest. Each step should come with offline tests alongside
the existing ones, using saved sample pages so the tests don't need the network.
