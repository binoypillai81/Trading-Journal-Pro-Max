"""Index F&O contract symbols, expiry dates and expiry settlement.

Symbol formats (Zerodha / NSE):
  weekly option   NIFTY2061810000PE   → NIFTY, 2020-06-18, 10000, PE   (month code 1-9, O, N, D)
  monthly option  NIFTY20JUN10300PE   → NIFTY, June 2020 monthly, 10300, PE
  monthly future  BANKNIFTY20MAYFUT   → BANKNIFTY, May 2020 monthly, future

Weekly symbols carry their exact expiry date. Monthly expiry is the LAST expiry weekday of the
month, using NSE's weekday for that underlying and period (table below), moved to the previous
trading session when that day was a holiday. Trading sessions come from the imported index bars.

Settlement at expiry (index derivatives are cash-settled):
  CE  = max(0, S − K)     PE = max(0, K − S)     FUT = S
where S is the official closing index value on expiry day, taken from imported daily ('1d') bars.
If no daily bar is imported, the last traded value from intraday bars is used and labelled as such
(it can differ from the official close by tens of points).
"""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

INDEX_UNDERLYINGS = ("BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTY")
_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
_WEEKLY_MONTH = {**{str(i): i for i in range(1, 10)}, "O": 10, "N": 11, "D": 12}
_WEEKLY = re.compile(r"^(BANKNIFTY|FINNIFTY|MIDCPNIFTY|NIFTY)(\d{2})([1-9OND])(\d{2})(\d+(?:\.\d+)?)(CE|PE)$")
_MONTHLY = re.compile(r"^(BANKNIFTY|FINNIFTY|MIDCPNIFTY|NIFTY)(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d+(?:\.\d+)?)?(CE|PE|FUT)$")
MON, TUE, WED, THU = 0, 1, 2, 3


def parse_symbol(symbol: str | None) -> dict | None:
    s = (symbol or "").strip().upper().replace(" ", "")
    m = _WEEKLY.match(s)
    if m:
        try:
            d = date(2000 + int(m[2]), _WEEKLY_MONTH[m[3]], int(m[4]))
        except ValueError:
            return None
        return {"underlying": m[1], "kind": "weekly", "year": d.year, "month": d.month, "nominal_date": d,
                "strike": float(m[5]), "type": m[6]}
    m = _MONTHLY.match(s)
    if m:
        if m[5] != "FUT" and not m[4]:
            return None
        return {"underlying": m[1], "kind": "monthly", "year": 2000 + int(m[2]), "month": _MONTHS.index(m[3]) + 1,
                "nominal_date": None, "strike": float(m[4]) if m[4] else None, "type": m[5]}
    return None


def monthly_expiry_weekday(underlying: str, year: int, month: int) -> int | None:
    """NSE monthly expiry weekday for an index, by contract month."""
    d = date(year, month, 1)
    if underlying == "NIFTY":
        return THU if d < date(2025, 9, 1) else TUE
    if underlying == "BANKNIFTY":
        if d < date(2024, 3, 1):
            return THU
        return WED if d < date(2025, 9, 1) else TUE
    if underlying == "FINNIFTY":
        return TUE
    if underlying == "MIDCPNIFTY":
        return MON
    return None


def _previous_session(d: date, is_session) -> date | None:
    for back in range(0, 8):
        c = d - timedelta(days=back)
        if is_session(c):
            return c
    return None


def expiry_date(parsed: dict, is_session) -> tuple[date | None, str]:
    """Return (expiry date, method). ``is_session(date) -> bool`` uses imported index sessions."""
    if parsed["kind"] == "weekly":
        nominal = parsed["nominal_date"]
        method = "date in weekly symbol"
    else:
        wd = monthly_expiry_weekday(parsed["underlying"], parsed["year"], parsed["month"])
        if wd is None:
            return None, "unknown monthly expiry weekday"
        last_day = calendar.monthrange(parsed["year"], parsed["month"])[1]
        nominal = date(parsed["year"], parsed["month"], last_day)
        while nominal.weekday() != wd:
            nominal -= timedelta(days=1)
        method = f"last {calendar.day_name[wd]} of month"
    actual = _previous_session(nominal, is_session)
    if actual is None:
        return None, f"{method}; no index data around {nominal}"
    if actual != nominal:
        method += f"; {nominal} was not a trading session, moved to {actual}"
    return actual, method


def settlement_value(parsed: dict, underlying_close: float) -> float:
    if parsed["type"] == "FUT":
        return underlying_close
    if parsed["type"] == "CE":
        return max(0.0, underlying_close - parsed["strike"])
    return max(0.0, parsed["strike"] - underlying_close)


def settle_open_position(conn, *, instrument: str, chart_instrument: str, last_fill_epoch: int, tz: str) -> dict | None | str:
    """Settlement details for a position still open at the end of the file.

    Returns a dict on success, or a string explaining why it could not be settled.
    """
    parsed = parse_symbol(instrument)
    if not parsed:
        return "not an index F&O contract (stock options are physically settled; not modelled)"
    under = parsed["underlying"]
    sessions = {r[0] for r in conn.execute(
        "SELECT DISTINCT session_date FROM market_bars WHERE instrument=? AND timeframe='15m'", (under,))}
    if not sessions:
        return f"no {under} market data imported"
    exp, method = expiry_date(parsed, lambda d: d.isoformat() in sessions)
    if exp is None:
        return method
    zone = ZoneInfo(tz)
    last_fill_day = datetime.fromtimestamp(last_fill_epoch, zone).date()
    if exp < last_fill_day:
        return f"computed expiry {exp} is before the last fill ({last_fill_day}); not settled"
    today = datetime.now(zone).date()
    if exp >= today:
        return f"contract expires {exp}; not yet expired"
    row = conn.execute("SELECT close FROM market_bars WHERE instrument=? AND timeframe='1d' AND session_date=?",
                       (under, exp.isoformat())).fetchone()
    if row:
        source = f"official {under} closing value on {exp} (daily bar)"
    else:
        for tf in ("1m", "15m"):
            row = conn.execute(
                "SELECT close, local FROM market_bars WHERE instrument=? AND timeframe=? AND session_date=? ORDER BY epoch DESC LIMIT 1",
                (under, tf, exp.isoformat())).fetchone()
            if row:
                source = (f"last traded {under} value ({row['local'][11:16]} bar close) — no daily close imported; "
                          "the official closing value may differ")
                break
    if not row:
        return f"no {under} bars on expiry date {exp}"
    close = float(row["close"])
    value = settlement_value(parsed, close)
    exit_dt = datetime(exp.year, exp.month, exp.day, 15, 30, tzinfo=zone)
    return {
        "expiry_date": exp.isoformat(), "method": method, "underlying": under, "underlying_value": close,
        "underlying_value_source": source,
        "strike": parsed["strike"], "type": parsed["type"], "settlement_price": round(value, 4),
        "exit_epoch": int(exit_dt.timestamp()),
    }
