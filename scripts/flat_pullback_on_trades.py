#!/usr/bin/env python3
"""Test the "flat pullbacks continue" rule on your own NIFTY option trades.

Each round trip is labelled by the pullback state of the 15-minute NIFTY chart
at its entry, using only bars that had closed by then (no lookahead):

  flat_with     shallow, slow pullback in a trend; you entered with the trend
                (the rule's trade)
  sharp_with    fast, deep pullback; you entered with the trend
                (the rule says this one reverses)
  flat_against  flat pullback; you entered against the trend
  sharp_against sharp pullback; you entered against the trend (the reversal trade)
  none          no qualifying trend leg and pullback at entry

The definitions match the index test that found +6 points of continuation over
chance on Nifty 2011-23:
  leg       the bar that set the day's newest extreme, measured from the lowest
            point (or highest, for a down leg) in the 8 bars up to it; at least
            4x the typical 15-min range
  flat      4-6 bars since the extreme, retraced 10-38% of the leg, no bar wider
            than 1.2x typical
  sharp     1-2 bars since the extreme, retraced at least 50% of the leg
  typical   median range of the previous 50 fifteen-minute bars

Results are split by state (first trade of the day / after a win / after a
loss), because after-a-win trades win far more often regardless of setup. A
setup only counts if it beats "none" within the same state.

Usage:
  python3 scripts/flat_pullback_on_trades.py --trades tradebook.csv --bars nifty.csv
  python3 scripts/flat_pullback_on_trades.py ... --out labelled.csv --grid

--trades accepts either
  * a Zerodha Kite tradebook export (symbol, trade_type, quantity, price,
    order_execution_time ...); fills are paired flat-to-flat per contract, or
  * one row per round trip with columns: symbol, entry_time, exit_time, pnl
    (optional: side = BUY/SELL for long/short the option).
--bars: NIFTY index bars at 15 minutes or finer, with columns timestamp (or
date + time), open, high, low, close, in IST. Finer bars are rebuilt into
15-minute bars anchored at 09:15.
"""
from __future__ import annotations

import argparse
import re
import sys

import numpy as np
import pandas as pd

IST = "Asia/Kolkata"
SESSION_START = 9 * 60 + 15
BAR_MIN = 15
LABELS = ["flat_with", "sharp_with", "flat_against", "sharp_against", "none"]
STATES = ["first", "after_win", "after_loss"]

# NIFTY25AUG24500CE (monthly) or NIFTY2581424500CE (weekly: YY, month 1-9/O/N/D, DD)
SYMBOL_RE = re.compile(r"^NIFTY(\d{2}[A-Z]{3}|\d{2}[1-9OND]\d{2})(\d{4,5})(CE|PE)$")


# ---------------------------------------------------------------- loading

def _norm_cols(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = [re.sub(r"[^a-z0-9]+", "_", str(c).strip().lower()).strip("_") for c in df.columns]
    return df


def _to_ist_naive(s: pd.Series) -> pd.Series:
    t = pd.to_datetime(s, errors="coerce", format="mixed")
    if getattr(t.dt, "tz", None) is not None:
        t = t.dt.tz_convert(IST).dt.tz_localize(None)
    return t


def option_type(symbol: str) -> str | None:
    m = SYMBOL_RE.match(str(symbol).strip().upper())
    return m.group(3) if m else None


def load_bars(path: str) -> pd.DataFrame:
    b = _norm_cols(pd.read_csv(path))
    if "timestamp" not in b and {"date", "time"} <= set(b.columns):
        b["timestamp"] = b["date"].astype(str) + " " + b["time"].astype(str)
    for alt in ("datetime", "date"):
        if "timestamp" not in b and alt in b:
            b["timestamp"] = b[alt]
    missing = {"timestamp", "open", "high", "low", "close"} - set(b.columns)
    if missing:
        sys.exit(f"--bars is missing columns: {sorted(missing)}")
    b["ts"] = _to_ist_naive(b["timestamp"])
    b = b.dropna(subset=["ts"]).sort_values("ts")
    b["day"] = b.ts.dt.normalize()
    mins = b.ts.dt.hour * 60 + b.ts.dt.minute
    b = b[(mins >= SESSION_START) & (mins < 15 * 60 + 30)]
    b["slot"] = (mins[b.index] - SESSION_START) // BAR_MIN
    m = (b.groupby(["day", "slot"])
           .agg(o=("open", "first"), h=("high", "max"), l=("low", "min"), c=("close", "last"))
           .reset_index())
    m["rng"] = m.h - m.l
    m["typ"] = m.rng.shift(1).rolling(50, min_periods=20).median()   # past bars only
    m["closes_at"] = m.day + pd.to_timedelta(SESSION_START + (m.slot + 1) * BAR_MIN, unit="m")
    return m


def _round_trips_from_fills(f: pd.DataFrame) -> pd.DataFrame:
    need = {"symbol", "trade_type", "quantity", "price", "order_execution_time"}
    if need - set(f.columns):
        sys.exit(f"Kite tradebook is missing columns: {sorted(need - set(f.columns))}")
    f = f.copy()
    f["t"] = _to_ist_naive(f["order_execution_time"])
    f = f.dropna(subset=["t"]).sort_values(["t"] + (["trade_id"] if "trade_id" in f else []))
    f["sgn"] = np.where(f.trade_type.str.lower().str.startswith("b"), 1, -1)
    f["q"] = f.quantity.astype(float) * f.sgn
    f["cash"] = -f.q * f.price.astype(float)
    out = []
    for sym, g in f.groupby("symbol", sort=False):
        pos, start, cash, side, fills = 0.0, None, 0.0, 0, 0
        for r in g.itertuples():
            if pos == 0:
                start, cash, side, fills = r.t, 0.0, int(np.sign(r.q)), 0
            pos += r.q
            cash += r.cash
            fills += 1
            if abs(pos) < 1e-9:
                out.append(dict(symbol=sym, entry_time=start, exit_time=r.t, pnl=cash,
                                side="BUY" if side > 0 else "SELL", fills=fills))
                pos = 0.0
        if abs(pos) > 1e-9:
            print(f"note: {sym} still open at end of file ({pos:+.0f}); skipped", file=sys.stderr)
    return pd.DataFrame(out)


def load_trades(path: str) -> pd.DataFrame:
    t = _norm_cols(pd.read_csv(path))
    if "order_execution_time" in t and "trade_type" in t:
        t = _round_trips_from_fills(t)
    else:
        need = {"symbol", "entry_time", "exit_time", "pnl"}
        if need - set(t.columns):
            sys.exit("--trades must be a Kite tradebook or have columns "
                     f"{sorted(need)}; missing {sorted(need - set(t.columns))}")
        t["entry_time"] = _to_ist_naive(t["entry_time"])
        t["exit_time"] = _to_ist_naive(t["exit_time"])
        t["pnl"] = pd.to_numeric(t["pnl"], errors="coerce")
        if "side" not in t:
            t["side"] = "BUY"
    t["opt"] = t.symbol.map(option_type)
    bad = t.opt.isna()
    if bad.any():
        print(f"note: {bad.sum()} trades skipped (not a NIFTY CE/PE symbol), e.g. "
              f"{t.loc[bad, 'symbol'].head(3).tolist()}", file=sys.stderr)
    t = t[~bad].dropna(subset=["entry_time", "exit_time", "pnl"]).copy()
    long_opt = t.side.astype(str).str.upper().str.startswith("B")
    # +1 = profits if NIFTY rises (long CE / short PE), -1 = profits if it falls
    t["dir"] = np.where((t.opt == "CE") == long_opt, 1, -1)
    t["hold_min"] = (t.exit_time - t.entry_time).dt.total_seconds() / 60
    return t.sort_values("entry_time").reset_index(drop=True)


# ------------------------------------------------------------ classification

def pullback_state(day_bars: pd.DataFrame, side: int, p: dict) -> str | None:
    """'flat', 'sharp' or None for a `side` (+1 up / -1 down) leg on closed bars."""
    n = len(day_bars)
    if n < 6:
        return None
    hi = side * np.where(side > 0, day_bars.h.to_numpy(), day_bars.l.to_numpy())
    lo = side * np.where(side > 0, day_bars.l.to_numpy(), day_bars.h.to_numpy())
    cl = side * day_bars.c.to_numpy()
    rng, typ = day_bars.rng.to_numpy(), day_bars.typ.to_numpy()
    t = n - 1
    # the day's extreme so far, and the bar that set it (latest one if tied)
    j = int(np.flatnonzero(hi == hi.max())[-1])
    k = t - j
    if j < 1 or not (1 <= k <= p["max_bars"]):
        return None
    a = typ[j]
    if not a > 0:
        return None
    start = lo[max(0, j - 8): j + 1].min()
    leg = hi[j] - start
    if leg < p["min_leg"] * a or lo[j + 1:].min() <= start:
        return None
    depth = (hi[j] - cl[t]) / leg
    if k <= p["sharp_bars"] and depth >= p["sharp_depth"]:
        return "sharp"
    if (k >= p["flat_bars"] and p["flat_min"] <= depth <= p["flat_max"]
            and rng[j + 1:].max() <= p["flat_bar_cap"] * a):
        return "flat"
    return None


def label_trades(trades: pd.DataFrame, bars: pd.DataFrame, p: dict) -> pd.Series:
    by_day = {d: g.reset_index(drop=True) for d, g in bars.groupby("day")}
    out = []
    for r in trades.itertuples():
        g = by_day.get(r.entry_time.normalize())
        if g is None:
            out.append("no_bars")
            continue
        closed = g[g.closes_at <= r.entry_time]
        with_s, against_s = pullback_state(closed, r.dir, p), pullback_state(closed, -r.dir, p)
        out.append(f"{with_s}_with" if with_s else f"{against_s}_against" if against_s else "none")
    return pd.Series(out, index=trades.index)


def add_state(t: pd.DataFrame) -> pd.Series:
    """Outcome of the most recent trade closed before this entry, same day."""
    st = []
    for i, r in t.iterrows():
        prev = t[(t.exit_time <= r.entry_time) & (t.exit_time.dt.normalize() == r.entry_time.normalize())
                 & (t.index != i)]
        st.append("first" if prev.empty else
                  "after_win" if prev.sort_values("exit_time").pnl.iloc[-1] > 0 else "after_loss")
    return pd.Series(st, index=t.index)


# ----------------------------------------------------------------- report

def summarise(g: pd.DataFrame) -> pd.Series:
    return pd.Series({"trades": len(g), "net_rs": g.pnl.sum(), "avg_rs": g.pnl.mean(),
                      "win_pct": (g.pnl > 0).mean() * 100, "median_hold_min": g.hold_min.median(),
                      "sessions": g.entry_time.dt.normalize().nunique()})


def win_rate_gap(a: pd.Series, b: pd.Series, n_boot: int = 4000, seed: int = 7):
    """Bootstrap 95% interval for win% of a minus win% of b."""
    if len(a) < 5 or len(b) < 5:
        return None
    rng = np.random.default_rng(seed)
    wa, wb = (a > 0).to_numpy(), (b > 0).to_numpy()
    d = [rng.choice(wa, len(wa)).mean() - rng.choice(wb, len(wb)).mean() for _ in range(n_boot)]
    return (wa.mean() - wb.mean()) * 100, np.percentile(d, 2.5) * 100, np.percentile(d, 97.5) * 100


def report(t: pd.DataFrame, title: str) -> None:
    pd.set_option("display.width", 160)
    fmt = {k: v.format for k, v in {"trades": "{:.0f}", "net_rs": "{:,.0f}", "avg_rs": "{:,.0f}",
                                    "win_pct": "{:.1f}", "median_hold_min": "{:.1f}",
                                    "sessions": "{:.0f}"}.items()}
    print(f"\n=== {title}")
    tab = t.groupby("label").apply(summarise, include_groups=False).reindex(LABELS).dropna(how="all")
    print(tab.to_string(formatters=fmt))
    print("\nby state (the comparison that matters):")
    tab = (t.groupby(["state", "label"]).apply(summarise, include_groups=False)
             .reindex(pd.MultiIndex.from_product([STATES, LABELS])).dropna(how="all"))
    print(tab.to_string(formatters=fmt))
    print("\nflat_with vs none, same state (win% gap, bootstrap 95% interval):")
    for s in STATES:
        a = t[(t.state == s) & (t.label == "flat_with")].pnl
        b = t[(t.state == s) & (t.label == "none")].pnl
        gap = win_rate_gap(a, b)
        if gap is None:
            print(f"  {s:<11} too few trades (flat_with {len(a)}, none {len(b)})")
        else:
            verdict = "beats none" if gap[1] > 0 else "worse than none" if gap[2] < 0 else "no clear difference"
            print(f"  {s:<11} {gap[0]:+5.1f} pts  [{gap[1]:+.1f}, {gap[2]:+.1f}]  {verdict}"
                  f"  (n {len(a)} vs {len(b)})")
    fw = t[t.label == "flat_with"]
    if len(fw) < 30:
        print(f"\nWARNING: only {len(fw)} flat_with trades. The flat state is present at well under 1% "
              "of bar closes, so a few hundred trades yield a handful. Treat any number above as "
              "anecdote, not evidence.")
    if len(fw):
        per_day = fw.groupby(fw.entry_time.dt.normalize()).pnl.sum()
        print(f"\nflat_with sessions net positive: {(per_day > 0).sum()} of {len(per_day)}")


DEFAULT = dict(min_leg=4.0, max_bars=6, sharp_bars=2, sharp_depth=0.5,
               flat_bars=4, flat_min=0.10, flat_max=0.38, flat_bar_cap=1.2)
GRID = {
    "looser flat (3+ bars, up to 50%)": dict(flat_bars=3, flat_max=0.50),
    "stricter flat (5+ bars, up to 30%)": dict(flat_bars=5, flat_max=0.30),
    "smaller legs (3x typical)": dict(min_leg=3.0),
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trades", required=True, help="Kite tradebook CSV or round-trips CSV")
    ap.add_argument("--bars", required=True, help="NIFTY index bars CSV, 15-min or finer, IST")
    ap.add_argument("--out", help="write every round trip with its label and state to this CSV")
    ap.add_argument("--grid", action="store_true", help="also rerun with nearby definitions")
    args = ap.parse_args()

    bars = load_bars(args.bars)
    trades = load_trades(args.trades)
    print(f"{len(trades)} round trips over {trades.entry_time.dt.normalize().nunique()} sessions, "
          f"net Rs {trades.pnl.sum():,.0f} (gross of charges)")
    print(f"{len(bars)} fifteen-minute bars, {bars.day.min():%Y-%m-%d} to {bars.day.max():%Y-%m-%d}")

    trades["state"] = add_state(trades)
    trades["label"] = label_trades(trades, bars, DEFAULT)
    nob = (trades.label == "no_bars").sum()
    if nob:
        print(f"note: {nob} trades on days with no bars; excluded")
    labelled = trades[trades.label != "no_bars"]
    if labelled.empty:
        sys.exit("No trade falls on a day covered by --bars. Check the bars' date range and that both "
                 "files are in IST.")
    report(labelled, "default definitions (same as the Nifty 2011-23 index test)")

    if args.grid:
        for name, over in GRID.items():
            t = labelled.copy()
            t["label"] = label_trades(t, bars, {**DEFAULT, **over})
            report(t, f"variant: {name}")

    if args.out:
        trades.to_csv(args.out, index=False)
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
