"""Alerting (Phase 4): detect new or expanded qualifying clusters after ingest.

State lives in the alert_state table; every fired alert is appended to the
alerts table and to data/alerts.jsonl for external consumption.
"""
import json
import logging
import sqlite3
from datetime import datetime, timezone

from . import clusters, config

log = logging.getLogger("alerts")

ALERTS_JSONL = config.DATA_DIR / "alerts.jsonl"


def check_alerts(conn: sqlite3.Connection,
                 window_days: int = config.DEFAULT_WINDOW_DAYS,
                 min_value: float = config.DEFAULT_MIN_BUY_VALUE,
                 min_cluster: int = config.DEFAULT_MIN_CLUSTER_SIZE) -> list[dict]:
    """Diff current qualifying clusters against alert_state; fire on new tickers
    and on clusters that gained insiders. Returns the fired alerts."""
    cl, _ = clusters.build_screen(conn, window_days, min_value, min_cluster)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    fired: list[dict] = []

    state = {r[0]: {"n_insiders": r[1], "total_value": r[2]}
             for r in conn.execute("SELECT ticker, n_insiders, total_value FROM alert_state")}

    for row in cl.itertuples() if not cl.empty else []:
        prev = state.get(row.ticker)
        kind = None
        if prev is None:
            kind = "new"
        elif row.n_insiders > prev["n_insiders"]:
            kind = "expanded"
        if kind:
            alert = {
                "ts": now, "ticker": row.ticker, "kind": kind,
                "n_insiders": int(row.n_insiders), "total_value": float(row.total_value),
                "message": (f"{row.ticker} ({row.company}): {kind} cluster — "
                            f"{row.n_insiders} insiders, ${row.total_value:,.0f} total"
                            + (f" (was {prev['n_insiders']})" if prev else "")),
            }
            fired.append(alert)
        with conn:
            conn.execute(
                """INSERT INTO alert_state (ticker, n_insiders, total_value, first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(ticker) DO UPDATE SET
                       n_insiders = excluded.n_insiders,
                       total_value = excluded.total_value,
                       last_seen = excluded.last_seen""",
                (row.ticker, int(row.n_insiders), float(row.total_value), now, now))

    if fired:
        with conn:
            conn.executemany(
                """INSERT INTO alerts (ts, ticker, kind, n_insiders, total_value, message)
                   VALUES (:ts, :ticker, :kind, :n_insiders, :total_value, :message)""",
                fired)
        ALERTS_JSONL.parent.mkdir(parents=True, exist_ok=True)
        with ALERTS_JSONL.open("a", encoding="utf-8") as fh:
            for a in fired:
                fh.write(json.dumps(a) + "\n")
        for a in fired:
            log.info("ALERT %s", a["message"])
    else:
        log.info("no new or expanded clusters")
    return fired


def recent_alerts(conn: sqlite3.Connection, limit: int = 20) -> list[tuple]:
    return conn.execute(
        "SELECT ts, ticker, kind, message FROM alerts ORDER BY id DESC LIMIT ?",
        (limit,)).fetchall()
