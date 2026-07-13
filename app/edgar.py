"""SEC EDGAR fetch layer: rate-limited, cached, compliant User-Agent."""
import json
import logging
import re
import time
from datetime import date

import requests

from . import config
from .ratelimit import RateLimiter

log = logging.getLogger(__name__)

_limiter = RateLimiter(config.MAX_REQUESTS_PER_SEC)
_session = requests.Session()
_session.headers.update({
    "User-Agent": config.SEC_USER_AGENT,
    "Accept-Encoding": "gzip, deflate",
})

RETRY_STATUSES = {403, 429, 500, 502, 503, 504}


def fetch(url: str, max_retries: int = 4) -> bytes:
    """Rate-limited GET with exponential backoff on SEC throttling responses."""
    for attempt in range(max_retries + 1):
        _limiter.acquire()
        resp = _session.get(url, timeout=30)
        if resp.status_code == 200:
            return resp.content
        if resp.status_code in RETRY_STATUSES and attempt < max_retries:
            wait = 2 ** attempt
            log.warning("HTTP %s for %s — retrying in %ss", resp.status_code, url, wait)
            time.sleep(wait)
            continue
        resp.raise_for_status()
    raise RuntimeError(f"unreachable: {url}")


# ---------------------------------------------------------------- daily index

def quarter_of(d: date) -> int:
    return (d.month - 1) // 3 + 1


def available_index_days(d_from: date, d_to: date) -> list[date]:
    """Business days in [d_from, d_to] that actually have a form.idx on EDGAR."""
    days: list[date] = []
    quarters = sorted({(d.year, quarter_of(d)) for d in (d_from, d_to)})
    # Cover any quarter between the two endpoints (window never spans >2 quarters
    # at 45 days, but be safe).
    if len(quarters) == 2:
        (y1, q1), (y2, q2) = quarters
        cur = (y1, q1)
        full = []
        while cur <= (y2, q2):
            full.append(cur)
            y, q = cur
            cur = (y + 1, 1) if q == 4 else (y, q + 1)
        quarters = full
    for year, q in quarters:
        listing = json.loads(fetch(f"{config.DAILY_INDEX_BASE}/{year}/QTR{q}/index.json"))
        for item in listing["directory"]["item"]:
            m = re.fullmatch(r"form\.(\d{8})\.idx", item["name"])
            if not m:
                continue
            d = date(int(m.group(1)[:4]), int(m.group(1)[4:6]), int(m.group(1)[6:8]))
            if d_from <= d <= d_to:
                days.append(d)
    return sorted(days)


def fetch_form_index(day: date) -> str:
    """Fetch (and cache — past days are immutable) one daily form index."""
    cache_file = config.INDEX_CACHE_DIR / f"form.{day:%Y%m%d}.idx"
    if cache_file.exists():
        return cache_file.read_text(encoding="utf-8", errors="replace")
    url = f"{config.DAILY_INDEX_BASE}/{day.year}/QTR{quarter_of(day)}/form.{day:%Y%m%d}.idx"
    text = fetch(url).decode("utf-8", errors="replace")
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(text, encoding="utf-8")
    return text


_IDX_ROW = re.compile(r"edgar/data/(\d+)/(\d{10}-\d{2}-\d{6})\.txt")


def form4_accessions(idx_text: str) -> dict[str, str]:
    """Extract Form 4 rows from a form.idx → {accession_no: submission path}.

    The index lists one row per filer (issuer AND each reporting owner), so the
    same accession appears multiple times — dedupe here. Form type is the first
    space-padded column; match exactly "4" (not 4/A, 424B5, ...).
    """
    out: dict[str, str] = {}
    for line in idx_text.splitlines():
        if not line.startswith("4 "):
            continue
        m = _IDX_ROW.search(line)
        if m:
            out[m.group(2)] = f"edgar/data/{m.group(1)}/{m.group(2)}.txt"
    return out


# ---------------------------------------------------------------- accessions

def fetch_accession(accession_no: str, path: str) -> str:
    """Fetch one full Form 4 submission (.txt). Filings are immutable → cache forever."""
    cache_file = config.RAW_CACHE_DIR / f"{accession_no}.txt"
    if cache_file.exists():
        return cache_file.read_text(encoding="utf-8", errors="replace")
    text = fetch(config.SEC_ARCHIVES_BASE + path).decode("utf-8", errors="replace")
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(text, encoding="utf-8")
    return text


# ---------------------------------------------------------------- ticker maps

def load_ticker_map(max_age_hours: float = 24.0) -> dict[str, str]:
    """CIK (no leading zeros, as str) → ticker, from SEC's company_tickers.json."""
    cache_file = config.CACHE_DIR / "company_tickers.json"
    if not cache_file.exists() or (time.time() - cache_file.stat().st_mtime) > max_age_hours * 3600:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(fetch(config.COMPANY_TICKERS_URL))
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    return {str(row["cik_str"]): row["ticker"] for row in data.values()}


def load_exchange_map(max_age_hours: float = 24.0) -> dict[str, str]:
    """Ticker → exchange (NYSE/Nasdaq/CBOE/OTC...), for the exclude-OTC filter."""
    cache_file = config.CACHE_DIR / "company_tickers_exchange.json"
    if not cache_file.exists() or (time.time() - cache_file.stat().st_mtime) > max_age_hours * 3600:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(fetch(config.COMPANY_TICKERS_EXCHANGE_URL))
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    fields = data["fields"]  # ["cik","name","ticker","exchange"]
    i_ticker, i_exch = fields.index("ticker"), fields.index("exchange")
    return {row[i_ticker]: (row[i_exch] or "") for row in data["data"] if row[i_ticker]}
