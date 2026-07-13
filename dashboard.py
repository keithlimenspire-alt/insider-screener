"""Insider-buying screener dashboard (Phases 1–4).

Run with:  streamlit run dashboard.py
"""
import altair as alt
import pandas as pd
import streamlit as st

from app import alerts, clusters, config, db, edgar, prices

st.set_page_config(page_title="Insider-Buying Screener", page_icon="📈", layout="wide")


# ------------------------------------------------------------------- data


@st.cache_data(ttl=300)
def get_screen(window_days: int, min_value: float, min_cluster: int,
               include_exercise: bool, solo_gc: bool) -> tuple[pd.DataFrame, pd.DataFrame]:
    conn = db.connect()
    try:
        return clusters.build_screen(conn, window_days, min_value, min_cluster,
                                     include_exercise_flagged=include_exercise,
                                     include_solo_gc=solo_gc)
    finally:
        conn.close()


@st.cache_data(ttl=300)
def get_ticker_txns(ticker: str) -> pd.DataFrame:
    conn = db.connect()
    try:
        return clusters.ticker_transactions(conn, ticker)
    finally:
        conn.close()


@st.cache_data(ttl=300)
def get_buy_history(ticker: str) -> pd.DataFrame:
    conn = db.connect()
    try:
        return clusters.insider_buy_history(conn, ticker)
    finally:
        conn.close()


@st.cache_data(ttl=3600)
def get_cik_exchange_map() -> dict:
    try:
        return edgar.load_cik_exchange_map()
    except Exception:
        return {}


@st.cache_data(ttl=3600)
def get_near_high(ticker: str):
    return prices.near_high(ticker)


@st.cache_data(ttl=3600)
def get_market_cap(ticker: str):
    return prices.market_cap(ticker)


@st.cache_data(ttl=3600)
def get_price_history(ticker: str) -> pd.DataFrame | None:
    return prices.get_history(ticker)


@st.cache_data(ttl=300)
def db_stats() -> dict:
    conn = db.connect()
    try:
        n_filings = conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0]
        n_days = conn.execute("SELECT COUNT(*) FROM ingested_days").fetchone()[0]
        span = conn.execute("SELECT MIN(filed_at), MAX(filed_at) FROM filings").fetchone()
        return {"filings": n_filings, "days": n_days, "span": span}
    finally:
        conn.close()


@st.cache_data(ttl=300)
def get_recent_alerts() -> list:
    conn = db.connect()
    try:
        return alerts.recent_alerts(conn)
    finally:
        conn.close()


# ------------------------------------------------------------------- sidebar

st.sidebar.title("Filters")
window_days = st.sidebar.slider("Cluster window (days)", 7, 90, config.DEFAULT_WINDOW_DAYS,
                                help="Rolling window over transaction dates (§2: 30–45d)")
min_value = st.sidebar.number_input("Min single-buy value ($)", min_value=0,
                                    value=config.DEFAULT_MIN_BUY_VALUE, step=25_000,
                                    help="§2: screen by dollar value, not shares")
min_cluster = st.sidebar.slider("Min insiders in cluster", 1, 5, config.DEFAULT_MIN_CLUSTER_SIZE,
                                help="§2–§3: ≥2 to screen, ≥3 ranks higher")
min_role_score = st.sidebar.slider("Min role score", 0.0, 3.0, 0.0, 0.1,
                                   help="§4 weighted roles: GC 1.0 · CFO 0.9 · other "
                                        "C-suite 0.8 · VP 0.7 · Dir 0.4 · CEO 0.3 · 10% 0.1. "
                                        "Cluster score = sum over distinct insiders.")
exclude_otc = st.sidebar.checkbox(
    "Exclude OTC", value=True,
    help="Drops issuers SEC classifies as OTC (matched by CIK). Issuers absent "
         "from SEC's exchange file are kept — absence is not proof of OTC.")
include_exercise = st.sidebar.checkbox(
    "Include exercise-flagged buys", value=False,
    help="§5 Starbucks trap: buys by insiders who filed an option exercise (M) "
         "and a sale (S) at this issuer the same day are excluded by default.")
solo_gc = st.sidebar.checkbox(
    "Include solo GC buys", value=True,
    help="§4: a General Counsel buying is a high signal even alone — kept on "
         "screen (flagged solo-GC) even below the cluster minimum.")

st.sidebar.divider()
st.sidebar.subheader("Market data (yfinance)")
price_context = st.sidebar.checkbox(
    "Near-highs context", value=False,
    help="§3: fetch price history per screened ticker and show % below the "
         "trailing 3-year high. First fetch takes ~1s per ticker, then cached.")
cap_choice = st.sidebar.selectbox(
    "Market-cap cap", ["Any", "≤ $500M", "≤ $2B", "≤ $10B", "≤ $50B"],
    help="Filters screened tickers by market cap (fetched per ticker, cached daily).")
CAP_LIMITS = {"Any": None, "≤ $500M": 5e8, "≤ $2B": 2e9, "≤ $10B": 1e10, "≤ $50B": 5e10}

stats = db_stats()
st.sidebar.divider()
st.sidebar.caption(
    f"DB: {stats['filings']:,} filings over {stats['days']} ingested days "
    f"({stats['span'][0]} → {stats['span'][1]})" if stats["filings"]
    else "DB is empty — run `python -m app.ingest --days 45` first.")

# ------------------------------------------------------------------ screener

st.title("Insider-Buying Screener")
st.caption("Open-market insider purchases (Form 4, code P/A, non-derivative) "
           "clustered per ticker, with the §4–§5 judgement layer. "
           "Screening tool only — no trade execution, no exit logic.")

recent = get_recent_alerts()
if recent:
    with st.expander(f"🔔 Alerts ({len(recent)} recent)"):
        for ts, ticker, kind, message in recent:
            st.write(f"`{ts}` **{kind}** — {message}")

df, kept_buys = get_screen(window_days, float(min_value), min_cluster,
                           include_exercise, solo_gc)

if exclude_otc and not df.empty:
    exch = get_cik_exchange_map()
    if exch:
        exchange = df["cik"].map(lambda c: exch.get(str(c), "").upper())
        df = df[exchange != "OTC"].copy()
    else:
        st.warning("SEC exchange data unavailable — the exclude-OTC filter is "
                   "inactive this session.")

if not df.empty and min_role_score > 0:
    df = df[df["role_score"] >= min_role_score].copy()

cap_limit = CAP_LIMITS[cap_choice]
if not df.empty and cap_limit is not None:
    with st.spinner(f"Fetching market caps for {len(df)} tickers…"):
        caps = {t: get_market_cap(t) for t in df["ticker"]}
    df["market_cap"] = df["ticker"].map(caps)
    # Unknown caps are kept — a Yahoo miss must not silently hide a cluster.
    df = df[df["market_cap"].isna() | (df["market_cap"] <= cap_limit)].copy()

if not df.empty and price_context:
    prog = st.progress(0.0, text="Fetching price history…")
    below = {}
    tickers = list(df["ticker"])
    for i, t in enumerate(tickers):
        nh = get_near_high(t)
        below[t] = nh[1] if nh else None
        prog.progress((i + 1) / len(tickers), text=f"Price history: {t}")
    prog.empty()
    df["pct_below_high"] = df["ticker"].map(below)
    df["near_high"] = df["pct_below_high"].notna() & (
        df["pct_below_high"] <= config.NEAR_HIGH_MAX_PCT_BELOW)

if df.empty:
    st.info("No clusters match the current filters. Widen the window, lower the "
            "minimum value or role score, or ingest more days of filings.")
    st.stop()

cols = ["ticker", "company", "n_insiders", "n_filers", "n_buys", "total_value",
        "largest_buy", "role_score", "max_trade_pct", "n_conviction", "n_first_time",
        "days_since_first", "days_since_last", "roles", "flags"]
if price_context:
    cols.insert(2, "near_high")
    cols.insert(3, "pct_below_high")
if cap_limit is not None:
    cols.insert(2, "market_cap")
show = df[cols]

event = st.dataframe(
    show,
    width="stretch",
    hide_index=True,
    on_select="rerun",
    selection_mode="single-row",
    column_config={
        "ticker": st.column_config.TextColumn("Ticker"),
        "company": st.column_config.TextColumn("Company", width="medium"),
        "market_cap": st.column_config.NumberColumn(
            "Mkt cap", format="$%.0f",
            help="From Yahoo Finance; clusters with unknown caps stay visible."),
        "near_high": st.column_config.CheckboxColumn(
            "Near high",
            help=f"§3: within {config.NEAR_HIGH_MAX_PCT_BELOW:.0f}% of the trailing "
                 f"{config.PRICE_HISTORY_YEARS}-year high"),
        "pct_below_high": st.column_config.NumberColumn(
            "% below high", format="%.1f%%",
            help=f"§3: distance below the trailing {config.PRICE_HISTORY_YEARS}-year high"),
        "n_insiders": st.column_config.NumberColumn(
            "# insiders",
            help="Independent buying units: co-filers of one joint Form 4 (a fund "
                 "family) count once. '# filers' shows the raw entity count."),
        "n_filers": st.column_config.NumberColumn(
            "# filers", help="Raw distinct reporting-owner CIKs, joint co-filers included"),
        "n_buys": st.column_config.NumberColumn("# buys"),
        "total_value": st.column_config.NumberColumn("Total $", format="$%.0f"),
        "largest_buy": st.column_config.NumberColumn("Largest buy", format="$%.0f"),
        "role_score": st.column_config.NumberColumn(
            "Role score", format="%.2f",
            help="§4: sum of role weights over distinct insiders "
                 "(GC 1.0 · CFO 0.9 · C-suite 0.8 · VP 0.7 · Dir 0.4 · CEO 0.3 · 10% 0.1)"),
        "max_trade_pct": st.column_config.NumberColumn(
            "Max trade %", format="%.1f%%",
            help="§3–§4: largest buy relative to that insider's prior holdings — "
                 "the real conviction signal"),
        "n_conviction": st.column_config.NumberColumn(
            "# conviction", help=f"§4: buys ≥ ${config.CONVICTION_MIN_VALUE:,}"),
        "n_first_time": st.column_config.NumberColumn(
            "# first-time", help="§3: insiders whose first recorded buy of this "
                                 "issuer falls in the window (grows more meaningful "
                                 "as DB history accumulates)"),
        "days_since_first": st.column_config.NumberColumn("Days since 1st"),
        "days_since_last": st.column_config.NumberColumn("Days since last"),
        "roles": st.column_config.TextColumn("Roles", width="medium"),
        "flags": st.column_config.TextColumn(
            "Flags", width="medium",
            help="solo-GC = General Counsel buying alone (§4 keeps it) · "
                 "exercise×N = N trades excluded for same-day M+S (§5) · fund-noise = "
                 ">50% of $ from 10%-owners buying <2% of their stake (§5) · stale = "
                 f"last buy >{config.STALE_AFTER_DAYS}d ago (§5) · routine = buys in "
                 f"≥{config.ROUTINE_MIN_DISTINCT_MONTHS} distinct months in the year "
                 "before the window (§3) · all-noise-sized = every buy in the "
                 "$3k–$15k 401(k)/ESPP band (§4)"),
    },
)
st.caption(f"{len(df)} cluster(s) · window {window_days}d · min buy ${min_value:,.0f} · "
           f"min {min_cluster} insider(s)"
           + (f" · role score ≥ {min_role_score}" if min_role_score > 0 else "")
           + (" · exercise-flagged included" if include_exercise else ""))

selected_ticker = None
if event.selection.rows:
    selected_ticker = show.iloc[event.selection.rows[0]]["ticker"]

# ---------------------------------------------------------------- drill-down

st.divider()
st.subheader("Ticker drill-down")
all_tickers = df["ticker"].tolist()
pick = st.selectbox("Ticker", all_tickers,
                    index=all_tickers.index(selected_ticker) if selected_ticker else 0,
                    help="Select a row above or pick a ticker here")

if pick:
    tab_txns, tab_chart, tab_record = st.tabs(
        ["Transactions", "Buys over price", "Insider track record"])

    with tab_txns:
        txns = get_ticker_txns(pick)
        only_buys = st.toggle("Open-market buys only (code P)", value=False)
        if only_buys:
            txns = txns[(txns["transaction_code"] == "P") & (txns["acquired_disposed"] == "A")
                        & (txns["is_derivative"] == 0)]
        if txns.empty:
            st.info("No stored transactions for this ticker under the current toggle.")
        else:
            view = txns[["insider_name", "role", "transaction_date", "transaction_code",
                         "acquired_disposed", "is_derivative", "shares", "price_per_share",
                         "value", "trade_pct", "shares_owned_after", "security_title",
                         "filing_url"]].copy()
            st.dataframe(
                view,
                width="stretch",
                hide_index=True,
                column_config={
                    "insider_name": st.column_config.TextColumn("Insider", width="medium"),
                    "role": st.column_config.TextColumn("Role"),
                    "transaction_date": st.column_config.TextColumn("Date"),
                    "transaction_code": st.column_config.TextColumn("Code",
                        help="P=open-market buy · S=sale · M=option exercise · A=grant · "
                             "G=gift · F=tax withholding"),
                    "acquired_disposed": st.column_config.TextColumn("A/D"),
                    "is_derivative": st.column_config.CheckboxColumn("Deriv?"),
                    "shares": st.column_config.NumberColumn("Shares", format="%.0f"),
                    "price_per_share": st.column_config.NumberColumn("Price", format="$%.2f"),
                    "value": st.column_config.NumberColumn("Value", format="$%.0f"),
                    "trade_pct": st.column_config.NumberColumn("Trade %", format="%.1f%%",
                        help="Shares traded relative to holdings before the trade — "
                             "the real conviction signal (§3–§4). Blank = new position "
                             "or unknown prior holdings."),
                    "shares_owned_after": st.column_config.NumberColumn("Shares after",
                                                                        format="%.0f"),
                    "security_title": st.column_config.TextColumn("Security"),
                    "filing_url": st.column_config.LinkColumn("Form 4", display_text="filing"),
                },
            )

    with tab_chart:
        hist = get_price_history(pick)
        buys_hist = get_buy_history(pick)
        if hist is None or hist.empty:
            st.info("No price history available for this ticker (delisted, OTC, "
                    "or unknown to Yahoo Finance).")
        else:
            line = alt.Chart(hist).mark_line(color="#4c78a8").encode(
                x=alt.X("date:T", title=None),
                y=alt.Y("close:Q", title="Close ($)", scale=alt.Scale(zero=False)),
            )
            layers = [line]
            if not buys_hist.empty:
                pts_df = buys_hist.copy()
                pts_df["transaction_date"] = pd.to_datetime(pts_df["transaction_date"],
                                                            errors="coerce")
                pts_df = pts_df.dropna(subset=["transaction_date"])
                pts = alt.Chart(pts_df).mark_point(
                    color="#2ca02c", size=120, filled=True, shape="triangle-up",
                ).encode(
                    x="transaction_date:T",
                    y=alt.Y("price_per_share:Q"),
                    tooltip=[
                        alt.Tooltip("insider_name:N", title="Insider"),
                        alt.Tooltip("transaction_date:T", title="Date"),
                        alt.Tooltip("shares:Q", title="Shares", format=",.0f"),
                        alt.Tooltip("price_per_share:Q", title="Price", format="$.2f"),
                        alt.Tooltip("value:Q", title="Value", format="$,.0f"),
                    ],
                )
                layers.append(pts)
            st.altair_chart(alt.layer(*layers).interactive(), width="stretch")
            nh = get_near_high(pick)
            if nh:
                last, pct_below = nh
                st.caption(f"Last close ${last:,.2f} · {pct_below:.1f}% below the "
                           f"trailing {config.PRICE_HISTORY_YEARS}-year high"
                           + (" · **near-high** (§3)"
                              if pct_below <= config.NEAR_HIGH_MAX_PCT_BELOW else ""))

    with tab_record:
        buys_hist = get_buy_history(pick)
        if buys_hist.empty:
            st.info("No recorded open-market buys for this ticker.")
        else:
            nh = get_near_high(pick)
            rec = buys_hist[["insider_name", "transaction_date", "shares",
                             "price_per_share", "value"]].copy()
            if nh:
                last_close = nh[0]
                rec["pct_since_buy"] = (last_close - rec["price_per_share"]) \
                    / rec["price_per_share"] * 100.0
                st.caption(f"Return measured against the last close (${last_close:,.2f}). "
                           "Track record depth grows as DB history accumulates.")
            st.dataframe(
                rec.sort_values("transaction_date", ascending=False),
                width="stretch",
                hide_index=True,
                column_config={
                    "insider_name": st.column_config.TextColumn("Insider", width="medium"),
                    "transaction_date": st.column_config.TextColumn("Buy date"),
                    "shares": st.column_config.NumberColumn("Shares", format="%.0f"),
                    "price_per_share": st.column_config.NumberColumn("Buy price",
                                                                     format="$%.2f"),
                    "value": st.column_config.NumberColumn("Value", format="$%.0f"),
                    "pct_since_buy": st.column_config.NumberColumn(
                        "% since buy", format="%.1f%%",
                        help="§3 insider track record: price change from that buy "
                             "to the last close"),
                },
            )
