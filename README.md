# Insider-Buying Screener (Phase 1)

Personal screener for genuine **open-market insider purchases** (SEC Form 4,
transaction code `P`, acquired, non-derivative), clustered per ticker over a
rolling window. Implements Phase 1 of
`Insider-Buying-Dashboard-Build-Brief-20260713-V1_1.md` (strategy §2).

**Not an auto-trader.** No order execution, no exit logic — it flags candidates.

## Setup

```powershell
py -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

## Ingest filings

```powershell
# Backfill the last 45 days of Form 4 filings (~10–15k filings, ~30–45 min
# at the built-in 8 req/s SEC rate limit; safe to interrupt and re-run —
# completed days are skipped and fetched filings are cached)
.\.venv\Scripts\python.exe -m app.ingest --days 45

# Single day / smoke test
.\.venv\Scripts\python.exe -m app.ingest --date 2026-07-10
.\.venv\Scripts\python.exe -m app.ingest --days 2 --limit 25
```

Data lands in `data/insider.db` (SQLite). Raw submissions are cached under
`data/cache/raw/` and never re-fetched (filings are immutable).

To refresh daily, re-run `--days 5` (skips already-ingested days) or schedule it.

## Run the dashboard

```powershell
.\.venv\Scripts\streamlit.exe run dashboard.py
```

- **Screener** — one row per cluster: ticker, # insiders, total $, largest buy,
  days since first/last buy, roles. Filters: window length, min single-buy $,
  min cluster size, exclude OTC.
- **Drill-down** — every stored transaction line for a ticker, with trade %
  (shares bought ÷ prior holdings) and a link to the raw Form 4.

## Layout

| Path | Purpose |
|---|---|
| `app/config.py` | User-Agent, rate limit, thresholds, paths |
| `app/ratelimit.py` | Token-bucket limiter (8 req/s, SEC cap is 10) |
| `app/edgar.py` | Daily-index + accession fetching, disk cache, ticker maps |
| `app/parse.py` | `ownershipDocument` XML → issuer / owners / transaction lines |
| `app/db.py` | SQLite schema + upserts |
| `app/ingest.py` | CLI: daily index → fetch → parse → upsert |
| `app/clusters.py` | On-the-fly cluster queries + trade % |
| `dashboard.py` | Streamlit UI |

## Notes / gotchas (§9)

- The daily index lists a Form 4 once per filer (issuer + each reporting
  owner) — ingestion dedupes by accession number.
- Joint filings (fund + GP entities) repeat one economic trade per owner;
  dollar aggregations dedupe on `(accession_no, txn_seq)`.
- All transaction lines (including sales, grants, and derivative rows) are
  stored so the Phase 2 judgement layer (option-exercise trap, role weighting,
  conviction bands) can be built without re-ingesting.
- Form `4/A` amendments are not ingested in Phase 1.
- US-listed issuers only by design; ticker comes from the filing itself with
  `company_tickers.json` as fallback.
