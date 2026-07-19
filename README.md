# Insider-Buying Screener

Personal screener for genuine **open-market insider purchases** (SEC Form 4,
transaction code `P`, acquired, non-derivative), clustered per ticker over a
rolling window, with the strategy's judgement and context layers on top.
Implements all four phases of
`Insider-Buying-Dashboard-Build-Brief-20260713-V1_1.md` plus the
**V2 upgrade** (`Insider-Buying-Strategy-20260719-V2.md`).

**Not an auto-trader.** No order execution, no exit logic — it flags candidates.

## V2 strategy layer (20260719)

- **Trade-type classifier (§A):** each cluster is routed **value** (down from
  highs → discount-to-insider-entry matters) or **momentum** (near multi-year
  highs → strength is the signal) instead of forcing one price rule globally.
  Catalyst trades need event data the system doesn't ingest — manual call.
- **50-day rule (§B):** value clusters are marked *actionable* only once price
  reclaims the 50-day MA (insiders are chronically early); momentum clusters
  are actionable by definition. It gates the badge, not the score.
- **Market breadth gauge (§B):** rolling share of buys among *unscheduled*
  (non-10b5-1) insider trades across the whole Form 4 firehose. ~33% is
  normal; >33% bullish, >50% historically very bullish. Shown as a banner.
- **Low-signal exclusions (§B):** 10b5-1 scheduled trades (checkbox or
  footnote), DRIP/401(k)/ESPP plan buys, offering/placement patterns
  (warrant-paired shares, or ≥3 buying units at one identical round price),
  and below-market discounted buys (flagged). Excluded before scoring;
  sidebar toggle to include.
- **Refinements (§B):** regime-flip flag (only sold last year → now buying),
  ~10% position-increase "notable" line, sell-side track record in the
  drill-down, FPI/new-reporter badge (first insider filing <12 months →
  first-time/routine signals suppressed as reporting-regime artifacts).
- **Spec items that don't map cleanly (flagged per §10):** this repo never
  contained the May-2026 "coiled-spring" system — the value branch's
  discount-to-entry logic here is new code implementing V2's description, not
  a port. Valuation-vs-own-5-year-range needs paid fundamentals data and is
  not implemented; use % below high + discount-to-entry as proxies.

## Setup

```powershell
py -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

## Ingest filings

```powershell
# Backfill the last 45 days of Form 4 filings (~18k filings, ~40 min at the
# built-in 8 req/s SEC rate limit; safe to interrupt and re-run — completed
# days are skipped and fetched filings are cached immutably)
.\.venv\Scripts\python.exe -m app.ingest --days 45

# Daily catch-up (last 7 days, skips finalized days) + cluster alerts
.\.venv\Scripts\python.exe -m app.ingest --daily

# Single day / smoke test
.\.venv\Scripts\python.exe -m app.ingest --date 2026-07-10
.\.venv\Scripts\python.exe -m app.ingest --days 2 --limit 25
```

Data lands in `data/insider.db` (SQLite, WAL). Raw submissions cache under
`data/cache/raw/` and are never re-fetched. Recent days re-ingest cheaply on
each run until their EDGAR index stops changing (~2 days), then are marked
final and skipped.

## Run the dashboard

```powershell
.\.venv\Scripts\streamlit.exe run dashboard.py
```

**Screener** — one row per cluster: # insiders, # buys, total $, largest buy,
**role score** (§4 weights: GC 1.0 · CFO 0.9 · C-suite 0.8 · VP 0.7 · Dir 0.4 ·
CEO 0.3 · 10% 0.1), max trade %, # conviction buys (≥$80k), # first-time
buyers, days since first/last, roles, and **flags**:

| Flag | Meaning (brief §) |
|---|---|
| `exercise×N` | §5: N buys excluded — insider filed an option exercise (M) and sale (S) the same day ("Starbucks trap"). Toggle to include. |
| `fund-noise` | §5: >50% of cluster $ from 10%-owners buying <2% of their existing stake |
| `stale` | §5: last buy >40 days ago with no follow-through |
| `routine` | §3: ticker has buys in ≥3 distinct months on record (~monthly recurrence) |
| `all-noise-sized` | §4: every buy in the $3k–$15k 401(k)/ESPP band |

Sidebar filters: window length, min single-buy $, min cluster size, min role
score, exclude-OTC (by issuer CIK), include exercise-flagged, near-highs
context, market-cap cap. The near-highs toggle and market-cap filter fetch
from Yahoo Finance (~1s per ticker on first use, cached ~20h on disk).

**Drill-down** — three tabs per ticker: every stored transaction line (with
trade % and Form 4 links), **buys-over-price chart** (3y closes with buy
markers; § 3 near-high call-out), and **insider track record** (% move since
each recorded buy).

First-time-buyer detection and track record depth are only as good as the
accumulated DB history — they sharpen the longer the daily ingest runs.

## Always-on (Phase 4)

Alerts: `--daily` diffs current qualifying clusters (default thresholds)
against the last run; new or expanded clusters append to the `alerts` table
and `data/alerts.jsonl`, and surface in the dashboard's 🔔 expander.

**Windows Task Scheduler** (run this yourself — it registers a scheduled task):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\schedule_daily.ps1
```

**Docker** (e.g. DigitalOcean):

```bash
docker compose up -d   # dashboard on :8501 + daily ingest sidecar
```

## Layout

| Path | Purpose |
|---|---|
| `app/config.py` | User-Agent, rate limit, thresholds, role weights, paths |
| `app/ratelimit.py` | Token-bucket limiter (8 req/s, SEC cap is 10) |
| `app/edgar.py` | Daily-index + accession fetching, atomic disk cache, ticker/exchange maps |
| `app/parse.py` | `ownershipDocument` XML → issuer / owners / transaction lines |
| `app/db.py` | SQLite schema + upserts + alert tables |
| `app/ingest.py` | CLI: daily index → fetch → parse → upsert (`--days/--date/--daily`) |
| `app/clusters.py` | Strategy engine: clusters (§2), judgement (§4–§5), context (§3) |
| `app/prices.py` | yfinance price history / near-high / market cap, disk-cached |
| `app/alerts.py` | New/expanded cluster detection |
| `dashboard.py` | Streamlit UI |
| `scripts/` | Task Scheduler registration + daily runner |

## Why not edgartools?

Evaluated live (v5.42.0, 2026-07-13) per the brief. Its Form 4 parse is
accurate, but: (a) `.obj()` issues an extra uncacheable `data.sec.gov` request
per reporting owner, roughly doubling bulk request volume; (b) v5.42.0 wipes
its persistent HTTP cache on every `import edgar` (two "one-time" cache-fix
markers delete each other); (c) it pulls 43 dependencies / ~275 MB. The direct
stdlib parse used here is ~150 lines, was validated against a 23-filing live
edge-case audit, and keeps rate limiting and caching fully in our control.
Revisit if the cache bug is fixed and bulk needs grow.

## Notes / gotchas (§9)

- The daily index lists a Form 4 once per filer (issuer + each reporting
  owner) — ingestion dedupes by accession number.
- Joint filings (fund + GP entities) repeat one economic trade per owner;
  dollar aggregations dedupe on `(accession_no, txn_seq)`, while cluster
  insider counts intentionally count each entity (the fund-noise flag is the
  §5 counterweight).
- All transaction lines (sales, grants, derivative rows) are stored — that is
  what powers the exercise-trap detection and drill-down.
- Form `4/A` amendments are not ingested.
- US-listed issuers only by design; tickers are canonicalized against SEC's
  `company_tickers.json` (CIK-keyed), self-reported symbols are validated,
  and debt securities are excluded from qualifying buys.
- Alerts use the default thresholds from `app/config.py`, independent of
  whatever sidebar filters you have set in the dashboard.
