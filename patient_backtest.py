"""Patient-strategy backtest: delayed entry, milestone scale-outs, 6-12mo holds.

Hypotheses under test (user-proposed):
- BLACKOUT DELAY: insiders buy in open windows; the catalyst lands later.
  Enter 60 calendar days AFTER the Tier-S signal instead of the next day.
- LONG HOLD: 6-12 months to reach fair value, not 21 trading days.
- SCALE-OUT: sell tranches at fixed growth milestones (limit-order semantics:
  a tranche fills AT the target price the first day the close reaches it),
  remainder at the terminal date.

Grid: entry {T+1, T+60} × ladder {thirds@+20/+40, quarters@+15/+30/+50,
halves@+25, none} × terminal {126td (~6mo), 252td (~12mo)}. Costs 10bps per
side per tranche. Benchmark: SPY over the same entry→final-exit span.

Also prints the MFE table (max favorable excursion within 12mo after entry)
— the empirical basis for choosing milestone percentages.

Run:  python patient_backtest.py    (reads data/backtest_snapshots.csv)
"""
import sys
from datetime import date, timedelta

import pandas as pd

sys.path.insert(0, ".")

from strategy_backtest import PriceBook  # noqa: E402  (wraps stdout utf-8)

COST_SIDE = 0.001
LADDERS = {
    "thirds@20/40": [(0.20, 1 / 3), (0.40, 1 / 3)],
    "quarters@15/30/50": [(0.15, 0.25), (0.30, 0.25), (0.50, 0.25)],
    "half@25": [(0.25, 0.5)],
    "none(B&H)": [],
}
ENTRIES = {"T+1": 1, "T+60": 60}       # calendar days after the signal date
TERMINALS = {"6mo": 126, "12mo": 252}  # trading days after entry


def events_from_snapshots() -> list[dict]:
    snap = pd.read_csv("data/backtest_snapshots.csv")
    s_rows = snap[snap["tier"] == "S"].sort_values(["ticker", "eval_date"])
    out = []
    for ticker, grp in s_rows.groupby("ticker"):
        prev = None
        for _, row in grp.iterrows():
            d = date.fromisoformat(row["eval_date"])
            if prev is None or (d - prev).days > 9:
                out.append({"signal": d, "ticker": ticker,
                            "company": row["company"], "score": row["score"]})
            prev = d
    return sorted(out, key=lambda e: e["signal"])


def simulate(ev, book, delay_days, ladder, term_td):
    s = book.series(ev["ticker"])
    if s is None:
        return None
    start = pd.Timestamp(ev["signal"] + timedelta(days=delay_days))
    ei = s.index.searchsorted(start, side="left")
    if ei >= len(s) - 5:
        return None
    entry_dt, entry_px = s.index[ei], float(s.iloc[ei])
    if entry_px <= 0:
        return None
    n = len(s)
    end_i = min(ei + term_td, n - 1)
    censored = ei + term_td > n - 1

    tranches = []          # (weight, exit_px, exit_idx)
    remaining = 1.0
    pending = list(ladder)
    for j in range(ei + 1, end_i + 1):
        px = float(s.iloc[j])
        while pending and px >= entry_px * (1 + pending[0][0]):
            gain, w = pending.pop(0)
            tranches.append((w, entry_px * (1 + gain), j))
            remaining -= w
    if remaining > 1e-9:
        tranches.append((remaining, float(s.iloc[end_i]), end_i))

    gross = sum(w * (px / entry_px - 1.0) for w, px, _ in tranches)
    n_exits = len(tranches)
    net = gross - COST_SIDE - n_exits * COST_SIDE   # one buy + n tranche sells
    last_i = max(j for _, _, j in tranches)
    exit_dt = s.index[last_i]
    spy = book.series("SPY")
    spy_ret = None
    if spy is not None:
        a = spy.index.searchsorted(entry_dt, side="right") - 1
        b = spy.index.searchsorted(exit_dt, side="right") - 1
        if a >= 0 and b >= 0 and spy.iloc[a] > 0:
            spy_ret = float(spy.iloc[b] / spy.iloc[a] - 1.0)
    window = s.iloc[ei + 1:min(ei + 253, n)]
    mfe = float(window.max() / entry_px - 1.0) if len(window) else None
    mae = float(window.min() / entry_px - 1.0) if len(window) else None
    return {"ticker": ev["ticker"], "signal": str(ev["signal"]),
            "entry_dt": str(entry_dt.date()), "entry_px": round(entry_px, 2),
            "exit_dt": str(exit_dt.date()), "gross%": gross * 100,
            "net%": net * 100,
            "excess%": (net - spy_ret) * 100 if spy_ret is not None else None,
            "n_tranches_hit": n_exits - 1, "censored": censored,
            "mfe%": mfe * 100 if mfe is not None else None,
            "mae%": mae * 100 if mae is not None else None}


def main():
    book = PriceBook()
    evs = events_from_snapshots()
    print(f"{len(evs)} Tier-S signals {evs[0]['signal']} → {evs[-1]['signal']}")

    # MFE distribution after each entry style — the basis for target-setting.
    for label, delay in ENTRIES.items():
        rows = [simulate(e, book, delay, [], 252) for e in evs]
        rows = [r for r in rows if r and r["mfe%"] is not None]
        m = pd.Series([r["mfe%"] for r in rows])
        print(f"\nMFE within 12mo after {label} entry (n={len(m)}): "
              + "  ".join(f"p{p}={m.quantile(p/100):+.0f}%"
                          for p in (25, 40, 50, 60, 75, 90)))

    grid = []
    trades_best = None
    for e_label, delay in ENTRIES.items():
        for l_label, ladder in LADDERS.items():
            for t_label, term in TERMINALS.items():
                rows = [simulate(e, book, delay, ladder, term) for e in evs]
                rows = [r for r in rows if r]
                df = pd.DataFrame(rows)
                x = df["excess%"].dropna()
                complete = df[~df["censored"]]["excess%"].dropna()
                grid.append({
                    "entry": e_label, "ladder": l_label, "terminal": t_label,
                    "n": len(x), "mean_excess%": x.mean(),
                    "median_excess%": x.median(), "hit%": (x > 0).mean() * 100,
                    "worst%": x.min(),
                    "n_complete": len(complete),
                    "median_complete%": complete.median() if len(complete) else None,
                })
                if (e_label, l_label, t_label) == ("T+60", "quarters@15/30/50", "12mo"):
                    trades_best = df
    g = pd.DataFrame(grid).sort_values("median_excess%", ascending=False)
    g.to_csv("data/patient_grid.csv", index=False)
    print("\n=== patient grid (net of costs, excess vs SPY) ===")
    print(g.round(1).to_string(index=False))
    if trades_best is not None:
        trades_best.to_csv("data/patient_trades_T60_quarters_12mo.csv", index=False)
        print("\nsaved data/patient_grid.csv and "
              "data/patient_trades_T60_quarters_12mo.csv")


if __name__ == "__main__":
    main()
