"""Cluster computation (strategy §2): group qualifying open-market buys per ticker.

Clusters are computed on the fly from SQLite so the rolling window stays fresh.
"""
import re
import sqlite3
from datetime import date, timedelta

import pandas as pd

QUALIFYING_BUYS_SQL = """
SELECT t.accession_no, t.txn_seq, t.n_owners, t.insider_name, t.insider_cik,
       t.is_director, t.is_officer, t.officer_title, t.is_ten_percent_owner,
       t.transaction_date, t.shares, t.price_per_share, t.value,
       t.shares_owned_after, t.direct_indirect, t.security_title,
       f.ticker, f.cik, f.company_name, f.filed_at, f.filing_url
FROM transactions t
JOIN filings f ON f.accession_no = t.accession_no
WHERE t.transaction_code = 'P'          -- open-market purchase…
  AND t.acquired_disposed = 'A'         -- …acquired
  AND t.is_derivative = 0               -- non-derivative table only (§9)
  AND t.transaction_date >= :cutoff
  AND t.transaction_date <= :as_of
  AND t.value IS NOT NULL AND t.value >= :min_value
  AND f.ticker IS NOT NULL AND f.ticker != ''
  -- Debt securities occasionally appear on Form 4 with principal amounts in
  -- both the shares and price fields, producing absurd value products —
  -- the strategy is equity-only, so drop them.
  AND lower(coalesce(t.security_title, '')) NOT LIKE '%note%'
  AND lower(coalesce(t.security_title, '')) NOT LIKE '%bond%'
  AND lower(coalesce(t.security_title, '')) NOT LIKE '%debenture%'
"""


def short_role(row) -> str:
    """Compact role label for display (weighting/scoring is Phase 2)."""
    raw = row.get("officer_title")
    # NULL comes back as float NaN under pandas 3 str dtype — NaN is truthy,
    # so `raw or ""` is not a safe guard here.
    title = raw.lower() if isinstance(raw, str) else ""
    if row.get("is_officer"):
        if re.search(r"chief executive|(^|\W)ceo(\W|$)", title):
            return "CEO"
        if re.search(r"chief financial|(^|\W)cfo(\W|$)", title):
            return "CFO"
        if re.search(r"general counsel|chief legal", title):
            return "GC"
        m = re.search(r"chief (\w+) officer", title)
        if m:
            return "C" + m.group(1)[0].upper() + "O"
        if re.search(r"vice president|(^|\W)[sae]?vp(\W|$)", title):
            return "VP"
        if "president" in title:
            return "Pres"
        return "Officer"
    if row.get("is_director"):
        return "Dir"
    if row.get("is_ten_percent_owner"):
        return "10%"
    return "Other"


def load_qualifying_buys(conn: sqlite3.Connection, window_days: int,
                         min_value: float, as_of: date | None = None) -> pd.DataFrame:
    as_of = as_of or date.today()
    cutoff = as_of - timedelta(days=window_days)
    df = pd.read_sql_query(QUALIFYING_BUYS_SQL, conn, params={
        "cutoff": cutoff.isoformat(), "as_of": as_of.isoformat(), "min_value": min_value,
    })
    if not df.empty:
        df["role"] = df.apply(short_role, axis=1)
    return df


def compute_clusters(buys: pd.DataFrame, min_cluster: int, as_of: date | None = None) -> pd.DataFrame:
    """One row per ticker with >= min_cluster distinct insiders buying in-window."""
    if buys.empty:
        return pd.DataFrame()
    as_of = as_of or date.today()

    # Joint filings repeat one economic trade per owner — dedupe for money math.
    unique_trades = buys.drop_duplicates(subset=["accession_no", "txn_seq"])

    insiders = buys.groupby("ticker").agg(
        n_insiders=("insider_cik", "nunique"),
        company=("company_name", "last"),
        cik=("cik", "last"),
        first_buy=("transaction_date", "min"),
        last_buy=("transaction_date", "max"),
        roles=("role", lambda r: ", ".join(sorted(set(r)))),
    )
    money = unique_trades.groupby("ticker").agg(
        total_value=("value", "sum"),
        largest_buy=("value", "max"),
        n_buys=("value", "size"),
    )
    out = insiders.join(money).reset_index()
    out = out[out["n_insiders"] >= min_cluster]
    if out.empty:
        return out
    out["days_since_first"] = (pd.Timestamp(as_of) - pd.to_datetime(out["first_buy"])).dt.days
    out["days_since_last"] = (pd.Timestamp(as_of) - pd.to_datetime(out["last_buy"])).dt.days
    return out.sort_values(["n_insiders", "total_value"], ascending=False).reset_index(drop=True)


TICKER_TXNS_SQL = """
SELECT t.insider_name, t.insider_cik, t.is_director, t.is_officer, t.officer_title,
       t.is_ten_percent_owner, t.transaction_date, t.transaction_code,
       t.acquired_disposed, t.is_derivative, t.shares, t.price_per_share, t.value,
       t.shares_owned_after, t.direct_indirect, t.security_title,
       f.filed_at, f.filing_url, t.accession_no, t.txn_seq
FROM transactions t
JOIN filings f ON f.accession_no = t.accession_no
WHERE f.ticker = :ticker
ORDER BY t.transaction_date DESC, t.accession_no, t.txn_seq
"""


def ticker_transactions(conn: sqlite3.Connection, ticker: str) -> pd.DataFrame:
    """All stored transaction lines for one ticker (drill-down, screen B)."""
    df = pd.read_sql_query(TICKER_TXNS_SQL, conn, params={"ticker": ticker})
    if df.empty:
        return df
    df["role"] = df.apply(short_role, axis=1)
    # Trade % (§3–§4): shares bought relative to holdings *before* the buy.
    # Left blank when prior holdings are zero (new position) or unknown.
    prior = df["shares_owned_after"] - df["shares"]
    df["trade_pct"] = None
    buy_mask = (df["acquired_disposed"] == "A") & (prior > 0) & df["shares"].notna()
    df.loc[buy_mask, "trade_pct"] = (df.loc[buy_mask, "shares"] / prior[buy_mask]) * 100
    return df
