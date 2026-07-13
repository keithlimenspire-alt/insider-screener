"""Deterministic tests for the Phase 2/3 judgement layer against a synthetic DB.

Run:  python tests/test_strategy.py
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import clusters, db  # noqa: E402

AS_OF = date(2026, 7, 13)


def seed(conn):
    def filing(acc, cik, ticker, filed_at):
        conn.execute(
            "INSERT INTO filings (accession_no, cik, company_name, ticker, filed_at,"
            " period_of_report, filing_url) VALUES (?,?,?,?,?,?,?)",
            (acc, cik, f"{ticker} Corp", ticker, filed_at, filed_at, "http://x"))

    def txn(acc, seq, insider, code, ad, shares, price, after, deriv=0,
            officer=0, title=None, director=0, tenpct=0, tdate=None, n_owners=1):
        conn.execute(
            """INSERT INTO transactions (accession_no, txn_seq, n_owners, is_derivative,
               insider_name, insider_cik, is_director, is_officer, officer_title,
               is_ten_percent_owner, transaction_date, transaction_code,
               acquired_disposed, shares, price_per_share, value, shares_owned_after,
               direct_indirect, security_title)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (acc, seq, n_owners, deriv, f"Insider {insider}", insider, director,
             officer, title, tenpct, tdate, code, ad, shares, price,
             shares * price if shares and price is not None else None,
             after, "D", "Common Stock"))

    # AAA: clean 2-insider cluster — GC ($250k, first-time) + CFO ($300k)
    filing("A1", "100", "AAA", "2026-07-01")
    txn("A1", 0, "gc1", "P", "A", 10_000, 25.0, 50_000, officer=1,
        title="EVP, General Counsel", tdate="2026-07-01")
    filing("A2", "100", "AAA", "2026-07-02")
    txn("A2", 0, "cfo1", "P", "A", 10_000, 30.0, 110_000, officer=1,
        title="Chief Financial Officer", tdate="2026-07-02")
    # cfo1 also bought long before the window -> NOT first-time
    filing("A0", "100", "AAA", "2026-01-05")
    txn("A0", 0, "cfo1", "P", "A", 1_000, 20.0, 100_000, officer=1,
        title="Chief Financial Officer", tdate="2026-01-05")

    # BBB: the Starbucks trap — CEO "buys" but filed M + S the same day
    filing("B1", "200", "BBB", "2026-07-03")
    txn("B1", 0, "ceo2", "P", "A", 20_000, 50.0, 40_000, officer=1,
        title="Chief Executive Officer", tdate="2026-07-03")
    txn("B1", 1, "ceo2", "M", "A", 20_000, 10.0, 60_000, tdate="2026-07-03",
        officer=1, title="Chief Executive Officer")
    txn("B1", 2, "ceo2", "S", "D", 20_000, 50.0, 40_000, tdate="2026-07-03",
        officer=1, title="Chief Executive Officer")
    # a clean director buy so BBB still exists as a 2-insider candidate pre-exclusion
    filing("B2", "200", "BBB", "2026-07-03")
    txn("B2", 0, "dir2", "P", "A", 5_000, 50.0, 10_000, director=1, tdate="2026-07-03")

    # CCC: fund noise — one 10%-owner trade reported jointly by 3 fund entities,
    # buying 0.5% of an existing stake (one economic $500k trade, 3 owner rows)
    filing("C1", "300", "CCC", "2026-07-05")
    for owner in ("fund3a", "fund3b", "fund3c"):
        txn("C1", 0, owner, "P", "A", 50_000, 10.0, 10_050_000, tenpct=1,
            tdate="2026-07-05", n_owners=3)
    # second distinct insider so CCC clusters
    filing("C2", "300", "CCC", "2026-07-06")
    txn("C2", 0, "dir3", "P", "A", 30_000, 10.0, 60_000, director=1, tdate="2026-07-06")

    # DDD: stale — 2 insiders but last buy 50 days ago
    filing("D1", "400", "DDD", "2026-05-24")
    txn("D1", 0, "d4a", "P", "A", 20_000, 15.0, 40_000, director=1, tdate="2026-05-24")
    filing("D2", "400", "DDD", "2026-05-24")
    txn("D2", 0, "d4b", "P", "A", 20_000, 15.0, 40_000, director=1, tdate="2026-05-24")

    conn.commit()


def main():
    conn = db.connect(Path(":memory:"))
    seed(conn)

    cl, kept = clusters.build_screen(conn, window_days=60, min_value=200_000,
                                     min_cluster=2, as_of=AS_OF)
    by = {r.ticker: r for r in cl.itertuples()}

    # AAA: clean cluster, role score = GC 1.0 + CFO 0.9, one first-timer (gc1)
    assert "AAA" in by, cl
    aaa = by["AAA"]
    assert aaa.n_insiders == 2
    assert abs(aaa.role_score - 1.9) < 1e-9, aaa.role_score
    assert aaa.n_first_time == 1, "cfo1 bought in January -> not first-time"
    assert aaa.n_conviction == 2
    assert not aaa.fund_noise and not aaa.stale

    # BBB: exercise-flagged CEO buy excluded -> only 1 clean insider -> no cluster
    assert "BBB" not in by, "exercise-flagged buy must not sustain a cluster"
    assert (kept[kept["ticker"] == "BBB"]["insider_cik"] == "ceo2").sum() == 0

    # ...but including flagged buys resurrects it, with the exercise flag shown
    cl_inc, _ = clusters.build_screen(conn, 60, 200_000, 2, as_of=AS_OF,
                                      include_exercise_flagged=True)
    by_inc = {r.ticker: r for r in cl_inc.itertuples()}
    assert "BBB" in by_inc

    # CCC: joint filing dedupes to one $500k trade + one $300k trade; the fund
    # trade is >50% of $ and 10%-owners bought <2% of stake -> fund-noise flag
    ccc = by["CCC"]
    assert ccc.n_insiders == 4          # 3 joint entities + 1 director (Phase 1 semantics)
    assert abs(ccc.total_value - 800_000) < 1e-6, ccc.total_value
    assert ccc.fund_noise
    assert "fund-noise" in ccc.flags

    # DDD: stale flag (50 days since last buy > 40)
    ddd = by["DDD"]
    assert ddd.stale and "stale" in ddd.flags

    # role weights
    assert clusters.role_weight("GC") == 1.0
    assert clusters.role_weight("CXO") == 0.8      # any Chief * Officer
    assert clusters.role_weight("CEO") == 0.3
    assert clusters.role_weight("10%") == 0.1
    assert clusters.role_weight("???") == 0.2      # unknown -> Other

    print("all strategy tests passed")


if __name__ == "__main__":
    main()
