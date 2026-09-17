"""Chronological review controller.

State machine per (session, trade):

    UNREVIEWED → IN_PROGRESS → THESIS_LOCKED → OUTCOME_REVEALED → COMPLETED
                      └──────────── SKIPPED_WITH_OVERRIDE (administrative override only)

Trades whose timestamps are unresolved are shown as REQUIRES_TIMESTAMP_REVIEW and
are not part of the sequence.

The CURRENT trade of a session is the earliest trade in the chronological
sequence that is neither COMPLETED nor SKIPPED_WITH_OVERRIDE. Every endpoint that
touches a specific trade checks access through ``access()``:

* the current trade                          → blind data only until its thesis is locked and outcome revealed
* trades COMPLETED/SKIPPED in this session   → full data (Completed Review Mode)
* anything later                             → LockedError
"""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from . import chronology, market, outcome, questionnaire
from .db import get_settings, log_event, now_iso, tx

STATES = ["UNREVIEWED", "IN_PROGRESS", "THESIS_LOCKED", "OUTCOME_REVEALED", "COMPLETED",
          "SKIPPED_WITH_OVERRIDE", "REQUIRES_TIMESTAMP_REVIEW"]
DONE = ("COMPLETED", "SKIPPED_WITH_OVERRIDE")

# Columns that are safe to expose before reveal. Exit, P&L and raw rows are never selected in blind queries.
# entry_price/quantity are the FIRST order only: later adds (and the average they produce) happened after entry.
BLIND_COLUMNS = ("id, trade_ref, instrument, chart_instrument, option_type, entry_original, entry_epoch, entry_local, "
                 "entry_tz_source, COALESCE(initial_entry_price, entry_price) AS entry_price, direction, "
                 "COALESCE(initial_quantity, quantity) AS quantity, exec_seq, chrono_seq")
FULL_COLUMNS = "*"
REVEAL_BARS_AFTER = 4  # extra 15-minute bars shown after the thesis window/exit on reveal


class LockedError(Exception):
    """Access to a trade that is later than the current chronological position."""


class StateError(Exception):
    """Action not allowed in the review's current state."""


# ----------------------------------------------------------------------------- sessions

def list_sessions(conn) -> list[dict]:
    out = []
    for s in conn.execute("SELECT * FROM review_sessions ORDER BY last_activity_at DESC"):
        d = dict(s)
        d.update(progress(conn, s["id"]))
        out.append(d)
    return out


def create_session(conn, name: str | None = None) -> dict:
    st = get_settings(conn)
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO review_sessions(name, created_at, last_activity_at, timezone, sort_method) VALUES (?,?,?,?,?)",
            (name or None, now_iso(), now_iso(), st["exchange_timezone"], chronology.SORT_METHOD))
        sid = cur.lastrowid
        log_event(conn, "SESSION_STARTED", session_id=sid, name=name, timezone=st["exchange_timezone"])
    return get_session(conn, sid)


def get_session(conn, sid: int) -> dict:
    s = conn.execute("SELECT * FROM review_sessions WHERE id=?", (sid,)).fetchone()
    if not s:
        raise LookupError("Session not found")
    d = dict(s)
    d.update(progress(conn, sid))
    return d


def touch(conn, sid: int, event: str | None = None) -> None:
    conn.execute("UPDATE review_sessions SET last_activity_at=? WHERE id=?", (now_iso(), sid))
    if event:
        log_event(conn, event, session_id=sid)


def archive_session(conn, sid: int) -> None:
    with tx(conn):
        conn.execute("UPDATE review_sessions SET status='ARCHIVED', last_activity_at=? WHERE id=?", (now_iso(), sid))
        log_event(conn, "SESSION_ARCHIVED", session_id=sid)


# ----------------------------------------------------------------------------- sequence

def _sequence(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        f"SELECT {BLIND_COLUMNS} FROM trades WHERE chrono_seq IS NOT NULL ORDER BY chrono_seq")]


def _statuses(conn, sid: int) -> dict[int, dict]:
    return {r["trade_id"]: dict(r) for r in conn.execute("SELECT * FROM reviews WHERE session_id=?", (sid,))}


def _current_index(seq, statuses) -> int | None:
    for i, t in enumerate(seq):
        if statuses.get(t["id"], {}).get("status") not in DONE:
            return i
    return None


def progress(conn, sid: int) -> dict:
    seq = _sequence(conn)
    st = _statuses(conn, sid)
    idx = _current_index(seq, st)
    completed = sum(1 for t in seq if st.get(t["id"], {}).get("status") == "COMPLETED")
    skipped = sum(1 for t in seq if st.get(t["id"], {}).get("status") == "SKIPPED_WITH_OVERRIDE")
    overrides = conn.execute("SELECT COUNT(*) FROM events WHERE session_id=? AND event_type LIKE 'OVERRIDE%'", (sid,)).fetchone()[0]
    unresolved = conn.execute(
        """SELECT COUNT(*) FROM trades t JOIN import_batches b ON b.id=t.batch_id
           WHERE b.status='CONFIRMED' AND t.chrono_status!='OK' AND COALESCE(t.excluded, 0)=0""").fetchone()[0]
    return {"total": len(seq), "completed": completed, "skipped": skipped, "remaining": len(seq) - completed - skipped,
            "current_position": (idx + 1) if idx is not None else None,
            "current_trade_id": seq[idx]["id"] if idx is not None else None,
            "override_events": overrides, "unresolved_timestamps": unresolved, "finished": idx is None and len(seq) > 0}


def queue(conn, sid: int, window: int | None = None) -> dict:
    """Chronological queue — timestamps and states only, never outcomes.

    ``window`` limits items to that many positions either side of the current trade (large histories)."""
    get_session(conn, sid)
    seq = _sequence(conn)
    st = _statuses(conn, sid)
    idx = _current_index(seq, st)
    items = []
    for i, t in enumerate(seq):
        rs = st.get(t["id"], {}).get("status")
        if rs == "COMPLETED":
            display = "COMPLETED"
        elif rs == "SKIPPED_WITH_OVERRIDE":
            display = "SKIPPED_WITH_OVERRIDE"
        elif i == idx:
            display = "CURRENT"
        else:
            display = "LOCKED"
        items.append({"position": i + 1, "trade_id": t["id"], "entry_local": t["entry_local"],
                      "instrument": t["instrument"], "queue_state": display,
                      "review_state": rs or "UNREVIEWED", "review_id": st.get(t["id"], {}).get("id")})
    total_items = len(items)
    if window is not None and idx is not None:
        items = items[max(0, idx - window): idx + window + 1]
    elif window is not None:
        items = items[-window:]
    unresolved = [dict(r, queue_state="REQUIRES_TIMESTAMP_REVIEW") for r in conn.execute(
        """SELECT t.id AS trade_id, t.instrument, t.entry_original, t.chrono_issue FROM trades t
           JOIN import_batches b ON b.id=t.batch_id WHERE b.status='CONFIRMED' AND t.chrono_status!='OK' AND COALESCE(t.excluded, 0)=0""")]
    return {"session": get_session(conn, sid), "items": items, "total_items": total_items, "unresolved": unresolved,
            "sort_method": chronology.SORT_METHOD}


# ----------------------------------------------------------------------------- access control

def _review_row(conn, rid: int) -> dict:
    r = conn.execute("SELECT * FROM reviews WHERE id=?", (rid,)).fetchone()
    if not r:
        raise LookupError("Review not found")
    return dict(r)


def access(conn, rid: int) -> tuple[dict, dict]:
    """Return (review, blind trade) if the review's trade is current or done in its session; else raise."""
    rv = _review_row(conn, rid)
    seq = _sequence(conn)
    st = _statuses(conn, rv["session_id"])
    idx = _current_index(seq, st)
    pos = next((i for i, t in enumerate(seq) if t["id"] == rv["trade_id"]), None)
    if pos is None:
        raise LockedError("Trade is not in the chronological sequence")
    if rv["status"] not in DONE and pos != idx:
        raise LockedError("This trade is later in the chronological sequence and is locked")
    return rv, seq[pos]


def _full_trade(conn, trade_id: int) -> dict:
    return dict(conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone())


def _entry_context(conn, trade: dict, chart: dict) -> dict:
    ref = chart["entry"]["reference_price"]
    ctx = {"reference_price": ref, "price_basis": chart["entry"]["price_basis"],
           "has_market_data": bool(chart["candles"]), "computed_from": "pre-entry data only"}
    if ref is None:
        return ctx
    if chart["ema"]["points"]:
        e = chart["ema"]["points"][-1]["value"]
        ctx.update({"ema": round(e, 2), "dist_to_ema": round(ref - e, 2), "above_ema": ref > e})
    today = next((p for p in chart["pivots"]["sets"] if p["session_date"] == chart["cutoff_local"][:10]), None)
    if today:
        levels = today["levels"]
        name, lvl = min(levels.items(), key=lambda kv: abs(kv[1] - ref))
        ctx.update({"nearest_pivot": name, "nearest_pivot_level": round(lvl, 2), "dist_to_pivot": round(ref - lvl, 2)})
        pdh, pdl = today["prior_day"]["high"], today["prior_day"]["low"]
        ctx.update({"prev_day_high": pdh, "prev_day_low": pdl, "dist_to_pdh": round(ref - pdh, 2), "dist_to_pdl": round(ref - pdl, 2)})
    if chart["candles"]:
        recent = chart["candles"][-4:]
        ctx["last_4_closes_rising"] = all(b["close"] > a["close"] for a, b in zip(recent, recent[1:]))
        ctx["last_4_closes_falling"] = all(b["close"] < a["close"] for a, b in zip(recent, recent[1:]))
        ctx["trend_last_hour"] = "rising" if ctx["last_4_closes_rising"] else "falling" if ctx["last_4_closes_falling"] else "mixed"
        if len(chart["candles"]) >= 4:
            ctx["move_last_hour"] = round(ref - chart["candles"][-4]["close"], 2)
        if "nearest_pivot" in ctx:
            ctx["pivot_side"] = "above" if ctx["dist_to_pivot"] >= 0 else "below"
    pos = chart["entry"].get("position") or {}
    ctx["seconds_into_candle"] = pos.get("seconds_into_candle")
    ctx["entered_mid_candle"] = pos.get("mid_candle")
    return ctx


# ----------------------------------------------------------------------------- day P&L at entry

# All real trades from confirmed imports, including ones excluded from review or awaiting a decision:
# the trader knew these results at the time, so they belong in the day context.
_REAL_TRADES = "FROM trades t JOIN import_batches b ON b.id=t.batch_id WHERE b.status='CONFIRMED' AND t.entry_epoch IS NOT NULL"


def _trade_pnl(t) -> float | None:
    if t["pnl"] is not None:
        return t["pnl"]
    if None in (t["entry_price"], t["exit_price"], t["quantity"]) or t["direction"] not in ("LONG", "SHORT"):
        return None
    return (t["exit_price"] - t["entry_price"]) * (1 if t["direction"] == "LONG" else -1) * t["quantity"]


def day_context(conn, trade: dict, sid: int | None = None) -> dict:
    """What the trader knew about their day at the moment of this entry.

    Realised day P&L = trades that EXITED on the same exchange-local day at or before this entry.
    Trades still open at the entry, and anything that closed later, are excluded — their result
    was not known yet. P&L is as imported (fills mode: realised, before charges).
    """
    E, day = trade["entry_epoch"], trade["entry_local"][:10]
    closed = conn.execute(
        f"""SELECT t.id, t.pnl, t.entry_price, t.exit_price, t.quantity, t.direction, t.exit_epoch {_REAL_TRADES}
           AND t.id != ? AND t.exit_epoch IS NOT NULL AND t.exit_epoch <= ?
           AND substr(t.exit_local, 1, 10) = ? ORDER BY t.exit_epoch""", (trade["id"], E, day)).fetchall()
    pnls = [(r["id"], _trade_pnl(r)) for r in closed]
    known = [p for _, p in pnls if p is not None]
    streak = 0
    for _, p in reversed(pnls):
        if p is not None and p < 0:
            streak += 1
        else:
            break
    open_at_entry = conn.execute(
        f"""SELECT COUNT(*) {_REAL_TRADES} AND t.id != ? AND t.entry_epoch < ?
             AND ((t.exit_epoch IS NOT NULL AND t.exit_epoch > ?) OR (t.exit_epoch IS NULL AND substr(t.entry_local, 1, 10) = ?))""",
        (trade["id"], E, E, day)).fetchone()[0]
    entered_today = conn.execute(
        f"""SELECT COUNT(*) {_REAL_TRADES} AND t.id != ? AND t.entry_epoch < ?
             AND substr(t.entry_local, 1, 10) = ?""", (trade["id"], E, day)).fetchone()[0]
    skipped = 0
    if sid is not None and closed:
        ids = [r["id"] for r in closed]
        skipped = conn.execute(
            f"SELECT COUNT(*) FROM reviews WHERE session_id=? AND status='SKIPPED_WITH_OVERRIDE' AND trade_id IN ({','.join('?' * len(ids))})",
            (sid, *ids)).fetchone()[0]
    realised = round(sum(known), 2) if known else (0.0 if not closed else None)
    return {
        "session_date": day,
        "realised_day_pnl": realised,
        "closed_before_entry": len(closed),
        "wins": sum(1 for p in known if p > 0), "losses": sum(1 for p in known if p < 0), "flat": sum(1 for p in known if p == 0),
        "pnl_unknown": len(closed) - len(known),
        "consecutive_losses": streak,
        "open_at_entry": open_at_entry,
        "entered_earlier_today": entered_today,
        "first_trade_of_day": entered_today == 0,
        "includes_skipped_trades": skipped,
        "basis": "Trades that closed today at or before this entry; as imported (before charges). Open positions excluded.",
    }


# ----------------------------------------------------------------------------- current trade

def current(conn, sid: int) -> dict:
    """Blind payload for the session's current trade."""
    sess = get_session(conn, sid)
    s = get_settings(conn)
    seq = _sequence(conn)
    st = _statuses(conn, sid)
    idx = _current_index(seq, st)
    base = {"session": sess, "total": len(seq)}
    if idx is None:
        return {**base, "finished": True, "trade": None}
    t = seq[idx]
    rv = st.get(t["id"])
    prev_t = seq[idx - 1] if idx > 0 else None
    next_t = seq[idx + 1] if idx + 1 < len(seq) else None
    payload = {
        **base, "finished": False, "position": idx + 1,
        "completed": sum(1 for x in seq if st.get(x["id"], {}).get("status") == "COMPLETED"),
        "remaining": len(seq) - idx - 1,
        "trade": {k: t[k] for k in ("id", "trade_ref", "instrument", "chart_instrument", "option_type", "entry_local",
                                   "entry_original", "entry_tz_source", "entry_price", "direction", "quantity", "chrono_seq")},
        "previous": {"entry_local": prev_t["entry_local"], "status": st.get(prev_t["id"], {}).get("status")} if prev_t else None,
        "next": ({"entry_local": next_t["entry_local"]} if s["show_next_timestamp"] else {"exists": True, "hidden": True})
        if next_t else None,
        "review": _public_review(conn, rv) if rv else None,
        "hindsight_cautions": [],
        "day_context": day_context(conn, t, sid),
        "market_data_available": conn.execute(
            "SELECT 1 FROM market_bars WHERE instrument=? AND timeframe='15m' AND session_date=? LIMIT 1",
            (t["chart_instrument"], t["entry_local"][:10])).fetchone() is not None,
    }
    # Overlap caution: an earlier trade's reveal may already have shown candles past this entry.
    if prev_t:
        latest_exit = conn.execute(
            """SELECT MAX(t.exit_epoch) FROM reviews r JOIN trades t ON t.id=r.trade_id
               WHERE r.session_id=? AND r.status IN ('COMPLETED','OUTCOME_REVEALED') AND t.chrono_seq < ?""",
            (sid, t["chrono_seq"])).fetchone()[0]
        if latest_exit and latest_exit > t["entry_epoch"]:
            payload["hindsight_cautions"].append(
                "An earlier trade was still open when this trade was entered. Its reveal already showed you market data "
                "after this entry time. Try to answer only from what the chart below shows.")
    if s["process_context_enabled"]:
        payload["process_context"] = process_context(conn, sid, t)
    return payload


def process_context(conn, sid: int, trade: dict, limit: int = 5) -> list[dict]:
    """The user's own earlier completed journals. Outcomes of trades still open at this entry are withheld."""
    rows = conn.execute(
        """SELECT r.id, t.chrono_seq, t.entry_local, t.exit_epoch, th.payload_json, po.outcome_json
           FROM reviews r JOIN trades t ON t.id=r.trade_id
           JOIN thesis_snapshots th ON th.review_id=r.id LEFT JOIN post_outcome po ON po.review_id=r.id
           WHERE r.session_id=? AND r.status='COMPLETED' AND t.chrono_seq < ? ORDER BY t.chrono_seq DESC LIMIT ?""",
        (sid, trade["chrono_seq"], limit)).fetchall()
    out = []
    for r in rows:
        th = json.loads(r["payload_json"])
        item = {"position": r["chrono_seq"], "entry_local": r["entry_local"], "setups": th.get("setups"),
                "one_sentence": th.get("one_sentence"), "emotions": th.get("emotions")}
        if r["exit_epoch"] and r["exit_epoch"] <= trade["entry_epoch"] and r["outcome_json"]:
            oc = json.loads(r["outcome_json"])
            item["result"] = oc.get("result")
        else:
            item["result"] = "(still open at this entry — withheld)"
        out.append(item)
    return out


def _public_review(conn, rv: dict | None) -> dict | None:
    if not rv:
        return None
    d = {k: rv[k] for k in ("id", "session_id", "trade_id", "status", "started_at", "thesis_locked_at",
                            "outcome_revealed_at", "completed_at", "skipped_override", "override_reason", "out_of_sequence")}
    d["draft"] = json.loads(rv["draft_json"] or "{}")
    d["post_draft"] = json.loads(rv["post_draft_json"] or "{}")
    th = conn.execute("SELECT * FROM thesis_snapshots WHERE review_id=?", (rv["id"],)).fetchone()
    if th:
        d["thesis"] = {"locked_at": th["locked_at"], "answers": json.loads(th["payload_json"]),
                       "text": th["snapshot_text"], "sha256": th["sha256"]}
    return d


def start_current(conn, sid: int) -> dict:
    with tx(conn):
        seq = _sequence(conn)
        st = _statuses(conn, sid)
        idx = _current_index(seq, st)
        if idx is None:
            raise StateError("No remaining trades in this session")
        t = seq[idx]
        rv = st.get(t["id"])
        if rv and rv["status"] != "UNREVIEWED":
            touch(conn, sid, "SESSION_RESUMED")
            return _public_review(conn, _review_row(conn, rv["id"]))
        chart = market.build_chart(conn, t, cutoff_epoch=t["entry_epoch"])
        ctx = _entry_context(conn, t, chart)
        ctx["day"] = day_context(conn, t, sid)
        draft = {"direction": {"LONG": "Long", "SHORT": "Short"}.get(t["direction"])} if t["direction"] else {}
        if rv:
            conn.execute("""UPDATE reviews SET status='IN_PROGRESS', started_at=?, entry_context_json=?, draft_json=?, updated_at=?
                            WHERE id=?""", (now_iso(), json.dumps(ctx), json.dumps(draft), now_iso(), rv["id"]))
            rid = rv["id"]
        else:
            cur = conn.execute(
                """INSERT INTO reviews(session_id, trade_id, status, chrono_seq_at_start, started_at, entry_context_json,
                   draft_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                (sid, t["id"], "IN_PROGRESS", t["chrono_seq"], now_iso(), json.dumps(ctx), json.dumps(draft), now_iso(), now_iso()))
            rid = cur.lastrowid
        touch(conn, sid)
        log_event(conn, "REVIEW_STARTED", session_id=sid, trade_id=t["id"], review_id=rid, position=idx + 1)
        return _public_review(conn, _review_row(conn, rid))


def get_review(conn, rid: int) -> dict:
    rv, t = access(conn, rid)
    d = _public_review(conn, rv)
    d["trade"] = t
    d["entry_context"] = json.loads(rv["entry_context_json"] or "{}")
    if rv["status"] in ("OUTCOME_REVEALED", "COMPLETED"):
        full = _full_trade(conn, rv["trade_id"])
        d["trade"] = {k: v for k, v in full.items() if k != "raw_row_json"}
    if rv["status"] == "COMPLETED":
        po = conn.execute("SELECT * FROM post_outcome WHERE review_id=?", (rid,)).fetchone()
        if po:
            d["post"] = {"created_at": po["created_at"], "answers": json.loads(po["payload_json"]),
                         "outcome": json.loads(po["outcome_json"]), "sha256": po["sha256"]}
        d["revisions"] = [dict(r, payload=json.loads(r["payload_json"])) for r in conn.execute(
            "SELECT id, section, payload_json, reason, created_at FROM journal_revisions WHERE review_id=? ORDER BY id", (rid,))]
    return d


def chart(conn, rid: int) -> dict:
    rv, t = access(conn, rid)
    if rv["status"] in ("OUTCOME_REVEALED", "COMPLETED"):
        full = _full_trade(conn, rv["trade_id"])
        thesis = _thesis_answers(conn, rid)
        ctx = json.loads(rv["entry_context_json"] or "{}")
        oc = outcome.compute(conn, full, thesis, ctx)
        until = oc.get("reveal_until_epoch") or full["exit_epoch"] or full["entry_epoch"]
        c = market.build_chart(conn, full, cutoff_epoch=full["entry_epoch"], reveal_until_epoch=until + REVEAL_BARS_AFTER * 900)
        c["exit"] = {"epoch": full["exit_epoch"], "price": full["exit_price"], "underlying_price": oc.get("underlying_at_exit")}
        c["thesis_levels"] = {k: thesis.get(k) for k in ("target_price", "stop_price", "invalidation_price")}
        return c
    if rv["status"] == "SKIPPED_WITH_OVERRIDE":
        # A skipped trade is earlier than the current position; its blind chart is still the safe default.
        pass
    return market.build_chart(conn, t, cutoff_epoch=t["entry_epoch"])


def _thesis_answers(conn, rid: int) -> dict:
    th = conn.execute("SELECT payload_json FROM thesis_snapshots WHERE review_id=?", (rid,)).fetchone()
    return json.loads(th["payload_json"]) if th else {}


# ----------------------------------------------------------------------------- stage transitions

def save_draft(conn, rid: int, answers: dict) -> dict:
    with tx(conn):
        rv, _ = access(conn, rid)
        if rv["status"] != "IN_PROGRESS":
            raise StateError(f"The thesis can only be edited while IN_PROGRESS (current: {rv['status']})")
        conn.execute("UPDATE reviews SET draft_json=?, updated_at=? WHERE id=?", (json.dumps(answers), now_iso(), rid))
        touch(conn, rv["session_id"])
    return {"saved": True}


def validate_thesis(conn, rid: int, answers: dict | None = None) -> dict:
    rv, t = access(conn, rid)
    if answers is None:
        answers = json.loads(rv["draft_json"] or "{}")
    v = questionnaire.validate("thesis", answers)
    seq_total = conn.execute("SELECT COUNT(*) FROM trades WHERE chrono_seq IS NOT NULL").fetchone()[0]
    day = json.loads(rv["entry_context_json"] or "{}").get("day") or day_context(conn, t, rv["session_id"])
    v["snapshot_text"] = questionnaire.snapshot_text(v["answers"], t["chrono_seq"], seq_total, t, day)
    v["can_lock"] = not v["errors"] and (not v["prompts"] or v["answers"]["specificity_acknowledged"])
    return v


def lock_thesis(conn, rid: int, answers: dict, confirm: bool) -> dict:
    if not confirm:
        raise StateError("Confirm that this represents what you believed at the time of entry")
    with tx(conn):
        rv, t = access(conn, rid)
        if rv["status"] != "IN_PROGRESS":
            raise StateError(f"Thesis can only be locked from IN_PROGRESS (current: {rv['status']})")
        conn.execute("UPDATE reviews SET draft_json=? WHERE id=?", (json.dumps(answers), rid))
        v = validate_thesis(conn, rid, answers)
        if v["errors"]:
            raise StateError("Questionnaire incomplete: " + ", ".join(f"{k}: {m}" for k, m in v["errors"].items()))
        if v["prompts"] and not v["answers"]["specificity_acknowledged"]:
            raise StateError("Some answers are vague. Make them more specific or confirm you have been as specific as you honestly can.")
        payload = v["answers"]
        locked_at = now_iso()
        conn.execute("INSERT INTO thesis_snapshots(review_id, locked_at, payload_json, snapshot_text, sha256) VALUES (?,?,?,?,?)",
                     (rid, locked_at, json.dumps(payload), v["snapshot_text"], questionnaire.canonical_hash(payload)))
        conn.execute("UPDATE reviews SET status='THESIS_LOCKED', thesis_locked_at=?, updated_at=? WHERE id=?",
                     (locked_at, now_iso(), rid))
        touch(conn, rv["session_id"])
        log_event(conn, "THESIS_LOCKED", session_id=rv["session_id"], trade_id=rv["trade_id"], review_id=rid,
                  sha256=questionnaire.canonical_hash(payload), vague_fields_acknowledged=list(v["prompts"].keys()))
    return _public_review(conn, _review_row(conn, rid))


def reveal(conn, rid: int) -> dict:
    with tx(conn):
        rv, _ = access(conn, rid)
        if rv["status"] == "OUTCOME_REVEALED":
            return _public_review(conn, rv)
        if rv["status"] != "THESIS_LOCKED":
            raise StateError("The outcome can only be revealed after the thesis is locked")
        conn.execute("UPDATE reviews SET status='OUTCOME_REVEALED', outcome_revealed_at=?, updated_at=? WHERE id=?",
                     (now_iso(), now_iso(), rid))
        touch(conn, rv["session_id"])
        log_event(conn, "OUTCOME_REVEALED", session_id=rv["session_id"], trade_id=rv["trade_id"], review_id=rid)
    return _public_review(conn, _review_row(conn, rid))


def get_outcome(conn, rid: int) -> dict:
    rv, _ = access(conn, rid)
    if rv["status"] not in ("OUTCOME_REVEALED", "COMPLETED"):
        raise StateError("Outcome is hidden until the thesis is locked and the outcome is revealed")
    if rv["status"] == "COMPLETED":
        po = conn.execute("SELECT outcome_json FROM post_outcome WHERE review_id=?", (rid,)).fetchone()
        if po:
            return json.loads(po["outcome_json"])
    return outcome.compute(conn, _full_trade(conn, rv["trade_id"]), _thesis_answers(conn, rid),
                           json.loads(rv["entry_context_json"] or "{}"))


def save_post_draft(conn, rid: int, answers: dict) -> dict:
    with tx(conn):
        rv, _ = access(conn, rid)
        if rv["status"] != "OUTCOME_REVEALED":
            raise StateError("Post-outcome answers can only be drafted after reveal and before completion")
        conn.execute("UPDATE reviews SET post_draft_json=?, updated_at=? WHERE id=?", (json.dumps(answers), now_iso(), rid))
        touch(conn, rv["session_id"])
    return {"saved": True}


def complete(conn, rid: int, answers: dict) -> dict:
    from . import analytics  # local import: analytics depends on review helpers
    with tx(conn):
        rv, t = access(conn, rid)
        if rv["status"] != "OUTCOME_REVEALED":
            raise StateError("A trade can only be completed after its thesis is locked and outcome revealed")
        v = questionnaire.validate("post", answers)
        if v["errors"]:
            raise StateError("Reflection incomplete: " + ", ".join(f"{k}: {m}" for k, m in v["errors"].items()))
        if v["prompts"] and not v["answers"]["specificity_acknowledged"]:
            raise StateError("Some answers are vague: " + " ".join(v["prompts"].values()))
        oc = outcome.compute(conn, _full_trade(conn, rv["trade_id"]), _thesis_answers(conn, rid),
                             json.loads(rv["entry_context_json"] or "{}"))
        conn.execute("INSERT INTO post_outcome(review_id, created_at, payload_json, outcome_json, sha256) VALUES (?,?,?,?,?)",
                     (rid, now_iso(), json.dumps(v["answers"]), json.dumps(oc), questionnaire.canonical_hash(v["answers"])))
        conn.execute("UPDATE reviews SET status='COMPLETED', completed_at=?, post_draft_json=NULL, updated_at=? WHERE id=?",
                     (now_iso(), now_iso(), rid))
        touch(conn, rv["session_id"])
        log_event(conn, "REVIEW_COMPLETED", session_id=rv["session_id"], trade_id=rv["trade_id"], review_id=rid)
    sid = rv["session_id"]
    seq = _sequence(conn)
    st = _statuses(conn, sid)
    idx = _current_index(seq, st)
    nxt = seq[idx] if idx is not None else None
    caution = None
    full = _full_trade(conn, rv["trade_id"])
    if nxt and full["exit_epoch"] and full["exit_epoch"] > nxt["entry_epoch"]:
        caution = ("Your next trade was entered while this trade was still open. The reveal you just saw includes market "
                   "data after the next trade's entry.")
    return {"completed_position": t["chrono_seq"], "total": len(seq), "next": {"entry_local": nxt["entry_local"]} if nxt else None,
            "finished": nxt is None, "hindsight_caution": caution,
            "observations": analytics.observations_for_trade(conn, sid, rv["trade_id"]),
            "trade_summary": analytics.trade_summary(_thesis_answers(conn, rid), v["answers"], oc,
                                                     json.loads(rv["entry_context_json"] or "{}"))}


def skip_with_override(conn, sid: int, reason: str, confirmation: str) -> dict:
    s = get_settings(conn)
    if not s["override_enabled"]:
        raise StateError("The administrative override is disabled. Enable it in Settings first.")
    if confirmation != "SKIP":
        raise StateError("Type SKIP to confirm the chronological-review exception")
    if not reason or len(reason.strip()) < 10:
        raise StateError("Give a reason for the override (at least 10 characters)")
    with tx(conn):
        seq = _sequence(conn)
        st = _statuses(conn, sid)
        idx = _current_index(seq, st)
        if idx is None:
            raise StateError("Nothing to skip")
        t = seq[idx]
        rv = st.get(t["id"])
        if rv and rv["status"] in ("THESIS_LOCKED", "OUTCOME_REVEALED"):
            raise StateError("This trade's thesis is already locked; complete the review instead of skipping it")
        if rv:
            conn.execute("""UPDATE reviews SET status='SKIPPED_WITH_OVERRIDE', skipped_override=1, override_reason=?, updated_at=?
                            WHERE id=?""", (reason.strip(), now_iso(), rv["id"]))
            rid = rv["id"]
        else:
            cur = conn.execute(
                """INSERT INTO reviews(session_id, trade_id, status, chrono_seq_at_start, skipped_override, override_reason, created_at, updated_at)
                   VALUES (?,?,?,?,1,?,?,?)""", (sid, t["id"], "SKIPPED_WITH_OVERRIDE", t["chrono_seq"], reason.strip(), now_iso(), now_iso()))
            rid = cur.lastrowid
        touch(conn, sid)
        log_event(conn, "OVERRIDE_SKIP", session_id=sid, trade_id=t["id"], review_id=rid, reason=reason.strip(),
                  user_action="skip current trade", position=idx + 1, entry_local=t["entry_local"],
                  previous_state=rv["status"] if rv else "UNREVIEWED")
    return {"skipped_trade_id": t["id"], "position": idx + 1}


def revise(conn, rid: int, section: str, answers: dict, reason: str | None) -> dict:
    if section not in ("post_outcome", "thesis_annotation"):
        raise ValueError("section must be post_outcome or thesis_annotation")
    with tx(conn):
        rv, _ = access(conn, rid)
        if rv["status"] != "COMPLETED":
            raise StateError("Only completed journals can be revised")
        if section == "post_outcome":
            v = questionnaire.validate("post", answers)
            if v["errors"]:
                raise StateError("Revision incomplete: " + ", ".join(v["errors"]))
            payload = v["answers"]
        else:
            payload = {"note": (answers.get("note") or "").strip()}
            if not payload["note"]:
                raise StateError("Annotation text is required")
        conn.execute("INSERT INTO journal_revisions(review_id, section, payload_json, reason, created_at) VALUES (?,?,?,?,?)",
                     (rid, section, json.dumps(payload), reason, now_iso()))
        log_event(conn, "JOURNAL_REVISED", session_id=rv["session_id"], trade_id=rv["trade_id"], review_id=rid, section=section)
    return get_review(conn, rid)


def completed_list(conn, sid: int) -> list[dict]:
    get_session(conn, sid)
    rows = conn.execute(
        """SELECT r.id AS review_id, r.status, r.completed_at, r.override_reason, t.id AS trade_id, t.chrono_seq,
                  t.entry_local, t.instrument, t.re_add_episodes, t.exit_kind, th.payload_json, po.outcome_json, po.payload_json AS post_json
           FROM reviews r JOIN trades t ON t.id=r.trade_id
           LEFT JOIN thesis_snapshots th ON th.review_id=r.id LEFT JOIN post_outcome po ON po.review_id=r.id
           WHERE r.session_id=? AND r.status IN ('COMPLETED','SKIPPED_WITH_OVERRIDE') ORDER BY t.chrono_seq""", (sid,)).fetchall()
    out = []
    for r in rows:
        th = json.loads(r["payload_json"]) if r["payload_json"] else {}
        oc = json.loads(r["outcome_json"]) if r["outcome_json"] else {}
        po = json.loads(r["post_json"]) if r["post_json"] else {}
        out.append({"review_id": r["review_id"], "trade_id": r["trade_id"], "position": r["chrono_seq"],
                    "entry_local": r["entry_local"], "instrument": r["instrument"], "status": r["status"],
                    "setups": th.get("setups", []), "emotions": th.get("emotions", []), "confidence": th.get("confidence"),
                    "result": oc.get("result"), "gross_points": oc.get("gross_points"), "pnl": oc.get("pnl"),
                    "psychology_flags": po.get("psychology_flags", []), "override_reason": r["override_reason"],
                    "re_add_episodes": r["re_add_episodes"] or 0, "exit_kind": r["exit_kind"]})
    return out


def events(conn, sid: int) -> list[dict]:
    return [dict(r, detail=json.loads(r["detail_json"] or "{}")) for r in conn.execute(
        "SELECT id, at, trade_id, review_id, event_type, detail_json FROM events WHERE session_id=? ORDER BY id", (sid,))]


def local_now(conn) -> str:
    return datetime.now(ZoneInfo(get_settings(conn)["exchange_timezone"])).strftime("%Y-%m-%d %H:%M:%S")
