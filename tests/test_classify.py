"""Tests for the V2 §A classifier / §B entry gate and the breadth gauge.

Run:  python tests/test_classify.py
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import breadth, db  # noqa: E402
from app.classify import market_context_for  # noqa: E402


def hist(closes):
    dates = pd.bdate_range("2025-01-02", periods=len(closes))
    return pd.DataFrame({"date": dates, "close": closes})


def buys_frame(rows, titles=None):
    df = pd.DataFrame(rows, columns=[
        "accession_no", "txn_seq", "shares", "value", "price_per_share",
        "transaction_date", "ticker"])
    df["security_title"] = titles if titles else "Common Stock"
    return df


def test_momentum():
    h = hist([50 + i * 50 / 299 for i in range(300)][:-1] + [95.0])
    b = buys_frame([("A1", 0, 1000, 90_000, 90.0, str(h["date"].iloc[-5].date()), "MOM")])
    ctx = market_context_for("MOM", b, get_history=lambda t: h)
    assert ctx["trade_type"] == "momentum", ctx
    assert ctx["actionable"] is True
    assert ctx["pct_below_high"] < 15


def test_value_below_ma_not_actionable():
    h = hist([100.0] * 250 + [90 - i for i in range(31)])   # sliding down to 60
    b = buys_frame([("B1", 0, 1000, 65_000, 65.0, str(h["date"].iloc[-2].date()), "VAL")])
    ctx = market_context_for("VAL", b, get_history=lambda t: h)
    assert ctx["trade_type"] == "value"
    assert ctx["actionable"] is False, "price below 50d MA must gate the value trade"
    assert ctx["discount_to_entry_pct"] is not None


def test_value_reclaimed_ma_actionable():
    h = hist([100.0] * 200 + [60.0] * 20 + [80.0] * 30)
    b = buys_frame([("C1", 0, 1000, 62_000, 62.0, str(h["date"].iloc[-40].date()), "REC")])
    ctx = market_context_for("REC", b, get_history=lambda t: h)
    assert ctx["trade_type"] == "value"
    assert ctx["actionable"] is True, "price above 50d MA -> actionable"
    # bought at 62, last 80 -> price above insider entry -> negative discount
    assert ctx["discount_to_entry_pct"] < 0


def test_below_market_flag():
    h = hist([60.0] * 100)
    d = str(h["date"].iloc[50].date())
    b = buys_frame([("D1", 0, 1000, 50_000, 50.0, d, "DIS"),   # 17% under close
                    ("D2", 0, 1000, 59_000, 59.0, d, "DIS")])  # fair price
    ctx = market_context_for("DIS", b, get_history=lambda t: h)
    assert ctx["n_below_market"] == 1, ctx["n_below_market"]


def test_preferred_excluded_from_vwap():
    h = hist([100.0] * 300)
    d = str(h["date"].iloc[250].date())
    b = buys_frame([("P1", 0, 1000, 100_000, 100.0, d, "MIX"),
                    ("P2", 0, 100, 100_000, 1000.0, d, "MIX")],
                   titles=["Common Stock", "Series D Convertible Preferred Stock"])
    ctx = market_context_for("MIX", b, get_history=lambda t: h)
    assert ctx["entry_vwap"] == 100.0, "preferred trade must not pollute the VWAP"


def test_foreign_price_not_comparable():
    h = hist([100.0] * 300)
    d = str(h["date"].iloc[250].date())
    # As-filed at 3.7 vs USD close 100 (foreign listing) -> excluded everywhere
    b = buys_frame([("F1", 0, 10_000, 37_000, 3.7, d, "FRN"),
                    ("F2", 0, 1000, 99_000, 99.0, d, "FRN")])
    ctx = market_context_for("FRN", b, get_history=lambda t: h)
    assert ctx["n_below_market"] == 0, "non-comparable price must not flag below-mkt"
    assert ctx["entry_vwap"] == 99.0


def test_breadth():
    conn = db.connect(Path(":memory:"))
    days = pd.bdate_range("2026-06-01", periods=30)
    for i, d in enumerate(days):
        ds = str(d.date())
        conn.execute("INSERT INTO filings (accession_no, cik, filed_at, aff_10b5_one)"
                     " VALUES (?,?,?,0)", (f"U{i}", "1", ds))
        # 1 unscheduled buy + 2 unscheduled sells per day -> share ~33.3%
        for seq, (code, ad) in enumerate((("P", "A"), ("S", "D"), ("S", "D"))):
            conn.execute(
                "INSERT INTO transactions (accession_no, txn_seq, insider_cik,"
                " transaction_date, transaction_code, acquired_disposed, is_derivative)"
                " VALUES (?,?,?,?,?,?,0)", (f"U{i}", seq, f"i{seq}", ds, code, ad))
        # scheduled (10b5-1) buys must be ignored by the gauge
        conn.execute("INSERT INTO filings (accession_no, cik, filed_at, aff_10b5_one)"
                     " VALUES (?,?,?,1)", (f"S{i}", "1", ds))
        for seq in range(10):
            conn.execute(
                "INSERT INTO transactions (accession_no, txn_seq, insider_cik,"
                " transaction_date, transaction_code, acquired_disposed, is_derivative)"
                " VALUES (?,?,?,?,'P','A',0)", (f"S{i}", seq, "x", ds))
    conn.commit()
    s = breadth.breadth_series(conn, lookback_days=400)
    assert not s.empty
    last = s["buy_share"].iloc[-1]
    assert abs(last - 33.33) < 0.5, last
    assert breadth.breadth_label(last) == "bullish"
    assert breadth.breadth_label(55.0) == "very bullish"
    assert breadth.breadth_label(20.0) == "weak"


if __name__ == "__main__":
    test_momentum()
    test_value_below_ma_not_actionable()
    test_value_reclaimed_ma_actionable()
    test_below_market_flag()
    test_preferred_excluded_from_vwap()
    test_foreign_price_not_comparable()
    test_breadth()
    print("all classify/breadth tests passed")
