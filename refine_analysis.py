"""Refinement lab: one-variable-at-a-time tests for entry filters, exit
rules, and scoring splits on the multi-year Tier-S events.

Discipline: 59 events is small — every test here is a single split or a
doc-motivated rule, checked for year-by-year consistency, not a free sweep.

Run:  python refine_analysis.py   (reads data/backtest_snapshots.csv)
"""
import sys
from datetime import date

import pandas as pd

sys.path.insert(0, ".")

from app import breadth, db  # noqa: E402
from strategy_backtest import PriceBook  # noqa: E402

COST = 0.002  # round trip


def entry_events(tier_set=("S",), block=("S",)):
    """First eval date a ticker's tier lands in tier_set (previous tier not
    in `block`), with the cluster features of that snapshot."""
    snap = pd.read_csv("data/backtest_snapshots.csv")
    snap = snap.sort_values(["ticker", "eval_date"])
    out = []
    prev_tier: dict[str, str] = {}
    prev_date: dict[str, date] = {}
    for r in snap.itertuples():
        d = date.fromisoformat(r.eval_date)
        stale = r.ticker in prev_date and (d - prev_date[r.ticker]).days > 9
        if stale:
            prev_tier.pop(r.ticker, None)
        if r.tier in tier_set and prev_tier.get(r.ticker) not in block:
            out.append({"signal": d, "ticker": r.ticker, "score": r.score,
                        "role_score": r.role_score, "n_insiders": r.n_insiders,
                        "total_value": r.total_value, "type": r.trade_type,
                        "actionable": r.actionable})
        prev_tier[r.ticker] = r.tier
        prev_date[r.ticker] = d
    # dedupe per ticker+signal
    seen, ded = set(), []
    for e in out:
        k = (e["ticker"], e["signal"])
        if k not in seen:
            seen.add(k)
            ded.append(e)
    return sorted(ded, key=lambda e: e["signal"])


def simulate(ev, book, rule="T21"):
    s = book.series(ev["ticker"])
    if s is None:
        return None
    ei = s.index.searchsorted(pd.Timestamp(ev["signal"]), side="right")
    n = len(s)
    if ei >= n - 2:
        return None
    entry_px = float(s.iloc[ei])
    if entry_px <= 0:
        return None
    end = min(ei + 21, n - 1)

    weights_exits = None
    if rule == "T21":
        xi = end
    elif rule.startswith("stop"):
        lvl = 1 - abs(float(rule[4:])) / 100
        xi = end
        for j in range(ei + 1, end):
            if s.iloc[j] <= entry_px * lvl:
                xi = min(j + 1, n - 1)
                break
    elif rule == "day10flat":
        mid = min(ei + 10, n - 1)
        xi = mid if float(s.iloc[mid]) < entry_px else end
    elif rule == "scale15+stop12":
        xi = end
        for j in range(ei + 1, end):
            if s.iloc[j] <= entry_px * 0.88:
                xi = min(j + 1, n - 1)
                break
        hit15 = None
        for j in range(ei + 1, xi + 1):
            if float(s.iloc[j]) >= entry_px * 1.15:
                hit15 = j
                break
        if hit15 is not None:
            weights_exits = [(0.5, entry_px * 1.15, hit15), (0.5, float(s.iloc[xi]), xi)]
    if weights_exits is None:
        weights_exits = [(1.0, float(s.iloc[xi]), xi)]

    gross = sum(w * (px / entry_px - 1) for w, px, _ in weights_exits)
    last_i = max(j for _, _, j in weights_exits)
    spy = book.series("SPY")
    a = spy.index.searchsorted(s.index[ei], side="right") - 1
    b = spy.index.searchsorted(s.index[last_i], side="right") - 1
    if a < 0 or b < 0 or spy.iloc[a] <= 0:
        return None
    spy_ret = float(spy.iloc[b] / spy.iloc[a] - 1)
    return {"excess": (gross - COST - spy_ret) * 100, "entry_px": entry_px,
            "days": last_i - ei, "year": str(ev["signal"])[:4]}


def stats(rows, label):
    if not rows:
        return
    x = pd.Series([r["excess"] for r in rows])
    print(f"  {label:34s} n={len(x):3d}  mean {x.mean():+6.2f}%  "
          f"med {x.median():+6.2f}%  hit {(x > 0).mean()*100:3.0f}%  "
          f"worst {x.min():+7.1f}%")


def main():
    book = PriceBook()
    conn = db.connect()
    bs = breadth.breadth_series(conn, lookback_days=1400)
    bshare = bs.set_index("date")["buy_share"].sort_index()

    def breadth_at(d):
        i = bshare.index.searchsorted(pd.Timestamp(d), side="right") - 1
        return float(bshare.iloc[i]) if i >= 0 else None

    S = entry_events()
    for e in S:
        e["breadth"] = breadth_at(e["signal"])
    base = [(e, simulate(e, book)) for e in S]
    base = [(e, r) for e, r in base if r]

    print("=== A. entry filters, one at a time (exit fixed at T21, no stop) ===")
    stats([r for _, r in base], "ALL S entries (baseline)")
    stats([r for e, r in base if e["breadth"] and e["breadth"] >= 33], "breadth ≥33%")
    stats([r for e, r in base if e["actionable"] is True or e["actionable"] == "True"],
          "50d gate passed")
    stats([r for e, r in base if r["entry_px"] >= 5], "entry price ≥ $5")
    stats([r for e, r in base if e["role_score"] >= 1.5], "role score ≥ 1.5")
    stats([r for e, r in base if e["n_insiders"] >= 3], "≥3 buying units")
    stats([r for e, r in base if e["total_value"] >= 5e6], "cluster ≥ $5M")
    combo = [r for e, r in base
             if e["breadth"] and e["breadth"] >= 33 and r["entry_px"] >= 5
             and (e["actionable"] is True or e["actionable"] == "True")]
    stats(combo, "COMBO: breadth+gate+price≥$5")

    print("\n=== B. cutting losers early (entries = breadth≥33 + price≥$5) ===")
    filt = [e for e, r in base
            if e["breadth"] and e["breadth"] >= 33 and r["entry_px"] >= 5]
    for rule, label in (("T21", "hold 21d, no stop"),
                        ("stop-8", "stop −8% + T21"),
                        ("stop-10", "stop −10% + T21"),
                        ("stop-12", "stop −12% + T21"),
                        ("stop-15", "stop −15% + T21"),
                        ("day10flat", "exit day 10 if below entry"),
                        ("scale15+stop12", "sell ½ at +15%, stop −12%, T21")):
        rows = [simulate(e, book, rule) for e in filt]
        stats([r for r in rows if r], label)

    print("\n=== C. year-by-year for the combo rule (consistency check) ===")
    for y in ("2023", "2024", "2025", "2026"):
        rows = [simulate(e, book, "day10flat") for e in filt
                if str(e["signal"]).startswith(y)]
        stats([r for r in rows if r], f"{y} (day10flat exit)")

    print("\n=== D. is S special? A-entries under the same combo filters ===")
    A = entry_events(tier_set=("A",), block=("S", "A"))
    for e in A:
        e["breadth"] = breadth_at(e["signal"])
    rowsA = [(e, simulate(e, book)) for e in A]
    rowsA = [(e, r) for e, r in rowsA if r]
    stats([r for _, r in rowsA], "ALL A entries")
    stats([r for e, r in rowsA
           if e["breadth"] and e["breadth"] >= 33 and r["entry_px"] >= 5],
          "A + breadth≥33 + price≥$5")


if __name__ == "__main__":
    main()
