"""Outcome metrics and thesis-vs-reality comparison (only ever computed after reveal).

Terminology
-----------
* trade points     – (exit − entry) × (+1 long / −1 short) in the TRADED instrument's prices.
* market bias      – which way the underlying had to move for the position to profit:
                     long/CE/index → UP; long PE → DOWN; short CE → DOWN; short PE → UP.
* reference price  – the underlying (chart) price known at entry (see market.build_chart).
* MFE / MAE        – maximum favourable / adverse excursion of the UNDERLYING from the reference
                     price, in the direction of the market bias, measured while the trade was open.
* thesis window    – from entry to max(exit, entry + upper bound of the expected timeframe), so the
                     thesis is judged on what the market did, not only on when the trader exited.
"""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from . import market
from .db import get_settings
from .questionnaire import TIMEFRAME_MINUTES

FACT, BELIEF, PATTERN, INTERPRETATION = "FACT", "USER'S STATED BELIEF", "OBSERVED PATTERN", "POSSIBLE INTERPRETATION"


def market_bias(direction: str | None, opt: str | None) -> str | None:
    if direction not in ("LONG", "SHORT"):
        return None
    up = direction == "LONG"
    if opt == "PE":
        up = not up
    return "UP" if up else "DOWN"


def _excursions(bars, ref, bias, start, end):
    """Favourable/adverse excursions of bars within [start, end) relative to ref."""
    fav = adv = 0.0
    t_fav = t_adv = None
    for b in bars:
        if b["epoch"] >= end:
            break
        f = (b["high"] - ref) if bias == "UP" else (ref - b["low"])
        a = (b["low"] - ref) if bias == "UP" else (ref - b["high"])
        if f > fav:
            fav, t_fav = f, b["epoch"]
        if a < adv:
            adv, t_adv = a, b["epoch"]
    return fav, adv, t_fav, t_adv


def _first_touch(bars, level, side, start, end):
    """First bar epoch in [start,end) where price reached `level` from `side` ('above' means high>=level)."""
    if level is None:
        return None
    for b in bars:
        if b["epoch"] >= end:
            break
        if (side == "above" and b["high"] >= level) or (side == "below" and b["low"] <= level):
            return b["epoch"]
    return None


def _first_excursion(bars, ref, bias, points, start, end):
    if points is None or bias is None:
        return None
    level = ref + points if bias == "UP" else ref - points
    return _first_touch(bars, level, "above" if bias == "UP" else "below", start, end)


def _local(epoch, tz):
    return datetime.fromtimestamp(epoch, ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M") if epoch else None


def compute(conn, trade: dict, thesis: dict, entry_context: dict) -> dict:
    s = get_settings(conn)
    tz = s["exchange_timezone"]
    E, X = trade["entry_epoch"], trade["exit_epoch"]
    inst = trade["chart_instrument"]
    ref = (entry_context or {}).get("reference_price")
    opt = trade.get("option_type")
    direction = trade.get("direction") or {"Long": "LONG", "Short": "SHORT"}.get(thesis.get("direction"))
    bias = market_bias(direction, opt)
    limitations = []

    out: dict = {"labels": {"computed": FACT, "stated": BELIEF}}
    out["entry"] = {"time": trade.get("entry_local"), "price": trade.get("entry_price"),
                    "first_order_price": trade.get("initial_entry_price"), "first_order_quantity": trade.get("initial_quantity"),
                    "opening_fills": trade.get("entry_fills")}
    out["exit"] = {"time": trade.get("exit_local"), "price": trade.get("exit_price"), "kind": trade.get("exit_kind"),
                   "settlement": json.loads(trade["settlement_json"]) if trade.get("settlement_json") else None}
    out["re_add_episodes"] = trade.get("re_add_episodes") or 0
    out["multi_entry"] = out["re_add_episodes"] > 0
    out["direction"] = direction
    out["market_bias"] = bias
    out["instrument"] = trade.get("instrument")

    pts = None
    if trade.get("entry_price") is not None and trade.get("exit_price") is not None and direction:
        pts = (trade["exit_price"] - trade["entry_price"]) * (1 if direction == "LONG" else -1)
    out["gross_points"] = pts
    out["pct_move"] = (pts / trade["entry_price"] * 100) if pts is not None and trade.get("entry_price") else None
    if trade.get("pnl") is not None:
        out["pnl"], out["pnl_source"] = trade["pnl"], "imported"
    elif pts is not None and trade.get("quantity"):
        out["pnl"], out["pnl_source"] = pts * trade["quantity"], "computed: points × quantity (before costs)"
    else:
        out["pnl"], out["pnl_source"] = None, "unavailable"
    out["result"] = None if pts is None and out["pnl"] is None else (
        "WIN" if (pts if pts is not None else out["pnl"]) > 0 else "LOSS" if (pts if pts is not None else out["pnl"]) < 0 else "FLAT")
    out["duration_minutes"] = round((X - E) / 60, 1) if E and X else None
    if not X:
        limitations.append("No exit timestamp: excursions and duration cannot be computed.")

    # ---- thesis window
    tf = thesis.get("expected_timeframe")
    lo, hi = TIMEFRAME_MINUTES.get(tf, (None, None))
    session_close = market.session_close_epoch(conn, inst, E) if E else None
    if tf == "Rest of session":
        window_end_thesis = session_close
    elif tf == "Multiple sessions":
        window_end_thesis = market.nth_session_close(conn, inst, E, 3)
    elif hi is not None:
        window_end_thesis = E + hi * 60
    else:
        window_end_thesis = None
    thesis_end = max(filter(None, [X, window_end_thesis])) if (X or window_end_thesis) else None
    out["thesis_window_end"] = _local(thesis_end, tz)
    out["reveal_until_epoch"] = thesis_end

    bars, tf_used = ([], None)
    if E and thesis_end and ref is not None:
        bars, tf_used = market.path_bars(conn, inst, E, thesis_end)
        if tf_used == "15m":
            limitations.append("Excursions use 15-minute bars (no 1-minute data): the entry and exit bars include prices "
                               "from before entry / after exit, so MFE/MAE are approximate.")
        elif tf_used == "1m":
            limitations.append("Excursions use 1-minute bars: the entry minute may include prices from seconds before entry.")
        if not bars:
            limitations.append("No market data covers the trade period.")
    elif ref is None:
        limitations.append("No reference price at entry (no market data): excursions unavailable.")
    out["path_timeframe"] = tf_used

    mfe = mae = None
    if bars and bias and X:
        fav, adv, t_fav, t_adv = _excursions(bars, ref, bias, E, X)
        mfe, mae = round(fav, 2), round(adv, 2)
        out["mfe"], out["mae"] = mfe, mae
        out["mfe_time"], out["mae_time"] = _local(t_fav, tz), _local(t_adv, tz)
        out["adverse_first"] = bool(t_adv and (not t_fav or t_adv < t_fav))
        out["max_excursion"] = max(abs(fav), abs(adv))
        ref_exit_bars = [b for b in bars if b["epoch"] < X]
        out["underlying_at_exit"] = ref_exit_bars[-1]["close"] if ref_exit_bars else None
        out["underlying_move"] = None if out["underlying_at_exit"] is None else round(
            (out["underlying_at_exit"] - ref) * (1 if bias == "UP" else -1), 2)
        if pts is not None and mfe and entry_context.get("price_basis") == "chart":
            out["capture_ratio"] = round(pts / mfe, 2) if mfe > 0 else None
    else:
        out.update({"mfe": None, "mae": None, "max_excursion": None, "underlying_move": None})
    out["reference_price"] = ref
    out["price_basis"] = (entry_context or {}).get("price_basis")

    # ---- thesis evaluation (uses the stated expected direction)
    exp_dir = thesis.get("expected_direction")
    t_bias = {"Up": "UP", "Down": "DOWN"}.get(exp_dir)
    rows = []

    def row(item, expectation, actual, verdict, basis=FACT):
        rows.append({"item": item, "expectation": expectation, "actual": actual, "verdict": verdict, "basis": basis})

    thesis_move = None
    if bars and ref is not None and thesis_end:
        closes_at_end = [b for b in bars if b["epoch"] < thesis_end]
        end_px = closes_at_end[-1]["close"] if closes_at_end else None
        net = None if end_px is None else end_px - ref
        tf_fav = tf_adv = None
        if t_bias:
            tf_fav, tf_adv, _, _ = _excursions(bars, ref, t_bias, E, thesis_end)
        if exp_dir in ("Up", "Down") and net is not None:
            signed = net if exp_dir == "Up" else -net
            eps = ref * 0.0002
            verdict = "Correct" if signed > eps else "Incorrect" if signed < -eps else "Flat"
            row("Direction", exp_dir, f"{'+' if net >= 0 else ''}{net:.1f} pts by {_local(thesis_end, tz)}", verdict)
        elif exp_dir == "Sideways" and bars:
            hi_px = max(b["high"] for b in bars if b["epoch"] < thesis_end)
            lo_px = min(b["low"] for b in bars if b["epoch"] < thesis_end)
            rng = hi_px - lo_px
            thr = thesis.get("expected_move_points") or ref * 0.003
            row("Direction", "Sideways", f"range {rng:.1f} pts", "Correct" if rng <= thr * 2 else "Incorrect")
        else:
            row("Direction", exp_dir or "—", "—", "Not evaluable (no directional expectation)")

        exp_pts = thesis.get("expected_move_points")
        if exp_pts is None and thesis.get("expected_move_pct") is not None:
            exp_pts = ref * thesis["expected_move_pct"] / 100
        if exp_pts is None and thesis.get("target_price") is not None:
            exp_pts = abs(thesis["target_price"] - ref)
        thesis_move = exp_pts
        if exp_pts is not None and t_bias:
            t_hit = _first_excursion(bars, ref, t_bias, exp_pts, E, thesis_end)
            row("Expected move", f"{'+' if t_bias == 'UP' else '−'}{exp_pts:.0f} pts",
                f"max favourable {tf_fav:+.1f} pts within thesis window", "Achieved" if t_hit else "Not achieved")
            mins = round((t_hit - E) / 60) if t_hit else None
            if hi is not None or tf == "Rest of session":
                limit_min = hi if hi is not None else round((session_close - E) / 60)
                if t_hit:
                    verdict = "Achieved in time" if mins <= limit_min else "Slower than expected"
                    if lo and mins < lo:
                        verdict = "Faster than expected"
                else:
                    verdict = "Not achieved within window"
                row("Expected time", tf, f"{mins} min" if mins is not None else "move not reached", verdict)
            else:
                row("Expected time", tf or "—", f"{mins} min" if mins is not None else "move not reached", "Not evaluable")
            out["time_to_expected_move_min"] = mins
        else:
            row("Expected move", "—", f"MFE {mfe:+.1f} pts" if mfe is not None else "—", "Not evaluable (no expected move)")
            row("Expected time", tf or "—", "—", "Not evaluable")

        if thesis.get("target_price") is not None and t_bias:
            hit = _first_touch(bars, thesis["target_price"], "above" if t_bias == "UP" else "below", E, thesis_end)
            row("Target", f"{thesis['target_price']:.1f}", _local(hit, tz) or "not reached", "Reached" if hit else "Not reached")
            out["target_reached"] = bool(hit)

        stop = thesis.get("stop_price")
        if stop is None and bias and thesis.get("stop_points") is not None:
            stop = ref - thesis["stop_points"] if bias == "UP" else ref + thesis["stop_points"]
        if stop is None and bias and thesis.get("stop_pct") is not None:
            d = ref * thesis["stop_pct"] / 100
            stop = ref - d if bias == "UP" else ref + d
        if stop is not None and bias and X:
            hit = _first_touch(bars, stop, "below" if bias == "UP" else "above", E, X)
            verdict = "Stop would have been reached" if hit else "Stop not reached"
            if hit and X - hit > 300:
                verdict += f" — position stayed open {round((X - hit) / 60)} min after"
            row("Stop", f"{stop:.1f}", _local(hit, tz) or "not reached while in trade", verdict)
            out["stop_reached"] = bool(hit)
            out["held_beyond_stop_min"] = round((X - hit) / 60) if hit else 0

        inv = thesis.get("invalidation_price")
        if inv is not None and t_bias:
            hit = _first_touch(bars, inv, "below" if t_bias == "UP" else "above", E, thesis_end)
            if not hit:
                verdict, actual = "Never occurred", "not traded through"
            elif X and hit < X and X - hit > 300:
                verdict, actual = "Violated — position held after invalidation", f"{_local(hit, tz)}; exit {_local(X, tz)}"
            elif X and hit < X:
                verdict, actual = "Respected — exited at invalidation", _local(hit, tz)
            else:
                verdict, actual = "Occurred after exit", _local(hit, tz)
            row("Invalidation", f"{thesis.get('invalidation', '')[:60]} (level {inv:.1f})", actual, verdict)
            out["invalidation_hit"] = bool(hit)
            out["invalidation_violated"] = verdict.startswith("Violated")
        else:
            row("Invalidation", (thesis.get("invalidation") or "—")[:80], "—",
                "Not measurable (no price level given)")
    else:
        row("Direction", exp_dir or "—", "—", "No market data")

    row("Maximum favourable move", "—", f"{mfe:+.1f} pts" if mfe is not None else "—", "—")
    row("Maximum adverse move", f"≤ {thesis['max_adverse_points']:.0f} pts" if thesis.get("max_adverse_points") else "—",
        f"{mae:+.1f} pts" if mae is not None else "—",
        ("Within stated tolerance" if abs(mae) <= thesis["max_adverse_points"] else "Beyond stated tolerance")
        if mae is not None and thesis.get("max_adverse_points") else "—")
    rf = thesis.get("rules_followed")
    row("Process followed", "—", rf or "—",
        {"Completely": "Followed (self-reported)", "Mostly": "Mostly followed (self-reported)",
         "Partially": "Partially followed (self-reported)", "Not at all": "Not followed (self-reported)",
         "I had no defined rules": "No defined rules (self-reported)"}.get(rf, "—"), BELIEF)
    if thesis.get("direction") and direction and {"Long": "LONG", "Short": "SHORT"}[thesis["direction"]] != direction:
        limitations.append(f"Your questionnaire direction ({thesis['direction']}) differs from the imported direction ({direction}).")

    out["thesis_vs_reality"] = rows
    out["expected_move_points"] = thesis_move
    out["limitations"] = limitations
    return out
