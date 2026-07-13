"""Insider-buying screener dashboard (Phase 1).

Run with:  streamlit run dashboard.py
"""
from datetime import date

import pandas as pd
import streamlit as st

from app import clusters, config, db, edgar

st.set_page_config(page_title="Insider-Buying Screener", page_icon="📈", layout="wide")


@st.cache_data(ttl=300)
def get_clusters(window_days: int, min_value: float, min_cluster: int) -> pd.DataFrame:
    conn = db.connect()
    try:
        buys = clusters.load_qualifying_buys(conn, window_days, min_value)
        return clusters.compute_clusters(buys, min_cluster)
    finally:
        conn.close()


@st.cache_data(ttl=300)
def get_ticker_txns(ticker: str) -> pd.DataFrame:
    conn = db.connect()
    try:
        return clusters.ticker_transactions(conn, ticker)
    finally:
        conn.close()


@st.cache_data(ttl=3600)
def get_exchange_map() -> dict:
    try:
        return edgar.load_exchange_map()
    except Exception:
        return {}


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


# ------------------------------------------------------------------- sidebar
st.sidebar.title("Filters")
window_days = st.sidebar.slider("Cluster window (days)", 7, 90, config.DEFAULT_WINDOW_DAYS,
                                help="Rolling window over transaction dates (§2: 30–45d)")
min_value = st.sidebar.number_input("Min single-buy value ($)", min_value=0,
                                    value=config.DEFAULT_MIN_BUY_VALUE, step=25_000,
                                    help="§2: screen by dollar value, not shares")
min_cluster = st.sidebar.slider("Min insiders in cluster", 1, 5, config.DEFAULT_MIN_CLUSTER_SIZE,
                                help="§2–§3: ≥2 to screen, ≥3 ranks higher")
exclude_otc = st.sidebar.checkbox("Exclude OTC / unlisted", value=True)

stats = db_stats()
st.sidebar.divider()
st.sidebar.caption(
    f"DB: {stats['filings']:,} filings over {stats['days']} ingested days "
    f"({stats['span'][0]} → {stats['span'][1]})" if stats["filings"]
    else "DB is empty — run `python -m app.ingest --days 45` first."
)

# ------------------------------------------------------------------ screener
st.title("Insider-Buying Screener")
st.caption("Open-market insider purchases (Form 4, code P/A, non-derivative) "
           "clustered per ticker. Screening tool only — no trade execution, no exit logic.")

df = get_clusters(window_days, float(min_value), min_cluster)

if exclude_otc and not df.empty:
    exch = get_exchange_map()
    if exch:
        listed = df["ticker"].map(lambda t: exch.get(t, "")).str.upper()
        df = df[listed.isin(["NYSE", "NASDAQ", "CBOE", "NYSE MKT", "NYSE ARCA"])]

if df.empty:
    st.info("No clusters match the current filters. Widen the window, lower the "
            "minimum value, or ingest more days of filings.")
else:
    show = df[["ticker", "company", "n_insiders", "n_buys", "total_value", "largest_buy",
               "days_since_first", "days_since_last", "roles"]]
    event = st.dataframe(
        show,
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "ticker": st.column_config.TextColumn("Ticker"),
            "company": st.column_config.TextColumn("Company", width="medium"),
            "n_insiders": st.column_config.NumberColumn("# insiders",
                help="Distinct insiders with qualifying buys in the window"),
            "n_buys": st.column_config.NumberColumn("# buys"),
            "total_value": st.column_config.NumberColumn("Total $", format="$%.0f"),
            "largest_buy": st.column_config.NumberColumn("Largest buy", format="$%.0f"),
            "days_since_first": st.column_config.NumberColumn("Days since 1st"),
            "days_since_last": st.column_config.NumberColumn("Days since last"),
            "roles": st.column_config.TextColumn("Roles", width="medium"),
        },
    )
    st.caption(f"{len(df)} cluster(s) · window {window_days}d · "
               f"min buy ${min_value:,.0f} · min {min_cluster} insider(s)")

    selected_ticker = None
    if event.selection.rows:
        selected_ticker = show.iloc[event.selection.rows[0]]["ticker"]

    # -------------------------------------------------------------- drill-down
    st.divider()
    st.subheader("Ticker drill-down")
    all_tickers = df["ticker"].tolist()
    pick = st.selectbox("Ticker", all_tickers,
                        index=all_tickers.index(selected_ticker) if selected_ticker else 0,
                        help="Select a row above or pick a ticker here")
    if pick:
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
            view["trade_pct"] = pd.to_numeric(view["trade_pct"], errors="coerce")
            st.dataframe(
                view,
                use_container_width=True,
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
