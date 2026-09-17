"""Sample / test data generation.

* ``synthetic_market`` – deterministic random-walk 1-minute bars plus 15-minute bars aggregated
  from them (used by the test suite, no external files needed).
* ``sample_trades_csv`` – round-trip trades built from given 1-minute bars, written in a
  deliberately awkward shape: shuffled rows, mixed timezone notations, a duplicate entry
  timestamp, a later-entered trade that exits earlier, and a row with a missing entry time.
"""
from __future__ import annotations

import csv
import io
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def synthetic_market(start_date="2026-01-12", sessions=5, seed=7, base=24000.0):
    rng = random.Random(seed)
    d = datetime.strptime(start_date, "%Y-%m-%d")
    one, fifteen = [], []
    price = base
    made = 0
    while made < sessions:
        if d.weekday() < 5:
            t = d.replace(hour=9, minute=15)
            end = d.replace(hour=15, minute=30)
            day = []
            while t < end:
                o = price
                c = o + rng.gauss(0, 6)
                h = max(o, c) + abs(rng.gauss(0, 3))
                l = min(o, c) - abs(rng.gauss(0, 3))
                day.append((t, round(o, 2), round(h, 2), round(l, 2), round(c, 2)))
                price = c
                t += timedelta(minutes=1)
            one += day
            for i in range(0, len(day), 15):
                chunk = day[i:i + 15]
                fifteen.append((chunk[0][0], chunk[0][1], max(x[2] for x in chunk), min(x[3] for x in chunk), chunk[-1][4]))
            made += 1
        d += timedelta(days=1)
    return one, fifteen


def bars_csv(bars) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
    for t, o, h, l, c in bars:
        w.writerow([t.strftime("%Y-%m-%d %H:%M:%S"), o, h, l, c, 0])
    return buf.getvalue()


def _fmt_variants(local_dt: datetime, k: int) -> str:
    aware = local_dt.replace(tzinfo=IST)
    if k % 3 == 0:
        return aware.isoformat()                                   # 2026-01-15T10:42:00+05:30
    if k % 3 == 1:
        return aware.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # UTC with Z
    return local_dt.strftime("%d-%b-%Y %H:%M:%S") + " +0530"      # 15-Jan-2026 10:42:00 +0530


def sample_trades_csv(one_min_bars, n=24, seed=11, shuffle=True) -> tuple[str, list[dict]]:
    """Return (csv_text, truth) where truth lists trades in true chronological order."""
    rng = random.Random(seed)
    by_time = {b[0]: b for b in one_min_bars}
    days = sorted({b[0].date() for b in one_min_bars})
    trades = []
    for i in range(n):
        day = days[i % len(days)]
        minute = rng.randrange(20, 300)
        second = rng.choice([0, 0, 17, 42])
        entry = datetime.combine(day, datetime.min.time()).replace(hour=9, minute=15) + timedelta(minutes=minute, seconds=second)
        hold = rng.randrange(8, 70)
        exit_ = entry + timedelta(minutes=hold)
        eb = by_time.get(entry.replace(second=0))
        xb = by_time.get(exit_.replace(second=0))
        if not eb or not xb:
            continue
        side = rng.choice(["BUY", "SELL"])
        qty = 75
        ep, xp = eb[1], xb[4]
        pnl = round((xp - ep) * (1 if side == "BUY" else -1) * qty, 2)
        trades.append({"entry": entry, "exit": exit_, "side": side, "qty": qty, "entry_price": ep, "exit_price": xp, "pnl": pnl})
    # Duplicate entry timestamp (tie-break by Trade ID)
    if len(trades) > 3:
        dup = dict(trades[2])
        dup["exit"] = dup["entry"] + timedelta(minutes=5)
        dup["side"] = "SELL" if dup["side"] == "BUY" else "BUY"
        xb = by_time.get(dup["exit"].replace(second=0))
        dup["exit_price"] = xb[4]
        dup["pnl"] = round((dup["exit_price"] - dup["entry_price"]) * (1 if dup["side"] == "BUY" else -1) * dup["qty"], 2)
        trades.append(dup)
    trades.sort(key=lambda t: t["entry"])
    # Later-entered trade that exits earlier than the previous trade
    if len(trades) > 6:
        a, b = trades[5], trades[6]
        if a["entry"].date() == b["entry"].date() and b["entry"] > a["entry"]:
            a["exit"] = b["entry"] + timedelta(minutes=30)
            b["exit"] = b["entry"] + timedelta(minutes=5)
            for t in (a, b):
                xb = by_time.get(t["exit"].replace(second=0))
                if xb:
                    t["exit_price"] = xb[4]
                    t["pnl"] = round((t["exit_price"] - t["entry_price"]) * (1 if t["side"] == "BUY" else -1) * t["qty"], 2)
    for i, t in enumerate(trades, start=1):
        t["trade_id"] = f"{100 + i}"
    rows = []
    for k, t in enumerate(trades):
        rows.append({"Trade ID": t["trade_id"], "Symbol": "NIFTY", "Entry Time": _fmt_variants(t["entry"], k),
                     "Exit Time": _fmt_variants(t["exit"], k + 1), "Side": t["side"], "Qty": t["qty"],
                     "Entry Price": t["entry_price"], "Exit Price": t["exit_price"], "P&L": t["pnl"]})
    rows.append({"Trade ID": "999", "Symbol": "NIFTY", "Entry Time": "", "Exit Time": "", "Side": "BUY", "Qty": 75,
                 "Entry Price": trades[0]["entry_price"], "Exit Price": trades[0]["entry_price"] + 10, "P&L": 750})
    if shuffle:
        rng.shuffle(rows)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue(), trades
