"""Strategy engine: qualifying buys and clusters (§2), judgement layer (§4–§5),
and context signals (§3). Everything is computed on the fly from SQLite so the
rolling window stays fresh.
"""
import re
import sqlite3
from datetime import date, timedelta

import pandas as pd

from . import config

QUALIFYING_BUYS_SQL = """
SELECT t.accession_no, t.txn_seq, t.n_owners, t.insider_name, t.insider_cik,
       t.is_director, t.is_officer, t.officer_title, t.is_ten_percent_owner,
       t.transaction_date, t.shares, t.price_per_share, t.value,
       t.shares_owned_after, t.direct_indirect, t.security_title,
       f.ticker, f.cik, f.company_name, f.filed_at, f.filing_url,
       f.aff_10b5_one, t.footnotes
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


# ------------------------------------------------------------------ roles §4

def short_role(row) -> str:
    """Compact role label for display and weighting."""
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


def role_weight(label: str) -> float:
    w = config.ROLE_WEIGHTS.get(label)
    if w is None and re.fullmatch(r"C[A-Z]O", label or ""):
        w = config.ROLE_WEIGHTS["C?O"]
    return w if w is not None else config.ROLE_WEIGHTS["Other"]


def _add_trade_pct(df: pd.DataFrame) -> pd.DataFrame:
    """Trade % (§3–§4): shares traded relative to holdings *before* the trade.
    Left blank when prior holdings are zero (new position) or unknown."""
    prior = df["shares_owned_after"] - df["shares"]
    df["prior_shares"] = prior
    df["trade_pct"] = None
    buy_mask = (df["acquired_disposed"] == "A") & (prior > 0) & df["shares"].notna()
    df.loc[buy_mask, "trade_pct"] = (df.loc[buy_mask, "shares"] / prior[buy_mask]) * 100
    df["trade_pct"] = pd.to_numeric(df["trade_pct"], errors="coerce")
    return df


# ------------------------------------------------------- buying units (§2–§3)

def assign_buying_units(buys: pd.DataFrame) -> pd.Series:
    """Union-find co-filing insiders into independent buying units.

    A joint Form 4 (fund + its GP entities) is one buying decision, not N
    independent insiders — counting each co-filer separately would let a
    single trade masquerade as the strongest cluster on the board. Insiders
    are merged when they co-appear on the same economic trade
    (accession_no, txn_seq); keys are (ticker, cik) so a person on two boards
    is never merged across issuers. Returns a per-row unit id (NaN for rows
    with no insider CIK)."""
    parent: dict = {}

    def find(x):
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    valid = buys["insider_cik"].notna()
    for key in set(zip(buys.loc[valid, "ticker"], buys.loc[valid, "insider_cik"])):
        parent[key] = key
    for _, grp in buys[valid].groupby(["accession_no", "txn_seq"]):
        members = list(set(zip(grp["ticker"], grp["insider_cik"])))
        for other in members[1:]:
            union(members[0], other)

    def unit_of(row):
        if pd.isna(row["insider_cik"]):
            return None
        root = find((row["ticker"], row["insider_cik"]))
        return f"{root[0]}/{root[1]}"

    return buys.apply(unit_of, axis=1)


# --------------------------------------------------------------- load buys

def _flag_inputs(conn: sqlite3.Connection, cutoff: date) -> dict:
    """Set-based inputs for the per-row judgement flags.

    Computed as whole-table passes and merged in pandas rather than as
    correlated EXISTS subqueries — with a few hundred thousand transaction
    rows the correlated form degenerated into per-row scans of filings
    (minutes instead of milliseconds), and set membership is planner-proof."""
    regime_start = (cutoff - timedelta(days=config.REGIME_LOOKBACK_DAYS)).isoformat()
    cut = cutoff.isoformat()
    q = conn.execute
    # V2 §B offering tell: filings where warrants were PURCHASED alongside the
    # shares (a placement package). Code P only — warrant exercises (M/X) are
    # the exercise trap's business, not an offering tell.
    warrant_accs = {r[0] for r in q(
        """SELECT DISTINCT accession_no FROM transactions
           WHERE is_derivative = 1 AND transaction_code = 'P'
             AND acquired_disposed = 'A'
             AND lower(coalesce(security_title, '')) LIKE '%warrant%'""")}
    # §5/§6 exercise trap: (insider, filed day, issuer) with an M and an S line.
    def _code_days(code: str) -> set:
        return {(r[0], r[1], r[2]) for r in q(
            """SELECT DISTINCT t.insider_cik, f.filed_at, f.cik
               FROM transactions t JOIN filings f ON f.accession_no = t.accession_no
               WHERE t.transaction_code = ? AND t.insider_cik IS NOT NULL""", (code,))}
    exercise_keys = _code_days("M") & _code_days("S")
    # V2 §B regime flip inputs + §3 first-time input: (insider, issuer) sets.
    def _pairs(sql: str, *params) -> set:
        return {(r[0], r[1]) for r in q(sql, params)}
    # "A run of sells" (V2 §B): at least REGIME_MIN_SELLS distinct sale filings
    # — one multi-line or co-filed sale must not masquerade as a run.
    lookback_sellers = _pairs(
        f"""SELECT t.insider_cik, f.cik
           FROM transactions t JOIN filings f ON f.accession_no = t.accession_no
           WHERE t.transaction_code = 'S' AND t.acquired_disposed = 'D'
             AND t.is_derivative = 0 AND t.insider_cik IS NOT NULL
             AND t.transaction_date >= ? AND t.transaction_date < ?
           GROUP BY t.insider_cik, f.cik
           HAVING COUNT(DISTINCT t.accession_no) >= {int(config.REGIME_MIN_SELLS)}""",
        regime_start, cut)
    lookback_buyers = _pairs(
        """SELECT DISTINCT t.insider_cik, f.cik
           FROM transactions t JOIN filings f ON f.accession_no = t.accession_no
           WHERE t.transaction_code = 'P' AND t.acquired_disposed = 'A'
             AND t.is_derivative = 0 AND t.insider_cik IS NOT NULL
             AND t.transaction_date >= ? AND t.transaction_date < ?""",
        regime_start, cut)
    prior_buyers = _pairs(
        """SELECT DISTINCT t.insider_cik, f.cik
           FROM transactions t JOIN filings f ON f.accession_no = t.accession_no
           WHERE t.transaction_code = 'P' AND t.acquired_disposed = 'A'
             AND t.is_derivative = 0 AND t.insider_cik IS NOT NULL
             AND t.transaction_date < ?""", cut)
    return {
        "warrant_accs": warrant_accs,
        "exercise_keys": exercise_keys,
        "regime_flip_keys": lookback_sellers - lookback_buyers,
        "prior_buyers": prior_buyers,
    }


def load_qualifying_buys(conn: sqlite3.Connection, window_days: int,
                         min_value: float, as_of: date | None = None) -> pd.DataFrame:
    """Qualifying open-market buys in-window, with per-row judgement columns."""
    as_of = as_of or date.today()
    cutoff = as_of - timedelta(days=window_days)
    df = pd.read_sql_query(QUALIFYING_BUYS_SQL, conn, params={
        "cutoff": cutoff.isoformat(), "as_of": as_of.isoformat(), "min_value": min_value,
    })
    if df.empty:
        return df
    flags = _flag_inputs(conn, cutoff)
    ins_day_iss = list(zip(df["insider_cik"], df["filed_at"], df["cik"]))
    ins_iss = list(zip(df["insider_cik"], df["cik"]))
    df["warrant_pair"] = df["accession_no"].isin(flags["warrant_accs"]).astype(int)
    df["exercise_flag"] = [int(k in flags["exercise_keys"]) for k in ins_day_iss]
    df["regime_flip"] = [int(k in flags["regime_flip_keys"]) for k in ins_iss]
    df["first_time"] = [int(k not in flags["prior_buyers"]) for k in ins_iss]
    df["acquired_disposed"] = "A"  # column not selected; needed by _add_trade_pct
    df = _add_trade_pct(df)
    df["role"] = df.apply(short_role, axis=1)
    df["role_weight"] = df["role"].map(role_weight)
    # §4 conviction bands (on the economic trade value)
    lo, hi = config.NOISE_BAND
    df["conviction"] = ""
    df.loc[df["value"].between(lo, hi), "conviction"] = "noise"
    df.loc[df["value"] >= config.CONVICTION_MIN_VALUE, "conviction"] = "conviction"
    # §5 fund noise: 10%-owner buying a tiny fraction of an existing stake, OR
    # with inconsistent/unknown holdings reporting ("de-weight, don't
    # auto-trust"). A genuine new position (prior == 0) is not noise.
    # V2 carve-out: institutions only — a founder-CEO whose stake crossed 10%
    # (the spec's Hannigan example) is a person, not a fund; the officer bit
    # identifies them. Directors are NOT exempt: deputized fund entities
    # routinely file with is_director=1.
    tiny = df["trade_pct"].notna() & (df["trade_pct"] < config.FUND_NOISE_MAX_TRADE_PCT)
    inconsistent = df["prior_shares"].isna() | (df["prior_shares"] < 0)
    df["fund_noise"] = ((df["is_ten_percent_owner"] == 1)
                        & (df["is_officer"] != 1) & (tiny | inconsistent))
    df["unit"] = assign_buying_units(df)

    # ---- V2 §B low-signal exclusions (nuke before scoring) ----
    fn = df["footnotes"].map(lambda s: s.lower() if isinstance(s, str) else "")
    # Scheduled trades: the 10b5-1 checkbox or a plan footnote.
    df["scheduled"] = is_scheduled(df)
    # Plan purchases: DRIP / 401(k) / ESPP mechanics, not conviction buys.
    df["plan_buy"] = (fn.str.contains("dividend reinvest", regex=False)
                      | fn.str.contains("distribution reinvest", regex=False)
                      | fn.str.contains("401(k)", regex=False)
                      | fn.str.contains("employee stock purchase", regex=False)
                      | fn.str.contains("espp", regex=False))
    # Offerings/placements — three independent tells per V2 §B: warrant-paired
    # shares, multiple independent buying units at one identical price, or a
    # footnote that says so outright.
    same_price_units = df.groupby(
        ["ticker", "transaction_date", "price_per_share"])["unit"].transform("nunique")
    offering_fn = (fn.str.contains("public offering", regex=False)
                   | fn.str.contains("underwrit", regex=False)
                   | fn.str.contains("private placement", regex=False)
                   | fn.str.contains("directed share", regex=False))
    df["offering"] = ((df["warrant_pair"] == 1)
                      | (same_price_units >= config.OFFERING_MIN_SAME_PRICE_UNITS)
                      | offering_fn)
    df["low_signal"] = df["scheduled"] | df["plan_buy"] | df["offering"]
    # V2 §B: ~10% position increase is the "notable" line for an individual.
    df["notable"] = df["trade_pct"].notna() & (df["trade_pct"] >= config.NOTABLE_TRADE_PCT)
    return df


def ticker_buy_months(conn: sqlite3.Connection, before: date,
                      lookback_days: int = config.ROUTINE_LOOKBACK_DAYS) -> dict[str, int]:
    """§3 routine detection input: distinct calendar months with any open-market
    buy per ticker in the lookback period *preceding* the current window (the
    window's own buys must not self-flag the cluster as routine)."""
    start = before - timedelta(days=lookback_days)
    rows = conn.execute("""
        SELECT f.ticker, COUNT(DISTINCT substr(t.transaction_date, 1, 7))
        FROM transactions t JOIN filings f ON f.accession_no = t.accession_no
        WHERE t.transaction_code = 'P' AND t.acquired_disposed = 'A'
          AND t.is_derivative = 0 AND f.ticker IS NOT NULL AND f.ticker != ''
          AND t.transaction_date >= :start AND t.transaction_date < :before
        GROUP BY f.ticker""",
        {"start": start.isoformat(), "before": before.isoformat()}).fetchall()
    return dict(rows)


# ----------------------------------------------------------------- clusters

def _unique_trades(buys: pd.DataFrame) -> pd.DataFrame:
    """One row per economic trade.

    First dedupe joint-filing owner rows on (accession_no, txn_seq); then
    collapse identical-parameter trades filed by the same buying unit across
    SEPARATE accessions — EDGAR caps reporting owners per filing, so large
    fund groups split one trade over several accessions."""
    trades = buys.drop_duplicates(subset=["accession_no", "txn_seq"]).copy()
    # Anonymous trades (no insider CIK) must never merge with each other.
    anon = trades["unit"].isna()
    trades.loc[anon, "unit"] = (trades.loc[anon, "accession_no"]
                                + "#" + trades.loc[anon, "txn_seq"].astype(str))
    return trades.drop_duplicates(
        subset=["ticker", "unit", "transaction_date", "shares", "price_per_share"])


def compute_clusters(buys: pd.DataFrame, min_cluster: int,
                     as_of: date | None = None,
                     routine_months: dict[str, int] | None = None,
                     excluded_counts: dict[str, int] | None = None,
                     lowsig_counts: dict[str, int] | None = None,
                     include_solo_gc: bool = True) -> pd.DataFrame:
    """One row per ticker with >= min_cluster independent buying units in-window
    (§2). Solo General-Counsel buys are kept regardless — §4 rates GC high
    even solo — and flagged."""
    if buys.empty:
        return pd.DataFrame()
    as_of = as_of or date.today()
    routine_months = routine_months or {}
    excluded_counts = excluded_counts or {}
    lowsig_counts = lowsig_counts or {}

    # Any owner's fund-noise flag marks the whole economic trade.
    trade_noise = buys.groupby(["accession_no", "txn_seq"])["fund_noise"].transform("max")
    buys = buys.assign(trade_fund_noise=trade_noise)
    trades = _unique_trades(buys)

    insiders = buys.groupby("ticker").agg(
        n_units=("unit", "nunique"),
        n_filers=("insider_cik", "nunique"),
        company=("company_name", "last"),
        cik=("cik", "last"),
        first_buy=("transaction_date", "min"),
        last_buy=("transaction_date", "max"),
        roles=("role", lambda r: ", ".join(sorted(set(r)))),
        max_trade_pct=("trade_pct", "max"),
        has_gc=("role", lambda r: (r == "GC").any()),
    )
    # §4 role score: each independent buying unit contributes its best role weight.
    per_unit = buys.groupby(["ticker", "unit"])["role_weight"].max()
    insiders["role_score"] = per_unit.groupby("ticker").sum().round(2)
    # §3 first-time: a buying unit is first-time only if every member is.
    unit_ft = buys.groupby(["ticker", "unit"])["first_time"].min()
    insiders["n_first_time"] = (unit_ft[unit_ft == 1].groupby("ticker").count()
                                .reindex(insiders.index).fillna(0).astype(int))
    # V2 §B: buying units whose members flipped from selling to buying.
    unit_rf = buys.groupby(["ticker", "unit"])["regime_flip"].max()
    insiders["n_regime_flip"] = (unit_rf[unit_rf == 1].groupby("ticker").count()
                                 .reindex(insiders.index).fillna(0).astype(int))
    # V2 §B: units with a notable (≥10% position-increase) buy.
    unit_nt = buys.groupby(["ticker", "unit"])["notable"].max()
    insiders["n_notable"] = (unit_nt[unit_nt].groupby("ticker").count()
                             .reindex(insiders.index).fillna(0).astype(int))

    money = trades.groupby("ticker").agg(
        total_value=("value", "sum"),
        largest_buy=("value", "max"),
        n_buys=("value", "size"),
        n_conviction=("conviction", lambda c: (c == "conviction").sum()),
        n_noise=("conviction", lambda c: (c == "noise").sum()),
    )
    noise_value = trades[trades["trade_fund_noise"]].groupby("ticker")["value"].sum()
    money["fund_noise_value"] = noise_value.reindex(money.index).fillna(0.0)

    out = insiders.join(money).reset_index().rename(columns={"n_units": "n_insiders"})
    solo_gc = (out["n_insiders"] < min_cluster) & out["has_gc"]
    keep = out["n_insiders"] >= min_cluster
    if include_solo_gc:
        keep |= solo_gc
    out = out[keep].copy()
    if out.empty:
        return out
    out["solo_gc"] = solo_gc[out.index]

    out["days_since_first"] = (pd.Timestamp(as_of) - pd.to_datetime(out["first_buy"])).dt.days
    out["days_since_last"] = (pd.Timestamp(as_of) - pd.to_datetime(out["last_buy"])).dt.days

    # §5 flags + §3 routine
    out["fund_noise"] = out["fund_noise_value"] > 0.5 * out["total_value"]
    out["stale"] = out["days_since_last"] > config.STALE_AFTER_DAYS
    out["routine"] = out["ticker"].map(
        lambda t: routine_months.get(t, 0) >= config.ROUTINE_MIN_DISTINCT_MONTHS)
    out["n_exercise_excluded"] = out["ticker"].map(
        lambda t: excluded_counts.get(t, 0)).astype(int)
    out["n_lowsig_excluded"] = out["ticker"].map(
        lambda t: lowsig_counts.get(t, 0)).astype(int)

    def _flags(row) -> str:
        f = []
        if row["solo_gc"]:
            f.append("solo-GC")
        if row["n_regime_flip"]:
            f.append(f"regime-flip×{row['n_regime_flip']}")
        if row["n_exercise_excluded"]:
            f.append(f"exercise×{row['n_exercise_excluded']}")
        if row["n_lowsig_excluded"]:
            f.append(f"lowsig×{row['n_lowsig_excluded']}")
        if row["fund_noise"]:
            f.append("fund-noise")
        if row["stale"]:
            f.append("stale")
        if row["routine"]:
            f.append("routine")
        if row["n_noise"] and row["n_noise"] >= row["n_buys"]:
            f.append("all-noise-sized")
        return ", ".join(f)

    out["flags"] = out.apply(_flags, axis=1)
    return out.sort_values(["n_insiders", "role_score", "total_value"],
                           ascending=False).reset_index(drop=True)


def build_screen(conn: sqlite3.Connection, window_days: int, min_value: float,
                 min_cluster: int, include_exercise_flagged: bool = False,
                 include_low_signal: bool = False, include_solo_gc: bool = True,
                 as_of: date | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Full screener pipeline: load → exclude exercise-flagged (§5) and
    low-signal (V2 §B: 10b5-1 / plan / offering) trades → cluster by buying
    unit. Returns (clusters, kept_buys)."""
    as_of = as_of or date.today()
    buys = load_qualifying_buys(conn, window_days, min_value, as_of=as_of)
    if buys.empty:
        return pd.DataFrame(), buys

    def _trade_counts(frame: pd.DataFrame) -> dict[str, int]:
        return (frame.drop_duplicates(subset=["accession_no", "txn_seq"])
                .groupby("ticker").size().to_dict())

    # One flagged owner taints the whole economic trade — exclude it entirely,
    # and count exclusions in trades, not owner-rows.
    exercise_trade = buys.groupby(
        ["accession_no", "txn_seq"])["exercise_flag"].transform("max") == 1
    lowsig_trade = buys.groupby(
        ["accession_no", "txn_seq"])["low_signal"].transform("max")

    drop = pd.Series(False, index=buys.index)
    excluded_counts: dict[str, int] = {}
    lowsig_counts: dict[str, int] = {}
    if not include_exercise_flagged:
        drop |= exercise_trade
        excluded_counts = _trade_counts(buys[exercise_trade])
    if not include_low_signal:
        drop |= lowsig_trade
        # Attribute both-flagged trades to the exercise count only when the
        # exercise exclusion is active — otherwise they'd vanish from both.
        lowsig_mask = (lowsig_trade if include_exercise_flagged
                       else lowsig_trade & ~exercise_trade)
        lowsig_counts = _trade_counts(buys[lowsig_mask])
    kept = buys[~drop]

    cutoff = as_of - timedelta(days=window_days)
    cl = compute_clusters(kept, min_cluster, as_of=as_of,
                          routine_months=ticker_buy_months(conn, before=cutoff),
                          excluded_counts=excluded_counts,
                          lowsig_counts=lowsig_counts,
                          include_solo_gc=include_solo_gc)
    return cl, kept


# --------------------------------------------------------------- drill-down

TICKER_TXNS_SQL = """
SELECT t.insider_name, t.insider_cik, t.is_director, t.is_officer, t.officer_title,
       t.is_ten_percent_owner, t.transaction_date, t.transaction_code,
       t.acquired_disposed, t.is_derivative, t.shares, t.price_per_share, t.value,
       t.shares_owned_after, t.direct_indirect, t.security_title,
       t.footnotes, f.aff_10b5_one,
       f.filed_at, f.filing_url, t.accession_no, t.txn_seq
FROM transactions t
JOIN filings f ON f.accession_no = t.accession_no
WHERE f.ticker = :ticker
ORDER BY t.transaction_date DESC, t.accession_no, t.txn_seq
"""


def is_scheduled(df: pd.DataFrame) -> pd.Series:
    """Shared 10b5-1 predicate: filing checkbox or plan footnote."""
    fn = df["footnotes"].map(lambda s: s.lower() if isinstance(s, str) else "")
    return (df["aff_10b5_one"] == 1) | fn.str.contains("10b5-1", regex=False)


def ticker_transactions(conn: sqlite3.Connection, ticker: str) -> pd.DataFrame:
    """All stored transaction lines for one ticker (drill-down, screen B)."""
    df = pd.read_sql_query(TICKER_TXNS_SQL, conn, params={"ticker": ticker})
    if df.empty:
        return df
    df["role"] = df.apply(short_role, axis=1)
    return _add_trade_pct(df)


def insider_summary(conn: sqlite3.Connection, ticker: str) -> pd.DataFrame:
    """Per-insider activity roll-up for one ticker (drill-down): who they are,
    what they bought and sold on the open market, their current stake, and
    when they last acted."""
    df = ticker_transactions(conn, ticker)
    if df.empty:
        return df
    nd = df[df["is_derivative"] == 0].copy()
    if nd.empty:
        return nd
    # ticker_transactions is ordered newest-first, so first() per group is the
    # most recent value.
    nd["is_buy"] = (nd["transaction_code"] == "P") & (nd["acquired_disposed"] == "A")
    nd["is_sell"] = (nd["transaction_code"] == "S") & (nd["acquired_disposed"] == "D")
    nd["bought_value"] = nd["value"].where(nd["is_buy"])
    nd["sold_value"] = nd["value"].where(nd["is_sell"])
    grouped = nd.groupby("insider_name", dropna=False)
    out = grouped.agg(
        role=("role", "first"),
        officer_title=("officer_title", "first"),
        n_buys=("is_buy", "sum"),
        n_sells=("is_sell", "sum"),
        bought_value=("bought_value", "sum"),
        sold_value=("sold_value", "sum"),
        last_activity=("transaction_date", "max"),
        first_activity=("transaction_date", "min"),
        filing_url=("filing_url", "first"),
    ).reset_index()
    # Current stake: most recent non-null shares-owned-after per insider.
    latest_stake = (nd.dropna(subset=["shares_owned_after"])
                    .groupby("insider_name", dropna=False)["shares_owned_after"].first())
    out["shares_owned"] = out["insider_name"].map(latest_stake)
    out["n_buys"] = out["n_buys"].astype(int)
    out["n_sells"] = out["n_sells"].astype(int)
    return out.sort_values(["bought_value", "last_activity"],
                           ascending=[False, False]).reset_index(drop=True)


def insider_buy_history(conn: sqlite3.Connection, ticker: str) -> pd.DataFrame:
    """Every stored open-market buy of this ticker, one row per economic trade
    (first listed owner), newest last. Powers the per-buy drill-down list,
    the chart markers, and the track record."""
    df = pd.read_sql_query("""
        SELECT t.insider_name, t.insider_cik, t.is_director, t.is_officer,
               t.officer_title, t.is_ten_percent_owner, t.transaction_date,
               t.shares, t.price_per_share, t.value, t.shares_owned_after,
               f.filing_url, t.accession_no, t.txn_seq
        FROM transactions t JOIN filings f ON f.accession_no = t.accession_no
        WHERE f.ticker = :ticker AND t.transaction_code = 'P'
          AND t.acquired_disposed = 'A' AND t.is_derivative = 0
          AND t.price_per_share IS NOT NULL AND t.price_per_share > 0
        ORDER BY t.transaction_date""", conn, params={"ticker": ticker})
    df = df.drop_duplicates(subset=["accession_no", "txn_seq"])
    if not df.empty:
        df["role"] = df.apply(short_role, axis=1)
    return df
