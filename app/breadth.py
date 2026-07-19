"""V2 §B market-wide breadth: the unscheduled-buy share of insider trades.

Of unscheduled (non-10b5-1) insider trades, roughly a third are normally buys.
When the rolling share crosses ~50% it has historically been very bullish for
the whole market (COVID ~60%, 2022 bottom ~55%); above ~33% bullish; well
below, underperformance. This is a "should I be buying anything right now"
overlay, not a stock picker.
"""
import sqlite3
from datetime import date, timedelta

import pandas as pd

from . import config

# Economic trades (deduped joint-owner rows), buys vs sells, unscheduled only.
# Dates are clamped to the plausible range so filer typos don't smear the tail.
_SQL = """
SELECT t.transaction_date AS d,
       CASE WHEN t.transaction_code = 'P' THEN 1 ELSE 0 END AS is_buy,
       COUNT(DISTINCT t.accession_no || '/' || t.txn_seq) AS n
FROM transactions t
JOIN filings f ON f.accession_no = t.accession_no
WHERE t.is_derivative = 0
  AND ((t.transaction_code = 'P' AND t.acquired_disposed = 'A')
       OR (t.transaction_code = 'S' AND t.acquired_disposed = 'D'))
  AND f.aff_10b5_one = 0
  AND (t.footnotes IS NULL OR lower(t.footnotes) NOT LIKE '%10b5-1%')
  AND t.transaction_date >= :start AND t.transaction_date <= :today
GROUP BY d, is_buy
"""


def breadth_series(conn: sqlite3.Connection, lookback_days: int = 150) -> pd.DataFrame:
    """Rolling unscheduled-buy share, one row per trading day with data."""
    today = date.today()
    df = pd.read_sql_query(_SQL, conn, params={
        "start": (today - timedelta(days=lookback_days)).isoformat(),
        "today": today.isoformat(),
    })
    if df.empty:
        return df
    daily = (df.pivot_table(index="d", columns="is_buy", values="n",
                            aggfunc="sum", fill_value=0)
             .rename(columns={0: "sells", 1: "buys"}).sort_index())
    for col in ("buys", "sells"):
        if col not in daily:
            daily[col] = 0
    win = config.BREADTH_WINDOW_DAYS
    roll = daily.rolling(win, min_periods=max(3, win // 4)).sum()
    out = pd.DataFrame({
        "date": pd.to_datetime(daily.index),
        "buys": daily["buys"].values,
        "sells": daily["sells"].values,
        "buy_share": (roll["buys"] / (roll["buys"] + roll["sells"]) * 100).values,
    })
    return out.dropna(subset=["buy_share"]).reset_index(drop=True)


def breadth_label(share: float) -> str:
    if share >= config.BREADTH_VERY_BULLISH_PCT:
        return "very bullish"
    if share >= config.BREADTH_BULLISH_PCT:
        return "bullish"
    if share >= config.BREADTH_BULLISH_PCT - 8:
        return "neutral"
    return "weak"


def current_breadth(conn: sqlite3.Connection) -> dict | None:
    """Latest rolling unscheduled-buy share + label, or None without data."""
    s = breadth_series(conn)
    if s.empty:
        return None
    last = s.iloc[-1]
    return {
        "as_of": str(last["date"].date()),
        "buy_share": float(last["buy_share"]),
        "label": breadth_label(float(last["buy_share"])),
        "series": s,
    }
