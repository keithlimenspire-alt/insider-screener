"""Market-data layer (Phase 3): daily price history, near-high %, market cap.

Uses yfinance, cached hard on disk — the dashboard only fetches tickers that
are actually on screen. Transient failures never overwrite good cached data
(stale prices beat no prices for a screener) and are never cached themselves.
"""
import json
import logging
import os
import re
import time

import pandas as pd

from . import config

log = logging.getLogger(__name__)

_SAFE = re.compile(r"[^A-Za-z0-9.\-]")


def _yahoo_symbol(ticker: str) -> str:
    """SEC/Form-4 class shares use dots (BRK.B); Yahoo uses dashes (BRK-B)."""
    return ticker.upper().replace(".", "-")


def _cache_path(ticker: str):
    return config.PRICE_CACHE_DIR / f"{_SAFE.sub('_', ticker.upper())}.csv"


def get_history(ticker: str, years: int = config.PRICE_HISTORY_YEARS) -> pd.DataFrame | None:
    """Daily closes for the trailing `years` (split-adjusted, NOT dividend-
    adjusted — dividend adjustment deflates past prices and misstates the
    distance below the real high). Returns DataFrame(date, close) or None."""
    path = _cache_path(ticker)
    cached: pd.DataFrame | None = None
    if path.exists():
        try:
            cached = pd.read_csv(path, parse_dates=["date"])
        except Exception:  # truncated/corrupt cache file → treat as miss
            cached = None
        if cached is not None and time.time() - path.stat().st_mtime \
                < config.PRICE_CACHE_TTL_HOURS * 3600:
            return cached if not cached.empty else None

    hist = None
    try:
        import yfinance as yf
        hist = yf.Ticker(_yahoo_symbol(ticker)).history(
            period=f"{years}y", interval="1d", auto_adjust=False)
    except Exception as e:
        log.warning("price history failed for %s: %s", ticker, e)

    path.parent.mkdir(parents=True, exist_ok=True)
    if hist is None or hist.empty:
        if cached is not None and not cached.empty:
            # Serve (and re-arm) the stale cache rather than clobbering good
            # data with a 20-hour "miss" marker on a transient failure.
            cached.to_csv(path, index=False)
            return cached
        if hist is not None:  # empty result (delisted/unknown) → cache the miss
            path.write_text("date,close\n", encoding="utf-8")
        return None
    df = hist.reset_index()[["Date", "Close"]].rename(columns={"Date": "date", "Close": "close"})
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    df.to_csv(path, index=False)
    return df


def near_high(ticker: str) -> tuple[float, float] | None:
    """(last_close, pct_below_trailing_high) for §3's near-multi-year-highs
    signal, or None when no price history is available."""
    df = get_history(ticker)
    if df is None or df.empty:
        return None
    last = float(df["close"].iloc[-1])
    high = float(df["close"].max())
    if high <= 0:
        return None
    return last, (high - last) / high * 100.0


def market_cap(ticker: str) -> float | None:
    """Best-effort market cap (for the §7A market-cap filter), cached daily.
    Failures are NOT cached — a transient Yahoo error must not blank a ticker
    out of the screen for 20 hours."""
    cache_file = config.PRICE_CACHE_DIR / "market_caps.json"
    caps: dict = {}
    if cache_file.exists():
        try:
            caps = json.loads(cache_file.read_text(encoding="utf-8"))
        except ValueError:
            caps = {}
    key = ticker.upper()
    entry = caps.get(key)
    if entry and time.time() - entry["ts"] < config.PRICE_CACHE_TTL_HOURS * 3600:
        return entry["cap"]
    try:
        import yfinance as yf
        info = yf.Ticker(_yahoo_symbol(ticker)).fast_info
        raw = getattr(info, "market_cap", None)
        cap = float(raw) if raw else None
    except Exception as e:
        log.warning("market cap failed for %s: %s", ticker, e)
        return entry["cap"] if entry else None  # serve stale, don't cache failure
    if cap is not None:
        caps[key] = {"cap": cap, "ts": time.time()}
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_file.with_name(cache_file.name + ".tmp")
        tmp.write_text(json.dumps(caps), encoding="utf-8")
        os.replace(tmp, cache_file)
    return cap
