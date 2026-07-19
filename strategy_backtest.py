"""Full-strategy backtest: entry + exit rules on Tier-S signals, daily walk.

Every trading day in the walk window the screener runs point-in-time (only
filings filed by that date; prices truncated to that date), clusters are
scored, and a ticker ENTERING Tier S is a buy signal. Trades enter at the
NEXT day's close and are managed daily under competing exit rules:

  T10 / T21 / T42   exit after a fixed number of trading days
  S12+T21           close-based -12% stop, else exit at 21 days
  TR15+T42          -15% trailing stop off the highest close, else 42 days
  SIG-A             exit when the cluster's grade falls below Tier A
                    (re-checked daily), hard cap 63 days

Two entry variants: ALL Tier-S entries, and only those passing the 50-day
timing gate (actionable). Costs: 10 bps per side. Benchmark: SPY over the
same calendar span of each trade.

Run:  python strategy_backtest.py     (results → data/strategy_grid.csv,
                                       data/strategy_trades_best.csv)
"""
import io
import sys
from datetime import date, timedelta

import pandas as pd

sys.path.insert(0, ".")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from app import classify, clusters, db, prices, scoring  # noqa: E402

WALK_START = date(2026, 2, 27)
WALK_END = date(2026, 7, 17)
COST_PER_SIDE = 0.001            # 10 bps each way
MAX_HOLD_SIG = 63


def business_days(start: date, end: date) -> list[date]:
    d, out = start, []
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


class PriceBook:
    def __init__(self):
        self.cache: dict[str, pd.DataFrame | None] = {}

    def full(self, t):
        if t not in self.cache:
            self.cache[t] = prices.get_history(t)
        return self.cache[t]

    def as_of(self, cutoff: date):
        ts = pd.Timestamp(cutoff)

        def get(t):
            h = self.full(t)
            if h is None or h.empty:
                return None
            tr = h[h["date"] <= ts]
            return tr if not tr.empty else None
        return get

    def series(self, t):
        h = self.full(t)
        if h is None or h.empty:
            return None
        return h.set_index("date")["close"]


def build_panel(conn, book, days):
    """date -> {ticker: row} for every eval day (point-in-time)."""
    panel = {}
    for i, d in enumerate(days):
        cl, kept = clusters.build_screen(conn, 45, 200_000, 2, as_of=d, filed_by=d)
        if cl.empty:
            panel[d] = {}
            continue
        cl = classify.enrich_clusters(cl, kept, get_history=book.as_of(d))
        cl = scoring.score_clusters(cl)
        panel[d] = {r.ticker: {"tier": r.tier, "score": r.score,
                               "actionable": r.actionable,
                               "trade_type": r.trade_type} for r in cl.itertuples()}
        if (i + 1) % 20 == 0:
            print(f"  panel {i+1}/{len(days)} days built")
    return panel


def simulate(entries, book, exit_rule, panel, days):
    """entries: list of (signal_date, ticker, meta). Returns trades df."""
    spy = book.series("SPY")
    trades = []
    open_until: dict[str, pd.Timestamp] = {}
    for sig_date, ticker, meta in entries:
        s = book.series(ticker)
        if s is None:
            continue
        ei = s.index.searchsorted(pd.Timestamp(sig_date), side="right")
        if ei >= len(s):
            continue
        entry_dt, entry_px = s.index[ei], float(s.iloc[ei])
        if entry_px <= 0:
            continue
        if ticker in open_until and entry_dt <= open_until[ticker]:
            continue  # already holding this name

        xi = None
        n = len(s)
        if exit_rule.startswith("T") and exit_rule[1:].isdigit():
            xi = min(ei + int(exit_rule[1:]), n - 1)
        elif exit_rule == "S12+T21":
            xi = min(ei + 21, n - 1)
            for j in range(ei + 1, min(ei + 21, n - 1)):
                if s.iloc[j] <= entry_px * 0.88:
                    xi = min(j + 1, n - 1)   # observed at close, sell next close
                    break
        elif exit_rule == "TR15+T42":
            xi = min(ei + 42, n - 1)
            peak = entry_px
            for j in range(ei + 1, min(ei + 42, n - 1)):
                peak = max(peak, float(s.iloc[j]))
                if s.iloc[j] <= peak * 0.85:
                    xi = min(j + 1, n - 1)
                    break
        elif exit_rule == "SIG-A":
            xi = min(ei + MAX_HOLD_SIG, n - 1)
            for d in days:
                ts = pd.Timestamp(d)
                if ts <= entry_dt:
                    continue
                row = panel.get(d, {}).get(ticker)
                if row is None or row["tier"] not in ("S", "A"):
                    k = s.index.searchsorted(ts, side="right")
                    xi = min(k, n - 1)
                    break
                if (ts - entry_dt).days > 100:
                    break
        if xi is None or xi <= ei:
            continue
        exit_dt, exit_px = s.index[xi], float(s.iloc[xi])
        gross = exit_px / entry_px - 1.0
        net = gross - 2 * COST_PER_SIDE
        spy_ret = None
        if spy is not None:
            a = spy.index.searchsorted(entry_dt, side="right") - 1
            b = spy.index.searchsorted(exit_dt, side="right") - 1
            if a >= 0 and b >= 0 and spy.iloc[a] > 0:
                spy_ret = float(spy.iloc[b] / spy.iloc[a] - 1.0)
        open_until[ticker] = exit_dt
        trades.append({
            "signal": str(sig_date), "ticker": ticker,
            "entry": str(entry_dt.date()), "exit": str(exit_dt.date()),
            "days_held": xi - ei, "net_ret%": net * 100,
            "excess%": (net - spy_ret) * 100 if spy_ret is not None else None,
            **meta})
    return pd.DataFrame(trades)


def main():
    conn = db.connect()
    book = PriceBook()
    days = business_days(WALK_START, WALK_END)
    print(f"building daily point-in-time panel: {len(days)} trading days…")
    panel = build_panel(conn, book, days)

    entries_all, prev = [], {}
    for d in days:
        cur = panel[d]
        for t, row in cur.items():
            if row["tier"] == "S" and prev.get(t, {}).get("tier") != "S":
                entries_all.append((d, t, {"actionable": row["actionable"],
                                           "trade_type": row["trade_type"],
                                           "score": row["score"]}))
        prev = cur
    entries_act = [e for e in entries_all if e[2]["actionable"] is True]
    print(f"S-entry signals: {len(entries_all)} total, "
          f"{len(entries_act)} passing the 50-day gate")

    grid = []
    best = None
    for ent_name, ent in (("all-S", entries_all), ("S+gate", entries_act)):
        for rule in ("T10", "T21", "T42", "S12+T21", "TR15+T42", "SIG-A"):
            tr = simulate(ent, book, rule, panel, days)
            if tr.empty:
                continue
            x = tr["excess%"].dropna()
            row = {"entry": ent_name, "exit": rule, "trades": len(tr),
                   "mean_excess%": x.mean(), "median_excess%": x.median(),
                   "hit%": (x > 0).mean() * 100, "worst%": x.min(),
                   "avg_days": tr["days_held"].mean(),
                   "mean_net%": tr["net_ret%"].mean()}
            grid.append(row)
            key = (x.median() + x.mean()) / 2
            if best is None or key > best[0]:
                best = (key, ent_name, rule, tr)
    g = pd.DataFrame(grid).sort_values("mean_excess%", ascending=False)
    g.to_csv("data/strategy_grid.csv", index=False)
    print("\n=== strategy grid (net of costs, excess vs SPY) ===")
    print(g.round(2).to_string(index=False))

    _, ent_name, rule, tr = best
    tr.to_csv("data/strategy_trades_best.csv", index=False)
    print(f"\n=== best combo: entry={ent_name}, exit={rule} — trade list ===")
    print(tr.round(1).to_string(index=False))


if __name__ == "__main__":
    main()
