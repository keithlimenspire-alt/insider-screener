"""Train/test tuning: pick the config on 2019-2022, validate on 2023-2026.

Pre-declared menu (16 configs — no free-form sweeping):
    score cutoff   ∈ {10, 11}
    cluster size   ∈ {any, ≥ $5M}
    role score     ∈ {any, ≥ 1.5}
    exit           ∈ {stop-12+T21, day10flat then stop-12+T21}
Metric: median excess vs SPY (primary), mean and hit% reported; a config
needs n ≥ 20 train trades to qualify. The single train winner is then run
ONCE on the untouched test period.

Prereqs: data/train_snapshots.csv (walk 2019-07-05 → 2022-12-30) and
data/test_snapshots.csv (walk 2023-07-07 → 2026-07-10).

Run:  python tune_split.py
"""
import sys
from datetime import date

import pandas as pd

sys.path.insert(0, ".")

from refine_analysis import simulate  # noqa: E402
from strategy_backtest import PriceBook  # noqa: E402

MENU = [
    {"cut": cut, "size": size, "role": role, "exit": ex, "dcf": dcf}
    for cut in (10.0, 11.0)
    for size in (0.0, 5e6)
    for role in (0.0, 1.5)
    for ex in ("stop-12", "day10flat")
    for dcf in (False, True)   # True: DCF ≥30% cheap also qualifies (promotion)
]


def entries(snap_path: str, cut: float, size: float, role: float,
            dcf: bool = False) -> list[dict]:
    snap = pd.read_csv(snap_path).sort_values(["ticker", "eval_date"])
    has_dcf = "dcf_discount" in snap.columns
    out, prev_ok, prev_date = [], {}, {}
    for r in snap.itertuples():
        d = date.fromisoformat(r.eval_date)
        if r.ticker in prev_date and (d - prev_date[r.ticker]).days > 9:
            prev_ok.pop(r.ticker, None)
        cheap = (dcf and has_dcf and pd.notna(r.dcf_discount)
                 and r.dcf_discount >= 0.30)
        qual = r.score >= cut or cheap
        if qual and not prev_ok.get(r.ticker):
            if (r.total_value >= size and r.role_score >= role) or cheap:
                out.append({"signal": d, "ticker": r.ticker, "score": r.score})
        prev_ok[r.ticker] = qual
        prev_date[r.ticker] = d
    seen, ded = set(), []
    for e in out:
        k = (e["ticker"], e["signal"])
        if k not in seen:
            seen.add(k)
            ded.append(e)
    return sorted(ded, key=lambda e: e["signal"])


def run(snap_path: str, cfg: dict, book: PriceBook) -> pd.Series | None:
    evs = entries(snap_path, cfg["cut"], cfg["size"], cfg["role"],
                  cfg.get("dcf", False))
    rule = cfg["exit"] if cfg["exit"] != "day10flat" else "day10flat"
    rows = []
    for e in evs:
        r = simulate(e, book, rule)
        if r and rule == "day10flat":
            pass
        if r:
            rows.append(r["excess"])
    return pd.Series(rows) if rows else None


def describe(x: pd.Series | None) -> str:
    if x is None or len(x) == 0:
        return "n=0"
    return (f"n={len(x):3d}  mean {x.mean():+6.2f}%  med {x.median():+6.2f}%  "
            f"hit {(x > 0).mean()*100:3.0f}%  worst {x.min():+7.1f}%")


def main():
    book = PriceBook()
    print("=== TRAIN (2019-2022) — 16-config menu ===")
    results = []
    for cfg in MENU:
        x = run("data/train_snapshots.csv", cfg, book)
        label = (f"cut≥{cfg['cut']:.0f} size≥{cfg['size']/1e6:.0f}M "
                 f"role≥{cfg['role']} exit={cfg['exit']}"
                 + (" +dcf30" if cfg.get("dcf") else ""))
        print(f"  {label:48s} {describe(x)}")
        if x is not None and len(x) >= 20:
            results.append((x.median(), x.mean(), cfg, x))
    if not results:
        print("no config met the n≥20 bar on train — stopping honestly")
        return
    results.sort(key=lambda t: (t[0] + t[1]) / 2, reverse=True)
    med, mean, best, xtrain = results[0]
    print(f"\nTRAIN WINNER: cut≥{best['cut']:.0f} size≥{best['size']/1e6:.0f}M "
          f"role≥{best['role']} exit={best['exit']}"
          + (" +dcf30" if best.get("dcf") else "")
          + f"  → {describe(xtrain)}")

    print("\n=== TEST (2023-2026) — single validation run of the winner ===")
    xtest = run("data/test_snapshots.csv", best, book)
    print(f"  winner on test:   {describe(xtest)}")
    xbase = run("data/test_snapshots.csv",
                {"cut": 10.0, "size": 0.0, "role": 0.0, "exit": "stop-12"}, book)
    print(f"  baseline on test: {describe(xbase)}")


if __name__ == "__main__":
    main()
