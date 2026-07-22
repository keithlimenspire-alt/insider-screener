"""Walk-forward backtest of the tier scoring system.

For each Friday in the walk window the screener runs POINT-IN-TIME: only
filings actually on file by that date are visible, and the market context
(value/momentum type, 50-day gate, near-high) is computed on price history
truncated to that date. Clusters are scored and tiered; forward returns are
measured from the NEXT trading day's close at +5/+10/+21/+63 trading days,
each benchmarked against SPY over the same calendar span.

Outputs:
- snapshot aggregates per tier (does the ordering S > A > B > C/D hold?)
- event study for Tier S: each time a ticker ENTERS Tier S, what happened next
- data/backtest_snapshots.csv + data/backtest_s_events.csv

Run:  python backtest.py
"""
import io
import sys
from datetime import date, timedelta

import pandas as pd

sys.path.insert(0, ".")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from app import classify, clusters, db, prices, scoring  # noqa: E402

WALK_START = date(2026, 2, 27)   # defaults; override:  python backtest.py 2023-07-07 2026-07-10
WALK_END = date(2026, 7, 10)
HORIZONS = (5, 10, 21, 63)       # trading days
BENCH = "SPY"

if len(sys.argv) >= 3:
    WALK_START = date.fromisoformat(sys.argv[1])
    WALK_END = date.fromisoformat(sys.argv[2])


def fridays(start: date, end: date) -> list[date]:
    d = start + timedelta(days=(4 - start.weekday()) % 7)
    out = []
    while d <= end:
        out.append(d)
        d += timedelta(days=7)
    return out


class PriceBook:
    """One full history fetch per ticker; point-in-time views by truncation."""

    def __init__(self):
        self.cache: dict[str, pd.DataFrame | None] = {}

    def full(self, ticker: str) -> pd.DataFrame | None:
        if ticker not in self.cache:
            self.cache[ticker] = prices.get_history(ticker)
        return self.cache[ticker]

    def as_of(self, cutoff: date):
        ts = pd.Timestamp(cutoff)

        def get(ticker: str) -> pd.DataFrame | None:
            h = self.full(ticker)
            if h is None or h.empty:
                return None
            trunc = h[h["date"] <= ts]
            return trunc if not trunc.empty else None
        return get

    def forward_return(self, ticker: str, eval_date: date,
                       horizon: int) -> tuple[float, str, str] | None:
        """(return, entry_date, exit_date) buying the first close AFTER
        eval_date and selling `horizon` trading days later."""
        h = self.full(ticker)
        if h is None or h.empty:
            return None
        idx = h["date"].searchsorted(pd.Timestamp(eval_date), side="right")
        if idx >= len(h) or idx + horizon >= len(h):
            return None
        entry, exit_ = h.iloc[idx], h.iloc[idx + horizon]
        if not entry["close"] or entry["close"] <= 0:
            return None
        return (float(exit_["close"] / entry["close"] - 1.0),
                str(entry["date"].date()), str(exit_["date"].date()))

    def span_return(self, ticker: str, d0: str, d1: str) -> float | None:
        """Close-to-close return between two calendar dates (as-of lookup)."""
        h = self.full(ticker)
        if h is None or h.empty:
            return None
        s = h.set_index("date")["close"]
        i0 = s.index.searchsorted(pd.Timestamp(d0), side="right") - 1
        i1 = s.index.searchsorted(pd.Timestamp(d1), side="right") - 1
        if i0 < 0 or i1 < 0 or i0 >= len(s) or s.iloc[i0] <= 0:
            return None
        return float(s.iloc[i1] / s.iloc[i0] - 1.0)


def main():
    conn = db.connect()
    book = PriceBook()
    evals = fridays(WALK_START, WALK_END)
    print(f"walk-forward: {len(evals)} Fridays {evals[0]} → {evals[-1]}")

    rows = []
    for d in evals:
        cl, kept = clusters.build_screen(conn, 45, 200_000, 2,
                                         as_of=d, filed_by=d)
        if cl.empty:
            continue
        cl = classify.enrich_clusters(cl, kept, get_history=book.as_of(d), as_of=d)
        cl = scoring.score_clusters(cl)
        for r in cl.itertuples():
            rows.append({
                "eval_date": d.isoformat(), "ticker": r.ticker,
                "company": r.company, "tier": r.tier, "score": r.score,
                "trade_type": r.trade_type, "actionable": r.actionable,
                "n_insiders": r.n_insiders, "role_score": r.role_score,
                "total_value": r.total_value, "dcf_discount": r.dcf_discount,
                "flags": r.flags,
            })
        counts = cl["tier"].value_counts().to_dict()
        print(f"  {d}: {len(cl)} clusters  {counts}")

    snap = pd.DataFrame(rows)

    # Forward returns per snapshot row, benchmarked to SPY over the same span.
    for h in HORIZONS:
        rets, exs = [], []
        for r in snap.itertuples():
            fr = book.forward_return(r.ticker, date.fromisoformat(r.eval_date), h)
            if fr is None:
                rets.append(None); exs.append(None)
                continue
            ret, d0, d1 = fr
            spy = book.span_return(BENCH, d0, d1)
            rets.append(ret)
            exs.append(ret - spy if spy is not None else None)
        snap[f"ret_{h}d"] = rets
        snap[f"excess_{h}d"] = exs

    snap.to_csv("data/backtest_snapshots.csv", index=False)

    print("\n=== Tier ordering (snapshot means; excess vs SPY) ===")
    for h in HORIZONS:
        col = f"excess_{h}d"
        g = snap.dropna(subset=[col]).groupby("tier")[col]
        agg = pd.DataFrame({"n": g.size(), "mean%": g.mean() * 100,
                            "median%": g.median() * 100,
                            "hit%": g.apply(lambda s: (s > 0).mean() * 100)})
        agg = agg.reindex([t for t in ("S", "A", "B", "C", "D") if t in agg.index])
        print(f"\n+{h} trading days:")
        print(agg.round(2).to_string())

    # Event study: first eval date of each consecutive Tier-S run per ticker.
    s_rows = snap[snap["tier"] == "S"].sort_values(["ticker", "eval_date"])
    events = []
    for ticker, grp in s_rows.groupby("ticker"):
        dates = [date.fromisoformat(x) for x in grp["eval_date"]]
        prev = None
        for dt, (_, row) in zip(dates, grp.iterrows()):
            if prev is None or (dt - prev).days > 9:   # gap => new entry event
                events.append(row)
            prev = dt
    ev = pd.DataFrame(events)
    if not ev.empty:
        for h in HORIZONS:
            rets, exs = [], []
            for r in ev.itertuples():
                fr = book.forward_return(r.ticker, date.fromisoformat(r.eval_date), h)
                if fr is None:
                    rets.append(None); exs.append(None); continue
                ret, d0, d1 = fr
                spy = book.span_return(BENCH, d0, d1)
                rets.append(ret * 100)
                exs.append((ret - spy) * 100 if spy is not None else None)
            ev[f"ret_{h}d%"] = rets
            ev[f"excess_{h}d%"] = exs
        ev.to_csv("data/backtest_s_events.csv", index=False)
        print(f"\n=== Tier S entry events ({len(ev)}) ===")
        show = ev[["eval_date", "ticker", "company", "score", "trade_type",
                   "actionable"] + [f"excess_{h}d%" for h in HORIZONS]]
        print(show.round(1).to_string(index=False))
        print("\nS-event stats (measurable events only):")
        for h in HORIZONS:
            x = pd.to_numeric(ev[f"excess_{h}d%"], errors="coerce").dropna()
            if len(x):
                print(f"  +{h}d: n={len(x)}  mean {x.mean():+.2f}%  "
                      f"median {x.median():+.2f}%  hit {(x > 0).mean()*100:.0f}%")
    else:
        print("\nNo Tier-S events in the walk window.")


if __name__ == "__main__":
    main()
