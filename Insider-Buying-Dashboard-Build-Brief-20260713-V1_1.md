# Insider-Buying Dashboard — Claude Code Build Brief

**Version:** V1 — 20260713
**Companion doc:** `Insider-Buying-Strategy-20260713-V1.md` (the ruleset this implements)
**Purpose:** A personal screener that reproduces §2–§5 of the strategy on free SEC data.

> Hand this whole file to Claude Code as the project brief. It's scoped narrow-first: Phase 1 is a working screener; later phases add the judgement layers.

---

## 1. Objective

A dashboard that ingests SEC Form 4 filings, isolates genuine **open-market insider purchases**, groups them into **clusters** per ticker, applies the strategy's filters and role weighting, and shows a ranked, filterable screen of candidates — plus a per-ticker drill-down.

**Explicit non-goal:** this is a screening/analysis tool, not an auto-trader. No order execution, no brokerage integration. Exit/risk logic is out of scope (it's undefined in the source material anyway).

---

## 2. Data source (decided)

| Source | Use | Notes |
|---|---|---|
| **SEC EDGAR daily index** | Enumerate every Form 4 filed each day | `https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{q}/` — filter rows where form type == `4`. This gets *all* Form 4s, which is what a cluster screener needs. |
| **SEC EDGAR Form 4 XML** | Parse each filing | Each accession has a structured `ownershipDocument` XML with transaction codes, price, shares, role flags, shares-owned-after. |
| **efts.sec.gov full-text search** | Company/keyword lookups | JSON, no key, `?q=...&forms=4&startdt=...&enddt=...`. Secondary — use for drill-down/verification, not bulk enumeration. |
| **yfinance (or similar)** | Price history for "near-highs" + charts | Phase 3 only. |
| OpenInsider | Optional cross-check | Scraped mirror of the same data; don't make it the source of truth. |

**Hard requirements (SEC will 403 / rate-limit you otherwise):**
- Send a `User-Agent` header identifying you + a contact email (e.g. `Keith Lim keith@kaizensystems.sg`).
- Cap requests at **<10/sec** aggregate across sec.gov + efts.sec.gov.
- Cache aggressively — filings are immutable once filed, so never re-fetch a parsed accession.
- **Verify current endpoint paths and the Form 4 XML schema live when you build** — SEC changes these occasionally. Evaluate the `edgartools` Python library first; it already parses Form 4 ownership documents and may save you the XML plumbing.

---

## 3. Stack

- **Language:** Python (best SEC/XML ecosystem).
- **Ingestion:** scheduled script (daily index → parse → upsert).
- **Store:** SQLite (single-user, zero-ops). Postgres only if it later goes multi-user/hosted.
- **UI:** **Streamlit** — fastest path to a filterable data dashboard with charts, Python-only.

> **⚠️ Open fork — confirm before Phase 1:** Streamlit assumes this stays a *personal tool*. If the real intent is a product foundation (customer-facing, auth, multi-tenant), switch the UI to **FastAPI + React** and the store to Postgres. Default assumption is personal tool → Streamlit. Don't build the heavier stack unless the goal is explicitly a product.

- **Deploy:** local first. If always-on is wanted later, Docker on DigitalOcean (already in the toolchain) with a daily cron/job.

---

## 4. Pipeline

```
daily index (Form 4s)  ──►  fetch + parse XML  ──►  normalize transactions  ──►  SQLite
                                                                                   │
                                                          filters + clustering ◄───┘
                                                                   │
                                                              Streamlit UI
```

---

## 5. Data model

**`filings`** — one row per Form 4 accession
`accession_no` (PK) · `cik` · `company_name` · `ticker` · `filed_at` · `period_of_report` · `filing_url`

**`transactions`** — one row per transaction line inside a filing
`id` (PK) · `accession_no` (FK) · `insider_name` · `insider_cik` · `is_director` · `is_officer` · `officer_title` · `is_ten_percent_owner` · `transaction_date` · `transaction_code` · `acquired_disposed` (A/D) · `shares` · `price_per_share` · `value` (= shares × price) · `shares_owned_after` · `direct_indirect` · `security_title`

**`insiders`** — for track-record lookups (Phase 3)
`insider_cik` (PK) · `name`

Clusters are computed on the fly (a query/grouping), not stored — keeps them always-fresh as the window rolls.

---

## 6. Strategy → code mapping (the core logic)

This is the part that matters. Each rule from the ruleset maps to concrete logic:

| Rule (ruleset §) | Implementation |
|---|---|
| **Open-market purchases only** (§2) | Keep rows where `transaction_code == 'P'` AND `acquired_disposed == 'A'`. Drop grants (`A`), option exercises (`M`), sales (`S`), gifts (`G`), tax (`F`). |
| **Option-exercise red flag** (§5) | Detect same `insider_cik` + same `filed_at` with both a `M` (acquire) and `S` (dispose) line → flag/exclude the "buy". This is the Starbucks trap. |
| **Screen by dollar value, not shares** (§2) | `value = shares × price_per_share`; filter `value ≥ threshold` (default $200,000, configurable). |
| **Cluster** (§2–§3) | Group qualifying P-buys by `ticker` over a rolling window (default 30–45 days, configurable). `cluster_size = count(distinct insider_cik)`. Screen `cluster_size ≥ 2`; rank higher when `≥ 3`. |
| **Fund/institutional noise** (§5) | Flag rows where `is_ten_percent_owner` AND trade % is tiny (see below). De-weight, don't auto-trust. |
| **Trade %** (§3–§4) | `prior = shares_owned_after − shares` (for a buy); `trade_pct = shares / prior`. Surface this column — it's the real conviction signal, more than raw $. |
| **Conviction size bands** (§4) | Flag `$3k–$15k` buys as likely 401(k)/ESPP noise; highlight `$80k–$500k+` as conviction. |
| **Role weighting** (§4) | Parse `officer_title`: CFO / "Chief * Officer" / VP → high; **General Counsel → high even solo**; CEO → low; `is_ten_percent_owner` → lowest. Compute a weighted role score per cluster. |
| **First-time buyer × tenure** (§3) | First-time = no prior P-buy for that `insider_cik` in history. Tenure needs appointment date — Phase 3 (Google/press-release lookup or manual field). |
| **Near multi-year highs** (§3) | Phase 3: pull price history, compute `% below trailing multi-year high`; flag within 5–15%. |
| **Opportunistic vs routine** (§3) | Compare current cluster against the ticker's historical buy frequency; flag "routine" if buys recur ~monthly. |
| **Stale signal** (§5) | Flag clusters whose most recent buy is >5–6 weeks old with no follow-through. |

---

## 7. Screens

**A. Screener (main)** — one row per qualifying cluster:
`ticker · company · # insiders · total $ · largest single buy · days since first/last buy · top roles · flags (option-exercise / fund-noise / stale / routine)`. Sortable, with a filters panel: cluster min, value min, window length, exclude OTC, market-cap cap, role-score toggle.

**B. Ticker drill-down** — transaction table (insider, role, date, code, shares, price, value, **trade %**, shares-after), the raw Form 4 link per row, and (Phase 3) an insider-buys-over-price chart.

---

## 8. Build phases (narrow first)

- **Phase 1 — MVP screener.** Ingest last 45 days of Form 4 `P` purchases → SQLite → Streamlit table with cluster/value/window filters. Reproduces ruleset §2. *Ship this before anything else.*
- **Phase 2 — judgement layer.** Option-exercise exclusion, role weighting/score, trade %, conviction bands, fund-noise flag (§4–§5 core).
- **Phase 3 — context layer.** Price history → near-highs flag + chart overlay; first-time-buyer detection; insider track-record (% gain/loss since past buys).
- **Phase 4 — always-on.** Daily scheduled ingest, Docker deploy, optional alerts on new qualifying clusters.

Do not build Phase 3/4 features into Phase 1. Get a working screen first, then layer judgement.

---

## 9. Known gotchas

- **US-listed only** — the 10b5-1 mechanic the whole method rests on is US-specific.
- **Ticker mapping** — Form 4s key on CIK, not ticker; you'll need a CIK↔ticker map (SEC publishes `company_tickers.json`).
- **Derivative vs non-derivative tables** — Form 4 has both; open-market purchases live in the non-derivative table. Don't count option/derivative rows as buys.
- **Immutability** — cache parsed accessions; never re-fetch.
- **This screen reproduces the signal half only.** Exit and position sizing are undefined in the source (ruleset §6) — the dashboard flags candidates, it does not tell you when to sell.

---

## 10. Kickoff prompt for Claude Code

> Build Phase 1 of the insider-buying screener described in this brief. Start by (a) confirming the current SEC EDGAR daily-index URL structure and Form 4 XML schema with a live check, (b) evaluating whether `edgartools` handles the parsing or whether we parse XML directly, then propose the Phase 1 file structure and the SQLite schema before writing code. Set a compliant User-Agent and a <10 req/s rate limiter from the start. Confirm the Streamlit-vs-React fork with me before building the UI.
