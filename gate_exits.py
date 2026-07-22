"""Exit design for margin-of-safety gate trades (S-score AND ≥30% DCF-cheap).

User rule under test: minimum 6-month hold, exit when the price target is
reached (the DCF fair value — i.e., the valuation gap closes), hard cap at
12 months. Variants: flat 6mo / flat 12mo / fair-value target (limit fill)
anytime with 12mo cap / halves at 50%-gap-closed and at fair / the short
21d+stop rule for comparison.

Run:  python gate_exits.py
"""
import sys
from datetime import date

import pandas as pd

sys.path.insert(0, ".")

from strategy_backtest import PriceBook  # noqa: E402

COST = 0.002
PATHS = ("data/train_snapshots.csv", "data/test_snapshots.csv")


def gate_events():
    out = []
    for path in PATHS:
        snap = pd.read_csv(path).sort_values(["ticker", "eval_date"])
        prev_ok, prev_date = {}, {}
        for r in snap.itertuples():
            d = date.fromisoformat(r.eval_date)
            if r.ticker in prev_date and (d - prev_date[r.ticker]).days > 9:
                prev_ok.pop(r.ticker, None)
            qual = (r.score >= 10 and pd.notna(r.dcf_discount)
                    and r.dcf_discount >= 0.30)
            if qual and not prev_ok.get(r.ticker):
                out.append({"signal": d, "ticker": r.ticker,
                            "disc": float(r.dcf_discount)})
            prev_ok[r.ticker] = qual
            prev_date[r.ticker] = d
    seen, ded = set(), []
    for e in sorted(out, key=lambda x: x["signal"]):
        k = (e["ticker"], e["signal"])
        if k not in seen:
            seen.add(k)
            ded.append(e)
    return ded


def sim(e, book, mode, delay_days=1):
    s = book.series(e["ticker"])
    if s is None:
        return None
    start = pd.Timestamp(e["signal"]) + pd.Timedelta(days=delay_days)
    ei = s.index.searchsorted(start, side="left")
    n = len(s)
    if ei >= n - 2 or s.iloc[ei] <= 0:
        return None
    entry = float(s.iloc[ei])
    fair = entry / (1 - e["disc"])          # price at which the gap is closed
    half_gap = entry + 0.5 * (fair - entry)
    cap = min(ei + 252, n - 1)
    censored = ei + 252 > n - 1

    def spy_ex(weighted):
        spy = book.series("SPY")
        last_i = max(j for _, _, j in weighted)
        a = spy.index.searchsorted(s.index[ei], side="right") - 1
        b = spy.index.searchsorted(s.index[last_i], side="right") - 1
        if a < 0 or b < 0 or spy.iloc[a] <= 0:
            return None
        spy_ret = float(spy.iloc[b] / spy.iloc[a] - 1)
        gross = sum(w * (px / entry - 1) for w, px, _ in weighted)
        return (gross - COST - spy_ret) * 100, s.index[last_i], \
            sum(w * px for w, px, _ in weighted)

    if mode in ("6mo", "12mo"):
        xi = min(ei + (126 if mode == "6mo" else 252), n - 1)
        weighted = [(1.0, float(s.iloc[xi]), xi)]
        reason = mode if xi < n - 1 or not censored else "end of data"
    elif mode == "fair-or-12mo":
        hit = next((j for j in range(ei + 1, cap + 1)
                    if float(s.iloc[j]) >= fair), None)
        if hit is not None:
            weighted, reason = [(1.0, fair, hit)], "target (fair value)"
        else:
            weighted, reason = [(1.0, float(s.iloc[cap]), cap)], "12mo cap"
    elif mode == "fair-after-6mo":
        # Strict reading: NOTHING exits before 6 months; from month 6 on,
        # exit at fair value if reached, else at the 12-month cap.
        floor = ei + 126
        hit = next((j for j in range(max(ei + 1, floor), cap + 1)
                    if float(s.iloc[j]) >= fair), None)
        if hit is not None:
            weighted, reason = [(1.0, max(fair, float(s.iloc[hit])), hit)], \
                "target after 6mo"
        else:
            weighted, reason = [(1.0, float(s.iloc[cap]), cap)], "12mo cap"
    elif mode == "halves":
        h1 = next((j for j in range(ei + 1, cap + 1)
                   if float(s.iloc[j]) >= half_gap), None)
        h2 = next((j for j in range(ei + 1, cap + 1)
                   if float(s.iloc[j]) >= fair), None)
        weighted = []
        if h1 is not None:
            weighted.append((0.5, half_gap, h1))
        if h2 is not None:
            weighted.append((0.5, fair, h2))
        w_used = sum(w for w, _, _ in weighted)
        if w_used < 1:
            weighted.append((1 - w_used, float(s.iloc[cap]), cap))
        reason = ("both targets" if h2 is not None else
                  "half target" if h1 is not None else "12mo cap")
    elif mode == "21d-stop12":
        end = min(ei + 21, n - 1)
        xi, reason = end, "21d"
        for j in range(ei + 1, end):
            if s.iloc[j] <= entry * 0.88:
                xi, reason = min(j + 1, n - 1), "stop"
                break
        weighted = [(1.0, float(s.iloc[xi]), xi)]
    r = spy_ex(weighted)
    if r is None:
        return None
    excess, exit_dt, avg_exit = r
    return {"signal": str(e["signal"]), "ticker": e["ticker"],
            "disc%": round(e["disc"] * 100),
            "entry_date": str(s.index[ei].date()), "entry": round(entry, 2),
            "target": round(fair, 2), "exit_date": str(exit_dt.date()),
            "avg_exit": round(avg_exit, 2), "reason": reason,
            "excess%": round(excess, 1), "censored": censored}


def main():
    book = PriceBook()
    evs = gate_events()
    print(f"{len(evs)} margin-of-safety gate events\n")
    for delay in (1, 90):
        print(f"--- entry T+{delay} ---")
        for mode, label in (("21d-stop12", "short rule (21d + stop), reference"),
                            ("6mo", "flat 6-month hold"),
                            ("12mo", "flat 12-month hold"),
                            ("fair-or-12mo", "target anytime, cap 12mo"),
                            ("fair-after-6mo", "USER RULE: no exit <6mo, then target, cap 12mo"),
                            ("halves", "halves at 50%-gap + fair, cap 12mo")):
            rows = [sim(e, book, mode, delay) for e in evs]
            rows = [r for r in rows if r]
            x = pd.Series([r["excess%"] for r in rows])
            print(f"  {label:48s} n={len(x):2d}  mean {x.mean():+6.1f}%  "
                  f"med {x.median():+6.1f}%  hit {(x > 0).mean()*100:3.0f}%  "
                  f"worst {x.min():+7.1f}%")
        print()
    print("=== per-trade — USER STRATEGY (T+90, no exit <6mo, target, 12mo cap) ===")
    rows = [sim(e, book, "fair-after-6mo", 90) for e in evs]
    df = pd.DataFrame([r for r in rows if r])
    print(df.to_string(index=False))
    df.to_csv("data/gate_trades_user_rule.csv", index=False)


if __name__ == "__main__":
    main()
