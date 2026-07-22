"""Bulk-load historical Form 4 data from SEC DERA quarterly datasets.

The SEC publishes parsed insider-transaction tables quarterly
(insider-transactions-data-sets): SUBMISSION, REPORTINGOWNER,
NONDERIV_TRANS, DERIV_TRANS, FOOTNOTES. One ~10 MB ZIP per quarter replaces
hundreds of thousands of individual EDGAR fetches, making multi-year history
practical while staying far under SEC rate limits.

Mapping rules mirror app.ingest exactly: Form 4 only (no 3/5, no
amendments), ticker canonicalized by CIK against SEC's map (validated
self-reported symbol as fallback), one row per transaction line × reporting
owner, footnote texts joined per line, CIKs stored unpadded. Accessions
already in the DB are skipped — the live pipeline's rows win.

Usage:
    python -m app.bulkload 2023q1 2025q4     # load a quarter range
"""
import io
import logging
import re
import sys
import zipfile
from pathlib import Path

import pandas as pd
import requests

from . import config, db, edgar
from .ingest import _TICKER_PLACEHOLDERS, _TICKER_RE

log = logging.getLogger("bulkload")

BULK_DIR = config.CACHE_DIR / "bulk"
URL = ("https://www.sec.gov/files/structureddata/data/"
       "insider-transactions-data-sets/{q}_form345.zip")

_MONTHS = {m: i for i, m in enumerate(
    "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split(), 1)}
_FN_SPLIT = re.compile(r"[^A-Za-z0-9]+")


def _date(s: str) -> str | None:
    """'31-OCT-2025' → '2025-10-31'."""
    if not s:
        return None
    try:
        d, mon, y = s.strip().split("-")
        return f"{y}-{_MONTHS[mon.upper()]:02d}-{int(d):02d}"
    except (ValueError, KeyError):
        return None


def _cik(s: str) -> str | None:
    s = (s or "").strip()
    return str(int(s)) if s.isdigit() else (s or None)


def _num(s: str) -> float | None:
    s = (s or "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def fetch_quarter(q: str) -> Path:
    BULK_DIR.mkdir(parents=True, exist_ok=True)
    path = BULK_DIR / f"{q}_form345.zip"
    if not path.exists():
        log.info("downloading %s…", q)
        r = requests.get(URL.format(q=q),
                         headers={"User-Agent": config.SEC_USER_AGENT}, timeout=300)
        r.raise_for_status()
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(r.content)
        tmp.replace(path)
    return path


def _read(z: zipfile.ZipFile, name: str) -> pd.DataFrame:
    with z.open(name) as fh:
        return pd.read_csv(io.TextIOWrapper(fh, encoding="utf-8", errors="replace"),
                           sep="\t", dtype=str, keep_default_na=False,
                           quoting=3, low_memory=False)


def load_quarter(conn, q: str, ticker_map: dict[str, str]) -> tuple[int, int]:
    """Returns (filings_inserted, filings_skipped_existing)."""
    z = zipfile.ZipFile(fetch_quarter(q))
    sub = _read(z, "SUBMISSION.tsv")
    own = _read(z, "REPORTINGOWNER.tsv")
    nd = _read(z, "NONDERIV_TRANS.tsv")
    dv = _read(z, "DERIV_TRANS.tsv")
    fns = _read(z, "FOOTNOTES.tsv")

    sub = sub[sub["DOCUMENT_TYPE"].str.strip() == "4"]
    existing = {r[0] for r in conn.execute("SELECT accession_no FROM filings")}
    sub = sub[~sub["ACCESSION_NUMBER"].isin(existing)]
    keep = set(sub["ACCESSION_NUMBER"])
    n_skip = len(existing & set(_read(z, "SUBMISSION.tsv")["ACCESSION_NUMBER"])) \
        if existing else 0

    fn_col = "FOOTNOTE_TXT" if "FOOTNOTE_TXT" in fns.columns else fns.columns[-1]
    fn_id_col = next((c for c in fns.columns if "ID" in c.upper()
                      and c != "ACCESSION_NUMBER"), fns.columns[1])
    fn_map = {(a, i): t for a, i, t in zip(
        fns["ACCESSION_NUMBER"], fns[fn_id_col], fns[fn_col]) if t}

    owners_by_acc: dict[str, list[dict]] = {}
    for r in own.itertuples(index=False):
        acc = r.ACCESSION_NUMBER
        if acc not in keep:
            continue
        rel = (r.RPTOWNER_RELATIONSHIP or "").lower()
        o = {"cik": _cik(r.RPTOWNERCIK), "name": (r.RPTOWNERNAME or "").strip() or None,
             "is_director": int("director" in rel),
             "is_officer": int("officer" in rel),
             "officer_title": (r.RPTOWNER_TITLE or "").strip() or None,
             "is_ten_percent_owner": int("tenpercent" in rel or "10" in rel)}
        lst = owners_by_acc.setdefault(acc, [])
        if o["cik"] is None or all(x["cik"] != o["cik"] for x in lst):
            lst.append(o)

    def txn_rows(frame: pd.DataFrame, deriv: int) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        fn_cols = [c for c in frame.columns if c.endswith("_FN")]
        sk_col = "NONDERIV_TRANS_SK" if not deriv else "DERIV_TRANS_SK"
        frame = frame.sort_values(sk_col)
        for r in frame.itertuples(index=False):
            d = r._asdict() if hasattr(r, "_asdict") else dict(zip(frame.columns, r))
            acc = d["ACCESSION_NUMBER"]
            if acc not in keep:
                continue
            ids = []
            for c in fn_cols:
                ids += [t for t in _FN_SPLIT.split(d.get(c, "") or "") if t]
            texts = list(dict.fromkeys(
                fn_map[(acc, i)] for i in ids if (acc, i) in fn_map))
            shares, price = _num(d.get("TRANS_SHARES")), _num(d.get("TRANS_PRICEPERSHARE"))
            out.setdefault(acc, []).append({
                "is_derivative": deriv,
                "security_title": (d.get("SECURITY_TITLE") or "").strip() or None,
                "transaction_date": _date(d.get("TRANS_DATE")),
                "transaction_code": (d.get("TRANS_CODE") or "").strip() or None,
                "acquired_disposed": (d.get("TRANS_ACQUIRED_DISP_CD") or "").strip() or None,
                "shares": shares, "price_per_share": price,
                "value": shares * price if shares is not None and price is not None else None,
                "shares_owned_after": _num(d.get("SHRS_OWND_FOLWNG_TRANS")),
                "direct_indirect": (d.get("DIRECT_INDIRECT_OWNERSHIP") or "").strip() or None,
                "footnotes": "; ".join(texts) or None,
            })
        return out

    nd_rows, dv_rows = txn_rows(nd, 0), txn_rows(dv, 1)

    f_batch, t_batch = [], []
    for r in sub.itertuples(index=False):
        acc = r.ACCESSION_NUMBER
        cik = _cik(r.ISSUERCIK) or ""
        raw_sym = (r.ISSUERTRADINGSYMBOL or "").strip().upper()
        ticker = ticker_map.get(cik) or (
            raw_sym if _TICKER_RE.fullmatch(raw_sym)
            and raw_sym not in _TICKER_PLACEHOLDERS else None)
        filed = _date(r.FILING_DATE)
        if not filed:
            continue
        acc_nodash = acc.replace("-", "")
        f_batch.append({
            "accession_no": acc, "cik": cik,
            "company_name": (r.ISSUERNAME or "").strip() or None,
            "ticker": ticker, "filed_at": filed,
            "period_of_report": _date(r.PERIOD_OF_REPORT),
            "filing_url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{acc}-index.htm",
            # The 10b5-1 checkbox column only exists from 2023 on (rule
            # change); older quarters rely on footnote text for scheduling.
            "aff_10b5_one": 1 if str(getattr(r, "AFF10B5ONE", "") or "").strip()
                                 in ("1", "true") else 0,
        })
        owners = owners_by_acc.get(acc) or [{
            "cik": None, "name": None, "is_director": 0, "is_officer": 0,
            "officer_title": None, "is_ten_percent_owner": 0}]
        seq = 0
        for txn in (nd_rows.get(acc) or []) + (dv_rows.get(acc) or []):
            for o in owners:
                t_batch.append({"accession_no": acc, "txn_seq": seq,
                                "n_owners": len(owners),
                                "insider_name": o["name"], "insider_cik": o["cik"],
                                "is_director": o["is_director"],
                                "is_officer": o["is_officer"],
                                "officer_title": o["officer_title"],
                                "is_ten_percent_owner": o["is_ten_percent_owner"],
                                **txn})
            seq += 1

    with conn:
        conn.executemany(
            """INSERT OR IGNORE INTO filings
               (accession_no, cik, company_name, ticker, filed_at,
                period_of_report, filing_url, aff_10b5_one)
               VALUES (:accession_no, :cik, :company_name, :ticker, :filed_at,
                       :period_of_report, :filing_url, :aff_10b5_one)""", f_batch)
        conn.executemany(
            """INSERT OR IGNORE INTO transactions
               (accession_no, txn_seq, n_owners, is_derivative, insider_name,
                insider_cik, is_director, is_officer, officer_title,
                is_ten_percent_owner, transaction_date, transaction_code,
                acquired_disposed, shares, price_per_share, value,
                shares_owned_after, direct_indirect, security_title, footnotes)
               VALUES (:accession_no, :txn_seq, :n_owners, :is_derivative,
                       :insider_name, :insider_cik, :is_director, :is_officer,
                       :officer_title, :is_ten_percent_owner, :transaction_date,
                       :transaction_code, :acquired_disposed, :shares,
                       :price_per_share, :value, :shares_owned_after,
                       :direct_indirect, :security_title, :footnotes)""", t_batch)
    log.info("%s: +%d filings (%d rows), %d already present",
             q, len(f_batch), len(t_batch), n_skip)
    return len(f_batch), n_skip


def quarters(start: str, end: str) -> list[str]:
    y0, q0 = int(start[:4]), int(start[5])
    y1, q1 = int(end[:4]), int(end[5])
    out = []
    y, q = y0, q0
    while (y, q) <= (y1, q1):
        out.append(f"{y}q{q}")
        q += 1
        if q == 5:
            y, q = y + 1, 1
    return out


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    start, end = sys.argv[1], sys.argv[2]
    conn = db.connect()
    ticker_map = edgar.load_ticker_map()
    total = 0
    for q in quarters(start, end):
        n, _ = load_quarter(conn, q, ticker_map)
        total += n
    log.info("done: %d filings loaded", total)


if __name__ == "__main__":
    main()
