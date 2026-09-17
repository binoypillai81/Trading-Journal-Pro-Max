"""Timestamp parsing and normalization.

Every parsed timestamp yields a ``ParsedTs`` carrying:

* ``epoch``   – UTC epoch seconds (None if unresolved)
* ``source``  – how the timezone was determined:
    ``offset:+05:30``      explicit offset in the value
    ``utc``                explicit Z / UTC / GMT marker
    ``column:<tz>``        a mapped timezone column
    ``assumed:<tz>``       naive value, interpreted in the timezone the user confirmed
    ``epoch``              numeric epoch value (always UTC)
* ``issue``   – reason the value cannot be placed in the chronology (None if fine)
* ``ambiguous_date`` – True when a numeric date like 03/04/2026 could be D/M or M/D

Timezone-naive values are *never* silently mixed with aware ones: a naive value
is only resolved when the user has explicitly supplied the timezone to assume.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

_TZ_TAIL = re.compile(
    r"\s*(?:(?P<z>Z)|(?P<name>UTC|GMT|IST)?\s*(?P<off>[+-]\d{2}:?\d{2})?)\s*$", re.IGNORECASE)
_TIME = re.compile(
    r"(?P<h>\d{1,2}):(?P<m>\d{2})(?::(?P<s>\d{2})(?:[.,](?P<f>\d{1,6}))?)?\s*(?P<ampm>[AaPp][Mm])?")


@dataclass
class ParsedTs:
    epoch: int | None
    source: str | None = None
    issue: str | None = None
    ambiguous_date: bool = False
    warning: str | None = None


def valid_timezone(name: str | None) -> bool:
    if not name:
        return False
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def _parse_date(text: str, date_order: str):
    """Return (year, month, day, ambiguous) or raise ValueError."""
    t = text.strip().rstrip(",")
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", t)
    if m:
        return int(m[1]), int(m[2]), int(m[3]), False
    m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})", t)
    if m:
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        if y < 100:
            y += 2000
        if date_order == "MDY":
            return y, a, b, False
        if date_order == "DMY":
            return y, b, a, False
        # auto: decide only when unambiguous
        if a > 12 and b <= 12:
            return y, b, a, False
        if b > 12 and a <= 12:
            return y, a, b, False
        if a == b:
            return y, a, b, False
        return y, b, a, True  # provisional DMY, flagged ambiguous
    m = re.fullmatch(r"(\d{1,2})[-\s/]([A-Za-z]{3,9})[-\s/,]+(\d{2,4})", t)
    if m and m[2][:3].lower() in MONTHS:
        y = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
        return y, MONTHS[m[2][:3].lower()], int(m[1]), False
    m = re.fullmatch(r"([A-Za-z]{3,9})[\s-]+(\d{1,2}),?[\s-]+(\d{4})", t)
    if m and m[1][:3].lower() in MONTHS:
        return int(m[3]), MONTHS[m[1][:3].lower()], int(m[2]), False
    raise ValueError(f"Unrecognised date '{text}'")


def _split_tz(text: str):
    """Strip a trailing timezone marker. Returns (rest, kind, offset_minutes, name)."""
    m = _TZ_TAIL.search(text)
    if not m or not (m["z"] or m["name"] or m["off"]):
        return text, None, None, None
    rest = text[: m.start()].rstrip()
    # Guard: "10:42" must not have its minutes read as an offset — offsets need a sign.
    if m["z"]:
        # Only accept Z when it follows a digit (ISO style)
        if not re.search(r"\d$", rest):
            return text, None, None, None
        return rest, "utc", 0, "Z"
    name = (m["name"] or "").upper()
    if m["off"]:
        off = m["off"].replace(":", "")
        if int(off[1:3]) > 14 or int(off[3:5]) >= 60 or not re.search(r"\d(:\d{2})*(\.\d+)?$", rest):
            return text, None, None, None  # e.g. the '-2026' of a trailing date, not an offset
        sign = 1 if off[0] == "+" else -1
        minutes = sign * (int(off[1:3]) * 60 + int(off[3:5]))
        return rest, "offset", minutes, name or None
    if name in ("UTC", "GMT"):
        return rest, "utc", 0, name
    if name == "IST":
        return rest, "abbrev_ist", 330, name
    return text, None, None, None


def parse_timestamp(
    value: str | None,
    *,
    time_value: str | None = None,
    naive_tz: str | None = None,
    tz_value: str | None = None,
    date_order: str = "auto",
) -> ParsedTs:
    """Parse a timestamp given either a combined value or separate date/time values."""
    raw = " ".join(str(v).strip() for v in (value, time_value) if v not in (None, "") and str(v).strip().lower() not in ("nan", "none", "null"))
    if not raw:
        return ParsedTs(None, issue="Missing entry timestamp")

    # Numeric epoch (seconds or milliseconds)
    if re.fullmatch(r"\d{10}(\.\d+)?|\d{13}", raw):
        n = float(raw)
        if n > 1e12:
            n /= 1000.0
        return ParsedTs(int(n), source="epoch")

    text = raw.replace("T", " ") if re.match(r"\d{4}-\d{2}-\d{2}T", raw) else raw
    text, tz_kind, off_min, tz_name = _split_tz(text)

    tm = None
    for cand in _TIME.finditer(text):
        tm = cand  # last time-looking token
    if tm is None:
        return ParsedTs(None, issue=f"No time component in '{raw}' — exact entry time unknown")
    date_part = (text[: tm.start()] + " " + text[tm.end():]).strip()
    if not date_part:
        return ParsedTs(None, issue=f"No date component in '{raw}'")
    try:
        y, mo, d, ambiguous = _parse_date(date_part, date_order)
        h, mi = int(tm["h"]), int(tm["m"])
        s = int(tm["s"] or 0)
        frac = int((tm["f"] or "0").ljust(6, "0")[:6])
        if tm["ampm"]:
            ap = tm["ampm"].lower()
            if h == 12:
                h = 0
            if ap == "pm":
                h += 12
        wall = datetime(y, mo, d, h, mi, s, frac)
    except ValueError as exc:
        return ParsedTs(None, issue=f"Unparseable timestamp '{raw}': {exc}")

    warning = None
    if tz_kind in ("utc", "offset", "abbrev_ist"):
        aware = wall.replace(tzinfo=timezone(timedelta(minutes=off_min)))
        if tz_kind == "utc":
            source = "utc"
        elif tz_kind == "abbrev_ist":
            source = "offset:+05:30"
            warning = "Interpreted 'IST' as India Standard Time (+05:30)"
        else:
            sign = "+" if off_min >= 0 else "-"
            source = f"offset:{sign}{abs(off_min)//60:02d}:{abs(off_min)%60:02d}"
        return ParsedTs(int(aware.timestamp()), source=source, ambiguous_date=ambiguous, warning=warning)

    # Naive value: need a timezone from a column or from explicit user confirmation.
    zone_name, source_prefix = None, None
    if tz_value and str(tz_value).strip():
        tzv = str(tz_value).strip()
        if valid_timezone(tzv):
            zone_name, source_prefix = tzv, "column"
        elif re.fullmatch(r"[+-]\d{2}:?\d{2}", tzv):
            off = tzv.replace(":", "")
            minutes = (1 if off[0] == "+" else -1) * (int(off[1:3]) * 60 + int(off[3:5]))
            aware = wall.replace(tzinfo=timezone(timedelta(minutes=minutes)))
            return ParsedTs(int(aware.timestamp()), source=f"column:{tzv}", ambiguous_date=ambiguous)
        elif tzv.upper() in ("UTC", "GMT", "Z"):
            aware = wall.replace(tzinfo=timezone.utc)
            return ParsedTs(int(aware.timestamp()), source="column:UTC", ambiguous_date=ambiguous)
        elif tzv.upper() == "IST":
            aware = wall.replace(tzinfo=timezone(timedelta(minutes=330)))
            return ParsedTs(int(aware.timestamp()), source="column:IST(+05:30)", ambiguous_date=ambiguous,
                            warning="Interpreted 'IST' as India Standard Time (+05:30)")
        else:
            return ParsedTs(None, issue=f"Unknown timezone value '{tzv}'", ambiguous_date=ambiguous)
    elif naive_tz:
        if not valid_timezone(naive_tz):
            return ParsedTs(None, issue=f"Unknown timezone '{naive_tz}'")
        zone_name, source_prefix = naive_tz, "assumed"
    else:
        return ParsedTs(None, issue="Timestamp has no timezone and no timezone has been confirmed",
                        ambiguous_date=ambiguous)

    zone = ZoneInfo(zone_name)
    first = wall.replace(tzinfo=zone, fold=0)
    second = wall.replace(tzinfo=zone, fold=1)
    roundtrip = datetime.fromtimestamp(first.timestamp(), zone).replace(tzinfo=None)
    if roundtrip != wall:
        return ParsedTs(None, issue=f"Non-existent local time '{raw}' in {zone_name} (daylight-saving gap)",
                        ambiguous_date=ambiguous)
    if first.utcoffset() != second.utcoffset():
        return ParsedTs(None, issue=f"Ambiguous local time '{raw}' in {zone_name} (daylight-saving overlap)",
                        ambiguous_date=ambiguous)
    return ParsedTs(int(first.timestamp()), source=f"{source_prefix}:{zone_name}", ambiguous_date=ambiguous)


def to_local(epoch: int | None, tz_name: str) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M:%S")


def local_to_epoch(local: str, tz_name: str) -> int:
    wall = datetime.strptime(local, "%Y-%m-%d %H:%M:%S")
    return int(wall.replace(tzinfo=ZoneInfo(tz_name)).timestamp())


def renormalize_all(conn, tz_name: str) -> None:
    """Recompute exchange-local strings after the exchange timezone changes.

    Epochs (the true instants) and chronological order are unaffected.
    """
    for r in conn.execute("SELECT id, entry_epoch, exit_epoch FROM trades").fetchall():
        conn.execute("UPDATE trades SET entry_local=?, exit_local=? WHERE id=?",
                     (to_local(r["entry_epoch"], tz_name), to_local(r["exit_epoch"], tz_name), r["id"]))
    zone = ZoneInfo(tz_name)
    rows = conn.execute("SELECT instrument, timeframe, epoch FROM market_bars").fetchall()
    for r in rows:
        dt = datetime.fromtimestamp(r["epoch"], zone)
        conn.execute("UPDATE market_bars SET local=?, session_date=? WHERE instrument=? AND timeframe=? AND epoch=?",
                     (dt.strftime("%Y-%m-%d %H:%M:%S"), dt.strftime("%Y-%m-%d"),
                      r["instrument"], r["timeframe"], r["epoch"]))
