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
    def filing(acc, cik, ticker, filed_at, aff=0):
        conn.execute(
            "INSERT INTO filings (accession_no, cik, company_name, ticker, filed_at,"
            " period_of_report, filing_url, aff_10b5_one) VALUES (?,?,?,?,?,?,?,?)",
            (acc, cik, f"{ticker} Corp", ticker, filed_at, filed_at, "http://x", aff))

    def txn(acc, seq, insider, code, ad, shares, price, after, deriv=0,
            officer=0, title=None, director=0, tenpct=0, tdate=None, n_owners=1,
            fn=None):
        conn.execute(
            """INSERT INTO transactions (accession_no, txn_seq, n_owners, is_derivative,
               insider_name, insider_cik, is_director, is_officer, officer_title,
               is_ten_percent_owner, transaction_date, transaction_code,
               acquired_disposed, shares, price_per_share, value, shares_owned_after,
               direct_indirect, security_title, footnotes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (acc, seq, n_owners, deriv, f"Insider {insider}", insider, director,
             officer, title, tenpct, tdate, code, ad, shares, price,
             shares * price if shares and price is not None else None,
             after, "D", "Common Stock", fn))

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

    # EEE: one economic trade split across TWO accessions (EDGAR owner cap),
    # linked by the shared owner fundYe — must dedupe to one trade, one unit
    filing("E1", "500", "EEE", "2026-07-07")
    for owner in ("fundXe", "fundYe"):
        txn("E1", 0, owner, "P", "A", 40_000, 10.0, 4_040_000, tenpct=1,
            tdate="2026-07-07", n_owners=2)
    filing("E2", "500", "EEE", "2026-07-07")
    for owner in ("fundYe", "fundZe"):
        txn("E2", 0, owner, "P", "A", 40_000, 10.0, 4_040_000, tenpct=1,
            tdate="2026-07-07", n_owners=2)

    # GGG: solo General Counsel buy (§4: high even solo)
    filing("G1", "600", "GGG", "2026-07-08")
    txn("G1", 0, "gc6", "P", "A", 8_000, 30.0, 20_000, officer=1,
        title="General Counsel", tdate="2026-07-08")

    # RRR: buys in 3 distinct months but all INSIDE the window -> not routine
    filing("R1", "700", "RRR", "2026-05-20")
    txn("R1", 0, "r7a", "P", "A", 15_000, 20.0, 30_000, director=1, tdate="2026-05-20")
    filing("R2", "700", "RRR", "2026-06-20")
    txn("R2", 0, "r7b", "P", "A", 15_000, 20.0, 30_000, director=1, tdate="2026-06-20")
    filing("R3", "700", "RRR", "2026-07-01")
    txn("R3", 0, "r7a", "P", "A", 15_000, 20.0, 45_000, director=1, tdate="2026-07-01")

    # HHH: 3 distinct buy-months in the year BEFORE the window -> routine
    for i, d in enumerate(("2026-01-10", "2026-02-10", "2026-03-10")):
        filing(f"H{i}", "800", "HHH", d)
        txn(f"H{i}", 0, "h8a", "P", "A", 1_000, 20.0, 10_000 + i, director=1, tdate=d)
    filing("H3", "800", "HHH", "2026-07-02")
    txn("H3", 0, "h8a", "P", "A", 15_000, 20.0, 40_000, director=1, tdate="2026-07-02")
    filing("H4", "800", "HHH", "2026-07-02")
    txn("H4", 0, "h8b", "P", "A", 15_000, 20.0, 40_000, director=1, tdate="2026-07-02")

    # III (V2): regime flip — i9a only SOLD in the lookback year, now buys
    filing("I0", "900", "III", "2026-02-15")
    txn("I0", 0, "i9a", "S", "D", 5_000, 22.0, 95_000, officer=1,
        title="Chief Operating Officer", tdate="2026-02-15")
    filing("I1", "900", "III", "2026-07-03")
    txn("I1", 0, "i9a", "P", "A", 12_000, 21.0, 107_000, officer=1,
        title="Chief Operating Officer", tdate="2026-07-03")
    filing("I2", "900", "III", "2026-07-03")
    txn("I2", 0, "i9b", "P", "A", 12_000, 21.5, 30_000, director=1, tdate="2026-07-03")

    # JJJ (V2): scheduled — one buy carries a 10b5-1 plan footnote
    filing("J1", "1000", "JJJ", "2026-07-06")
    txn("J1", 0, "j10a", "P", "A", 11_000, 19.5, 40_000, director=1, tdate="2026-07-06",
        fn="Shares purchased pursuant to a Rule 10b5-1 trading plan adopted 2026-03-01.")
    filing("J2", "1000", "JJJ", "2026-07-06")
    txn("J2", 0, "j10b", "P", "A", 11_000, 19.7, 40_000, director=1, tdate="2026-07-06")

    # KKK (V2): offering tell — 3 independent units at one identical round price
    for i, who in enumerate(("k11a", "k11b", "k11c")):
        filing(f"K{i}", "1100", "KKK", "2026-07-07")
        txn(f"K{i}", 0, who, "P", "A", 25_000, 10.0, 25_000, director=1,
            tdate="2026-07-07")

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

    # CCC: 3 joint co-filers are ONE buying unit + 1 director = 2 units;
    # money dedupes to one $500k trade + one $300k trade; the fund trade is
    # >50% of $ and the 10%-owners bought <2% of stake -> fund-noise flag
    ccc = by["CCC"]
    assert ccc.n_insiders == 2, "joint co-filers must collapse to one buying unit"
    assert ccc.n_filers == 4
    assert abs(ccc.role_score - 0.5) < 1e-9      # 10% unit 0.1 + Dir 0.4
    assert abs(ccc.total_value - 800_000) < 1e-6, ccc.total_value
    assert ccc.fund_noise
    assert "fund-noise" in ccc.flags

    # DDD: stale flag (50 days since last buy > 40)
    ddd = by["DDD"]
    assert ddd.stale and "stale" in ddd.flags

    # EEE: one trade split across two accessions with an overlapping owner —
    # one unit, ONE economic $400k trade, not two
    cl1, _ = clusters.build_screen(conn, 60, 200_000, 1, as_of=AS_OF,
                                   include_solo_gc=False)
    eee = {r.ticker: r for r in cl1.itertuples()}["EEE"]
    assert eee.n_insiders == 1 and eee.n_filers == 3
    assert eee.n_buys == 1, "split joint trade must dedupe to one economic trade"
    assert abs(eee.total_value - 400_000) < 1e-6, eee.total_value

    # GGG: solo GC kept below the cluster minimum, flagged; dropped when off
    ggg = by.get("GGG")
    assert ggg is not None and "solo-GC" in ggg.flags
    cl_nogc, _ = clusters.build_screen(conn, 60, 200_000, 2, as_of=AS_OF,
                                       include_solo_gc=False)
    assert "GGG" not in {r.ticker for r in cl_nogc.itertuples()}

    # RRR: 3 buy-months inside the window -> NOT routine; HHH: 3 buy-months in
    # the year before the window -> routine
    rrr = by["RRR"]
    assert not rrr.routine, "window's own buys must not self-flag routine"
    hhh = by["HHH"]
    assert hhh.routine and "routine" in hhh.flags
    assert hhh.n_first_time == 1, "h8a bought before the window -> not first-time"

    # III (V2): regime-flip flag — i9a sold all of last year, now buys
    iii = by["III"]
    assert iii.n_regime_flip == 1 and "regime-flip×1" in iii.flags
    assert hhh.n_regime_flip == 0, "prior buyer h8a is not a regime flip"

    # JJJ (V2): the 10b5-1 scheduled buy is excluded -> only 1 clean unit left
    assert "JJJ" not in by, "scheduled buy must not sustain the cluster"
    # KKK (V2): 3 units at one identical round price = offering tell -> gone
    assert "KKK" not in by, "offering-pattern buys must be excluded"
    # DDD (2 units at same round price) stays — threshold is 3
    assert "DDD" in by
    # ...and both reappear when low-signal buys are included
    cl_ls, _ = clusters.build_screen(conn, 60, 200_000, 2, as_of=AS_OF,
                                     include_low_signal=True)
    by_ls = {r.ticker: r for r in cl_ls.itertuples()}
    assert "JJJ" in by_ls and "KKK" in by_ls

    # role weights
    assert clusters.role_weight("GC") == 1.0
    assert clusters.role_weight("CXO") == 0.8      # any Chief * Officer
    assert clusters.role_weight("CEO") == 0.3
    assert clusters.role_weight("10%") == 0.1
    assert clusters.role_weight("???") == 0.2      # unknown -> Other

    print("all strategy tests passed")


if __name__ == "__main__":
    main()
