"""Deep-dive on Tier-S entry events over the multi-year walk.

Reads data/backtest_snapshots.csv (written by backtest.py), rebuilds the
S-entry event study, and answers three questions the aggregate can't:
per-year performance, how many events are unmeasurable (delisted/acquired —
survivorship), and whether the strategy doc's own breadth condition
(only act when market-wide unscheduled-buy share is bullish) concentrates
the edge.

Run:  python events_analysis.py
"""
import io
import sys
from datetime import date

import pandas as pd

sys.path.insert(0, ".")

# backtest.py wraps stdout for utf-8 at import time — don't wrap twice.
from app import breadth, db  # noqa: E402
from backtest import HORIZONS, PriceBook  # noqa: E402

_ = io  # keep import for parity with sibling scripts


def main():
    snap = pd.read_csv("data/backtest_snapshots.csv")
    book = PriceBook()
    conn = db.connect()

    # Market breadth series over the whole span (aff flag exists for all years).
    bs = breadth.breadth_series(conn, lookback_days=1400)
    bshare = bs.set_index("date")["buy_share"].sort_index()

    def breadth_at(d: str) -> float | None:
        i = bshare.index.searchsorted(pd.Timestamp(d), side="right") - 1
        return float(bshare.iloc[i]) if i >= 0 else None

    s_rows = snap[snap["tier"] == "S"].sort_values(["ticker", "eval_date"])
    events = []
    for ticker, grp in s_rows.groupby("ticker"):
        prev = None
        for _, row in grp.iterrows():
            d = date.fromisoformat(row["eval_date"])
            if prev is None or (d - prev).days > 9:
                events.append(row)
            prev = d
    ev = pd.DataFrame(events).reset_index(drop=True)
    print(f"S entry events: {len(ev)} across "
          f"{ev['eval_date'].min()} → {ev['eval_date'].max()}")

    unmeasurable = 0
    rows = []
    for r in ev.itertuples():
        full = book.full(r.ticker)
        if full is None or full.empty:
            unmeasurable += 1
            continue
        rec = {"eval_date": r.eval_date, "ticker": r.ticker, "company": r.company,
               "score": r.score, "trade_type": r.trade_type,
               "actionable": r.actionable,
               "breadth": breadth_at(r.eval_date)}
        for h in HORIZONS:
            fr = book.forward_return(r.ticker, date.fromisoformat(r.eval_date), h)
            if fr is None:
                rec[f"excess_{h}d%"] = None
                continue
            ret, d0, d1 = fr
            spy = book.span_return("SPY", d0, d1)
            rec[f"excess_{h}d%"] = (ret - spy) * 100 if spy is not None else None
        rows.append(rec)
    full_ev = pd.DataFrame(rows)
    full_ev.to_csv("data/backtest_s_events_full.csv", index=False)
    print(f"unmeasurable events (no price data — delisted/acquired): "
          f"{unmeasurable} of {len(ev)} ({unmeasurable/len(ev)*100:.0f}%)")

    def stats(df: pd.DataFrame, label: str):
        parts = []
        for h in HORIZONS:
            x = pd.to_numeric(df[f"excess_{h}d%"], errors="coerce").dropna()
            if len(x) >= 5:
                parts.append(f"+{h}d: n={len(x)} med {x.median():+.1f}% "
                             f"hit {(x > 0).mean()*100:.0f}%")
        print(f"  {label}: " + " | ".join(parts))

    print("\n=== overall ===")
    stats(full_ev, "all S events")

    print("\n=== by year ===")
    full_ev["year"] = full_ev["eval_date"].str[:4]
    for y, grp in full_ev.groupby("year"):
        stats(grp, y)

    print("\n=== by market breadth at entry (strategy doc's own condition) ===")
    hasb = full_ev.dropna(subset=["breadth"])
    stats(hasb[hasb["breadth"] >= 33], "breadth ≥33% (bullish)")
    stats(hasb[hasb["breadth"] < 33], "breadth <33% (weak)")

    print("\n=== by trade type ===")
    for t, grp in full_ev.groupby("trade_type"):
        stats(grp, str(t))
    print("\n=== timing gate ===")
    stats(full_ev[full_ev["actionable"] == True], "gate passed")   # noqa: E712
    stats(full_ev[full_ev["actionable"] != True], "gate not passed")


if __name__ == "__main__":
    main()
