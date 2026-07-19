"""Insider-buying screener dashboard (Phases 1–4).

Run with:  streamlit run dashboard.py
"""
from datetime import date, timedelta

import altair as alt
import pandas as pd
import streamlit as st

from app import (alerts, bootstrap, breadth, classify, clusters, config, db,
                 edgar, ingest, prices)

st.set_page_config(page_title="Insider-Buying Screener", page_icon="📈", layout="wide")

# Hosted deployments boot with an empty disk — pull the latest data snapshot
# from the repo's GitHub release before anything queries the DB.
if bootstrap.db_is_empty():
    try:
        _token = st.secrets.get("GITHUB_TOKEN")
    except Exception:  # no secrets.toml configured (local runs)
        _token = None
    with st.spinner("First run: downloading the filings database (~40 MB)…"):
        _ok, _msg = bootstrap.ensure_db(_token)
    if not _ok:
        st.error(
            f"Could not fetch the data snapshot ({_msg}). For a private repo, "
            "add a GitHub token with read access as the app secret "
            "`GITHUB_TOKEN`, or run `python -m app.ingest --days 45` locally.")
        st.stop()


# ------------------------------------------------------------------- data


@st.cache_data(ttl=300)
def get_screen(window_days: int, min_value: float, min_cluster: int,
               include_exercise: bool, include_lowsig: bool,
               solo_gc: bool) -> tuple[pd.DataFrame, pd.DataFrame]:
    conn = db.connect()
    try:
        return clusters.build_screen(conn, window_days, min_value, min_cluster,
                                     include_exercise_flagged=include_exercise,
                                     include_low_signal=include_lowsig,
                                     include_solo_gc=solo_gc)
    finally:
        conn.close()


@st.cache_data(ttl=3600)
def get_breadth() -> dict | None:
    conn = db.connect()
    try:
        return breadth.current_breadth(conn)
    finally:
        conn.close()


@st.cache_data(ttl=86400)
def get_first_insider_filing(cik: str) -> str | None:
    return edgar.first_insider_filing_date(cik)


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


@st.cache_data(ttl=300)
def get_insider_summary(ticker: str) -> pd.DataFrame:
    conn = db.connect()
    try:
        return clusters.insider_summary(conn, ticker)
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
                                help="How far back to look for insider purchases. Buys "
                                     "within this many days are grouped into one signal; "
                                     "the strategy favors 30–45 days.")
min_value = st.sidebar.number_input("Min single-buy value ($)", min_value=0,
                                    value=config.DEFAULT_MIN_BUY_VALUE, step=25_000,
                                    help="Ignore purchases smaller than this dollar amount. "
                                         "Dollar value matters more than share count — a "
                                         "$500 buy of a cheap stock isn't a signal.")
min_cluster = st.sidebar.slider("Min insiders in cluster", 1, 5, config.DEFAULT_MIN_CLUSTER_SIZE,
                                help="Only show companies where at least this many different "
                                     "insiders bought. Two or more buying together is the "
                                     "core 'cluster' signal; three or more is stronger.")
min_role_score = st.sidebar.slider("Min role score", 0.0, 3.0, 0.0, 0.1,
                                   help="Filter by WHO is buying. Each company's score adds "
                                        "up its buyers' role weights: General Counsel 1.0 "
                                        "(the top lawyer rarely buys carelessly), CFO 0.9, "
                                        "other chief officers 0.8, VP 0.7, director 0.4, "
                                        "CEO 0.3 (often performative), 10%-owner funds 0.1.")
exclude_otc = st.sidebar.checkbox(
    "Exclude OTC", value=True,
    help="Hide companies whose shares trade only over-the-counter rather than "
         "on a major exchange (NYSE, Nasdaq). Companies the SEC's exchange "
         "list doesn't cover are kept — absence doesn't prove they're OTC.")
include_exercise = st.sidebar.checkbox(
    "Include exercise-flagged buys", value=False,
    help="Some insiders 'buy' by exercising stock options and sell the shares "
         "the same day — that's compensation being cashed out, not a vote of "
         "confidence. Such buys are hidden by default; tick to show them.")
include_lowsig = st.sidebar.checkbox(
    "Include low-signal buys", value=False,
    help="Show purchases that look automatic rather than deliberate: trades "
         "under pre-scheduled plans (SEC Rule 10b5-1), dividend-reinvestment / "
         "401(k) / employee-stock-plan purchases, shares bought in a company "
         "offering or placement (where the price came from a deal, not the "
         "open market), and internal transfers (a same-day sale of the exact "
         "same size — shares moving between the insider's own entities, not "
         "new money). Hidden by default.")
solo_gc = st.sidebar.checkbox(
    "Include solo GC buys", value=True,
    help="A General Counsel — the company's top lawyer — buying stock is a "
         "strong signal even with no other buyers alongside. This keeps solo "
         "GC purchases on screen even below the cluster minimum.")

st.sidebar.divider()
st.sidebar.subheader("Market data (yfinance)")
price_context = st.sidebar.checkbox(
    "Market context (type + entry gate)", value=True,
    help="Fetch price history for each screened stock (Yahoo Finance) to label "
         "it value vs momentum, check how close it is to its highs, test the "
         "50-day-average timing gate, and compare today's price with what the "
         "insiders paid. First load ~1s per stock, then cached for the day.")
cap_choice = st.sidebar.selectbox(
    "Market-cap cap", ["Any", "≤ $500M", "≤ $2B", "≤ $10B", "≤ $50B"],
    help="Only show companies below this total market value. Fetched per stock "
         "and cached daily; companies whose size can't be determined stay visible.")
CAP_LIMITS = {"Any": None, "≤ $500M": 5e8, "≤ $2B": 2e9, "≤ $10B": 1e10, "≤ $50B": 5e10}

stats = db_stats()
st.sidebar.divider()
st.sidebar.caption(
    f"DB: {stats['filings']:,} filings over {stats['days']} ingested days "
    f"({stats['span'][0]} → {stats['span'][1]})" if stats["filings"]
    else "DB is empty — run `python -m app.ingest --days 45` first.")

if st.sidebar.button("🔄 Catch up filings",
                     help="Fetch any SEC filings newer than the database "
                          "(a few minutes at most), then re-check alerts."):
    conn = db.connect()
    try:
        ticker_map = edgar.load_ticker_map()
        days = ingest.catch_up_days(conn)
        prog = st.sidebar.progress(0.0, text="Catching up…")
        for i, d in enumerate(days):
            prog.progress((i + 1) / max(len(days), 1), text=f"Ingesting {d}")
            ingest.ingest_day(conn, d, ticker_map)
        fired = alerts.check_alerts(conn)
        prog.empty()
        st.sidebar.success(f"Up to date · {len(fired)} new alert(s)")
    finally:
        conn.close()
    st.cache_data.clear()
    st.rerun()

# ------------------------------------------------------------------ screener

st.title("Insider-Buying Screener")
st.caption("Companies where insiders are buying their own stock with their own "
           "money on the open market (from SEC Form 4 filings), grouped per "
           "company and filtered for noise. A research tool only — it doesn't "
           "trade, and it doesn't tell you when to sell.")

# V2 §B market-wide breadth gauge: a top-down "should I be buying anything
# right now" overlay computed from the full Form 4 firehose.
b = get_breadth()
if b:
    c1, c2 = st.columns([1, 3])
    with c1:
        st.metric(f"Insiders buying vs selling ({config.BREADTH_WINDOW_DAYS}d)",
                  f"{b['buy_share']:.0f}% buys", b["label"], delta_color="off",
                  delta_arrow="off",
                  help="A market-wide mood gauge. Of all insider trades made by "
                       "choice (ignoring sales and purchases that run on "
                       "pre-set autopilot plans), this is the share that were "
                       "BUYS over the last "
                       f"{config.BREADTH_WINDOW_DAYS} trading days. In normal "
                       "times only about a third are buys — insiders mostly "
                       "sell. When the share rises above "
                       f"{config.BREADTH_BULLISH_PCT:.0f}%, insiders as a group "
                       "are unusually optimistic, which has historically been "
                       "bullish for the whole market; above "
                       f"{config.BREADTH_VERY_BULLISH_PCT:.0f}% strongly so — "
                       "it hit ~60% at the 2020 COVID low and ~55% at the 2022 "
                       "market bottom.")
    with c2:
        with st.expander(f"Breadth history (as of {b['as_of']})"):
            s = b["series"]
            line = alt.Chart(s).mark_line(color="#4c78a8").encode(
                x=alt.X("date:T", title=None),
                y=alt.Y("buy_share:Q", title="Buy share %", scale=alt.Scale(domain=[0, 100])))
            rules = alt.Chart(pd.DataFrame({
                "y": [config.BREADTH_BULLISH_PCT, config.BREADTH_VERY_BULLISH_PCT]
            })).mark_rule(strokeDash=[4, 4], color="#888").encode(y="y:Q")
            st.altair_chart(alt.layer(line, rules), width="stretch")

recent = get_recent_alerts()
if recent:
    with st.expander(f"🔔 Alerts ({len(recent)} recent)"):
        for ts, ticker, kind, message in recent:
            st.write(f"`{ts}` **{kind}** — {message}")

df, kept_buys = get_screen(window_days, float(min_value), min_cluster,
                           include_exercise, include_lowsig, solo_gc)

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
    # V2 §A/§B: classify value vs momentum, apply the 50-day entry gate, and
    # attach discount-to-entry + below-market tells.
    prog = st.progress(0.0, text="Fetching market context…")
    df = classify.enrich_clusters(
        df, kept_buys,
        progress=lambda frac, t: prog.progress(frac, text=f"Market context: {t}"))
    prog.empty()

# V2 §B FPI/new-reporter (Tier 1 — independent of the market-context toggle):
# first insider filing < 12 months → history-based signals (first-time,
# routine) are artifacts of the reporting change, not conviction floods.
if not df.empty:
    cutoff_new = (date.today() - timedelta(days=config.NEW_REPORTER_MONTHS * 30)).isoformat()
    first_dates = {c: get_first_insider_filing(str(c)) for c in df["cik"].unique()}
    new_rep = df["cik"].map(lambda c: (first_dates.get(c) or "") > cutoff_new)
    if new_rep.any():
        df.loc[new_rep, "n_first_time"] = None
        df.loc[new_rep, "routine"] = False
        df.loc[new_rep, "flags"] = (df.loc[new_rep, "flags"] + ", new-reporter"
                                    ).str.strip(", ")

if df.empty:
    st.info("No clusters match the current filters. Widen the window, lower the "
            "minimum value or role score, or ingest more days of filings.")
    st.stop()

cols = ["ticker", "company", "n_insiders", "n_filers", "n_buys", "total_value",
        "largest_buy", "role_score", "max_trade_pct", "n_conviction", "n_notable",
        "n_first_time", "days_since_first", "days_since_last", "roles", "flags"]
if price_context:
    cols[2:2] = ["trade_type", "actionable", "pct_below_high", "discount_to_entry_pct"]
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
            "Mkt cap", format="compact",
            help="Company size (total market value), from Yahoo Finance. "
                 "Companies whose size couldn't be fetched stay visible."),
        "trade_type": st.column_config.TextColumn(
            "Type",
            help="What kind of bet this looks like. momentum = the stock is "
                 "near its multi-year high and insiders are buying into "
                 "strength · value = the stock is well below its highs and "
                 "insiders are betting on a recovery. Blank = no price "
                 "history available."),
        "actionable": st.column_config.CheckboxColumn(
            "Actionable",
            help="Whether the timing test passes. Beaten-down (value) stocks "
                 f"must first climb back above their {config.MA_GATE_DAYS}-day "
                 "average price and hold it — insiders tend to buy too early, "
                 "and waiting avoids catching a falling knife. Stocks near "
                 "their highs pass automatically. A judgement aid, not advice."),
        "pct_below_high": st.column_config.NumberColumn(
            "% below high", format="%.1f%%",
            help="How far today's price is below the stock's highest close of "
                 f"the past {config.PRICE_HISTORY_YEARS} years."),
        "discount_to_entry_pct": st.column_config.NumberColumn(
            "Disc. to entry", format="%.1f%%",
            help="Today's price compared with the average price the insiders "
                 "paid. Positive = you can buy cheaper than they did; negative "
                 "= the stock has already run above their entry."),
        "n_notable": st.column_config.NumberColumn(
            "# notable",
            help="How many buyers increased their existing stake by at least "
                 f"{config.NOTABLE_TRADE_PCT:.0f}% — a meaningful addition, "
                 "not pocket change."),
        "n_insiders": st.column_config.NumberColumn(
            "# insiders",
            help="Number of independent buyers. Related parties who file one "
                 "purchase together (e.g. a fund and its affiliates) count as "
                 "ONE buyer, so a single decision can't look like a crowd."),
        "n_filers": st.column_config.NumberColumn(
            "# filers",
            help="Raw number of names on the filings, counting each "
                 "affiliated co-filer separately."),
        "n_buys": st.column_config.NumberColumn("# buys"),
        "total_value": st.column_config.NumberColumn("Total $", format="dollar"),
        "largest_buy": st.column_config.NumberColumn("Largest buy", format="dollar"),
        "role_score": st.column_config.NumberColumn(
            "Role score", format="%.2f",
            help="Adds up WHO is buying, weighted by how meaningful each "
                 "role's buying tends to be: General Counsel 1.0 · CFO 0.9 · "
                 "other chief officers 0.8 · VP 0.7 · director 0.4 · CEO 0.3 · "
                 "10%-owner funds 0.1. Higher = better-informed buyers."),
        "max_trade_pct": st.column_config.NumberColumn(
            "Max trade %", format="%.1f%%",
            help="The largest single purchase relative to what that buyer "
                 "already owned. Someone growing their stake 50% is a far "
                 "stronger signal than a fund adding 0.5%."),
        "n_conviction": st.column_config.NumberColumn(
            "# conviction",
            help=f"Purchases of ${config.CONVICTION_MIN_VALUE:,} or more — big "
                 "enough to represent real personal conviction."),
        "n_first_time": st.column_config.NumberColumn(
            "# first-time",
            help="Buyers making their first recorded purchase of this "
                 "company's stock. An insider who has never bought before "
                 "suddenly buying is a tell. (Only as reliable as the filing "
                 "history collected so far.)"),
        "days_since_first": st.column_config.NumberColumn("Days since 1st"),
        "days_since_last": st.column_config.NumberColumn("Days since last"),
        "roles": st.column_config.TextColumn("Roles", width="medium"),
        "flags": st.column_config.TextColumn(
            "Flags", width="medium",
            help="Warning labels. solo-GC = the only buyer is the company's top "
                 "lawyer (still notable) · regime-flip×N = N buyers who only "
                 "SOLD over the past year and have now switched to buying · "
                 "exercise×N = N buys hidden because the insider exercised "
                 "options and sold shares the same day (cashing out, not "
                 "conviction) · lowsig×N = N buys hidden as automatic plan, "
                 "offering, or internal-transfer purchases · below-mkt×N = N buys made ≥5% below the "
                 "market price that day (the insider got a deal you can't) · "
                 "fund-noise = most of the money is large funds adding tiny "
                 "amounts to existing stakes (routine rebalancing, not "
                 "conviction) · stale = no new buys in over "
                 f"{config.STALE_AFTER_DAYS} days · routine = this company's "
                 "insiders buy almost every month, so another buy means "
                 "little · new-reporter = the company only recently began "
                 "insider filings (often a foreign company that changed "
                 "reporting status), so history-based signals are unreliable · "
                 "all-noise-sized = every buy is in the $3k–$15k range typical "
                 "of automatic payroll plans"),
    },
)
st.caption(f"{len(df)} cluster(s) · window {window_days}d · min buy ${min_value:,.0f} · "
           f"min {min_cluster} insider(s)"
           + (f" · role score ≥ {min_role_score}" if min_role_score > 0 else "")
           + (" · exercise-flagged included" if include_exercise else "")
           + (" · low-signal included" if include_lowsig else ""))

selected_ticker = None
if event.selection.rows:
    try:
        selected_ticker = show.iloc[event.selection.rows[0]]["ticker"]
    except IndexError:  # selection from a previous, larger render
        selected_ticker = None

# ---------------------------------------------------------------- drill-down

st.divider()
st.subheader("Ticker drill-down")
all_tickers = df["ticker"].tolist()
# Row clicks must reliably drive the drill-down: write the clicked ticker into
# the selectbox's session state (an `index=` default is ignored once the
# widget has state, which made clicks appear to do nothing).
if selected_ticker:
    st.session_state["drill_ticker"] = selected_ticker
if st.session_state.get("drill_ticker") not in all_tickers:
    st.session_state["drill_ticker"] = all_tickers[0]
pick = st.selectbox("Ticker", all_tickers, key="drill_ticker",
                    help="Click a row above or pick a company here")

if pick:
    tab_insiders, tab_txns, tab_chart, tab_record = st.tabs(
        ["Insiders", "Transactions", "Buys over price", "Insider track record"])

    with tab_insiders:
        summary = get_insider_summary(pick)
        if summary.empty:
            st.info("No stored non-derivative transactions for this ticker.")
        else:
            st.dataframe(
                summary[["insider_name", "role", "officer_title", "n_buys", "n_sells",
                         "bought_value", "sold_value", "shares_owned",
                         "first_activity", "last_activity", "filing_url"]],
                width="stretch",
                hide_index=True,
                column_config={
                    "insider_name": st.column_config.TextColumn("Insider", width="medium"),
                    "role": st.column_config.TextColumn("Role"),
                    "officer_title": st.column_config.TextColumn("Title", width="medium"),
                    "n_buys": st.column_config.NumberColumn(
                        "# buys", help="Purchases made on the open market with "
                                       "the insider's own money"),
                    "n_sells": st.column_config.NumberColumn(
                        "# sells", help="Sales made on the open market"),
                    "bought_value": st.column_config.NumberColumn("Bought $", format="dollar"),
                    "sold_value": st.column_config.NumberColumn("Sold $", format="dollar"),
                    "shares_owned": st.column_config.NumberColumn(
                        "Shares held", format="localized",
                        help="The share count this insider most recently "
                             "reported holding (owned directly and through "
                             "trusts/entities, as filed)"),
                    "first_activity": st.column_config.TextColumn("First seen"),
                    "last_activity": st.column_config.TextColumn("Last activity"),
                    "filing_url": st.column_config.LinkColumn("Latest Form 4",
                                                              display_text="filing"),
                },
            )
            st.caption("Totals cover every share transaction on file for this "
                       "company (records collected since Jan 2026). Related "
                       "parties who file together — e.g. a fund and its "
                       "affiliates — appear as separate rows here.")

        buys_detail = get_buy_history(pick)
        if not buys_detail.empty:
            st.subheader("Each open-market buy")
            detail = buys_detail.sort_values("transaction_date",
                                             ascending=False).copy()
            st.dataframe(
                detail[["transaction_date", "insider_name", "role", "shares",
                        "price_per_share", "value", "security_title",
                        "shares_owned_after", "filing_url"]],
                width="stretch",
                hide_index=True,
                column_config={
                    "transaction_date": st.column_config.TextColumn("Buy date"),
                    "insider_name": st.column_config.TextColumn("Insider", width="medium"),
                    "role": st.column_config.TextColumn("Role"),
                    "shares": st.column_config.NumberColumn("Shares", format="localized"),
                    "price_per_share": st.column_config.NumberColumn("Price",
                                                                     format="dollar"),
                    "value": st.column_config.NumberColumn("Value", format="dollar"),
                    "security_title": st.column_config.TextColumn(
                        "Security", width="medium",
                        help="Which instrument was bought. Companies can have "
                             "several traded securities at very different "
                             "prices — e.g. common shares near $7 and "
                             "preference shares near $20 — so compare prices "
                             "only within the same security."),
                    "shares_owned_after": st.column_config.NumberColumn(
                        "Shares held after", format="localized",
                        help="The stake they reported holding right after this buy"),
                    "filing_url": st.column_config.LinkColumn("Filing",
                                                              display_text="view"),
                },
            )
            st.caption("One row per purchase (joint co-filed trades shown once). "
                       "Sales, grants, and option activity are in the "
                       "Transactions tab.")

    with tab_txns:
        txns = get_ticker_txns(pick)
        col_a, col_b = st.columns([2, 3])
        with col_a:
            only_buys = st.toggle("Open-market buys only (code P)", value=False)
        with col_b:
            insider_names = sorted(txns["insider_name"].dropna().unique().tolist())
            who = st.selectbox("Insider", ["All insiders"] + insider_names,
                               label_visibility="collapsed",
                               help="Show one insider's transactions only")
        if who != "All insiders":
            txns = txns[txns["insider_name"] == who]
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
                        help="The SEC's transaction code: P = bought on the open "
                             "market · S = sold on the open market · M = "
                             "exercised stock options · A = received a company "
                             "grant/award · G = gift · F = shares withheld to "
                             "cover taxes"),
                    "acquired_disposed": st.column_config.TextColumn("A/D"),
                    "is_derivative": st.column_config.CheckboxColumn("Deriv?"),
                    "shares": st.column_config.NumberColumn("Shares", format="localized"),
                    "price_per_share": st.column_config.NumberColumn("Price", format="dollar"),
                    "value": st.column_config.NumberColumn("Value", format="dollar"),
                    "trade_pct": st.column_config.NumberColumn("Trade %", format="%.1f%%",
                        help="The size of this trade relative to what the "
                             "insider already owned — growing a stake by 50% "
                             "says far more than adding 1%. Blank = a brand-new "
                             "position or unknown prior holdings."),
                    "shares_owned_after": st.column_config.NumberColumn("Shares after",
                                                                        format="localized"),
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
                st.caption(f"Last close ${last:,.2f} · {pct_below:.1f}% below its "
                           f"highest close of the past {config.PRICE_HISTORY_YEARS} years"
                           + (" · **near its multi-year high**"
                              if pct_below <= config.NEAR_HIGH_MAX_PCT_BELOW else ""))

    with tab_record:
        buys_hist = get_buy_history(pick)
        nh = get_near_high(pick)
        last_close = nh[0] if nh else None
        if buys_hist.empty:
            st.info("No recorded open-market buys for this ticker.")
        else:
            rec = buys_hist[["insider_name", "transaction_date", "shares",
                             "price_per_share", "value"]].copy()
            if last_close:
                rec["pct_since_buy"] = (last_close - rec["price_per_share"]) \
                    / rec["price_per_share"] * 100.0
                st.caption(f"Price change measured against the latest close "
                           f"(${last_close:,.2f}). The record deepens as more "
                           "filing history is collected.")
            st.dataframe(
                rec.sort_values("transaction_date", ascending=False),
                width="stretch",
                hide_index=True,
                column_config={
                    "insider_name": st.column_config.TextColumn("Insider", width="medium"),
                    "transaction_date": st.column_config.TextColumn("Buy date"),
                    "shares": st.column_config.NumberColumn("Shares", format="localized"),
                    "price_per_share": st.column_config.NumberColumn("Buy price",
                                                                     format="dollar"),
                    "value": st.column_config.NumberColumn("Value", format="dollar"),
                    "pct_since_buy": st.column_config.NumberColumn(
                        "% since buy", format="%.1f%%",
                        help="How the stock has moved since that purchase — the "
                             "insider's track record on their past buys."),
                },
            )

        # V2 §B sell-side track record: do their sells precede drops?
        sells = get_ticker_txns(pick)
        sells = sells[(sells["transaction_code"] == "S")
                      & (sells["acquired_disposed"] == "D")
                      & (sells["is_derivative"] == 0)
                      & sells["price_per_share"].notna()
                      & (sells["price_per_share"] > 0)]
        sells = sells.drop_duplicates(subset=["accession_no", "txn_seq"]).copy()
        # 10b5-1 plan sells carry no timing signal — keep the record honest.
        scheduled = clusters.is_scheduled(sells) if not sells.empty else pd.Series(dtype=bool)
        n_sched = int(scheduled.sum()) if not sells.empty else 0
        if not sells.empty:
            sells = sells[~scheduled]
        if not sells.empty:
            st.subheader("Sell-side record")
            if n_sched:
                st.caption(f"{n_sched} pre-scheduled sell(s) excluded — sales "
                           "that run on autopilot plans (SEC Rule 10b5-1) say "
                           "nothing about timing skill.")
            sv = sells[["insider_name", "role", "transaction_date", "shares",
                        "price_per_share", "value"]].copy()
            if last_close:
                # Negative = stock fell after the sell = a well-timed sell.
                sv["pct_since_sell"] = (last_close - sv["price_per_share"]) \
                    / sv["price_per_share"] * 100.0
            st.dataframe(
                sv.sort_values("transaction_date", ascending=False),
                width="stretch",
                hide_index=True,
                column_config={
                    "insider_name": st.column_config.TextColumn("Insider", width="medium"),
                    "role": st.column_config.TextColumn("Role"),
                    "transaction_date": st.column_config.TextColumn("Sell date"),
                    "shares": st.column_config.NumberColumn("Shares", format="localized"),
                    "price_per_share": st.column_config.NumberColumn("Sell price",
                                                                     format="dollar"),
                    "value": st.column_config.NumberColumn("Value", format="dollar"),
                    "pct_since_sell": st.column_config.NumberColumn(
                        "% since sell", format="%.1f%%",
                        help="How the stock has moved since that sale. Negative "
                             "= it fell after they sold, meaning the sale was "
                             "well timed. Insiders whose sells precede drops "
                             "deserve extra attention when they buy."),
                },
            )
