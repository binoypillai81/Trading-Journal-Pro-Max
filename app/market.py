"""Market data import and temporally-cut chart construction.

Bar convention
--------------
Bars are stored labelled by their OPEN time: the 10:30 15-minute bar covers
[10:30, 10:45). If a source file labels bars by close time, import it with
``label="close"`` and it is shifted back by one bar.

Hard temporal cutoff (Blind Mode)
---------------------------------
For an entry at instant E, the blind chart is built from SQL queries that can
only return:

* COMPLETED 15-minute bars:  ``epoch + 900 <= E``
* the FORMING bar (the one containing E), reconstructed from
    - 1-minute bars that had fully closed before E: ``epoch + 60 <= E`` (preferred), or
    - only the ``open`` column of the 15-minute bar (its high/low/close are never
      selected, because they were not known at E),
  plus the trade's own entry price when that price is on the chart's scale.

Nothing after E is selected, so nothing after E can reach the frontend. EMA and
pivots are computed from those same pre-cutoff rows only.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .db import get_settings, log_event, now_iso, tx

TF_SECONDS = {"15m": 900, "1m": 60, "1d": 86400}  # 1d is used only for official closing values (expiry settlement), never for charts

PIVOT_DOC = {
    "classic": "P=(H+L+C)/3; R1=2P−L; S1=2P−H; R2=P+(H−L); S2=P−(H−L); R3=H+2(P−L); S3=L−2(H−P)",
    "fibonacci": "P=(H+L+C)/3; R1=P+0.382(H−L); R2=P+0.618(H−L); R3=P+(H−L); S1..S3 mirrored",
    "camarilla": "R1=C+1.1(H−L)/12; R2=C+1.1(H−L)/6; R3=C+1.1(H−L)/4; S1..S3 mirrored; P=(H+L+C)/3",
}
PIVOT_SOURCE_NOTE = ("H, L, C are the high, low and last close of the most recent COMPLETED prior session "
                     "in the imported 15-minute data. The current session's data is never used.")


# ----------------------------------------------------------------------------- import

def _read_any(path_or_buffer):
    return pd.read_csv(path_or_buffer, dtype=str, keep_default_na=False, sep=None, engine="python")


def market_upload_preview(content: bytes) -> dict:
    import io
    df = _read_any(io.BytesIO(content))
    headers = [c.strip() for c in df.columns]
    lower = {h.lower(): h for h in headers}
    guess = {}
    for field, names in {
        "timestamp": ["timestamp", "datetime", "date_time", "time_stamp", "date"],
        "time": ["time"],
        "open": ["open", "o"], "high": ["high", "h"], "low": ["low", "l"], "close": ["close", "c", "ltp"],
        "volume": ["volume", "vol", "v"],
    }.items():
        for n in names:
            if n in lower:
                guess[field] = lower[n]
                break
    if guess.get("timestamp") == guess.get("time"):
        guess.pop("time", None)
    return {"headers": headers, "sample_rows": df.head(5).to_dict("records"), "suggested_mapping": guess,
            "row_count": len(df)}


def import_market(conn, source, *, mapping: dict, instrument: str, timeframe: str = "auto",
                  naive_timezone: str | None = None, label: str = "open", filename: str | None = None) -> dict:
    """Import OHLC bars. ``source`` is a path or a file-like object."""
    if label not in ("open", "close"):
        raise ValueError("label must be 'open' or 'close'")
    instrument = (instrument or "").strip().upper()
    if not instrument:
        raise ValueError("Instrument is required (e.g. NIFTY)")
    df = _read_any(source)
    df.columns = [c.strip() for c in df.columns]
    for f in ("timestamp", "open", "high", "low", "close"):
        if not mapping.get(f):
            raise ValueError(f"Map the '{f}' column")
        if mapping[f] not in df.columns:
            raise ValueError(f"Column '{mapping[f]}' not in file")
    settings = get_settings(conn)
    exch = ZoneInfo(settings["exchange_timezone"])

    ts_text = df[mapping["timestamp"]].astype(str)
    if mapping.get("time"):
        ts_text = ts_text + " " + df[mapping["time"]].astype(str)
    has_tz = ts_text.str.contains(r"(?:Z|[+-]\d{2}:?\d{2})\s*$", regex=True)
    if has_tz.any() and not has_tz.all():
        raise ValueError("Market file mixes timestamps with and without timezone offsets. Fix the file so they are consistent.")
    aware = bool(has_tz.all()) and len(ts_text) > 0
    try:
        parsed = pd.to_datetime(ts_text, errors="raise", format="ISO8601", utc=aware)
    except (ValueError, TypeError):
        parsed = pd.to_datetime(ts_text, errors="coerce", format="mixed", utc=aware)
    if not aware:
        if not naive_timezone:
            raise ValueError("Market timestamps have no timezone. Confirm the timezone they were recorded in.")
        parsed = parsed.dt.tz_localize(naive_timezone, ambiguous="NaT", nonexistent="NaT")
        tz_source = f"assumed:{naive_timezone}"
    else:
        tz_source = "explicit offsets"
    parsed = parsed.dt.tz_convert(exch)

    out = pd.DataFrame({
        "ts": parsed,
        "open": pd.to_numeric(df[mapping["open"]].str.replace(",", ""), errors="coerce"),
        "high": pd.to_numeric(df[mapping["high"]].str.replace(",", ""), errors="coerce"),
        "low": pd.to_numeric(df[mapping["low"]].str.replace(",", ""), errors="coerce"),
        "close": pd.to_numeric(df[mapping["close"]].str.replace(",", ""), errors="coerce"),
        "volume": pd.to_numeric(df[mapping["volume"]].str.replace(",", ""), errors="coerce") if mapping.get("volume") else None,
    })
    total = len(out)
    bad_ts = int(out["ts"].isna().sum())
    out = out.dropna(subset=["ts", "open", "high", "low", "close"])
    bad_values = total - bad_ts - len(out)
    invalid_ohlc = (out["high"] < out[["open", "close"]].max(axis=1) - 1e-9) | (out["low"] > out[["open", "close"]].min(axis=1) + 1e-9)
    n_invalid = int(invalid_ohlc.sum())
    out = out[~invalid_ohlc].sort_values("ts")

    if timeframe == "auto":
        diffs = out["ts"].diff().dt.total_seconds().dropna()
        med = diffs[diffs > 0].median() if len(diffs) else None
        if med == 60:
            timeframe = "1m"
        elif med == 900:
            timeframe = "15m"
        elif med == 86400:
            timeframe = "1d"
        else:
            raise ValueError(f"Could not detect timeframe (median spacing {med}s). Choose 15m, 1m or 1d.")
    if timeframe not in TF_SECONDS:
        raise ValueError("Supported timeframes: 15m, 1m, 1d")
    step = TF_SECONDS[timeframe]
    if label == "close":
        out["ts"] = out["ts"] - pd.Timedelta(seconds=step)
    minutes = out["ts"].dt.hour * 60 + out["ts"].dt.minute
    misaligned = int(((minutes % (step // 60)) != 0).sum() + (out["ts"].dt.second != 0).sum())
    dupes = int(out["ts"].duplicated(keep="last").sum())
    out = out.drop_duplicates("ts", keep="last")

    warnings = []
    if timeframe == "15m" and len(out):
        first_bar = out.groupby(out["ts"].dt.date)["ts"].min().dt.strftime("%H:%M").mode()
        if len(first_bar) and first_bar.iloc[0] == "09:30" and label == "open":
            warnings.append("Most sessions start at 09:30 — the file may label bars by CLOSE time. "
                            "If so, re-import with bar label = close, otherwise the cutoff is shifted by one bar.")
    if misaligned:
        warnings.append(f"{misaligned} bars are not aligned to {timeframe} boundaries")

    # unit-independent (pandas 3 may store datetimes as s/ms/us rather than ns)
    epochs = ((out["ts"] - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(seconds=1)).astype(int)
    rows = list(zip(
        [instrument] * len(out), [timeframe] * len(out), epochs.tolist(),
        out["ts"].dt.strftime("%Y-%m-%d %H:%M:%S").tolist(), out["ts"].dt.strftime("%Y-%m-%d").tolist(),
        out["open"].tolist(), out["high"].tolist(), out["low"].tolist(), out["close"].tolist(),
        (out["volume"].tolist() if mapping.get("volume") else [None] * len(out)),
    ))
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO import_batches(kind, filename, created_at, status, mapping_json, options_json, confirmed_at) VALUES ('market',?,?,?,?,?,?)",
            (filename, now_iso(), "CONFIRMED", json.dumps(mapping),
             json.dumps({"instrument": instrument, "timeframe": timeframe, "naive_timezone": naive_timezone, "label": label}),
             now_iso()))
        bid = cur.lastrowid
        conn.executemany(
            """INSERT OR REPLACE INTO market_bars(instrument, timeframe, epoch, local, session_date, open, high, low, close, volume, batch_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,""" + str(bid) + ")", rows)
        summary = {"instrument": instrument, "timeframe": timeframe, "rows_in_file": total, "imported": len(rows),
                   "rejected_bad_timestamp": bad_ts, "rejected_bad_values": bad_values, "rejected_invalid_ohlc": n_invalid,
                   "duplicates_collapsed": dupes, "misaligned": misaligned, "timezone_source": tz_source,
                   "bar_label": label, "warnings": warnings,
                   "first": out["ts"].min().strftime("%Y-%m-%d %H:%M") if len(out) else None,
                   "last": out["ts"].max().strftime("%Y-%m-%d %H:%M") if len(out) else None}
        conn.execute("UPDATE import_batches SET summary_json=? WHERE id=?", (json.dumps(summary), bid))
        log_event(conn, "MARKET_DATA_IMPORTED", **summary)
    return summary


def coverage(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT instrument, timeframe, COUNT(*) AS bars, COUNT(DISTINCT session_date) AS sessions,
                  MIN(local) AS first, MAX(local) AS last
           FROM market_bars GROUP BY instrument, timeframe ORDER BY instrument, timeframe""")]


# ----------------------------------------------------------------------------- indicators

def ema(values: list[float], length: int) -> list[float | None]:
    """Standard recursive EMA seeded with the SMA of the first `length` values.

    Each output depends only on inputs at or before its own index (no look-ahead).
    """
    out: list[float | None] = []
    k = 2.0 / (length + 1)
    prev = None
    for i, v in enumerate(values):
        if i + 1 < length:
            out.append(None)
        elif i + 1 == length:
            prev = sum(values[:length]) / length
            out.append(prev)
        else:
            prev = v * k + prev * (1 - k)
            out.append(prev)
    return out


def pivots(h: float, l: float, c: float, method: str = "classic") -> dict:
    p = (h + l + c) / 3
    r = h - l
    if method == "fibonacci":
        return {"P": p, "R1": p + 0.382 * r, "S1": p - 0.382 * r, "R2": p + 0.618 * r, "S2": p - 0.618 * r,
                "R3": p + r, "S3": p - r}
    if method == "camarilla":
        return {"P": p, "R1": c + 1.1 * r / 12, "S1": c - 1.1 * r / 12, "R2": c + 1.1 * r / 6, "S2": c - 1.1 * r / 6,
                "R3": c + 1.1 * r / 4, "S3": c - 1.1 * r / 4}
    return {"P": p, "R1": 2 * p - l, "S1": 2 * p - h, "R2": p + r, "S2": p - r,
            "R3": h + 2 * (p - l), "S3": l - 2 * (h - p)}


# ----------------------------------------------------------------------------- chart building

def _floor_15m(epoch: int, tz: str) -> int:
    local = datetime.fromtimestamp(epoch, ZoneInfo(tz))
    floored = local.replace(minute=local.minute - local.minute % 15, second=0, microsecond=0)
    return int(floored.timestamp())


def _bar_dict(r) -> dict:
    d = {"epoch": r["epoch"], "local": r["local"], "session_date": r["session_date"],
         "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"]}
    if "volume" in r.keys():
        d["volume"] = r["volume"]
    return d


def swing_levels(bars: list[dict], lookaround: int = 2, keep: int = 3) -> list[dict]:
    """Support/resistance from swing highs and lows (fractals) in COMPLETED bars.

    A bar is a swing high when its high exceeds the highs of ``lookaround`` bars on each side (lows
    likewise). The bars after a swing must already be complete, so only bars with ``lookaround``
    completed successors qualify — no information after the cutoff is used.
    """
    out = []
    for i in range(lookaround, len(bars) - lookaround):
        win = bars[i - lookaround: i + lookaround + 1]
        b = bars[i]
        if all(b["high"] > x["high"] for j, x in enumerate(win) if j != lookaround):
            out.append({"kind": "resistance", "price": b["high"], "local": b["local"]})
        if all(b["low"] < x["low"] for j, x in enumerate(win) if j != lookaround):
            out.append({"kind": "support", "price": b["low"], "local": b["local"]})
    res = [x for x in out if x["kind"] == "resistance"][-keep:]
    sup = [x for x in out if x["kind"] == "support"][-keep:]
    return res + sup


def session_vwap(bars: list[dict]) -> tuple[bool, list[dict]]:
    """Cumulative session VWAP per completed bar; unavailable when the series has no volume (e.g. indices)."""
    if not bars or not any((b.get("volume") or 0) > 0 for b in bars):
        return False, []
    pts, cur_day, pv, vol = [], None, 0.0, 0.0
    for b in bars:
        if b["session_date"] != cur_day:
            cur_day, pv, vol = b["session_date"], 0.0, 0.0
        v = b.get("volume") or 0
        pv += (b["high"] + b["low"] + b["close"]) / 3 * v
        vol += v
        if vol > 0:
            pts.append({"epoch": b["epoch"], "local": b["local"], "value": pv / vol})
    return True, pts


MIN_FULL_SESSION_BARS = 20  # sessions with fewer 15m bars (Muhurat, special Saturdays, partial data) are not used for pivots


def _prior_session_ohlc(conn, instrument: str, before_date: str, cutoff_epoch: int):
    """Daily H/L/C for the most recent FULL session strictly before `before_date` (and fully before cutoff).

    Short sessions are skipped and reported in ``skipped_short_sessions``.
    """
    rows = conn.execute(
        """SELECT session_date, COUNT(*) AS n FROM market_bars WHERE instrument=? AND timeframe='15m'
           AND session_date < ? AND epoch + 900 <= ? GROUP BY session_date ORDER BY session_date DESC LIMIT 10""",
        (instrument, before_date, cutoff_epoch)).fetchall()
    skipped = []
    d = None
    for r in rows:
        if r["n"] >= MIN_FULL_SESSION_BARS:
            d = r["session_date"]
            break
        skipped.append(r["session_date"])
    if d is None:
        return None
    agg = conn.execute(
        """SELECT MAX(high) AS h, MIN(low) AS l, COUNT(*) AS n FROM market_bars
           WHERE instrument=? AND timeframe='15m' AND session_date=? AND epoch + 900 <= ?""",
        (instrument, d, cutoff_epoch)).fetchone()
    last = conn.execute(
        """SELECT close FROM market_bars WHERE instrument=? AND timeframe='15m' AND session_date=? AND epoch + 900 <= ?
           ORDER BY epoch DESC LIMIT 1""", (instrument, d, cutoff_epoch)).fetchone()
    return {"session_date": d, "high": agg["h"], "low": agg["l"], "close": last["close"], "bars": agg["n"],
            "skipped_short_sessions": skipped}


def price_basis(entry_price, reference, mode: str = "auto") -> str:
    """'chart' when the traded price is on the chart's scale (index / near-index), else 'separate' (e.g. options)."""
    if mode in ("chart", "separate"):
        return mode
    if entry_price is None or reference is None or reference == 0:
        return "separate"
    return "chart" if abs(entry_price - reference) / reference <= 0.0025 else "separate"


def build_chart(conn, trade: dict, *, cutoff_epoch: int, reveal_until_epoch: int | None = None,
                basis_mode: str = "auto") -> dict:
    """Build chart data.

    ``trade`` must contain: chart_instrument, entry_epoch, entry_price.
    With ``reveal_until_epoch=None`` this is the BLIND chart: every query is bounded by ``cutoff_epoch``.
    """
    s = get_settings(conn)
    tz, inst = s["exchange_timezone"], trade["chart_instrument"]
    E = int(cutoff_epoch)
    limitations: list[str] = []

    # --- which sessions to show: entry session + N earlier sessions that exist before E
    entry_date = datetime.fromtimestamp(E, ZoneInfo(tz)).strftime("%Y-%m-%d")
    sess = [r["session_date"] for r in conn.execute(
        """SELECT DISTINCT session_date FROM market_bars WHERE instrument=? AND timeframe='15m'
           AND session_date < ? AND epoch + 900 <= ? ORDER BY session_date DESC LIMIT ?""",
        (inst, entry_date, E, int(s["lookback_sessions"])))]
    display_start_date = min(sess) if sess else entry_date

    # --- completed bars (hard cutoff in SQL)
    ema_len = int(s["ema_length"])
    warm = max(ema_len * 10, 200)
    display = [_bar_dict(r) for r in conn.execute(
        """SELECT epoch, local, session_date, open, high, low, close, volume FROM market_bars
           WHERE instrument=? AND timeframe='15m' AND session_date >= ? AND epoch + 900 <= ? ORDER BY epoch""",
        (inst, display_start_date, E))]
    first_display_epoch = display[0]["epoch"] if display else E
    warmup = [dict(r) for r in conn.execute(
        """SELECT epoch, close FROM market_bars WHERE instrument=? AND timeframe='15m' AND epoch < ?
           ORDER BY epoch DESC LIMIT ?""", (inst, first_display_epoch, warm))][::-1]

    # --- forming bar at entry
    forming_open = _floor_15m(E, tz)
    forming = None
    minute_rows = conn.execute(
        """SELECT epoch, open, high, low, close FROM market_bars WHERE instrument=? AND timeframe='1m'
           AND epoch >= ? AND epoch + 60 <= ? ORDER BY epoch""", (inst, forming_open, E)).fetchall()
    has_1m_day = conn.execute(
        "SELECT 1 FROM market_bars WHERE instrument=? AND timeframe='1m' AND session_date=? AND epoch <= ? LIMIT 1",
        (inst, entry_date, E)).fetchone() is not None
    last_completed_close = display[-1]["close"] if display else None
    forming_note = None
    if forming_open < E or minute_rows:
        if minute_rows:
            forming = {"open": minute_rows[0]["open"], "high": max(r["high"] for r in minute_rows),
                       "low": min(r["low"] for r in minute_rows), "close": minute_rows[-1]["close"]}
            forming_note = (f"Forming candle rebuilt from {len(minute_rows)} completed 1-minute bar(s) before entry.")
        elif not has_1m_day:
            o = conn.execute(
                """SELECT open FROM market_bars WHERE instrument=? AND timeframe='15m' AND epoch=? AND epoch <= ?""",
                (inst, forming_open, E)).fetchone()
            if o:
                forming = {"open": o["open"], "high": o["open"], "low": o["open"], "close": o["open"]}
                forming_note = ("No 1-minute data: only the forming candle's OPEN is shown. Its high/low/close were "
                                "not known at entry and are withheld.")
    reference = forming["close"] if forming else last_completed_close
    basis = price_basis(trade.get("entry_price"), reference, basis_mode)
    if forming and basis == "chart" and trade.get("entry_price") is not None:
        ep = trade["entry_price"]
        forming = {"open": forming["open"], "high": max(forming["high"], ep), "low": min(forming["low"], ep), "close": ep}
        forming_note = (forming_note or "") + " Your entry price is used as the last known print."
        reference = ep
    if forming:
        forming.update({"epoch": forming_open, "local": datetime.fromtimestamp(forming_open, ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M:%S"),
                        "session_date": entry_date, "partial": True})
        limitations.append(forming_note.strip())
        limitations.append("Tick data is not available: prices within the current minute before entry are unknown.")
    if not display:
        limitations.append(f"No 15-minute market data for {inst} before this entry. Import market data to see the chart.")

    # --- where the entry falls inside its 15-minute candle (spec §31)
    zone = ZoneInfo(tz)
    secs_in = E - forming_open
    candle_label = (f"{datetime.fromtimestamp(forming_open, zone).strftime('%H:%M')}–"
                    f"{datetime.fromtimestamp(forming_open + 900, zone).strftime('%H:%M')}")
    if secs_in == 0:
        where = f"at the open of the {candle_label} candle — no part of that candle was known yet"
    else:
        where = (f"{secs_in // 60} min {secs_in % 60:02d} s into the {candle_label} candle — "
                 f"it was still forming; the previous candle was the last completed one")
    entry_position = {"candle": candle_label, "seconds_into_candle": secs_in, "at_candle_open": secs_in == 0,
                      "mid_candle": secs_in > 0, "description": f"Entry {datetime.fromtimestamp(E, zone).strftime('%H:%M:%S')} is {where}."}

    # --- EMA on completed bars only
    closes = [w["close"] for w in warmup] + [b["close"] for b in display]
    ema_all = ema(closes, ema_len)[len(warmup):]
    ema_points = [{"epoch": b["epoch"], "value": v} for b, v in zip(display, ema_all) if v is not None]

    # --- pivots: one level set per displayed session, each from its own prior completed session
    pivot_sets = []
    day_list = sorted({b["session_date"] for b in display} | {entry_date})
    for d in day_list:
        prior = _prior_session_ohlc(conn, inst, d, E)
        if not prior:
            continue
        lv = pivots(prior["high"], prior["low"], prior["close"], s["pivot_method"])
        if prior["skipped_short_sessions"]:
            limitations.append(f"Pivots for {d} use {prior['session_date']}; skipped short session(s) "
                               f"{', '.join(prior['skipped_short_sessions'])} (fewer than {MIN_FULL_SESSION_BARS} bars).")
        pivot_sets.append({"session_date": d, "from_session": prior["session_date"], "levels": lv,
                           "prior_day": {"high": prior["high"], "low": prior["low"], "close": prior["close"]}})

    day_open = None
    first_today = conn.execute(
        """SELECT open FROM market_bars WHERE instrument=? AND timeframe='15m' AND session_date=? AND epoch <= ?
           ORDER BY epoch LIMIT 1""", (inst, entry_date, E)).fetchone()
    if first_today:
        day_open = first_today["open"]

    result = {
        "instrument": inst, "timeframe": "15m", "timezone": tz, "mode": "BLIND",
        "cutoff_epoch": E, "cutoff_local": datetime.fromtimestamp(E, ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M:%S"),
        "candles": display, "forming_candle": forming, "ema": {"length": ema_len, "points": ema_points},
        "pivots": {"method": s["pivot_method"], "formula": PIVOT_DOC[s["pivot_method"]], "source": PIVOT_SOURCE_NOTE,
                   "sets": pivot_sets},
        "day_open": day_open,
        "entry": {"epoch": trade["entry_epoch"], "price": trade.get("entry_price"), "reference_price": reference,
                  "price_basis": basis, "position": entry_position},
        "support_resistance": {"method": "swing highs/lows (2 completed bars each side) from completed candles only",
                               "levels": swing_levels(display)},
        "vwap": dict(zip(("available", "points"), session_vwap(display)),
                     note="Needs volume; index series have none, so VWAP is unavailable for them."),
        "limitations": limitations,
        "max_epoch_in_payload": max([b["epoch"] for b in display] + ([forming_open] if forming else []), default=None),
    }
    if basis == "separate":
        limitations.append("The traded price is not on this chart's scale (e.g. an option). The entry marker is placed "
                           "at the index level at entry; excursions are measured on the index.")

    if reveal_until_epoch is not None:
        after = [_bar_dict(r) for r in conn.execute(
            """SELECT epoch, local, session_date, open, high, low, close, volume FROM market_bars
               WHERE instrument=? AND timeframe='15m' AND epoch + 900 > ? AND epoch <= ? ORDER BY epoch""",
            (inst, E, int(reveal_until_epoch)))]
        all_bars = display + after
        closes_full = [w["close"] for w in warmup] + [b["close"] for b in all_bars]
        ema_full = ema(closes_full, ema_len)[len(warmup):]
        extra_days = sorted({b["session_date"] for b in after} - set(day_list))
        for d in extra_days:
            prior = _prior_session_ohlc(conn, inst, d, int(reveal_until_epoch) + 900)
            if prior:
                pivot_sets.append({"session_date": d, "from_session": prior["session_date"],
                                   "levels": pivots(prior["high"], prior["low"], prior["close"], s["pivot_method"]),
                                   "prior_day": {"high": prior["high"], "low": prior["low"], "close": prior["close"]}})
        result.update({
            "mode": "REVEALED",
            "after_candles": after,
            "ema": {"length": ema_len, "points": [{"epoch": b["epoch"], "value": v} for b, v in zip(all_bars, ema_full) if v is not None],
                    "blind_points_count": len(ema_points)},
            "max_epoch_in_payload": max([b["epoch"] for b in all_bars], default=None),
        })
    return result


def path_bars(conn, instrument: str, start_epoch: int, end_epoch: int) -> tuple[list[dict], str]:
    """Bars covering [start, end) for excursion analysis: 1-minute if available, else 15-minute."""
    one = [dict(r) for r in conn.execute(
        """SELECT epoch, open, high, low, close FROM market_bars WHERE instrument=? AND timeframe='1m'
           AND epoch + 60 > ? AND epoch < ? ORDER BY epoch""", (instrument, start_epoch, end_epoch))]
    if one:
        return one, "1m"
    fifteen = [dict(r) for r in conn.execute(
        """SELECT epoch, open, high, low, close FROM market_bars WHERE instrument=? AND timeframe='15m'
           AND epoch + 900 > ? AND epoch < ? ORDER BY epoch""", (instrument, start_epoch, end_epoch))]
    return fifteen, "15m"


def session_close_epoch(conn, instrument: str, epoch: int) -> int | None:
    tz = get_settings(conn)["exchange_timezone"]
    d = datetime.fromtimestamp(epoch, ZoneInfo(tz)).strftime("%Y-%m-%d")
    r = conn.execute("SELECT MAX(epoch) AS m FROM market_bars WHERE instrument=? AND timeframe='15m' AND session_date=?",
                     (instrument, d)).fetchone()
    if r and r["m"]:
        return r["m"] + 900
    hh, mm = map(int, get_settings(conn)["session_close"].split(":"))
    local = datetime.fromtimestamp(epoch, ZoneInfo(tz)).replace(hour=hh, minute=mm, second=0, microsecond=0)
    return int(local.timestamp())


def nth_session_close(conn, instrument: str, epoch: int, n: int) -> int | None:
    tz = get_settings(conn)["exchange_timezone"]
    d = datetime.fromtimestamp(epoch, ZoneInfo(tz)).strftime("%Y-%m-%d")
    rows = conn.execute(
        """SELECT session_date, MAX(epoch) AS m FROM market_bars WHERE instrument=? AND timeframe='15m' AND session_date >= ?
           GROUP BY session_date ORDER BY session_date LIMIT ?""", (instrument, d, n)).fetchall()
    return rows[-1]["m"] + 900 if rows else None
