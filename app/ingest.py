"""Ingestion CLI: daily index -> fetch Form 4s -> parse -> SQLite.

Usage:
    python -m app.ingest --days 45          # backfill window ending today
    python -m app.ingest --date 2026-07-10  # one specific day
    python -m app.ingest --days 3 --limit 20  # smoke test
"""
import argparse
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

from . import config, db, edgar, parse

log = logging.getLogger("ingest")

# Self-reported issuerTradingSymbol is unreliable ('NYSE: KRC', '(SIRI)',
# 'BFA, BFB', foreign symbols). Accept it only when the CIK is missing from
# SEC's canonical map AND the string actually looks like a US ticker.
_TICKER_RE = re.compile(r"[A-Z]{1,5}([.-][A-Z0-9]{1,3})?")


def _fetch_and_parse(accession_no: str, path: str) -> tuple[str, dict | None, str | None]:
    """Worker: fetch one accession and parse it. Returns (accession, parsed, error)."""
    try:
        text = edgar.fetch_accession(accession_no, path)
        return accession_no, parse.parse_form4(text), None
    except Exception as e:  # keep the batch going; report per-filing failures
        return accession_no, None, f"{type(e).__name__}: {e}"


def _rows_for(accession_no: str, filed_at: str, parsed: dict,
              ticker_map: dict[str, str]) -> tuple[dict, list[dict]]:
    issuer = parsed["issuer"]
    cik = issuer["cik"] or ""
    raw_symbol = (issuer["ticker"] or "").strip().upper()
    ticker = ticker_map.get(cik) or (
        raw_symbol if _TICKER_RE.fullmatch(raw_symbol) else None
    )
    acc_nodash = accession_no.replace("-", "")
    filing = {
        "accession_no": accession_no,
        "cik": cik,
        "company_name": issuer["name"],
        "ticker": ticker,
        "filed_at": filed_at,
        "period_of_report": parsed["period_of_report"],
        "filing_url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{accession_no}-index.htm",
    }
    owners = parsed["owners"] or [{
        "cik": None, "name": None, "is_director": 0, "is_officer": 0,
        "officer_title": None, "is_ten_percent_owner": 0,
    }]
    # A filing occasionally lists the same owner CIK twice, which would violate
    # UNIQUE(accession_no, txn_seq, insider_cik) — keep the first occurrence.
    seen: set = set()
    owners = [o for o in owners
              if o["cik"] is None or (o["cik"] not in seen and not seen.add(o["cik"]))]
    rows = []
    for txn in parsed["txns"]:
        for owner in owners:
            rows.append({
                "accession_no": accession_no,
                "n_owners": len(owners),
                "insider_name": owner["name"],
                "insider_cik": owner["cik"],
                "is_director": owner["is_director"],
                "is_officer": owner["is_officer"],
                "officer_title": owner["officer_title"],
                "is_ten_percent_owner": owner["is_ten_percent_owner"],
                **{k: txn[k] for k in (
                    "txn_seq", "is_derivative", "security_title", "transaction_date",
                    "transaction_code", "acquired_disposed", "shares", "price_per_share",
                    "value", "shares_owned_after", "direct_indirect",
                )},
            })
    return filing, rows


def ingest_day(conn, day: date, ticker_map: dict[str, str],
               limit: int | None = None, force: bool = False) -> None:
    day_str = day.isoformat()
    if not force and db.day_ingested(conn, day_str):
        log.info("%s already ingested — skipping", day_str)
        return
    idx_text = edgar.fetch_form_index(day, force=force)
    accessions = edgar.form4_accessions(idx_text)
    todo = {a: p for a, p in accessions.items() if force or not db.filing_exists(conn, a)}
    if limit is not None:
        todo = dict(list(todo.items())[:limit])
    log.info("%s: %d Form 4 accessions (%d to fetch)", day_str, len(accessions), len(todo))

    n_err = 0
    with ThreadPoolExecutor(max_workers=config.FETCH_WORKERS) as pool:
        futures = [pool.submit(_fetch_and_parse, a, p) for a, p in todo.items()]
        for i, fut in enumerate(as_completed(futures), 1):
            accession_no, parsed, err = fut.result()
            if err:
                n_err += 1
                log.warning("  %s failed: %s", accession_no, err)
                continue
            try:
                filing, rows = _rows_for(accession_no, day_str, parsed, ticker_map)
                db.upsert_filing(conn, filing, rows)
            except Exception as e:
                n_err += 1
                log.warning("  %s db upsert failed: %s: %s", accession_no, type(e).__name__, e)
                continue
            if i % 100 == 0:
                log.info("  %s: %d/%d done", day_str, i, len(todo))

    # Only mark complete when the full day went in cleanly AND the index has
    # stopped changing (EDGAR rewrites recent daily indexes) — otherwise the
    # day re-runs cheaply on the next ingest until it is final.
    if limit is None and n_err == 0 and edgar.index_is_final(day):
        db.mark_day_ingested(conn, day_str, len(accessions))
    elif n_err:
        log.warning("%s: %d filings failed — day left unmarked for retry", day_str, n_err)


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest SEC Form 4 filings into SQLite")
    ap.add_argument("--days", type=int, help="backfill this many calendar days ending today")
    ap.add_argument("--date", type=str, help="ingest a single day (YYYY-MM-DD)")
    ap.add_argument("--limit", type=int, help="max accessions per day (testing)")
    ap.add_argument("--force", action="store_true", help="re-ingest even if already done")
    args = ap.parse_args()
    if not args.days and not args.date:
        ap.error("provide --days or --date")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    conn = db.connect()
    ticker_map = edgar.load_ticker_map()

    if args.date:
        days = [date.fromisoformat(args.date)]
    else:
        today = date.today()
        days = edgar.available_index_days(today - timedelta(days=args.days), today)
    log.info("ingesting %d day(s)", len(days))
    for d in days:
        ingest_day(conn, d, ticker_map, limit=args.limit, force=args.force)
    log.info("done")


if __name__ == "__main__":
    main()
