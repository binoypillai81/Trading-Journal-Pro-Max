"""Build sample data for trying the app.

Usage:
    .venv/bin/python sample_data/make_sample.py                        # synthetic bars (default)
    .venv/bin/python sample_data/make_sample.py --bars-15m A.csv --bars-1m B.csv --start 2026-01-05 --end 2026-01-16

With your own 15-minute and 1-minute CSVs (columns timestamp,open,high,low,close,volume), a date slice
of them is written; otherwise deterministic synthetic bars are generated. Sample trades are derived
from the 1-minute bars and written in shuffled order with mixed timezone notations, a duplicate entry
timestamp, a later-entered trade that exits earlier, and one row with a missing entry time.
These trades are SAMPLE DATA, not real trades.
"""
import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from app import samplegen  # noqa: E402


def slice_csv(src: Path, dst: Path, start: str, end: str):
    bars = []
    with src.open() as f, dst.open("w", newline="") as out:
        r = csv.reader(f)
        w = csv.writer(out)
        w.writerow(next(r))
        for row in r:
            if start <= row[0][:10] <= end:
                w.writerow(row)
                bars.append((datetime.strptime(row[0][:19], "%Y-%m-%d %H:%M:%S"), float(row[1]), float(row[2]), float(row[3]), float(row[4])))
    return bars


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars-15m", help="your 15-minute index CSV (optional)")
    ap.add_argument("--bars-1m", help="your 1-minute index CSV (optional)")
    ap.add_argument("--start", default="2026-01-05")
    ap.add_argument("--end", default="2026-01-16")
    ap.add_argument("--trades", type=int, default=30)
    a = ap.parse_args()
    if a.bars_15m and a.bars_1m:
        slice_csv(Path(a.bars_15m).expanduser(), HERE / "market_nifty_15m.csv", a.start, a.end)
        one = slice_csv(Path(a.bars_1m).expanduser(), HERE / "market_nifty_1m.csv", a.start, a.end)
        print(f"Sliced your bars {a.start}..{a.end}: {len(one)} 1-minute bars")
    else:
        one, fifteen = samplegen.synthetic_market(start_date=a.start, sessions=10)
        (HERE / "market_nifty_15m.csv").write_text(samplegen.bars_csv(fifteen))
        (HERE / "market_nifty_1m.csv").write_text(samplegen.bars_csv(one))
        print(f"Wrote synthetic bars: {len(one)} 1-minute, {len(fifteen)} 15-minute")
    text, truth = samplegen.sample_trades_csv(one, n=a.trades)
    (HERE / "sample_trades_random_order.csv").write_text(text)
    print(f"Wrote {len(truth)} sample trades (+1 with a missing timestamp) in shuffled order")


if __name__ == "__main__":
    main()
