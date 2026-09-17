"""Process analytics over completed journals — scoped so future trades cannot leak.

Scopes
------
``session_before_current`` (default while a session is active)
    Trades COMPLETED in the session whose position is before the current trade AND whose exit
    happened at or before the current trade's entry. Earlier trades still open at the current
    entry are excluded (their outcome was not known at that moment) and counted as excluded.
``session_up_to``      Trades completed in the session up to a chosen chronological position.
``session_completed``  All trades completed in the session.
``all_completed``      Latest completed review of each trade across all sessions.
``full_history``       Every imported trade (outcomes only; unreviewed trades have no journal).
                       Requires explicit acknowledgement because it can reveal unreviewed trades.

Language: results are reported as observations ("trades tagged X had …"), never causation.
"""
from __future__ import annotations

import json
import re
import statistics
from collections import Counter, defaultdict

from . import market, outcome
from .db import get_settings
from .review import _current_index, _sequence, _statuses

SCOPES = ["session_before_current", "session_up_to", "session_completed", "all_completed", "full_history"]

THEMES = {
    "EMA": [r"\bema\b", r"moving average", r"\b15 ?ema\b"],
    "Pivot levels": [r"\bpivot", r"\b[rs][123]\b", r"\bcpr\b"],
    "Breakout / breakdown": [r"break ?out", r"break ?down", r"broke (above|below)", r"breaking"],
    "Reclaim / rejection": [r"reclaim", r"reject", r"wick"],
    "Support / resistance": [r"support", r"resistance", r"\blevel\b"],
    "Momentum / big candle": [r"momentum", r"strong candle", r"big candle", r"marubozu", r"strong move"],
    "Previous day levels": [r"\bpdh\b", r"\bpdl\b", r"previous day", r"yesterday", r"prior day"],
    "Reversal / exhaustion": [r"reversal", r"revers", r"exhaust", r"double (top|bottom)"],
    "Trend": [r"\btrend", r"higher high", r"lower low", r"higher low", r"lower high"],
    "Confirmation": [r"confirm"],
    "Gap / open": [r"\bgap", r"opening", r"\bopen(ed)?\b"],
    "Chasing / missed move": [r"missed", r"chas", r"running away", r"left behind", r"catch"],
}

DECLARED_RULE_TYPES = {
    "require_stop": "Every trade has a defined stop",
    "require_predefined_setup": "Only trade predefined setups",
    "max_trades_per_day": "Maximum trades per day",
    "no_trade_after_consecutive_losses": "Stop trading for the day after N consecutive losses",
    "min_expected_rr": "Minimum expected reward:risk",
    "min_setup_quality": "Minimum setup quality score",
    "avoid_emotions": "Do not trade when experiencing these states",
    "only_setups": "Only trade these setups",
    "require_confirmation": "Only enter after the confirming 15-minute candle has closed",
}

# Spec §36 psychology dashboard: fixed items and how each is identified.
# basis: "stated" (pre-trade answers), "flagged" (post-outcome reflection), "objective" (computed from data)
PSYCH_ITEMS = [
    ("FOMO", "stated pre-trade state FOMO, influence 'Fear of missing the move', or reflection flags FOMO / Fear of missing out",
     lambda r: "FOMO" in _emo(r) or "Fear of missing the move" in _infl(r) or {"FOMO", "Fear of missing out"} & _flags(r)),
    ("Revenge", "pre-trade 'Revenge feeling' / 'Desire to recover losses', or flags Revenge trading / Trying to recover previous loss",
     lambda r: {"Revenge feeling", "Desire to recover losses"} & _emo(r) or {"Revenge trading", "Trying to recover previous loss"} & _flags(r)),
    ("Overconfidence", "pre-trade state Overconfident", lambda r: "Overconfident" in _emo(r)),
    ("Underconfidence", "pre-trade state Underconfident", lambda r: "Underconfident" in _emo(r)),
    ("Boredom", "pre-trade state Bored or influence Boredom", lambda r: "Bored" in _emo(r) or "Boredom" in _infl(r)),
    ("Impatience", "pre-trade state Impatient", lambda r: "Impatient" in _emo(r)),
    ("Rule violation", "rules followed answered Partially or Not at all", lambda r: r["thesis"].get("rules_followed") in ("Partially", "Not at all")),
    ("Premature exit", "reflection flag Premature exit", lambda r: "Premature exit" in _flags(r)),
    ("Holding loser", "reflection flag Holding loser, or objectively held past the stated stop/invalidation",
     lambda r: "Holding loser" in _flags(r) or (r["outcome"].get("held_beyond_stop_min") or 0) > 5 or bool(r["outcome"].get("invalidation_violated"))),
    ("Moving stop", "reflection flag Moving stop", lambda r: "Moving stop" in _flags(r)),
    ("Oversizing", "reflection flag Increasing size", lambda r: "Increasing size" in _flags(r)),
    ("Trading without setup", "reflection flag Trading without setup, or predefined setup answered No",
     lambda r: "Trading without setup" in _flags(r) or r["thesis"].get("predefined_setup") == "No"),
]


def _emo(r):
    return set(r["thesis"].get("emotions") or [])


def _flags(r):
    return set(r["post"].get("psychology_flags") or [])


def _infl(r):
    return set(r["thesis"].get("outside_influences") or [])


def psychology_dashboard(recs) -> dict:
    j = [r for r in recs if r["journaled"]]
    items = []
    for name, how, test in PSYCH_ITEMS:
        g = [r for r in j if test(r)]
        st = _stats(g, len(j))
        items.append({"item": name, "identified_by": how, **st,
                      "wording": f"Trades identified as '{name}' had {st['wins']} wins and {st['losses']} losses across {len(g)} trades."})
    return {"items": items, "journaled_trades": len(j),
            "note": "Occurrences and associated outcomes only. Co-occurrence is not causation."}


def structure_signature(ctx: dict) -> str | None:
    """A coarse description of the chart at entry, built from pre-entry facts only."""
    if not ctx or ctx.get("above_ema") is None or not ctx.get("trend_last_hour") or not ctx.get("nearest_pivot"):
        return None
    return (f"{'above' if ctx['above_ema'] else 'below'} EMA · {ctx['trend_last_hour']} last hour · "
            f"{ctx.get('pivot_side', 'near')} {ctx['nearest_pivot']}")


def _reliability(n: int) -> str:
    if n < 5:
        return "Needs more trades before it is reliable"
    if n < 15:
        return "Emerging — limited sample"
    return "Established within this scope"


# ----------------------------------------------------------------------------- dataset

def _with_day(conn, rec: dict) -> dict:
    from .review import day_context
    day = rec["ctx"].get("day") if rec.get("ctx") else None
    if not day:
        t = conn.execute("SELECT id, entry_epoch, entry_local FROM trades WHERE id=?", (rec["trade_id"],)).fetchone()
        day = day_context(conn, dict(t))
    rec["day"] = day
    return rec


def _records_for_reviews(conn, rows) -> list[dict]:
    out = []
    for r in rows:
        th = json.loads(r["thesis_json"]) if r["thesis_json"] else {}
        po = json.loads(r["post_json"]) if r["post_json"] else {}
        oc = json.loads(r["outcome_json"]) if r["outcome_json"] else {}
        ctx = json.loads(r["context_json"]) if r["context_json"] else {}
        out.append({"review_id": r["review_id"], "trade_id": r["trade_id"], "seq": r["chrono_seq"],
                    "entry_epoch": r["entry_epoch"], "exit_epoch": r["exit_epoch"], "entry_local": r["entry_local"],
                    "date": (r["entry_local"] or "")[:10], "thesis": th, "post": po, "outcome": oc, "ctx": ctx,
                    "result": oc.get("result"), "points": oc.get("gross_points"), "pnl": oc.get("pnl"),
                    "mfe": oc.get("mfe"), "mae": oc.get("mae"), "journaled": True})
    return [_with_day(conn, r) for r in out]


_REVIEW_SQL = """SELECT r.id AS review_id, r.session_id, r.completed_at, t.id AS trade_id, t.chrono_seq, t.entry_epoch, t.exit_epoch,
                        t.entry_local, th.payload_json AS thesis_json, po.payload_json AS post_json, po.outcome_json,
                        r.entry_context_json AS context_json
                 FROM reviews r JOIN trades t ON t.id=r.trade_id
                 JOIN thesis_snapshots th ON th.review_id=r.id JOIN post_outcome po ON po.review_id=r.id
                 WHERE r.status='COMPLETED' AND t.chrono_seq IS NOT NULL"""


def dataset(conn, *, session_id: int | None, scope: str, upto: int | None = None,
            acknowledge_full_history: bool = False) -> tuple[list[dict], dict]:
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}")
    info = {"scope": scope, "excluded_open_at_current_entry": 0, "warnings": []}
    if scope.startswith("session"):
        if session_id is None:
            raise ValueError("session_id is required for session scopes")
        rows = conn.execute(_REVIEW_SQL + " AND r.session_id=? ORDER BY t.chrono_seq", (session_id,)).fetchall()
        recs = _records_for_reviews(conn, rows)
        seq = _sequence(conn)
        idx = _current_index(seq, _statuses(conn, session_id))
        if scope == "session_before_current":
            if idx is None:
                info["label"] = "All trades completed in this session (session finished)"
            else:
                cur = seq[idx]
                before = [r for r in recs if r["seq"] < cur["chrono_seq"]]
                recs = [r for r in before if r["exit_epoch"] is None or r["exit_epoch"] <= cur["entry_epoch"]]
                info["excluded_open_at_current_entry"] = len(before) - len(recs)
                info["label"] = (f"Chronologically completed trades before the current trade (position {idx + 1}, "
                                 f"{cur['entry_local']}). Unreviewed future trades are excluded.")
                info["current_position"] = idx + 1
        elif scope == "session_up_to":
            if upto is None:
                raise ValueError("upto (chronological position) is required")
            recs = [r for r in recs if r["seq"] <= upto]
            info["label"] = f"Trades completed in this session up to chronological position {upto}"
        else:
            info["label"] = "All trades completed in this session"
            if idx is not None:
                info["warnings"].append("Includes every completed trade in this session, which is the same set as before-current unless trades were reinstated.")
    elif scope == "all_completed":
        rows = conn.execute(_REVIEW_SQL + " ORDER BY t.chrono_seq, r.completed_at").fetchall()
        latest = {}
        for r in rows:
            latest[r["trade_id"]] = r
        recs = _records_for_reviews(conn, sorted(latest.values(), key=lambda r: r["chrono_seq"]))
        info["label"] = "Completed trades across all review sessions (latest review of each trade)"
        active = conn.execute("SELECT COUNT(*) FROM review_sessions WHERE status='ACTIVE'").fetchone()[0]
        if active:
            info["warnings"].append("An active chronological session exists. Cross-session analytics may include trades that session has not reached yet.")
    else:
        if not acknowledge_full_history:
            raise PermissionError("Full-history analytics include trades you have not reviewed yet. Acknowledge this explicitly to continue.")
        rows = conn.execute(_REVIEW_SQL + " ORDER BY t.chrono_seq, r.completed_at").fetchall()
        latest = {r["trade_id"]: r for r in rows}
        recs = _records_for_reviews(conn, latest.values())
        have = {r["trade_id"] for r in recs}
        for t in conn.execute("SELECT * FROM trades WHERE chrono_seq IS NOT NULL ORDER BY chrono_seq").fetchall():
            if t["id"] in have:
                continue
            t = dict(t)
            ch = market.build_chart(conn, t, cutoff_epoch=t["entry_epoch"])
            ctx = {"reference_price": ch["entry"]["reference_price"], "price_basis": ch["entry"]["price_basis"]}
            oc = outcome.compute(conn, t, {}, ctx)
            recs.append({"review_id": None, "trade_id": t["id"], "seq": t["chrono_seq"], "entry_epoch": t["entry_epoch"],
                         "exit_epoch": t["exit_epoch"], "entry_local": t["entry_local"], "date": (t["entry_local"] or "")[:10],
                         "thesis": {}, "post": {}, "outcome": oc, "ctx": ctx, "result": oc.get("result"),
                         "points": oc.get("gross_points"), "pnl": oc.get("pnl"), "mfe": oc.get("mfe"), "mae": oc.get("mae"),
                         "journaled": False})
            _with_day(conn, recs[-1])
        recs.sort(key=lambda r: r["seq"])
        info["label"] = "FULL IMPORTED DATASET — includes unreviewed trades (outcomes only, no journals)"
        info["warnings"].append("This view includes outcomes of trades you have not reviewed. It is excluded from chronological-review feedback.")
    info["trades"] = len(recs)
    info["journaled_trades"] = sum(1 for r in recs if r["journaled"])
    info["positions"] = [r["seq"] for r in recs]
    return recs, info


# ----------------------------------------------------------------------------- helpers

def _stats(group: list[dict], total: int) -> dict:
    n = len(group)
    wins = sum(1 for r in group if r["result"] == "WIN")
    losses = sum(1 for r in group if r["result"] == "LOSS")
    pts = [r["points"] for r in group if r["points"] is not None]
    mfe = [r["mfe"] for r in group if r["mfe"] is not None]
    mae = [r["mae"] for r in group if r["mae"] is not None]
    rules = [r["thesis"].get("rules_followed") for r in group if r["thesis"].get("rules_followed")]
    dir_rows = [_verdict(r, "Direction") for r in group]
    dir_eval = [v for v in dir_rows if v in ("Correct", "Incorrect", "Flat")]
    return {"n": n, "pct_of_trades": round(100 * n / total, 1) if total else None, "wins": wins, "losses": losses,
            "win_rate": round(100 * wins / (wins + losses), 1) if wins + losses else None,
            "avg_points": round(statistics.mean(pts), 2) if pts else None,
            "total_pnl": round(sum(r["pnl"] for r in group if r["pnl"] is not None), 2) if any(r["pnl"] is not None for r in group) else None,
            "avg_mfe": round(statistics.mean(mfe), 2) if mfe else None, "avg_mae": round(statistics.mean(mae), 2) if mae else None,
            "rule_compliance_rate": round(100 * sum(1 for x in rules if x in ("Completely", "Mostly")) / len(rules), 1) if rules else None,
            "direction_correct_rate": round(100 * sum(1 for v in dir_eval if v == "Correct") / len(dir_eval), 1) if dir_eval else None,
            "positions": [r["seq"] for r in group], "reliability": _reliability(n)}


def _verdict(r, item):
    for row in r["outcome"].get("thesis_vs_reality", []):
        if row["item"] == item:
            return row["verdict"]
    return None


def _bucket(v):
    if v is None:
        return None
    v = float(v)
    for lo, hi in ((0, 20), (21, 40), (41, 60), (61, 80), (81, 100)):
        if v <= hi:
            return f"{lo}–{hi}"
    return "81–100"


def _themes(text: str) -> list[str]:
    t = (text or "").lower()
    return [name for name, pats in THEMES.items() if any(re.search(p, t) for p in pats)]


# ----------------------------------------------------------------------------- sections

def overview(recs):
    return _stats(recs, len(recs))


def setup_analysis(recs):
    groups = defaultdict(list)
    for r in recs:
        for s in r["thesis"].get("setups", []) or []:
            groups[s].append(r)
    out = []
    for s, g in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        st = _stats(g, len(recs))
        exp = [r["outcome"].get("expected_move_points") for r in g if r["outcome"].get("expected_move_points") is not None]
        st.update({"setup": s, "avg_expected_move": round(statistics.mean(exp), 1) if exp else None,
                   "avg_confidence": round(statistics.mean([r["thesis"]["confidence"] for r in g if r["thesis"].get("confidence") is not None]), 1)
                   if any(r["thesis"].get("confidence") is not None for r in g) else None,
                   "avg_setup_quality": round(statistics.mean([r["thesis"]["setup_quality"] for r in g if r["thesis"].get("setup_quality") is not None]), 1)
                   if any(r["thesis"].get("setup_quality") is not None for r in g) else None})
        out.append(st)
    return out


def calibration(recs):
    conf, qual = defaultdict(list), defaultdict(list)
    for r in recs:
        if (b := _bucket(r["thesis"].get("confidence"))):
            conf[b].append(r)
        if (b := _bucket(r["thesis"].get("setup_quality"))):
            qual[b].append(r)
    order = ["0–20", "21–40", "41–60", "61–80", "81–100"]
    expected = []
    for r in recs:
        e = r["outcome"].get("expected_move_points")
        if e is None:
            continue
        fav = None
        for row in r["outcome"].get("thesis_vs_reality", []):
            if row["item"] == "Expected move":
                m = re.search(r"([+-]?\d+(?:\.\d+)?) pts", row["actual"])
                fav = float(m.group(1)) if m else None
        expected.append({"position": r["seq"], "expected": round(e, 1), "actual_max_favourable": fav, "mfe_in_trade": r["mfe"],
                         "achieved": _verdict(r, "Expected move") == "Achieved"})
    ratios = [x["actual_max_favourable"] / x["expected"] for x in expected if x["expected"] and x["actual_max_favourable"] is not None]
    timing = Counter(v for r in recs if (v := _verdict(r, "Expected time")) and not v.startswith("Not evaluable"))
    slow = timing.get("Slower than expected", 0) + timing.get("Not achieved within window", 0)
    fast = timing.get("Faster than expected", 0)
    ok = timing.get("Achieved in time", 0)
    tot = slow + fast + ok
    if tot < 3:
        timing_summary = "Not enough evaluable trades"
    elif slow / tot >= 0.5:
        timing_summary = "Expectations tend to be too fast (the market took longer than expected, or did not get there)"
    elif fast / tot >= 0.5:
        timing_summary = "Expectations tend to be too slow (moves arrived sooner than expected)"
    else:
        timing_summary = "Reasonably calibrated in this sample"
    inv_with_level = [r for r in recs if r["thesis"].get("invalidation_price") is not None]
    inv_hit = [r for r in inv_with_level if r["outcome"].get("invalidation_hit")]
    inv_viol = [r for r in inv_with_level if r["outcome"].get("invalidation_violated")]
    journaled = [r for r in recs if r["journaled"]]
    return {
        "confidence_buckets": [{"bucket": b, **_stats(conf[b], len(recs))} for b in order],
        "setup_quality_buckets": [{"bucket": b, **_stats(qual[b], len(recs))} for b in order],
        "expected_vs_actual": {"trades": expected, "achieved_rate": round(100 * sum(x["achieved"] for x in expected) / len(expected), 1) if expected else None,
                               "median_actual_to_expected_ratio": round(statistics.median(ratios), 2) if ratios else None},
        "timing": {"counts": dict(timing), "summary": timing_summary},
        "invalidation": {"journaled_trades": len(journaled),
                         "with_price_level": len(inv_with_level),
                         "pct_measurable": round(100 * len(inv_with_level) / len(journaled), 1) if journaled else None,
                         "traded_through_level": len(inv_hit), "held_after_invalidation": len(inv_viol),
                         "positions_held_after_invalidation": [r["seq"] for r in inv_viol],
                         "note": "Invalidations without a price level cannot be checked objectively; a high share may indicate theses that were not precisely defined before entry."},
    }


def psychology(recs):
    total = len(recs)
    pre, post = defaultdict(list), defaultdict(list)
    for r in recs:
        for e in r["thesis"].get("emotions", []) or []:
            pre[e].append(r)
        for f in r["post"].get("psychology_flags", []) or []:
            post[f].append(r)
    conditions = {}
    ordered = sorted(recs, key=lambda r: r["seq"])
    after_loss, after_win, after_loss_day, after_win_day = [], [], [], []
    for prev, cur in zip(ordered, ordered[1:]):
        if prev["exit_epoch"] and cur["entry_epoch"] and prev["exit_epoch"] > cur["entry_epoch"]:
            continue  # previous result unknown at this entry
        if prev["result"] == "LOSS":
            after_loss.append(cur)
            if prev["date"] == cur["date"]:
                after_loss_day.append(cur)
        elif prev["result"] == "WIN":
            after_win.append(cur)
            if prev["date"] == cur["date"]:
                after_win_day.append(cur)
    conditions["After previous loss"] = after_loss
    conditions["After previous win"] = after_win
    conditions["After a loss earlier the same day"] = after_loss_day
    conditions["After a win earlier the same day"] = after_win_day
    conditions["Confidence ≥ 70"] = [r for r in recs if (r["thesis"].get("confidence") or -1) >= 70]
    conditions["Confidence ≤ 40"] = [r for r in recs if r["thesis"].get("confidence") is not None and r["thesis"]["confidence"] <= 40]
    conditions["Rules followed (completely/mostly)"] = [r for r in recs if r["thesis"].get("rules_followed") in ("Completely", "Mostly")]
    conditions["Rules not followed (partially/not at all)"] = [r for r in recs if r["thesis"].get("rules_followed") in ("Partially", "Not at all")]
    conditions["Felt they HAD to take the trade"] = [r for r in recs if r["thesis"].get("had_to_take") == "Yes"]
    conditions["Not a predefined setup"] = [r for r in recs if r["thesis"].get("predefined_setup") in ("No", "I don't know")]
    conditions["Held to expiry (settled, no closing fill)"] = [r for r in recs if (r["outcome"].get("exit") or {}).get("kind") == "expiry_settlement"]
    conditions["Multi-entry (re-added after a partial exit)"] = [r for r in recs if r["outcome"].get("multi_entry")]
    day = lambda r: r.get("day") or {}
    conditions["Day P&L red before entry"] = [r for r in recs if (day(r).get("realised_day_pnl") or 0) < 0]
    conditions["Day P&L green before entry"] = [r for r in recs if (day(r).get("realised_day_pnl") or 0) > 0]
    conditions["First trade of the day"] = [r for r in recs if day(r).get("first_trade_of_day")]
    conditions["After 2+ losses in a row today"] = [r for r in recs if (day(r).get("consecutive_losses") or 0) >= 2]
    conditions["Another position still open at entry"] = [r for r in recs if (day(r).get("open_at_entry") or 0) > 0]
    for opt in ("Yes — trying to recover a loss", "Yes — trying to protect gains", "Yes — trying to reach a daily target"):
        conditions[f"Said day P&L was on mind: {opt[6:]}"] = [r for r in recs if r["thesis"].get("day_pnl_influence") == opt]
    return {
        "pre_trade_states": [{"tag": k, **_stats(v, total), "wording": f"Trades tagged '{k}' had {sum(1 for r in v if r['result']=='WIN')} wins and {sum(1 for r in v if r['result']=='LOSS')} losses across {len(v)} trades."}
                             for k, v in sorted(pre.items(), key=lambda kv: -len(kv[1]))],
        "post_trade_flags": [{"tag": k, **_stats(v, total), "wording": f"Trades flagged '{k}' in reflection had {sum(1 for r in v if r['result']=='WIN')} wins and {sum(1 for r in v if r['result']=='LOSS')} losses across {len(v)} trades."}
                             for k, v in sorted(post.items(), key=lambda kv: -len(kv[1]))],
        "conditions": [{"condition": k, **_stats(v, total)} for k, v in conditions.items()],
        "note": "These are observed associations within the selected scope. They do not show that a state caused an outcome.",
    }


def why_analysis(recs):
    by_setup, by_theme = defaultdict(list), defaultdict(list)
    for r in recs:
        for s in r["thesis"].get("setups", []) or []:
            by_setup[s].append(r)
        text = " ".join(filter(None, [r["thesis"].get("primary_reason"), r["thesis"].get("one_sentence"), r["thesis"].get("chart_observation")]))
        for th in _themes(text):
            by_theme[th].append(r)

    def items(g):
        return [{"position": r["seq"], "entry_local": r["entry_local"], "one_sentence": r["thesis"].get("one_sentence"),
                 "primary_reason": r["thesis"].get("primary_reason"), "result": r["result"]} for r in g]

    return {
        "declared_setups": [{"group": k, "count": len(v), **{x: _stats(v, len(recs))[x] for x in ("win_rate", "avg_points", "reliability")}, "trades": items(v)}
                            for k, v in sorted(by_setup.items(), key=lambda kv: -len(kv[1]))],
        "stated_reason_themes": [{"group": k, "count": len(v), **{x: _stats(v, len(recs))[x] for x in ("win_rate", "avg_points", "reliability")}, "trades": items(v)}
                                 for k, v in sorted(by_theme.items(), key=lambda kv: -len(kv[1]))],
        "note": "Declared setups are what you selected; stated-reason themes are keyword groupings of what you actually wrote. Differences between the two show the logic you actually use versus the strategy you believe you use.",
    }


def contradictions(conn, recs):
    s = get_settings(conn)
    pct = float(s["context_threshold_pct"]) / 100
    found = []

    def add(title, detail, group, basis):
        if group:
            found.append({"label": "Potential process inconsistency detected", "title": title, "detail": detail,
                          "evidence_positions": [r["seq"] for r in group], "count": len(group),
                          "reliability": _reliability(len(group)), "basis": basis})

    j = [r for r in recs if r["journaled"]]
    emo_flags = {"FOMO", "Revenge feeling", "Desire to recover losses"}
    g = [r for r in j if r["thesis"].get("rules_followed") in ("Completely", "Mostly")
         and (emo_flags & set(r["thesis"].get("emotions") or []) or r["thesis"].get("had_to_take") == "Yes")]
    add("Rules reported as followed while FOMO/revenge/recovery states or compulsion were present",
        f"{len(g)} of {len(j)} trades reported rules followed completely/mostly while also recording FOMO, revenge, a desire to recover losses, or feeling you HAD to take the trade.", g, "USER'S STATED BELIEF vs USER'S STATED BELIEF")

    g = [r for r in j if r["thesis"].get("confidence") is not None and r["thesis"].get("setup_quality") is not None
         and r["thesis"]["confidence"] - r["thesis"]["setup_quality"] >= 30]
    add("Emotional confidence well above rule-based setup quality",
        f"{len(g)} trades had confidence at least 30 points higher than the setup-quality score.", g, "USER'S STATED BELIEF")

    g = [r for r in j if r["thesis"].get("predefined_setup") in ("No",) and r["thesis"].get("rules_followed") == "Completely"]
    add("Not a predefined setup, but rules reported as completely followed", f"{len(g)} trades.", g, "USER'S STATED BELIEF")

    g = [r for r in j if r["outcome"].get("market_bias") and r["thesis"].get("expected_direction") in ("Up", "Down")
         and {"Up": "UP", "Down": "DOWN"}[r["thesis"]["expected_direction"]] != r["outcome"]["market_bias"]]
    add("Expected market direction opposite to the position's direction",
        f"{len(g)} trades expected the market to move opposite to the way the position profits.", g, "FACT vs USER'S STATED BELIEF")

    def tagged(tag):
        return [r for r in j if tag in (r["thesis"].get("setups") or []) and r["ctx"].get("reference_price")]

    g_all = tagged("EMA interaction")
    g = [r for r in g_all if r["ctx"].get("dist_to_ema") is not None and abs(r["ctx"]["dist_to_ema"]) > pct * r["ctx"]["reference_price"]]
    add("'EMA interaction' entries away from the EMA",
        f"{len(g)} of {len(g_all)} trades tagged 'EMA interaction' were entered more than {s['context_threshold_pct']}% of price away from the {s['ema_length']} EMA (last completed candle).", g, "FACT vs USER'S STATED BELIEF")

    g_all = tagged("Pivot interaction")
    g = [r for r in g_all if r["ctx"].get("dist_to_pivot") is not None and abs(r["ctx"]["dist_to_pivot"]) > pct * r["ctx"]["reference_price"]]
    add("'Pivot interaction' entries away from any pivot level",
        f"{len(g)} of {len(g_all)} trades tagged 'Pivot interaction' were entered more than {s['context_threshold_pct']}% of price from the nearest pivot.", g, "FACT vs USER'S STATED BELIEF")

    g_all = tagged("Previous day high/low")
    g = [r for r in g_all if r["ctx"].get("dist_to_pdh") is not None
         and min(abs(r["ctx"]["dist_to_pdh"]), abs(r["ctx"]["dist_to_pdl"])) > pct * r["ctx"]["reference_price"]]
    add("'Previous day high/low' entries away from those levels",
        f"{len(g)} of {len(g_all)} trades tagged 'Previous day high/low' were more than {s['context_threshold_pct']}% of price from both levels.", g, "FACT vs USER'S STATED BELIEF")

    g_all = [r for r in tagged("Trend continuation") if r["outcome"].get("market_bias") and r["ctx"].get("above_ema") is not None]
    g = [r for r in g_all if (r["outcome"]["market_bias"] == "UP") != r["ctx"]["above_ema"]]
    add("'Trend continuation' entries on the other side of the EMA",
        f"{len(g)} of {len(g_all)} trend-continuation trades were bullish below the EMA or bearish above it at entry.", g, "FACT vs USER'S STATED BELIEF")

    g = [r for r in j if r["post"].get("hindsight_assessment") == "Mostly hindsight"]
    add("Reasoning later judged to be mostly hindsight", f"{len(g)} trades.", g, "USER'S STATED BELIEF")

    wants_conf = [r for r in j if "Confirmation" in _themes(" ".join(filter(None, [r["thesis"].get("primary_reason"),
                  r["thesis"].get("one_sentence"), r["thesis"].get("chart_observation")])))
                  or r["thesis"].get("confirmation_before_entry") in ("Yes — it had completed", "No — I entered before it completed")]
    g = [r for r in wants_conf if r["ctx"].get("entered_mid_candle") or r["thesis"].get("confirmation_before_entry") == "No — I entered before it completed"]
    add("Entered before the stated confirmation completed",
        f"{len(g)} of {len(wants_conf)} trades that relied on confirmation were entered while the 15-minute candle was still forming, "
        "or you said the confirmation had not completed yet.", g, "FACT vs USER'S STATED BELIEF")

    # Declared rules
    rules = s.get("declared_rules") or []
    for rule in rules:
        t, v = rule.get("type"), rule.get("value")
        name = f"Declared rule: {DECLARED_RULE_TYPES.get(t, t)}" + (f" ({v})" if v not in (None, [], "") else "")
        if t == "require_stop":
            g = [r for r in j if all(r["thesis"].get(k) is None for k in ("stop_price", "stop_points", "stop_pct"))]
            add(name, f"{len(g)} trades had no stop recorded before entry.", g, "USER'S STATED BELIEF")
        elif t == "require_predefined_setup":
            g = [r for r in j if r["thesis"].get("predefined_setup") in ("No", "I don't know")]
            add(name, f"{len(g)} trades were not identified as a predefined setup.", g, "USER'S STATED BELIEF")
        elif t == "max_trades_per_day" and v:
            per = defaultdict(list)
            for r in recs:
                per[r["date"]].append(r)
            g = [r for d, rs in per.items() if len(rs) > int(v) for r in sorted(rs, key=lambda x: x["seq"])[int(v):]]
            add(name, f"{len(g)} trades were beyond the daily limit.", g, "FACT")
        elif t == "no_trade_after_consecutive_losses" and v:
            g, per = [], defaultdict(list)
            for r in sorted(recs, key=lambda x: x["seq"]):
                per[r["date"]].append(r)
            for rs in per.values():
                streak = 0
                for prev, cur in zip(rs, rs[1:]):
                    streak = streak + 1 if prev["result"] == "LOSS" else 0
                    if streak >= int(v) and not (prev["exit_epoch"] and prev["exit_epoch"] > cur["entry_epoch"]):
                        g.append(cur)
            add(name, f"{len(g)} trades followed {v}+ consecutive losses the same day.", g, "FACT")
        elif t == "min_expected_rr" and v:
            g = [r for r in j if r["thesis"].get("expected_rr") is not None and r["thesis"]["expected_rr"] < float(v)]
            add(name, f"{len(g)} trades had expected R:R below {v}.", g, "USER'S STATED BELIEF")
        elif t == "min_setup_quality" and v:
            g = [r for r in j if r["thesis"].get("setup_quality") is not None and r["thesis"]["setup_quality"] < float(v)]
            add(name, f"{len(g)} trades had setup quality below {v}.", g, "USER'S STATED BELIEF")
        elif t == "avoid_emotions" and v:
            g = [r for r in j if set(v) & set(r["thesis"].get("emotions") or [])]
            add(name, f"{len(g)} trades were taken while recording one of: {', '.join(v)}.", g, "USER'S STATED BELIEF")
        elif t == "require_confirmation":
            g = [r for r in j if r["ctx"].get("entered_mid_candle") or r["thesis"].get("confirmation_before_entry") == "No — I entered before it completed"]
            add(name, f"{len(g)} of {len(j)} trades were entered while a 15-minute candle was still forming, or before the stated confirmation completed.", g, "FACT")
        elif t == "only_setups" and v:
            g = [r for r in j if r["thesis"].get("setups") and not set(r["thesis"]["setups"]) & set(v)]
            add(name, f"{len(g)} trades used none of the allowed setups.", g, "USER'S STATED BELIEF")
    return found


def patterns(recs, scope_label: str):
    out = []
    j = [r for r in recs if r["journaled"]]

    def add(kind, title, detail, group, interpretation=None):
        if len(group) >= 2:
            out.append({"kind": kind, "title": title, "detail": detail, "evidence_positions": [r["seq"] for r in group],
                        "count": len(group), "reliability": _reliability(len(group)), "scope": scope_label,
                        "label": "OBSERVED PATTERN", "interpretation": interpretation,
                        "interpretation_label": "POSSIBLE INTERPRETATION" if interpretation else None})

    emo = defaultdict(list)
    for r in j:
        for e in r["thesis"].get("emotions") or []:
            if e not in ("None of these", "Calm"):
                emo[e].append(r)
    for e, g in sorted(emo.items(), key=lambda kv: -len(kv[1])):
        if len(g) >= 3:
            add("emotional_state", f"Recurring pre-trade state: {e}", f"Recorded before {len(g)} of {len(j)} trades.", g)

    ordered = sorted(recs, key=lambda r: r["seq"])
    gaps_loss, gaps_win, after_loss = [], [], []
    for prev, cur in zip(ordered, ordered[1:]):
        if prev["date"] != cur["date"] or not prev["exit_epoch"] or prev["exit_epoch"] > cur["entry_epoch"]:
            continue
        gap = (cur["entry_epoch"] - prev["exit_epoch"]) / 60
        if prev["result"] == "LOSS":
            gaps_loss.append(gap)
            after_loss.append(cur)
        elif prev["result"] == "WIN":
            gaps_win.append(gap)
    if len(gaps_loss) >= 2 and len(gaps_win) >= 2:
        ml, mw = statistics.median(gaps_loss), statistics.median(gaps_win)
        if ml < mw * 0.6:
            add("reaction_after_loss", "Faster re-entry after losses",
                f"Median time to the next same-day trade was {ml:.0f} min after a loss vs {mw:.0f} min after a win.", after_loss,
                "Re-entries after losses may be quicker than after wins; worth checking whether these follow your setup criteria.")
    if after_loss:
        emo_after = [r for r in after_loss if {"Frustrated", "Revenge feeling", "Desire to recover losses", "Impatient"} & set(r["thesis"].get("emotions") or [])]
        add("reaction_after_loss", "Frustration / recovery states after losses",
            f"{len(emo_after)} of {len(after_loss)} same-day trades following a loss recorded frustration, revenge, impatience or a desire to recover.", emo_after)

    after_win = [cur for prev, cur in zip(ordered, ordered[1:])
                 if prev["date"] == cur["date"] and prev["exit_epoch"] and prev["exit_epoch"] <= cur["entry_epoch"] and prev["result"] == "WIN"]
    if len(gaps_loss) >= 2 and len(gaps_win) >= 2:
        ml, mw = statistics.median(gaps_loss), statistics.median(gaps_win)
        if mw < ml * 0.6:
            add("reaction_after_win", "Faster re-entry after wins",
                f"Median time to the next same-day trade was {mw:.0f} min after a win vs {ml:.0f} min after a loss.", after_win,
                "Quick re-entries after wins may reflect confidence carrying over rather than a fresh setup.")
    if after_win:
        buoyant = [r for r in after_win if {"Confident", "Overconfident", "Excited"} & _emo(r)]
        base = sum(1 for r in j if {"Confident", "Overconfident", "Excited"} & _emo(r)) / len(j) if j else 0
        add("reaction_after_win", "Confident / excited states after wins",
            f"{len(buoyant)} of {len(after_win)} same-day trades following a win recorded confidence, overconfidence or excitement "
            f"(across all journaled trades: {100 * base:.0f}%).", buoyant)
        loss_after_win = [r for r in after_win if r["result"] == "LOSS"]
        add("reaction_after_win", "Losses on the trade after a win",
            f"{len(loss_after_win)} of {len(after_win)} same-day trades following a win were losses.", loss_after_win)

    late = [r for r in j if r["ctx"].get("move_last_hour") is not None and r["ctx"].get("reference_price")
            and r["outcome"].get("market_bias")
            and (r["ctx"]["move_last_hour"] if r["outcome"]["market_bias"] == "UP" else -r["ctx"]["move_last_hour"])
                >= 0.004 * r["ctx"]["reference_price"]
            and r["mfe"] is not None and r["mae"] is not None and abs(r["mae"]) > r["mfe"]]
    add("entry_timing", "Possible late entries",
        f"{len(late)} trades were entered after the underlying had already moved at least 0.4% in the trade's direction over "
        "the preceding hour, and then moved further against the position than in its favour.", late,
        "May indicate chasing a move that had already happened.")

    by_struct = defaultdict(list)
    for r in j:
        sig = structure_signature(r["ctx"])
        if sig:
            by_struct[(sig, r["outcome"].get("market_bias"))].append(r)
    for (sig, bias), g in sorted(by_struct.items(), key=lambda kv: -len(kv[1])):
        if len(g) >= 3:
            wins = sum(1 for r in g if r["result"] == "WIN")
            add("chart_structure", f"Similar chart structure at entry: {sig} ({'bullish' if bias == 'UP' else 'bearish'} trades)",
                f"{len(g)} trades were entered in this structure ({wins} wins, {sum(1 for r in g if r['result'] == 'LOSS')} losses).", g)

    adverse_first = [r for r in j if r["outcome"].get("adverse_first") and r["mae"] is not None and r["mfe"] is not None and abs(r["mae"]) > r["mfe"]]
    add("entry_timing", "Adverse move first, larger than favourable move",
        f"{len(adverse_first)} trades moved against the position first and further than they ever moved in favour.", adverse_first,
        "May indicate early entries (before the trigger fully developed).")
    slow = [r for r in j if _verdict(r, "Expected time") in ("Slower than expected", "Not achieved within window")]
    add("entry_timing", "Expected move slower than expected or not reached",
        f"{len(slow)} trades did not reach the expected move within the expected time.", slow)

    premature = [r for r in j if "Premature exit" in (r["post"].get("psychology_flags") or [])
                 or (r["outcome"].get("capture_ratio") is not None and 0 <= r["outcome"]["capture_ratio"] < 0.3 and _verdict(r, "Direction") == "Correct")]
    add("exit_behaviour", "Exits capturing little of the favourable move",
        f"{len(premature)} trades were flagged as premature exits or captured < 30% of the maximum favourable move while the direction call was correct.", premature)
    holding = [r for r in j if (r["outcome"].get("held_beyond_stop_min") or 0) > 5 or r["outcome"].get("invalidation_violated")
               or "Holding loser" in (r["post"].get("psychology_flags") or [])]
    add("exit_behaviour", "Positions held beyond stated stop or invalidation",
        f"{len(holding)} trades stayed open after the stated stop/invalidation was reached, or were flagged as holding a loser.", holding)
    stops = [r for r in j if {"Moving stop", "Profit target interference"} & set(r["post"].get("psychology_flags") or [])]
    add("exit_behaviour", "Stop movement or target changes", f"{len(stops)} trades were flagged with stop movement or target interference.", stops)

    themes = defaultdict(list)
    for r in j:
        for th in _themes(" ".join(filter(None, [r["thesis"].get("primary_reason"), r["thesis"].get("one_sentence")]))):
            themes[th].append(r)
    for th, g in sorted(themes.items(), key=lambda kv: -len(kv[1])):
        if len(g) >= 3:
            wins = sum(1 for r in g if r["result"] == "WIN")
            add("entry_reason", f"Repeated stated reason: {th}", f"Appears in {len(g)} trades' stated reasons ({wins} wins).", g)

    per_day = defaultdict(list)
    for r in recs:
        per_day[r["date"]].append(r)
    busy = [r for d, g in per_day.items() if len(g) >= 4 for r in g]
    add("frequency", "Days with 4 or more trades", f"{len({r['date'] for r in busy})} days had 4+ trades.", busy)

    hind = [r for r in j if r["post"].get("hindsight_assessment") in ("Mostly hindsight", "I cannot tell")]
    add("hindsight", "Reasoning not clearly visible at entry", f"{len(hind)} trades were later judged mostly hindsight or unclear.", hind)
    return out


def analytics(conn, *, session_id=None, scope="session_before_current", upto=None, acknowledge_full_history=False) -> dict:
    recs, info = dataset(conn, session_id=session_id, scope=scope, upto=upto, acknowledge_full_history=acknowledge_full_history)
    scope_label = ("Patterns observed so far in the chronological review" if scope.startswith("session")
                   else "Patterns based on the complete historical dataset" if scope == "full_history"
                   else "Patterns across completed reviews in all sessions")
    return {"scope": info, "overview": overview(recs), "setups": setup_analysis(recs), "calibration": calibration(recs),
            "psychology": psychology(recs), "why": why_analysis(recs), "contradictions": contradictions(conn, recs),
            "patterns": patterns(recs, scope_label), "psychology_dashboard": psychology_dashboard(recs),
            "evidence_index": {str(r["seq"]): r["review_id"] for r in recs if r["review_id"]},
            "classification_legend": ["FACT", "USER'S STATED BELIEF", "OBSERVED PATTERN", "POSSIBLE INTERPRETATION"]}


def observations_for_trade(conn, sid: int, trade_id: int) -> list[dict]:
    """Rule-extraction candidates shown on completion. Uses only trades up to and including this one."""
    t = conn.execute("SELECT chrono_seq FROM trades WHERE id=?", (trade_id,)).fetchone()
    rows = conn.execute(_REVIEW_SQL + " AND r.session_id=? AND t.chrono_seq <= ? ORDER BY t.chrono_seq", (sid, t["chrono_seq"])).fetchall()
    recs = _records_for_reviews(conn, rows)
    me = next((r for r in recs if r["trade_id"] == trade_id), None)
    if not me:
        return []
    out = []
    keys = [("setup", s) for s in me["thesis"].get("setups") or []] + \
           [("state", e) for e in me["thesis"].get("emotions") or [] if e not in ("None of these",)] + \
           [("theme", th) for th in _themes(" ".join(filter(None, [me["thesis"].get("primary_reason"), me["thesis"].get("one_sentence")])))]
    for kind, key in keys:
        if kind == "setup":
            g = [r for r in recs if key in (r["thesis"].get("setups") or [])]
            what = f"trades tagged with the setup '{key}'"
        elif kind == "state":
            g = [r for r in recs if key in (r["thesis"].get("emotions") or [])]
            what = f"trades where you recorded feeling '{key}'"
        else:
            g = [r for r in recs if key in _themes(" ".join(filter(None, [r["thesis"].get("primary_reason"), r["thesis"].get("one_sentence")])))]
            what = f"trades whose stated reason mentions '{key}'"
        if len(g) < 2:
            continue
        n = len(g)
        facts = []
        wins = sum(1 for r in g if r["result"] == "WIN")
        losses = sum(1 for r in g if r["result"] == "LOSS")
        facts.append(f"{wins} wins, {losses} losses")
        dirs = [v for r in g if (v := _verdict(r, "Direction")) in ("Correct", "Incorrect", "Flat")]
        if dirs:
            facts.append(f"direction call correct in {dirs.count('Correct')} of {len(dirs)}")
        adv = sum(1 for r in g if r["outcome"].get("adverse_first"))
        facts.append(f"moved against the position first in {adv} of {n}")
        held = sum(1 for r in g if r["outcome"].get("invalidation_violated") or (r["outcome"].get("held_beyond_stop_min") or 0) > 5)
        if held:
            facts.append(f"held past stated stop/invalidation in {held}")
        followed = sum(1 for r in g if r["thesis"].get("rules_followed") in ("Completely", "Mostly"))
        facts.append(f"rules reported followed in {followed} of {n}")
        reasons = Counter(th for r in g for th in _themes(r["thesis"].get("primary_reason") or ""))
        issues = []
        if adv / n >= 0.6 and n >= 3:
            issues.append("Entries frequently saw the adverse move first — possibly entered before the trigger was fully established.")
        if held / n >= 0.4 and n >= 3:
            issues.append("Positions were often held past the stated stop or invalidation.")
        if losses / n >= 0.7 and n >= 4:
            issues.append("Most of these trades lost; the stated logic may not be working as intended in this sample.")
        consistent = max(adv, held, losses, wins) / n
        status = ("REPEATED EVIDENCE — CANDIDATE FOR YOUR REVIEW (not a rule until you decide)"
                  if n >= 8 and consistent >= 0.7 else "OBSERVATION — NOT YET A RULE")
        out.append({"status": status, "summary": f"You have now reviewed {n} {what}.", "facts": facts,
                    "common_reasoning": [k for k, _ in reasons.most_common(3)],
                    "potential_recurring_issues": issues, "issues_label": "POSSIBLE INTERPRETATION",
                    "evidence_positions": [r["seq"] for r in g], "evidence_reviews": {str(r["seq"]): r["review_id"] for r in g},
                    "reliability": _reliability(n),
                    "scope": f"Patterns visible in trades already reviewed (positions 1–{t['chrono_seq']} of this session). Future trades were not used."})
    return out


def _num_txt(v) -> str:
    if v is None:
        return "—"
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


def trade_summary(thesis: dict, post: dict, oc: dict, ctx: dict) -> dict:
    """Structured summary of one completed review. Facts are separated from stated beliefs."""
    verdict = lambda item: next((r["verdict"] for r in oc.get("thesis_vs_reality", []) if r["item"] == item), None)
    facts = [f"Result: {oc.get('result') or 'unknown'}"]
    for item in ("Direction", "Expected move", "Expected time", "Invalidation", "Stop"):
        v = verdict(item)
        if v and not v.startswith("Not evaluable") and not v.startswith("Not measurable") and v != "No market data":
            facts.append(f"{item}: {v}")
    if ctx.get("entered_mid_candle") is not None:
        facts.append("Entered while the 15-minute candle was still forming" if ctx["entered_mid_candle"] else "Entered at a 15-minute candle open")
    if oc.get("multi_entry"):
        facts.append(f"Multi-entry: added {oc['re_add_episodes']} time(s) after a partial exit")
    if (oc.get("exit") or {}).get("kind") == "expiry_settlement":
        facts.append("Held to expiry")
    beliefs = [f"Setup: {', '.join(thesis.get('setups') or []) or '—'}",
               f"Confidence {_num_txt(thesis.get('confidence'))}/100 vs setup quality {_num_txt(thesis.get('setup_quality'))}/100",
               f"Pre-trade state: {', '.join(thesis.get('emotions') or []) or '—'}",
               f"Rules followed: {thesis.get('rules_followed') or '—'}",
               f"Hindsight check: {post.get('hindsight_assessment') or '—'}"]
    if post.get("psychology_flags"):
        beliefs.append(f"Psychology flags: {', '.join(post['psychology_flags'])}")
    questions = []
    if verdict("Direction") == "Correct" and oc.get("result") == "LOSS":
        questions.append("The direction call was right but the trade lost — was it entry timing, stop placement or exit?")
    if verdict("Direction") == "Incorrect" and oc.get("result") == "WIN":
        questions.append("The trade won although the market moved against the thesis — what made it profitable?")
    if (thesis.get("confidence") or 0) - (thesis.get("setup_quality") or 0) >= 30:
        questions.append("Confidence was well above your own setup-quality score — where did the extra confidence come from?")
    if post.get("hindsight_assessment") in ("Mostly hindsight", "I cannot tell"):
        questions.append("Which part of the reasoning can you point to on the blind chart?")
    return {"status": "OBSERVATION — NOT YET A RULE (single trade)", "facts": facts, "facts_label": "FACT",
            "stated": beliefs, "stated_label": "USER'S STATED BELIEF", "lesson": post.get("lesson"),
            "questions": questions, "questions_label": "QUESTIONS FOR FURTHER INVESTIGATION"}
