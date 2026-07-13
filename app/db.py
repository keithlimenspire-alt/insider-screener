"""SQLite store: schema, upserts, and screener queries."""
import sqlite3
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS filings (
    accession_no     TEXT PRIMARY KEY,
    cik              TEXT NOT NULL,
    company_name     TEXT,
    ticker           TEXT,
    filed_at         TEXT NOT NULL,          -- date the form hit EDGAR (from daily index)
    period_of_report TEXT,
    filing_url       TEXT
);

-- One row per (transaction line x reporting owner). Joint filings (e.g. a fund
-- plus its GP entities) repeat the same economic trade for each owner, so
-- dollar aggregations must dedupe on (accession_no, txn_seq) — see clusters.py.
CREATE TABLE IF NOT EXISTS transactions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    accession_no         TEXT NOT NULL REFERENCES filings(accession_no),
    txn_seq              INTEGER NOT NULL,   -- position of the line within the filing
    n_owners             INTEGER NOT NULL DEFAULT 1,
    is_derivative        INTEGER NOT NULL DEFAULT 0,
    insider_name         TEXT,
    insider_cik          TEXT,
    is_director          INTEGER,
    is_officer           INTEGER,
    officer_title        TEXT,
    is_ten_percent_owner INTEGER,
    transaction_date     TEXT,
    transaction_code     TEXT,
    acquired_disposed    TEXT,               -- A / D
    shares               REAL,
    price_per_share      REAL,               -- NULL when the filing footnotes the price
    value                REAL,               -- shares * price_per_share
    shares_owned_after   REAL,
    direct_indirect      TEXT,               -- D / I
    security_title       TEXT,
    UNIQUE (accession_no, txn_seq, insider_cik)
);

CREATE TABLE IF NOT EXISTS insiders (
    insider_cik TEXT PRIMARY KEY,
    name        TEXT
);

-- Daily index files already ingested, so re-runs skip completed days.
CREATE TABLE IF NOT EXISTS ingested_days (
    day          TEXT PRIMARY KEY,           -- YYYY-MM-DD
    n_accessions INTEGER,
    ingested_at  TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_txn_screen
    ON transactions (transaction_code, acquired_disposed, is_derivative, transaction_date);
CREATE INDEX IF NOT EXISTS idx_txn_accession ON transactions (accession_no);
CREATE INDEX IF NOT EXISTS idx_filings_ticker ON filings (ticker);
"""


def connect(db_path: Path = config.DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def filing_exists(conn: sqlite3.Connection, accession_no: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM filings WHERE accession_no = ?", (accession_no,)
    ).fetchone()
    return row is not None


def upsert_filing(conn: sqlite3.Connection, filing: dict, transactions: list[dict]) -> None:
    """Insert a parsed filing and its transaction rows atomically (replace on re-ingest)."""
    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO filings
               (accession_no, cik, company_name, ticker, filed_at, period_of_report, filing_url)
               VALUES (:accession_no, :cik, :company_name, :ticker, :filed_at,
                       :period_of_report, :filing_url)""",
            filing,
        )
        conn.execute(
            "DELETE FROM transactions WHERE accession_no = ?", (filing["accession_no"],)
        )
        conn.executemany(
            """INSERT INTO transactions
               (accession_no, txn_seq, n_owners, is_derivative, insider_name, insider_cik,
                is_director, is_officer, officer_title, is_ten_percent_owner,
                transaction_date, transaction_code, acquired_disposed, shares,
                price_per_share, value, shares_owned_after, direct_indirect, security_title)
               VALUES (:accession_no, :txn_seq, :n_owners, :is_derivative, :insider_name,
                       :insider_cik, :is_director, :is_officer, :officer_title,
                       :is_ten_percent_owner, :transaction_date, :transaction_code,
                       :acquired_disposed, :shares, :price_per_share, :value,
                       :shares_owned_after, :direct_indirect, :security_title)""",
            transactions,
        )
        conn.executemany(
            "INSERT OR IGNORE INTO insiders (insider_cik, name) VALUES (?, ?)",
            {(t["insider_cik"], t["insider_name"]) for t in transactions if t["insider_cik"]},
        )


def mark_day_ingested(conn: sqlite3.Connection, day: str, n_accessions: int) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO ingested_days (day, n_accessions) VALUES (?, ?)",
            (day, n_accessions),
        )


def day_ingested(conn: sqlite3.Connection, day: str) -> bool:
    return conn.execute("SELECT 1 FROM ingested_days WHERE day = ?", (day,)).fetchone() is not None
