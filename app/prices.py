"""Market-data layer (Phase 3): daily price history, near-high %, market cap.

Uses yfinance, cached hard on disk — the dashboard only fetches tickers that
are actually on screen, and misses are cached too so dead tickers don't get
re-queried every rerun.
"""
import json
import logging
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
    """Daily closes for the trailing `years`. Returns DataFrame(date, close) or None."""
    path = _cache_path(ticker)
    if path.exists() and time.time() - path.stat().st_mtime < config.PRICE_CACHE_TTL_HOURS * 3600:
        df = pd.read_csv(path, parse_dates=["date"])
        return df if not df.empty else None
    try:
        import yfinance as yf
        hist = yf.Ticker(_yahoo_symbol(ticker)).history(
            period=f"{years}y", interval="1d", auto_adjust=True)
    except Exception as e:
        log.warning("price history failed for %s: %s", ticker, e)
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    if hist is None or hist.empty:
        path.write_text("date,close\n", encoding="utf-8")  # cache the miss
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
    """Best-effort market cap (for the §7A market-cap filter), cached daily."""
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
    cap = None
    try:
        import yfinance as yf
        info = yf.Ticker(_yahoo_symbol(ticker)).fast_info
        raw = getattr(info, "market_cap", None)
        cap = float(raw) if raw else None
    except Exception as e:
        log.warning("market cap failed for %s: %s", ticker, e)
    caps[key] = {"cap": cap, "ts": time.time()}
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(caps), encoding="utf-8")
    return cap
