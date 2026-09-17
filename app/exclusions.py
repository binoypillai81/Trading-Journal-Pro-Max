"""Excluding trades from review (e.g. no market data), and restoring them.

An excluded trade:
* leaves the chronological sequence, the queue and analytics (chrono_seq is cleared on resequence);
* is not deleted — its record, fills and any draft stay, and it can be restored;
* still counts toward day P&L context, because at the time the trader knew its result.

Trades whose thesis has been locked (or later) in any session cannot be excluded, so reviewed
history is never removed from the sequence. Every exclusion and restore is logged.
"""
from __future__ import annotations

from . import chronology
from .db import log_event, now_iso, tx

REASONS = ["No market data for this instrument", "Data error in the trade record",
           "Not a real trade (transfer, adjustment, test)", "Other"]
_LOCKED_STATES = ("THESIS_LOCKED", "OUTCOME_REVEALED", "COMPLETED")

_ACTIVE_TRADES = """FROM trades t JOIN import_batches b ON b.id=t.batch_id
                    WHERE b.status='CONFIRMED' AND COALESCE(t.excluded, 0)=0"""
_NO_DATA = """NOT EXISTS (SELECT 1 FROM market_bars m WHERE m.instrument=t.chart_instrument AND m.timeframe='15m'
                          AND m.session_date=substr(t.entry_local, 1, 10))"""


def coverage(conn) -> dict:
    """Active trades without 15-minute chart data on their entry day, grouped by chart instrument."""
    groups = [dict(r) for r in conn.execute(
        f"""SELECT t.chart_instrument AS instrument, COUNT(*) AS trades, MIN(t.entry_local) AS first, MAX(t.entry_local) AS last,
                   SUM(CASE WHEN t.chrono_seq IS NULL THEN 1 ELSE 0 END) AS unresolved
            {_ACTIVE_TRADES} AND t.entry_epoch IS NOT NULL AND {_NO_DATA}
            GROUP BY t.chart_instrument ORDER BY COUNT(*) DESC""")]
    first_positions = [r[0] for r in conn.execute(
        f"SELECT t.chrono_seq {_ACTIVE_TRADES} AND t.chrono_seq IS NOT NULL AND {_NO_DATA} ORDER BY t.chrono_seq LIMIT 5")]
    total_active = conn.execute(f"SELECT COUNT(*) {_ACTIVE_TRADES}").fetchone()[0]
    return {"groups": groups, "trades_without_data": sum(g["trades"] for g in groups),
            "first_positions_without_data": first_positions, "active_trades": total_active}


def _ids_for(conn, *, trade_ids=None, no_market_data=False, instrument=None) -> list[int]:
    if trade_ids:
        marks = ",".join("?" * len(trade_ids))
        return [r[0] for r in conn.execute(f"SELECT t.id {_ACTIVE_TRADES} AND t.id IN ({marks})", list(trade_ids))]
    if no_market_data:
        sql = f"SELECT t.id {_ACTIVE_TRADES} AND t.entry_epoch IS NOT NULL AND {_NO_DATA}"
        args = []
        if instrument:
            sql += " AND t.chart_instrument=?"
            args.append(instrument)
        return [r[0] for r in conn.execute(sql, args)]
    raise ValueError("Give trade_ids, or no_market_data=true (optionally with an instrument)")


def exclude(conn, *, reason: str, note: str | None = None, trade_ids=None, no_market_data=False,
            instrument=None, session_id=None) -> dict:
    if reason not in REASONS:
        raise ValueError(f"reason must be one of {REASONS}")
    if reason == "Other" and not (note or "").strip():
        raise ValueError("Describe the reason when choosing Other")
    with tx(conn):
        ids = _ids_for(conn, trade_ids=trade_ids, no_market_data=no_market_data, instrument=instrument)
        if not ids:
            return {"excluded": 0, "refused_reviewed": 0}
        marks = ",".join("?" * len(ids))
        locked = {r[0] for r in conn.execute(
            f"SELECT DISTINCT trade_id FROM reviews WHERE trade_id IN ({marks}) AND status IN ({','.join('?' * len(_LOCKED_STATES))})",
            [*ids, *_LOCKED_STATES])}
        ok = [i for i in ids if i not in locked]
        if ok:
            full_reason = reason + (f" — {note.strip()}" if (note or "").strip() else "")
            ts = now_iso()
            conn.executemany("UPDATE trades SET excluded=1, exclusion_reason=?, excluded_at=?, updated_at=? WHERE id=?",
                             [(full_reason, ts, ts, i) for i in ok])
            chronology.resequence(conn)
            log_event(conn, "TRADES_EXCLUDED", session_id=session_id, trade_id=ok[0] if len(ok) == 1 else None,
                      count=len(ok), reason=full_reason, trade_ids=ok,
                      selection="trade_ids" if trade_ids else f"no_market_data{':' + instrument if instrument else ''}")
    return {"excluded": len(ok), "refused_reviewed": len(locked)}


def restore(conn, *, trade_ids=None, all_trades=False) -> dict:
    with tx(conn):
        if all_trades:
            ids = [r[0] for r in conn.execute("SELECT id FROM trades WHERE excluded=1")]
        elif trade_ids:
            marks = ",".join("?" * len(trade_ids))
            ids = [r[0] for r in conn.execute(f"SELECT id FROM trades WHERE excluded=1 AND id IN ({marks})", list(trade_ids))]
        else:
            raise ValueError("Give trade_ids or all=true")
        if ids:
            conn.executemany("UPDATE trades SET excluded=0, exclusion_reason=NULL, excluded_at=NULL, updated_at=? WHERE id=?",
                             [(now_iso(), i) for i in ids])
            chronology.resequence(conn)
            log_event(conn, "TRADES_RESTORED", count=len(ids), trade_ids=ids)
    return {"restored": len(ids)}


def excluded_list(conn) -> list[dict]:
    # Entry-side facts only: no exits or P&L, so browsing exclusions can't leak outcomes.
    return [dict(r) for r in conn.execute(
        """SELECT id AS trade_id, instrument, chart_instrument, entry_local, direction, exclusion_reason, excluded_at
           FROM trades WHERE excluded=1 ORDER BY entry_epoch""")]
