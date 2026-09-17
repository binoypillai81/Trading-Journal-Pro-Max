"""Chronological ordering of trades.

The sequence is determined ONLY by entry time, never by exit time, P&L,
import order or trade ID (except as tie-breakers):

1. Normalized entry instant (UTC epoch seconds — equivalent to exchange-local
   order because every value was converted to a single absolute instant)
2. Execution sequence column, if mapped (trades without one sort after those with one)
3. Trade ID (natural order: numeric IDs compared as numbers, before text IDs)
4. Import batch (earlier import first)
5. Original CSV row number

Instrument is deliberately NOT a primary sort key: sorting by instrument first
would review all NIFTY trades before all BANKNIFTY trades and break the
chronology of decisions. Trades on different instruments at the same instant
fall through to the tie-breakers above.

Trades whose entry timestamp is unresolved, and trades the user has excluded from review
(e.g. no market data), receive no sequence number.
"""
from __future__ import annotations

import re

SORT_METHOD = (
    "entry_instant(UTC-normalized) ASC → execution_sequence ASC (missing last) → "
    "trade_id natural ASC → import_batch ASC → import_row ASC; unresolved timestamps excluded"
)


def _natural(ref):
    if ref is None or str(ref).strip() == "":
        return (2, 0, "")
    s = str(ref).strip()
    if re.fullmatch(r"-?\d+", s):
        return (0, int(s), "")
    return (1, 0, s.lower())


def sort_key(t) -> tuple:
    return (
        t["entry_epoch"],
        t["exec_seq"] is None,
        t["exec_seq"] if t["exec_seq"] is not None else 0,
        _natural(t["trade_ref"]),
        t["batch_id"],
        t["import_row"],
    )


def ordered(trades) -> list:
    return sorted(trades, key=sort_key)


def duplicate_epochs(trades) -> set[int]:
    seen, dup = set(), set()
    for t in trades:
        e = t["entry_epoch"]
        if e is None:
            continue
        (dup if e in seen else seen).add(e)
    return dup


def resequence(conn) -> None:
    """Assign chrono_seq (1..N) to all CONFIRMED trades with resolved timestamps."""
    rows = conn.execute(
        """SELECT t.id, t.entry_epoch, t.exec_seq, t.trade_ref, t.batch_id, t.import_row
           FROM trades t JOIN import_batches b ON b.id = t.batch_id
           WHERE b.status = 'CONFIRMED' AND t.chrono_status = 'OK' AND t.entry_epoch IS NOT NULL
             AND COALESCE(t.excluded, 0) = 0"""
    ).fetchall()
    conn.execute(
        """UPDATE trades SET chrono_seq = NULL
           WHERE chrono_status != 'OK' OR COALESCE(excluded, 0) = 1
              OR batch_id NOT IN (SELECT id FROM import_batches WHERE status='CONFIRMED')"""
    )
    for i, r in enumerate(ordered([dict(r) for r in rows]), start=1):
        conn.execute("UPDATE trades SET chrono_seq = ? WHERE id = ?", (i, r["id"]))
