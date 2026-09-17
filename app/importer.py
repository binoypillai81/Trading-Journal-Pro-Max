"""Trade CSV import: upload → column mapping → staged preview → confirmation.

One CSV row = one round-trip trade (entry and exit on the same row).
No column names are assumed: the user maps columns to internal fields. A
heuristic *suggestion* is offered but never applied without the user saving it.
"""
from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

from . import chronology
from .db import db_path, get_settings, log_event, now_iso, tx
from .timeutil import parse_timestamp, to_local, valid_timezone


PREVIEW_LIMIT = 1500

TRADE_FIELDS = {
    "entry_datetime": "Entry date+time (single column)",
    "entry_date": "Entry date (separate column)",
    "entry_time": "Entry time (separate column)",
    "exit_datetime": "Exit date+time (single column)",
    "exit_date": "Exit date (separate column)",
    "exit_time": "Exit time (separate column)",
    "timezone": "Timezone (per-row column)",
    "instrument": "Instrument / symbol",
    "trade_ref": "Trade ID",
    "exec_seq": "Execution sequence",
    "entry_price": "Entry price",
    "exit_price": "Exit price",
    "direction": "Direction (Buy/Sell, Long/Short)",
    "quantity": "Quantity",
    "pnl": "P&L",
    "underlying": "Underlying / chart symbol (e.g. NIFTY)",
    "option_type": "Option type (CE/PE)",
}

FILL_MODE_LABELS = {
    "entry_datetime": "Fill date+time (single column)",
    "entry_date": "Fill date (separate column)",
    "entry_time": "Fill time (separate column)",
    "entry_price": "Fill price",
    "direction": "Side (Buy/Sell)",
    "quantity": "Fill quantity",
    "trade_ref": "Order / trade ID",
}

_SUGGEST = [
    ("entry_datetime", r"^(entry[\s_]*(date[\s_]*time|datetime|timestamp|time[\s_]*stamp)|open[\s_]*time|timestamp|datetime)$"),
    ("entry_date", r"^(entry[\s_]*date|trade[\s_]*date|date)$"),
    ("entry_time", r"^(entry[\s_]*time|time)$"),
    ("exit_datetime", r"^(exit[\s_]*(date[\s_]*time|datetime|timestamp)|close[\s_]*time)$"),
    ("exit_date", r"^exit[\s_]*date$"),
    ("exit_time", r"^exit[\s_]*time$"),
    ("timezone", r"^(tz|time[\s_]*zone)$"),
    ("instrument", r"^(instrument|symbol|tradingsymbol|trading[\s_]*symbol|scrip|contract)$"),
    ("trade_ref", r"^(trade[\s_]*id|id|order[\s_]*id|ref)$"),
    ("exec_seq", r"^(exec(ution)?[\s_]*(seq(uence)?|no)|seq(uence)?)$"),
    ("entry_price", r"^(entry|entry[\s_]*price|buy[\s_]*price|avg[\s_]*entry|open[\s_]*price)$"),
    ("exit_price", r"^(exit|exit[\s_]*price|sell[\s_]*price|avg[\s_]*exit|close[\s_]*price)$"),
    ("direction", r"^(buy/sell|side|direction|type|trade[\s_]*type|b/s|long/short)$"),
    ("quantity", r"^(qty|quantity|lots?|size)$"),
    ("pnl", r"^(p&l|pnl|p/l|profit|net[\s_]*p&l|realized[\s_]*p&l|profit/loss)$"),
]


def suggest_mapping(headers: list[str]) -> dict:
    out, used = {}, set()
    for field, pattern in _SUGGEST:
        for h in headers:
            if h in used:
                continue
            if re.match(pattern, h.strip().lower()):
                out[field] = h
                used.add(h)
                break
    for side in ("entry", "exit"):
        if f"{side}_datetime" in out and (f"{side}_date" in out or f"{side}_time" in out):
            out.pop(f"{side}_date", None)
            out.pop(f"{side}_time", None)
        elif f"{side}_time" in out and f"{side}_date" not in out:
            # a lone "Entry Time" column usually holds the full date+time
            out[f"{side}_datetime"] = out.pop(f"{side}_time")
    return out


def suggest_row_mode(headers: list[str]) -> str:
    """Broker tradebooks have one price + a side column and no exit columns."""
    low = {h.strip().lower() for h in headers}
    has_exit = any("exit" in h or h in ("p&l", "pnl") for h in low)
    has_fill = bool(low & {"price", "average_price", "fill_price", "trade_price"}) and bool(low & {"trade_type", "side", "transaction_type", "buy/sell"})
    return "fills" if has_fill and not has_exit else "round_trip"


def _read_csv(text: str) -> tuple[list[str], list[dict]]:
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    headers = [h.strip() for h in (reader.fieldnames or [])]
    rows = []
    for r in reader:
        rows.append({(k or "").strip(): (v.strip() if isinstance(v, str) else v) for k, v in r.items()})
    return headers, rows


def _num(v):
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace("₹", "")
    if s == "" or s.lower() in ("nan", "none", "null", "-"):
        return None
    if re.fullmatch(r"\(.*\)", s):
        s = "-" + s[1:-1]
    try:
        return float(s)
    except ValueError:
        return None


def parse_direction(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("buy", "b", "long", "l", "bot", "bought", "buy to open"):
        return "LONG"
    if s in ("sell", "s", "short", "sld", "sold", "sell short", "sell to open"):
        return "SHORT"
    return None


def option_type(instrument: str | None) -> str | None:
    if not instrument:
        return None
    s = instrument.upper()
    if re.search(r"(\d|\s)(CE|CALL)\b|CE$", s):
        return "CE"
    if re.search(r"(\d|\s)(PE|PUT)\b|PE$", s):
        return "PE"
    return None


def chart_instrument_for(instrument: str | None, default: str, known: list[str]) -> str:
    if instrument:
        s = instrument.upper().replace(" ", "")
        for k in sorted(known, key=len, reverse=True):
            if s.startswith(k.upper().replace(" ", "")):
                return k
    return default


def create_upload(conn, filename: str, content: bytes) -> dict:
    text = content.decode("utf-8-sig", errors="replace")
    headers, rows = _read_csv(text)
    if not headers:
        raise ValueError("The file has no header row")
    upload_dir = db_path().parent / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO import_batches(kind, filename, created_at, status, headers_json) VALUES ('trades',?,?,?,?)",
            (filename, now_iso(), "UPLOADED", json.dumps(headers)),
        )
        bid = cur.lastrowid
        path = upload_dir / f"trades_batch_{bid}.csv"
        path.write_text(text, encoding="utf-8")
        conn.execute("UPDATE import_batches SET raw_csv_path=? WHERE id=?", (str(path), bid))
        log_event(conn, "TRADE_CSV_UPLOADED", batch_id=bid, filename=filename, rows=len(rows))
    # Raw sample rows can contain exits and P&L, so the mapping screen gets masked column
    # profiles by default; raw rows are only returned by sample_rows() on explicit request.
    return {"batch_id": bid, "headers": headers, "row_count": len(rows),
            "column_profiles": column_profiles(headers, rows), "suggested_mapping": suggest_mapping(headers),
            "suggested_row_mode": suggest_row_mode(headers),
            "fields": TRADE_FIELDS}


def column_profiles(headers: list[str], rows: list[dict]) -> dict:
    """Describe each column without exposing outcome values: numbers and dates become shape patterns."""
    out = {}
    for h in headers:
        vals = [r.get(h) for r in rows if (r.get(h) or "").strip()][:25]
        if not vals:
            out[h] = {"kind": "empty", "examples": []}
        elif all(_num(v) is not None for v in vals):
            out[h] = {"kind": "number", "examples": sorted({re.sub(r"\d", "9", v) for v in vals})[:3]}
        elif all(re.search(r"\d{1,4}[-/.:]\d{1,2}", v) for v in vals):
            out[h] = {"kind": "date/time", "examples": sorted({re.sub(r"\d", "9", v) for v in vals})[:3]}
        else:
            distinct = sorted(set(vals))
            out[h] = {"kind": "text", "examples": distinct[:4] if len(distinct) <= 12 else [re.sub(r"\d", "9", v) for v in distinct[:3]]}
    return out


def sample_rows(conn, batch_id: int, n: int = 5) -> list[dict]:
    batch = conn.execute("SELECT raw_csv_path FROM import_batches WHERE id=? AND kind='trades'", (batch_id,)).fetchone()
    if not batch:
        raise LookupError("Import batch not found")
    _, rows = _read_csv(Path(batch["raw_csv_path"]).read_text(encoding="utf-8"))
    log_event(conn, "RAW_SAMPLE_ROWS_VIEWED", batch_id=batch_id, rows=n)
    return rows[:n]


def _opt(v):
    s = (v or "").strip().upper()
    return {"CE": "CE", "CALL": "CE", "C": "CE", "PE": "PE", "PUT": "PE", "P": "PE"}.get(s)


CONTRACT_FIRST_SELL = "contract_first_sell"


def _pair_fills(numbered_rows, col, mapping, naive_tz, date_order, exch_tz, default_chart, known,
                drop_first_sell: set | frozenset = frozenset()):
    """Rebuild round-trip trades from individual fills.

    Per instrument (exact symbol), fills are walked in time order (ties: file order). A trade
    opens when the net position leaves zero and closes when it returns to zero. Adds and partial
    exits stay inside the same trade. A fill that crosses zero closes the trade and opens a new
    one in the opposite direction with the remainder.
      entry price          = quantity-weighted average of ALL opening fills (used after reveal)
      initial entry price  = the first order only (same order ID, or same second) — what was known at entry
      exit price           = quantity-weighted average of closing fills
      P&L                  = realised (sell value − buy value), before charges
      re-add episodes      = times the position was added to again after a partial exit ("multi-entry")
    Positions never flattened are returned with ``_open`` details so the caller can settle them at expiry.
    ``drop_first_sell``: symbols whose first fill is a sell closing a position opened before the file
    started; that fill is excluded from pairing (recorded, not used).
    """
    needs_tz = ambiguous = False
    fills, bad = [], []
    for i, row in numbered_rows:
        if not any((v or "").strip() for v in row.values() if isinstance(v, str)):
            continue
        tzv = col(row, "timezone")
        if mapping.get("entry_datetime"):
            ep = parse_timestamp(col(row, "entry_datetime"), naive_tz=naive_tz, tz_value=tzv, date_order=date_order)
            original = col(row, "entry_datetime")
        else:
            ep = parse_timestamp(col(row, "entry_date"), time_value=col(row, "entry_time"), naive_tz=naive_tz, tz_value=tzv, date_order=date_order)
            original = " ".join(x for x in (col(row, "entry_date"), col(row, "entry_time")) if x)
        needs_tz = needs_tz or bool(ep.issue and "no timezone" in ep.issue)
        ambiguous = ambiguous or ep.ambiguous_date
        f = {"row": i, "raw": row, "epoch": ep.epoch, "source": ep.source, "issue": ep.issue, "original": original,
             "side": parse_direction(col(row, "direction")), "qty": _num(col(row, "quantity")), "price": _num(col(row, "entry_price")),
             "instrument": (col(row, "instrument") or "").strip(), "ref": col(row, "trade_ref"),
             "underlying": (col(row, "underlying") or "").strip().upper(), "opt": _opt(col(row, "option_type"))}
        if ep.epoch is None or not f["side"] or not f["qty"] or f["price"] is None or not f["instrument"]:
            why = ep.issue or ("Unrecognised side" if not f["side"] else "Missing quantity/price/instrument")
            bad.append((f, why))
        else:
            fills.append(f)

    staged, first_side, dropped = [], {}, []

    def emit(tr, open_at_end=False):
        o, c = tr["open"], tr["close"]
        oq = sum(q for _, q in o)
        cq = sum(q for _, q in c)
        entry_px = sum(f["price"] * q for f, q in o) / oq
        exit_px = sum(f["price"] * q for f, q in c) / cq if cq else None
        sign = 1 if tr["dir"] == "LONG" else -1
        pnl = (exit_px - entry_px) * cq * sign if cq else None
        first, last = o[0][0], (c[-1][0] if c else None)
        initial = [(f, q) for f, q in o if (first["ref"] and f["ref"] == first["ref"]) or f["epoch"] == first["epoch"]]
        iq = sum(q for _, q in initial)
        initial_px = sum(f["price"] * q for f, q in initial) / iq
        re_adds, seen_close = 0, False
        for kind in tr["events"]:
            if kind == "close":
                seen_close = True
            elif seen_close:
                re_adds += 1
                seen_close = False
        warnings = []
        if len(o) > 1:
            warnings.append(f"Built from {len(o)} opening fills (scaled in)")
        if len(c) > 1:
            warnings.append(f"Closed in {len(c)} fills (partial exits)")
        if re_adds:
            warnings.append(f"Multi-entry: added to the position {re_adds} time(s) after a partial exit")
        if last and not open_at_end and to_local(first["epoch"], exch_tz)[:10] != to_local(last["epoch"], exch_tz)[:10]:
            warnings.append("Held overnight")
        rec = {
            "import_row": first["row"],
            "raw_row_json": json.dumps({"fills": [dict(fl["raw"], _row=fl["row"], _qty_used=q) for fl, q in o + c]}),
            "trade_ref": first["ref"],
            "instrument": first["instrument"],
            "chart_instrument": first["underlying"] or chart_instrument_for(first["instrument"], default_chart, known),
            "option_type": first["opt"] or option_type(first["instrument"]),
            "entry_original": first["original"],
            "entry_epoch": first["epoch"],
            "entry_local": to_local(first["epoch"], exch_tz),
            "entry_tz_source": first["source"],
            "exit_original": last["original"] if last and not open_at_end else None,
            "exit_epoch": last["epoch"] if last and not open_at_end else None,
            "exit_local": to_local(last["epoch"], exch_tz) if last and not open_at_end else None,
            "exit_tz_source": last["source"] if last and not open_at_end else None,
            "exec_seq": None,
            "entry_price": round(entry_px, 4),
            "exit_price": round(exit_px, 4) if exit_px is not None and not open_at_end else None,
            "direction": tr["dir"],
            "quantity": oq,
            "pnl": round(pnl, 2) if pnl is not None and not open_at_end else None,
            "chrono_status": "OK",
            "chrono_issue": None,
            "warnings_json": json.dumps(warnings),
            "initial_entry_price": round(initial_px, 4),
            "initial_quantity": iq,
            "entry_fills": len(o),
            "re_add_episodes": re_adds,
            "exit_kind": "fill" if not open_at_end else None,
            "settlement_json": None,
            "issue_kind": None,
        }
        if open_at_end:
            rec["_open"] = {"opened_qty": oq, "closed_qty": cq, "closed_value": sum(f["price"] * q for f, q in c),
                            "entry_avg": entry_px, "last_fill_epoch": max(f["epoch"] for f, _ in o + c)}
        staged.append(rec)

    by_inst = {}
    for f in fills:
        by_inst.setdefault(f["instrument"], []).append(f)
    for inst, inst_fills in by_inst.items():
        inst_fills.sort(key=lambda f: (f["epoch"], f["row"]))
        first_side[inst] = inst_fills[0]["side"]
        if inst in drop_first_sell and inst_fills[0]["side"] == "SHORT":
            dropped.append(inst_fills.pop(0))
        pos, tr = 0.0, None
        for f in inst_fills:
            signed_q = f["qty"] if f["side"] == "LONG" else -f["qty"]
            remaining = signed_q
            while abs(remaining) > 1e-9:
                if tr is None:
                    tr = {"dir": "LONG" if remaining > 0 else "SHORT", "open": [], "close": [], "events": ["open"]}
                    tr["open"].append((f, abs(remaining)))
                    pos, remaining = remaining, 0.0
                elif (pos > 0) == (remaining > 0):
                    tr["open"].append((f, abs(remaining)))
                    tr["events"].append("open")
                    pos, remaining = pos + remaining, 0.0
                else:
                    closing = min(abs(remaining), abs(pos))
                    tr["close"].append((f, closing))
                    tr["events"].append("close")
                    pos += closing if pos < 0 else -closing
                    remaining += closing if remaining < 0 else -closing
                    if abs(pos) < 1e-9:
                        emit(tr)
                        tr, pos = None, 0.0
        if tr is not None:
            emit(tr, open_at_end=True)

    for f, why in bad:
        staged.append(_blank_trade(
            import_row=f["row"], raw_row_json=json.dumps({"fills": [f["raw"]]}), trade_ref=f["ref"],
            instrument=f["instrument"] or default_chart, chart_instrument=f["underlying"] or default_chart,
            option_type=f["opt"], entry_original=f["original"], entry_price=f["price"], direction=f["side"],
            quantity=f["qty"], chrono_status="UNRESOLVED", chrono_issue=f"Fill excluded from round-trip pairing: {why}",
            issue_kind="fill_excluded"))
    for rec in staged:
        rec["_first_side"] = first_side.get(rec["instrument"])
    return staged, needs_tz, ambiguous, dropped


TRADE_COLUMNS = ["import_row", "raw_row_json", "trade_ref", "instrument", "chart_instrument", "option_type",
                 "entry_original", "entry_epoch", "entry_local", "entry_tz_source", "exit_original", "exit_epoch",
                 "exit_local", "exit_tz_source", "exec_seq", "entry_price", "exit_price", "direction", "quantity", "pnl",
                 "chrono_status", "chrono_issue", "warnings_json", "initial_entry_price", "initial_quantity",
                 "entry_fills", "re_add_episodes", "exit_kind", "settlement_json", "issue_kind"]


def _blank_trade(**kw) -> dict:
    rec = {c: None for c in TRADE_COLUMNS}
    rec.update({"warnings_json": "[]", "re_add_episodes": 0, "entry_fills": 1})
    rec.update(kw)
    return rec


def _finish_fill_trades(conn, staged: list[dict], exch_tz: str, confirmed_real_short: set, drop_first_sell: set) -> None:
    """Settle positions still open at the end of the file at expiry, and flag contracts that start with a sell."""
    from . import expiry
    for rec in staged:
        op = rec.pop("_open", None)
        if op:
            res = expiry.settle_open_position(conn, instrument=rec["instrument"], chart_instrument=rec["chart_instrument"],
                                              last_fill_epoch=op["last_fill_epoch"], tz=exch_tz)
            warnings = json.loads(rec["warnings_json"])
            if isinstance(res, dict):
                remaining = op["opened_qty"] - op["closed_qty"]
                exit_value = op["closed_value"] + res["settlement_price"] * remaining
                exit_avg = exit_value / op["opened_qty"]
                sign = 1 if rec["direction"] == "LONG" else -1
                rec.update({
                    "exit_epoch": res["exit_epoch"], "exit_local": to_local(res["exit_epoch"], exch_tz),
                    "exit_original": f"expiry settlement {res['expiry_date']}", "exit_tz_source": "expiry",
                    "exit_price": round(exit_avg, 4),
                    "pnl": round((exit_avg - op["entry_avg"]) * op["opened_qty"] * sign, 2),
                    "exit_kind": "expiry_settlement",
                    "settlement_json": json.dumps({**res, "settled_quantity": remaining,
                                                   "closed_by_fills_quantity": op["closed_qty"]}),
                })
                approx = "" if res["underlying_value_source"].startswith("official") else ", approximate"
                warnings.append(f"Settled at expiry {res['expiry_date']}: {remaining:g} held to expiry at "
                                f"{res['settlement_price']:g} ({res['type']} intrinsic value; {res['underlying']} "
                                f"close {res['underlying_value']:g}{approx})")
            else:
                warnings.append(f"Position never returned to zero and was not settled: {res} — no exit")
            rec["warnings_json"] = json.dumps(warnings)
        first = rec.pop("_first_side", None)
        if (first == "SHORT" and rec["chrono_status"] == "OK" and rec["instrument"] not in confirmed_real_short
                and rec["instrument"] not in drop_first_sell):
            rec["chrono_status"] = "UNRESOLVED"
            rec["issue_kind"] = CONTRACT_FIRST_SELL
            rec["chrono_issue"] = (f"{rec['instrument']}: the contract's first fill in the file is a SELL. Either this was a "
                                   "real short, or the opening BUY is missing from the file (position opened before the "
                                   "file starts). Confirm which before these trades enter the sequence.")


def stage(conn, batch_id: int, mapping: dict, options: dict) -> dict:
    batch = conn.execute("SELECT * FROM import_batches WHERE id=? AND kind='trades'", (batch_id,)).fetchone()
    if not batch:
        raise LookupError("Import batch not found")
    if batch["status"] in ("CONFIRMED", "CANCELLED"):
        raise PermissionError(f"Batch is already {batch['status']}")
    headers, rows = _read_csv(Path(batch["raw_csv_path"]).read_text(encoding="utf-8"))
    mapping = {k: v for k, v in (mapping or {}).items() if v}
    unknown = [v for v in mapping.values() if v not in headers]
    if unknown:
        raise ValueError(f"Mapped columns not in file: {unknown}")
    if unknown_fields := [k for k in mapping if k not in TRADE_FIELDS]:
        raise ValueError(f"Unknown fields: {unknown_fields}")
    row_mode = options.get("row_mode", "round_trip")
    if row_mode not in ("round_trip", "fills"):
        raise ValueError("row_mode must be round_trip or fills")
    if not (mapping.get("entry_datetime") or mapping.get("entry_date")):
        raise ValueError("Map the entry timestamp: either 'Entry date+time' or 'Entry date' (+ 'Entry time')"
                         if row_mode == "round_trip" else "Map the fill timestamp column")
    if row_mode == "fills":
        missing = [FILL_MODE_LABELS[f] for f in ("direction", "quantity", "entry_price") if not mapping.get(f)]
        if not mapping.get("instrument"):
            missing.append("Instrument / symbol")
        if missing:
            raise ValueError("Fills mode needs: " + ", ".join(missing))

    settings = get_settings(conn)
    exch_tz = settings["exchange_timezone"]
    naive_tz = options.get("naive_timezone") or None
    if naive_tz and not valid_timezone(naive_tz):
        raise ValueError(f"Unknown timezone '{naive_tz}'")
    date_order = options.get("date_order", "auto")
    default_chart = (options.get("chart_instrument") or "NIFTY").strip()
    known = [r["instrument"] for r in conn.execute("SELECT DISTINCT instrument FROM market_bars")] or []
    known = sorted(set(known + [default_chart, "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"]))

    def col(row, field):
        h = mapping.get(field)
        return row.get(h) if h else None

    staged = []
    needs_tz = ambiguous = False
    if row_mode == "fills":
        drop = set(options.get("missing_opening_buy") or [])
        real_short = set(options.get("confirmed_real_short") or [])
        staged, needs_tz, ambiguous, _ = _pair_fills(list(enumerate(rows, start=1)), col, mapping, naive_tz, date_order,
                                                     exch_tz, default_chart, known, drop_first_sell=drop)
        _finish_fill_trades(conn, staged, exch_tz, real_short, drop)
    for i, row in enumerate(rows if row_mode == "round_trip" else [], start=1):
        if not any((v or "").strip() for v in row.values() if isinstance(v, str)):
            continue  # blank line
        tzv = col(row, "timezone")
        if mapping.get("entry_datetime"):
            ep = parse_timestamp(col(row, "entry_datetime"), naive_tz=naive_tz, tz_value=tzv, date_order=date_order)
        else:
            ep = parse_timestamp(col(row, "entry_date"), time_value=col(row, "entry_time"),
                                 naive_tz=naive_tz, tz_value=tzv, date_order=date_order)
        if mapping.get("exit_datetime"):
            xp = parse_timestamp(col(row, "exit_datetime"), naive_tz=naive_tz, tz_value=tzv, date_order=date_order)
        elif mapping.get("exit_date") or mapping.get("exit_time"):
            # an exit time with no exit date inherits the entry date
            xd = col(row, "exit_date") or (col(row, "entry_date") if not mapping.get("exit_date") else None)
            xp = parse_timestamp(xd, time_value=col(row, "exit_time"), naive_tz=naive_tz, tz_value=tzv, date_order=date_order)
        else:
            xp = None

        if ep.issue and "no timezone" in ep.issue:
            needs_tz = True
        ambiguous = ambiguous or ep.ambiguous_date or bool(xp and xp.ambiguous_date)
        instrument = col(row, "instrument") or options.get("default_instrument") or default_chart
        warnings = [w for w in (ep.warning, xp.warning if xp else None) if w]
        entry_price, exit_price = _num(col(row, "entry_price")), _num(col(row, "exit_price"))
        if entry_price is None:
            warnings.append("Entry price missing")
        if xp is not None and xp.epoch is None and xp.issue:
            warnings.append(f"Exit: {xp.issue}")
        if ep.epoch and xp and xp.epoch and xp.epoch < ep.epoch:
            warnings.append("Exit is before entry — check timestamps")
        raw_dir = col(row, "direction")
        direction = parse_direction(raw_dir)
        if raw_dir and not direction:
            warnings.append(f"Unrecognised direction '{raw_dir}'")
        exec_seq = _num(col(row, "exec_seq"))
        entry_original = col(row, "entry_datetime") if mapping.get("entry_datetime") else \
            " ".join(x for x in (col(row, "entry_date"), col(row, "entry_time")) if x)
        exit_original = col(row, "exit_datetime") if mapping.get("exit_datetime") else \
            " ".join(x for x in (col(row, "exit_date"), col(row, "exit_time")) if x) or None
        if tzv:
            entry_original = f"{entry_original} [tz column: {tzv}]"
        staged.append({
            "import_row": i,
            "raw_row_json": json.dumps(row),
            "trade_ref": col(row, "trade_ref"),
            "instrument": instrument,
            "chart_instrument": (col(row, "underlying") or "").strip().upper() or chart_instrument_for(instrument, default_chart, known),
            "option_type": _opt(col(row, "option_type")) or option_type(instrument),
            "entry_original": entry_original,
            "entry_epoch": ep.epoch,
            "entry_local": to_local(ep.epoch, exch_tz),
            "entry_tz_source": ep.source,
            "exit_original": exit_original,
            "exit_epoch": xp.epoch if xp else None,
            "exit_local": to_local(xp.epoch, exch_tz) if xp else None,
            "exit_tz_source": xp.source if xp else None,
            "exec_seq": int(exec_seq) if exec_seq is not None else None,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "direction": direction,
            "quantity": _num(col(row, "quantity")),
            "pnl": _num(col(row, "pnl")),
            "chrono_status": "OK" if ep.epoch is not None else "UNRESOLVED",
            "chrono_issue": ep.issue,
            "initial_entry_price": entry_price,
            "initial_quantity": _num(col(row, "quantity")),
            "entry_fills": 1,
            "re_add_episodes": 0,
            "exit_kind": "fill" if xp and xp.epoch else None,
            "settlement_json": None,
            "issue_kind": "timestamp" if ep.epoch is None else None,
            "warnings_json": json.dumps(warnings),
        })

    blocking = []
    if needs_tz:
        blocking.append({"code": "timezone_required",
                         "message": "Some timestamps have no timezone. Choose the timezone these times were recorded in."})
    if ambiguous and date_order == "auto":
        blocking.append({"code": "date_order_required",
                         "message": "Some dates are ambiguous (e.g. 03/04/2026 could be 3 April or 4 March). Choose the date format."})

    ts = now_iso()
    with tx(conn):
        conn.execute("DELETE FROM trades WHERE batch_id=?", (batch_id,))
        _insert_trades(conn, staged, batch_id, ts)
        conn.execute(
            "UPDATE import_batches SET status='STAGED', mapping_json=?, options_json=?, summary_json=? WHERE id=?",
            (json.dumps(mapping), json.dumps(options), json.dumps({"blocking": blocking}), batch_id),
        )
        log_event(conn, "TRADE_IMPORT_STAGED", mapping=mapping, options=options, rows=len(staged))
    return preview(conn, batch_id)


def _insert_trades(conn, staged: list[dict], batch_id: int, ts: str) -> None:
    if not staged:
        return
    cols = TRADE_COLUMNS + ["batch_id", "created_at", "updated_at"]
    conn.executemany(f"INSERT INTO trades({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                     [[st.get(c) for c in TRADE_COLUMNS] + [batch_id, ts, ts] for st in staged])


def preview(conn, batch_id: int) -> dict:
    batch = conn.execute("SELECT * FROM import_batches WHERE id=?", (batch_id,)).fetchone()
    if not batch:
        raise LookupError("Import batch not found")
    summary = json.loads(batch["summary_json"] or "{}")
    staged = [dict(r) for r in conn.execute("SELECT * FROM trades WHERE batch_id=?", (batch_id,))]
    existing = [dict(r) for r in conn.execute(
        """SELECT t.id, t.entry_epoch, t.exec_seq, t.trade_ref, t.batch_id, t.import_row, t.entry_price,
                  t.direction, t.chart_instrument
           FROM trades t JOIN import_batches b ON b.id=t.batch_id
           WHERE b.status='CONFIRMED' AND t.chrono_status='OK' AND COALESCE(t.excluded, 0)=0""")]
    resolved = [t for t in staged if t["chrono_status"] == "OK"]
    combined = chronology.ordered(resolved + existing)
    dup = chronology.duplicate_epochs(combined)
    pos = {id(t): i for i, t in enumerate(combined, start=1)}
    existing_keys = {(e["entry_epoch"], e["entry_price"], e["direction"], e["chart_instrument"]): e["id"] for e in existing}

    rows = []
    for t in chronology.ordered(resolved):
        warnings = json.loads(t["warnings_json"] or "[]")
        status = "OK"
        if t["entry_epoch"] in dup:
            status = "DUPLICATE_TIMESTAMP"
            warnings.append("Same entry timestamp as another trade — tie-break: execution sequence → Trade ID → import order")
        k = (t["entry_epoch"], t["entry_price"], t["direction"], t["chart_instrument"])
        if k in existing_keys:
            warnings.append(f"Possible duplicate of already-imported trade (internal id {existing_keys[k]})")
        rows.append({
            "chronological_position": pos[id(t)],
            "trade_id": t["id"], "trade_ref": t["trade_ref"], "import_row": t["import_row"],
            "instrument": t["instrument"], "chart_instrument": t["chart_instrument"],
            "original_entry": t["entry_original"], "normalized_entry": t["entry_local"],
            "tz_source": t["entry_tz_source"], "entry_price": t["entry_price"],
            "direction": t["direction"], "exec_seq": t["exec_seq"],
            "status": status, "warnings": warnings,
        })
    unresolved = [{"trade_id": t["id"], "trade_ref": t["trade_ref"], "import_row": t["import_row"],
                   "instrument": t["instrument"], "original_entry": t["entry_original"], "normalized_entry": t["entry_local"],
                   "status": "REQUIRES_TIMESTAMP_REVIEW", "issue": t["chrono_issue"], "issue_kind": t["issue_kind"]}
                  for t in staged if t["chrono_status"] != "OK"]
    blocking = summary.get("blocking", [])
    status_counts = {}
    for r in rows:
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1
    total_rows = len(rows)
    if total_rows > PREVIEW_LIMIT:
        # keep flagged rows visible, then the earliest rows in order
        flagged = [r for r in rows if r["status"] != "OK" or r["warnings"]]
        head = [r for r in rows if r not in flagged][: max(0, PREVIEW_LIMIT - len(flagged))]
        rows = sorted((flagged + head)[:PREVIEW_LIMIT], key=lambda r: r["chronological_position"])
    earliest_existing = min((e["entry_epoch"] for e in existing), default=None)
    backfill = [r for r in resolved if earliest_existing is not None and r["entry_epoch"] < max(e["entry_epoch"] for e in existing)]
    return {
        "batch_id": batch_id, "status": batch["status"], "filename": batch["filename"],
        "mapping": json.loads(batch["mapping_json"] or "{}"), "options": json.loads(batch["options_json"] or "{}"),
        "exchange_timezone": get_settings(conn)["exchange_timezone"],
        "sort_method": chronology.SORT_METHOD,
        "existing_confirmed_trades": len(existing),
        "rows": rows, "total_rows": total_rows, "rows_truncated": total_rows > len(rows), "status_counts": status_counts,
        "row_mode": json.loads(batch["options_json"] or "{}").get("row_mode", "round_trip"),
        "unresolved": unresolved[:PREVIEW_LIMIT], "unresolved_total": len(unresolved), "blocking_issues": blocking,
        "interleaves_with_existing": len(backfill),
        "can_confirm": batch["status"] == "STAGED" and not blocking and total_rows + len(unresolved) > 0,
    }


def confirm(conn, batch_id: int) -> dict:
    p = preview(conn, batch_id)
    if p["status"] != "STAGED":
        raise PermissionError(f"Batch is {p['status']}, not STAGED")
    if p["blocking_issues"]:
        raise PermissionError("Resolve blocking issues first: " + "; ".join(b["message"] for b in p["blocking_issues"]))
    with tx(conn):
        conn.execute("UPDATE import_batches SET status='CONFIRMED', confirmed_at=?, sort_method=? WHERE id=?",
                     (now_iso(), chronology.SORT_METHOD, batch_id))
        chronology.resequence(conn)
        log_event(conn, "CHRONOLOGICAL_ORDER_CONFIRMED", batch_id=batch_id, trades=p["total_rows"],
                  unresolved=p["unresolved_total"], sort_method=chronology.SORT_METHOD, row_mode=p["row_mode"])
        if p["interleaves_with_existing"]:
            # Newly imported trades that fall before trades already in the sequence.
            for s in conn.execute("SELECT id FROM review_sessions WHERE status='ACTIVE'").fetchall():
                log_event(conn, "BACKFILL_INTO_EXISTING_SEQUENCE", session_id=s["id"], batch_id=batch_id,
                          count=p["interleaves_with_existing"])
    return {"confirmed": True, "trades": p["total_rows"], "unresolved": p["unresolved_total"]}


def cancel(conn, batch_id: int) -> None:
    with tx(conn):
        b = conn.execute("SELECT status FROM import_batches WHERE id=?", (batch_id,)).fetchone()
        if not b:
            raise LookupError("Import batch not found")
        if b["status"] == "CONFIRMED":
            raise PermissionError("A confirmed import cannot be cancelled")
        conn.execute("DELETE FROM trades WHERE batch_id=?", (batch_id,))
        conn.execute("UPDATE import_batches SET status='CANCELLED' WHERE id=?", (batch_id,))
        log_event(conn, "TRADE_IMPORT_CANCELLED", batch_id=batch_id)


def contract_evidence(signed_quantities: list[float]) -> dict:
    """Compare the two explanations for a contract whose first fill is a sell.

    A real short leaves the running position flat at the end; a missing opening buy means the file
    as-is ends short, while dropping the first sell ends flat. Positions held to expiry can end non-flat
    either way, so this is evidence, not proof.
    """
    def walk(seq):
        pos, flats = 0.0, 0
        for v in seq:
            pos += v
            flats += abs(pos) < 1e-9
        return pos, flats
    a_end, a_flats = walk(signed_quantities)
    b_end, b_flats = walk(signed_quantities[1:])
    if abs(a_end) < 1e-9 and abs(b_end) > 1e-9:
        suggestion = "real_short"
    elif abs(b_end) < 1e-9 and abs(a_end) > 1e-9:
        suggestion = "missing_opening_buy"
    else:
        suggestion = "unclear"
    return {"as_is_end_position": a_end, "as_is_times_flat": a_flats,
            "missing_buy_end_position": b_end, "missing_buy_times_flat": b_flats, "suggestion": suggestion}


def unresolved_trades(conn) -> list[dict]:
    # Only entry-side information: raw rows may contain exits/P&L and are not exposed here.
    rows = [dict(r) for r in conn.execute(
        """SELECT t.id AS trade_id, t.batch_id, t.trade_ref, t.import_row, t.instrument, t.entry_original, t.entry_local,
                  t.direction, t.chrono_issue, t.issue_kind
           FROM trades t JOIN import_batches b ON b.id=t.batch_id
           WHERE b.status='CONFIRMED' AND t.chrono_status!='OK' AND COALESCE(t.excluded, 0)=0
           ORDER BY t.batch_id, t.instrument, t.entry_epoch, t.import_row""")]
    contracts = {(r["batch_id"], r["instrument"]) for r in rows if r["issue_kind"] == CONTRACT_FIRST_SELL}
    evidence = {}
    for bid in {b for b, _ in contracts}:
        batch = conn.execute("SELECT raw_csv_path, mapping_json, options_json FROM import_batches WHERE id=?", (bid,)).fetchone()
        mapping, options = json.loads(batch["mapping_json"] or "{}"), json.loads(batch["options_json"] or "{}")
        _, csv_rows = _read_csv(Path(batch["raw_csv_path"]).read_text(encoding="utf-8"))
        wanted = {sym for b, sym in contracts if b == bid}
        per = {sym: [] for sym in wanted}
        naive = options.get("naive_timezone") or None
        for i, r in enumerate(csv_rows, start=1):
            sym = (r.get(mapping["instrument"]) or "").strip()
            if sym not in wanted:
                continue
            side, qty = parse_direction(r.get(mapping["direction"])), _num(r.get(mapping["quantity"]))
            ts = r.get(mapping["entry_datetime"]) if mapping.get("entry_datetime") else r.get(mapping.get("entry_date", ""))
            ep = parse_timestamp(ts, naive_tz=naive, date_order=options.get("date_order", "auto")).epoch
            if side and qty and ep is not None:
                per[sym].append((ep, i, qty if side == "LONG" else -qty))
        for sym, fl in per.items():
            evidence[(bid, sym)] = contract_evidence([q for _, _, q in sorted(fl)])
    for r in rows:
        if r["issue_kind"] == CONTRACT_FIRST_SELL:
            r["evidence"] = evidence.get((r["batch_id"], r["instrument"]))
    return rows


def resolve_contract(conn, batch_id: int, symbol: str, decision: str) -> dict:
    """Resolve a contract whose first fill is a sell.

    ``real_short``           the trades are kept exactly as paired and enter the sequence.
    ``missing_opening_buy``  the first sell closed a position opened before the file; it is excluded and
                             the contract's remaining fills are re-paired (and settled at expiry if needed).
    """
    if decision not in ("real_short", "missing_opening_buy"):
        raise ValueError("decision must be real_short or missing_opening_buy")
    batch = conn.execute("SELECT * FROM import_batches WHERE id=? AND kind='trades'", (batch_id,)).fetchone()
    if not batch:
        raise LookupError("Import batch not found")
    options = json.loads(batch["options_json"] or "{}")
    if options.get("row_mode") != "fills":
        raise PermissionError("Contract resolution applies to fills imports only")
    existing = conn.execute("SELECT id, issue_kind FROM trades WHERE batch_id=? AND instrument=?", (batch_id, symbol)).fetchall()
    if not any(r["issue_kind"] == CONTRACT_FIRST_SELL for r in existing):
        raise LookupError(f"{symbol} has no unresolved first-sell issue in this batch")
    reviewed = conn.execute(
        f"SELECT COUNT(*) FROM reviews WHERE trade_id IN ({','.join('?' * len(existing))}) AND status!='UNREVIEWED'",
        [r["id"] for r in existing]).fetchone()[0]
    if reviewed:
        raise PermissionError("Trades of this contract already have reviews and cannot be re-paired")
    mapping = json.loads(batch["mapping_json"] or "{}")
    key = "confirmed_real_short" if decision == "real_short" else "missing_opening_buy"
    options[key] = sorted(set(options.get(key) or []) | {symbol})
    exch_tz = get_settings(conn)["exchange_timezone"]
    _, rows = _read_csv(Path(batch["raw_csv_path"]).read_text(encoding="utf-8"))
    numbered = [(i, r) for i, r in enumerate(rows, start=1) if (r.get(mapping["instrument"]) or "").strip() == symbol]

    def col(row, field):
        h = mapping.get(field)
        return row.get(h) if h else None

    default_chart = (options.get("chart_instrument") or "NIFTY").strip()
    known = sorted({r["instrument"] for r in conn.execute("SELECT DISTINCT instrument FROM market_bars")} |
                   {default_chart, "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"})
    drop = set(options.get("missing_opening_buy") or [])
    staged, _, _, dropped = _pair_fills(numbered, col, mapping, options.get("naive_timezone") or None,
                                        options.get("date_order", "auto"), exch_tz, default_chart, known, drop_first_sell=drop)
    _finish_fill_trades(conn, staged, exch_tz, set(options.get("confirmed_real_short") or []), drop)
    ts = now_iso()
    with tx(conn):
        conn.execute("DELETE FROM trades WHERE batch_id=? AND instrument=?", (batch_id, symbol))
        _insert_trades(conn, staged, batch_id, ts)
        conn.execute("UPDATE import_batches SET options_json=? WHERE id=?", (json.dumps(options), batch_id))
        if batch["status"] == "CONFIRMED":
            chronology.resequence(conn)
        log_event(conn, "CONTRACT_RESOLVED", batch_id=batch_id, symbol=symbol, decision=decision,
                  excluded_fill_rows=[f["row"] for f in dropped], trades_before=len(existing), trades_after=len(staged))
    return {"symbol": symbol, "decision": decision, "trades": len(staged), "excluded_fill_rows": [f["row"] for f in dropped]}


def resolve_timestamp(conn, trade_id: int, local_value: str, tz_name: str, reason: str | None,
                      field: str = "entry") -> dict:
    if field not in ("entry", "exit"):
        raise ValueError("field must be entry or exit")
    if not valid_timezone(tz_name):
        raise ValueError(f"Unknown timezone '{tz_name}'")
    parsed = parse_timestamp(local_value, naive_tz=tz_name, date_order="auto")
    if parsed.epoch is None:
        raise ValueError(parsed.issue or "Could not parse timestamp")
    if parsed.ambiguous_date:
        raise ValueError("Ambiguous date — use YYYY-MM-DD HH:MM[:SS]")
    t = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
    if not t:
        raise LookupError("Trade not found")
    if (t["chrono_issue"] or "").startswith("Fill excluded"):
        raise PermissionError("This is a single fill excluded from round-trip pairing. Fix the fill in the CSV and re-import; "
                              "a lone fill cannot be placed as a trade.")
    if t["issue_kind"] == CONTRACT_FIRST_SELL:
        raise PermissionError("This contract starts with a sell. Resolve it as a real short or a missing opening buy instead.")
    if field == "entry":
        # A reviewed trade's entry must not be moved silently; corrections to reviewed trades are refused.
        reviewed = conn.execute("SELECT COUNT(*) FROM reviews WHERE trade_id=? AND status!='UNREVIEWED'", (trade_id,)).fetchone()[0]
        if reviewed:
            raise PermissionError("This trade already has a review; its entry timestamp cannot be changed")
    exch = get_settings(conn)["exchange_timezone"]
    with tx(conn):
        conn.execute(
            """INSERT INTO timestamp_corrections(trade_id, field, old_original, old_epoch, new_input, new_epoch, tz_used, reason, corrected_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (trade_id, field, t[f"{field}_original"], t[f"{field}_epoch"], local_value, parsed.epoch, tz_name, reason, now_iso()))
        if field == "entry":
            conn.execute(
                """UPDATE trades SET entry_epoch=?, entry_local=?, entry_tz_source=?, chrono_status='OK', chrono_issue=NULL, updated_at=?
                   WHERE id=?""", (parsed.epoch, to_local(parsed.epoch, exch), f"corrected:{tz_name}", now_iso(), trade_id))
        else:
            conn.execute("UPDATE trades SET exit_epoch=?, exit_local=?, exit_tz_source=?, updated_at=? WHERE id=?",
                         (parsed.epoch, to_local(parsed.epoch, exch), f"corrected:{tz_name}", now_iso(), trade_id))
        chronology.resequence(conn)
        log_event(conn, "TIMESTAMP_CORRECTED", trade_id=trade_id, field=field, new=to_local(parsed.epoch, exch), tz=tz_name, reason=reason)
    return {"trade_id": trade_id, "normalized": to_local(parsed.epoch, exch)}
