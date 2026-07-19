"""V2 §A trade-type classifier + §B entry gate + price-dependent flags.

Routes each cluster to a price rule instead of forcing one globally:
- momentum (near multi-year highs) → strength is the signal, no entry gate
- value (down from highs)          → discount-to-entry + 50-day MA gate
Catalyst trades need event data the system does not ingest — they are not
auto-classified (V2 flags this as a manual call).
"""
import logging
from typing import Callable

import pandas as pd

from . import config, prices

log = logging.getLogger(__name__)


def market_context_for(ticker: str, buys: pd.DataFrame,
                       get_history: Callable = prices.get_history) -> dict:
    """Price-context columns for one ticker's cluster.

    `buys` must be that ticker's kept qualifying buys (owner-level rows are
    fine — trades are deduped here for the entry VWAP)."""
    out = {
        "last_close": None, "pct_below_high": None, "near_high": None,
        "ma50": None, "above_ma50": None, "trade_type": None,
        "actionable": None, "entry_vwap": None, "discount_to_entry_pct": None,
        "n_below_market": 0,
    }
    hist = get_history(ticker)

    trades = buys.drop_duplicates(subset=["accession_no", "txn_seq"])
    shares = trades["shares"].sum()
    if shares and shares > 0:
        out["entry_vwap"] = float(trades["value"].sum() / shares)

    if hist is None or hist.empty:
        return out
    closes = hist["close"]
    last = float(closes.iloc[-1])
    high = float(closes.max())
    out["last_close"] = last
    if high > 0:
        out["pct_below_high"] = (high - last) / high * 100.0
        out["near_high"] = out["pct_below_high"] <= config.NEAR_HIGH_MAX_PCT_BELOW
    ma_win = closes.tail(config.MA_GATE_DAYS)
    if len(ma_win) >= config.MA_GATE_DAYS // 2:  # tolerate recent listings
        out["ma50"] = float(ma_win.mean())
        out["above_ma50"] = last > out["ma50"]

    # §A router: momentum when near highs, value otherwise.
    if out["near_high"] is not None:
        out["trade_type"] = "momentum" if out["near_high"] else "value"
    if out["entry_vwap"]:
        out["discount_to_entry_pct"] = (out["entry_vwap"] - last) / out["entry_vwap"] * 100.0

    # §B 50-day rule: value trades are actionable only once price reclaims the
    # 50-day MA (insiders are chronically early); momentum trades already are.
    if out["trade_type"] == "momentum":
        out["actionable"] = True
    elif out["trade_type"] == "value" and out["above_ma50"] is not None:
        out["actionable"] = bool(out["above_ma50"])

    # V2 §B discounted-purchase tell: buy price well under that day's close.
    dated = hist.set_index("date")["close"].sort_index()
    for t in trades.itertuples():
        try:
            ts = pd.Timestamp(t.transaction_date)
        except (ValueError, TypeError):
            continue
        idx = dated.index.searchsorted(ts, side="right") - 1
        if idx < 0:
            continue
        close = float(dated.iloc[idx])
        if t.price_per_share and close > 0 and \
                t.price_per_share < close * (1 - config.BELOW_MARKET_DISCOUNT_PCT / 100):
            out["n_below_market"] += 1
    return out


def enrich_clusters(cl: pd.DataFrame, kept_buys: pd.DataFrame,
                    get_history: Callable = prices.get_history,
                    progress: Callable[[float, str], None] | None = None) -> pd.DataFrame:
    """Adds the §A/§B market-context columns to a cluster frame."""
    if cl.empty:
        return cl
    rows = []
    tickers = list(cl["ticker"])
    for i, ticker in enumerate(tickers):
        rows.append(market_context_for(
            ticker, kept_buys[kept_buys["ticker"] == ticker], get_history))
        if progress:
            progress((i + 1) / len(tickers), ticker)
    ctx = pd.DataFrame(rows, index=cl.index)
    out = pd.concat([cl, ctx], axis=1)
    # Fold the below-market tell into the flags column.
    extra = out["n_below_market"].map(lambda n: f"below-mkt×{n}" if n else "")
    out["flags"] = (out["flags"] + extra.map(lambda s: ", " + s if s else "")
                    ).str.strip(", ")
    return out
