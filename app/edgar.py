"""SEC EDGAR fetch layer: rate-limited, cached, compliant User-Agent."""
import json
import logging
import os
import re
import time
from datetime import date, timedelta
from pathlib import Path

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
    """Rate-limited GET with exponential backoff on throttling and transport errors."""
    for attempt in range(max_retries + 1):
        _limiter.acquire()
        try:
            resp = _session.get(url, timeout=30)
        except requests.exceptions.RequestException as e:
            if attempt < max_retries:
                wait = 2 ** attempt
                log.warning("%s for %s — retrying in %ss", type(e).__name__, url, wait)
                time.sleep(wait)
                continue
            raise
        if resp.status_code == 200:
            return resp.content
        if resp.status_code in RETRY_STATUSES and attempt < max_retries:
            wait = 2 ** attempt
            log.warning("HTTP %s for %s — retrying in %ss", resp.status_code, url, wait)
            time.sleep(wait)
            continue
        resp.raise_for_status()
    raise RuntimeError(f"unreachable: {url}")


def _atomic_write(path: Path, data: bytes) -> None:
    """Write-to-temp-then-rename so a killed process never leaves a truncated
    cache file at the final path (the .exists() checks would trust it forever)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


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


# EDGAR rewrites a day's index for up to ~2 days after first publication,
# so a cached copy is only trustworthy once it was fetched that far after.
INDEX_FINAL_AFTER_DAYS = 2


def index_is_final(day: date, as_of: date | None = None) -> bool:
    return (as_of or date.today()) >= day + timedelta(days=INDEX_FINAL_AFTER_DAYS)


def fetch_form_index(day: date, force: bool = False) -> str:
    """Fetch one daily form index, cached once the index has stopped changing."""
    cache_file = config.INDEX_CACHE_DIR / f"form.{day:%Y%m%d}.idx"
    if cache_file.exists() and not force:
        fetched_on = date.fromtimestamp(cache_file.stat().st_mtime)
        if index_is_final(day, as_of=fetched_on):
            return cache_file.read_text(encoding="utf-8", errors="replace")
    url = f"{config.DAILY_INDEX_BASE}/{day.year}/QTR{quarter_of(day)}/form.{day:%Y%m%d}.idx"
    data = fetch(url)
    _atomic_write(cache_file, data)
    return data.decode("utf-8", errors="replace")


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
    data = fetch(config.SEC_ARCHIVES_BASE + path)
    _atomic_write(cache_file, data)
    return data.decode("utf-8", errors="replace")


# ------------------------------------------------------------- issuer history

_OWNERSHIP_FORMS = {"3", "3/A", "4", "4/A", "5", "5/A"}


def _cached_json(cache_file: Path, url: str, max_age_days: float) -> dict | None:
    if not cache_file.exists() or \
            (time.time() - cache_file.stat().st_mtime) > max_age_days * 86400:
        try:
            _atomic_write(cache_file, fetch(url))
        except Exception:
            return None
    try:
        return json.loads(cache_file.read_text(encoding="utf-8"))
    except ValueError:
        return None


def first_insider_filing_date(cik: str, max_age_days: float = 7.0,
                              max_archive_chunks: int = 8) -> str | None:
    """Earliest Form 3/4/5 filing date for an issuer, from the SEC submissions
    API (cached). Used for the V2 FPI/new-reporter heuristic: a first insider
    filing younger than ~12 months usually means the company just converted
    from foreign-private-issuer status and has NO insider history —
    first-time/routine/track-record signals are meaningless there.

    The API's "recent" window holds only the last 1000 filings, so archived
    chunks are scanned oldest-first for the true earliest ownership form
    (bounded; if the bound is hit, the earliest date seen so far is returned)."""
    sub_dir = config.CACHE_DIR / "submissions"
    j = _cached_json(sub_dir / f"{cik}.json",
                     f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json",
                     max_age_days)
    if not j:
        return None
    try:
        recent = j["filings"]["recent"]
        earliest = min((d for f, d in zip(recent["form"], recent["filingDate"])
                        if f in _OWNERSHIP_FORMS), default=None)
        # Archived chunks predate "recent"; the oldest chunk containing an
        # ownership form contains the earliest one.
        files = sorted(j["filings"].get("files") or [],
                       key=lambda f: f.get("filingFrom", "9999"))
        for chunk in files[:max_archive_chunks]:
            cj = _cached_json(sub_dir / chunk["name"],
                              f"https://data.sec.gov/submissions/{chunk['name']}",
                              max_age_days * 4)
            if cj is None:
                # Unreachable archive → the true earliest date is unknown.
                # Fail safe (None = not flagged new) rather than falsely
                # reporting a long-history filer as a fresh reporter.
                return None
            found = min((d for f, d in zip(cj["form"], cj["filingDate"])
                         if f in _OWNERSHIP_FORMS), default=None)
            if found:
                return found
        return earliest
    except (KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------- ticker maps

def load_ticker_map(max_age_hours: float = 24.0) -> dict[str, str]:
    """CIK (no leading zeros, as str) → ticker, from SEC's company_tickers.json."""
    cache_file = config.CACHE_DIR / "company_tickers.json"
    if not cache_file.exists() or (time.time() - cache_file.stat().st_mtime) > max_age_hours * 3600:
        _atomic_write(cache_file, fetch(config.COMPANY_TICKERS_URL))
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    # A CIK can appear multiple times (common stock, warrants, units...); the
    # file lists the primary security first, so first occurrence wins.
    out: dict[str, str] = {}
    for row in data.values():
        out.setdefault(str(row["cik_str"]), row["ticker"])
    return out


def load_cik_exchange_map(max_age_hours: float = 24.0) -> dict[str, str]:
    """CIK (no leading zeros, as str) → exchange (NYSE/Nasdaq/CBOE/OTC), for the
    exclude-OTC filter. Keyed by CIK because filer-typed ticker symbols routinely
    differ from SEC's canonical forms (BRK.B vs BRK-B, 'NYSE: KRC', ...)."""
    cache_file = config.CACHE_DIR / "company_tickers_exchange.json"
    if not cache_file.exists() or (time.time() - cache_file.stat().st_mtime) > max_age_hours * 3600:
        _atomic_write(cache_file, fetch(config.COMPANY_TICKERS_EXCHANGE_URL))
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    fields = data["fields"]  # ["cik","name","ticker","exchange"]
    i_cik, i_exch = fields.index("cik"), fields.index("exchange")
    out: dict[str, str] = {}
    for row in data["data"]:
        cik, exch = str(row[i_cik]), row[i_exch] or ""
        if exch and not out.get(cik):  # first non-empty exchange per CIK wins
            out[cik] = exch
    return out
