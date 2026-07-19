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

from . import clusters, config, prices

log = logging.getLogger(__name__)


def _trades_for_pricing(buys: pd.DataFrame) -> pd.DataFrame:
    """Economic trades comparable to the US common-stock price series.

    Uses the same dedupe as the money columns (split-accession joint trades
    collapse), then drops preferred/preference classes — pooling a $1000
    preferred into a common-stock VWAP flips the discount sign."""
    if "unit" in buys.columns:
        trades = clusters._unique_trades(buys)
    else:
        trades = buys.drop_duplicates(subset=["accession_no", "txn_seq"])
    if "security_title" in trades.columns:
        trades = trades[~trades["security_title"].str.contains(
            "prefer", case=False, na=False)]
    return trades


def market_context_for(ticker: str, buys: pd.DataFrame,
                       get_history: Callable = prices.get_history) -> dict:
    """Price-context columns for one ticker's cluster.

    `buys` must be that ticker's kept qualifying buys (owner-level rows are
    fine — trades are deduped here)."""
    out = {
        "last_close": None, "pct_below_high": None, "near_high": None,
        "ma50": None, "above_ma50": None, "trade_type": None,
        "actionable": None, "entry_vwap": None, "discount_to_entry_pct": None,
        "n_below_market": 0, "below_market_value": 0.0,
    }
    hist = get_history(ticker)
    trades = _trades_for_pricing(buys)

    dated = None
    if hist is not None and not hist.empty:
        closes = hist["close"]
        last = float(closes.iloc[-1])
        high = float(closes.max())
        out["last_close"] = last
        if high > 0:
            out["pct_below_high"] = (high - last) / high * 100.0
            out["near_high"] = out["pct_below_high"] <= config.NEAR_HIGH_MAX_PCT_BELOW
        if len(closes) >= config.MA_GATE_DAYS // 2:
            ma = closes.rolling(config.MA_GATE_DAYS,
                                min_periods=config.MA_GATE_DAYS // 2).mean()
            out["ma50"] = float(ma.iloc[-1])
            # §B "reclaim and HOLD": the last N closes must each sit above
            # that day's 50-day MA — one pop above doesn't count.
            n = config.MA_GATE_HOLD_DAYS
            tail_c, tail_m = closes.tail(n), ma.tail(n)
            out["above_ma50"] = bool((tail_c.values > tail_m.values).all())
        dated = hist.set_index("date")["close"].sort_index()

    def _close_on(d):
        if dated is None:
            return None
        try:
            ts = pd.Timestamp(d)
        except (ValueError, TypeError):
            return None
        idx = dated.index.searchsorted(ts, side="right") - 1
        return float(dated.iloc[idx]) if idx >= 0 else None

    # Price comparability (V2/IPX finding): as-filed prices in a foreign
    # currency or per-unit basis are not comparable to the US listing series —
    # a >2x mismatch vs that day's close excludes the trade from the VWAP and
    # the below-market tell rather than poisoning both.
    vwap_value = vwap_shares = 0.0
    for t in trades.itertuples():
        price, value, shares = t.price_per_share, t.value, t.shares
        if not price or not shares:
            continue
        close = _close_on(t.transaction_date)
        if close and close > 0 and not 0.5 <= price / close <= 2.0:
            continue  # not comparable
        vwap_value += value or 0.0
        vwap_shares += shares
        if close and close > 0 and \
                price < close * (1 - config.BELOW_MARKET_DISCOUNT_PCT / 100):
            out["n_below_market"] += 1
            out["below_market_value"] += value or 0.0
    if vwap_shares > 0:
        out["entry_vwap"] = vwap_value / vwap_shares

    # §A router: momentum when near highs, value otherwise.
    if out["near_high"] is not None:
        out["trade_type"] = "momentum" if out["near_high"] else "value"
    if out["entry_vwap"] and out["last_close"] is not None:
        out["discount_to_entry_pct"] = \
            (out["entry_vwap"] - out["last_close"]) / out["entry_vwap"] * 100.0

    # §B 50-day rule: value trades are actionable only once price reclaims and
    # holds the 50-day MA (insiders are chronically early); momentum already is.
    if out["trade_type"] == "momentum":
        out["actionable"] = True
    elif out["trade_type"] == "value" and out["above_ma50"] is not None:
        out["actionable"] = bool(out["above_ma50"])
    return out


def enrich_clusters(cl: pd.DataFrame, kept_buys: pd.DataFrame,
                    get_history: Callable = prices.get_history,
                    progress: Callable[[float, str], None] | None = None) -> pd.DataFrame:
    """Adds the §A/§B market-context columns to a cluster frame, de-weighting
    discounted (below-market) money in the ranking per V2 §B."""
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
    # Fold the below-market tell into the flags column; call out clusters whose
    # money is mostly discounted paper ("insider got a deal you can't").
    extra = out["n_below_market"].map(lambda n: f"below-mkt×{n}" if n else "")
    discounted = out["below_market_value"] > 0.5 * out["total_value"]
    extra = extra.where(~discounted, extra + ", discounted")
    out["flags"] = (out["flags"] + extra.map(lambda s: ", " + s if s else "")
                    ).str.strip(", ")
    # De-weight (not exclude) discounted money in the ranking (V2 §B).
    out["rank_value"] = out["total_value"] - out["below_market_value"].fillna(0.0)
    return out.sort_values(["n_insiders", "role_score", "rank_value"],
                           ascending=False).reset_index(drop=True)
