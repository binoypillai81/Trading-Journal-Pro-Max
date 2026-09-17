"""The 18 critical acceptance tests from the specification (§47), plus workflow checks."""
import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import market as mkt
from app import samplegen
from conftest import (MAPPING_M, complete_current, dumps, good_post, good_thesis, import_trades, load_market, new_session,
                      simple_trades_csv)

IST = ZoneInfo("Asia/Kolkata")


def ep(local):
    return int(datetime.strptime(local, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST).timestamp())


def bar_at(bars, local):
    t = datetime.strptime(local, "%Y-%m-%d %H:%M")
    return next(b for b in bars if b[0] == t)


def sample_setup(client, market, n=24):
    load_market(client, market)
    csv_text, truth = samplegen.sample_trades_csv(market["one"], n=n)
    _, preview = import_trades(client, csv_text)
    sid = new_session(client)
    return sid, truth, preview


# 1 ─────────────────────────────────────────────────────────────────────────
def test_01_chronological_sorting_random_csv_order(client, market):
    sid, truth, preview = sample_setup(client, market)
    q = client.get(f"/api/sessions/{sid}/queue").json()
    got = [i["entry_local"] for i in q["items"]]
    assert got == sorted(got)
    expected = [t["entry"].strftime("%Y-%m-%d %H:%M:%S") for t in sorted(truth, key=lambda t: (t["entry"], int(t["trade_id"])))]
    assert got == expected
    assert [i["position"] for i in q["items"]] == list(range(1, len(got) + 1))
    # the CSV really was shuffled
    assert [r["import_row"] for r in sorted(preview["rows"], key=lambda r: r["chronological_position"])] != sorted(r["import_row"] for r in preview["rows"])


# 2 ─────────────────────────────────────────────────────────────────────────
def test_02_progression_earliest_first_and_next_locked(client, market, db_env):
    sid, truth, _ = sample_setup(client, market)
    cur = client.get(f"/api/sessions/{sid}/current").json()
    assert cur["position"] == 1
    assert cur["trade"]["entry_local"] == min(t["entry"] for t in truth).strftime("%Y-%m-%d %H:%M:%S")
    q = client.get(f"/api/sessions/{sid}/queue").json()["items"]
    assert q[0]["queue_state"] == "CURRENT" and all(i["queue_state"] == "LOCKED" for i in q[1:])

    # Tamper: create a review row for trade #2 directly and try to use it → locked
    conn = sqlite3.connect(db_env / "test.sqlite3", isolation_level=None)
    t2 = q[1]["trade_id"]
    conn.execute("INSERT INTO reviews(session_id, trade_id, status, created_at, updated_at) VALUES (?,?,?,?,?)",
                 (sid, t2, "IN_PROGRESS", "x", "x"))
    conn.commit()
    rid2 = conn.execute("SELECT id FROM reviews WHERE trade_id=?", (t2,)).fetchone()[0]
    for method, url in [("get", f"/api/reviews/{rid2}"), ("get", f"/api/reviews/{rid2}/chart"),
                        ("get", f"/api/reviews/{rid2}/outcome"), ("post", f"/api/reviews/{rid2}/reveal")]:
        r = getattr(client, method)(url)
        assert r.status_code == 423, (url, r.text)
    r = client.post(f"/api/reviews/{rid2}/lock", json={"answers": good_thesis(), "confirm": True})
    assert r.status_code == 423

    # Completing trade 1 unlocks trade 2
    _, done = complete_current(client, sid)
    assert done["next"]["entry_local"] == q[1]["entry_local"]
    q = client.get(f"/api/sessions/{sid}/queue").json()["items"]
    assert [i["queue_state"] for i in q[:3]] == ["COMPLETED", "CURRENT", "LOCKED"]


def test_02b_cannot_complete_without_each_stage(client, market):
    sid, _, _ = sample_setup(client, market)
    rid = client.post(f"/api/sessions/{sid}/current/start").json()["id"]
    assert client.post(f"/api/reviews/{rid}/complete", json=good_post()).status_code == 409   # not locked
    assert client.post(f"/api/reviews/{rid}/reveal").status_code == 409                        # not locked
    bad = good_thesis()
    del bad["invalidation"]
    r = client.post(f"/api/reviews/{rid}/lock", json={"answers": bad, "confirm": True})
    assert r.status_code == 409 and "invalidation" in r.text
    assert client.post(f"/api/reviews/{rid}/lock", json={"answers": good_thesis(), "confirm": False}).status_code == 409
    assert client.post(f"/api/reviews/{rid}/lock", json={"answers": good_thesis(), "confirm": True}).status_code == 200
    assert client.post(f"/api/reviews/{rid}/complete", json=good_post()).status_code == 409   # not revealed
    assert client.post(f"/api/reviews/{rid}/reveal").status_code == 200
    r = client.post(f"/api/reviews/{rid}/complete", json={"reflection": "", "hindsight_assessment": None})
    assert r.status_code == 409
    assert client.post(f"/api/reviews/{rid}/complete", json=good_post()).status_code == 200


# 3 ─────────────────────────────────────────────────────────────────────────
def test_03_exit_time_independence(client, market):
    load_market(client, market)
    one = market["one"]
    a_in, b_in = bar_at(one, "2026-01-13 10:00"), bar_at(one, "2026-01-13 10:20")
    csv_text = simple_trades_csv([
        # B entered later but exits first; listed first in CSV
        ["B", "NIFTY", "2026-01-13 10:20:00+05:30", "2026-01-13 10:25:00+05:30", "BUY", 75, b_in[1], b_in[4], 0],
        ["A", "NIFTY", "2026-01-13 10:00:00+05:30", "2026-01-13 11:30:00+05:30", "SELL", 75, a_in[1], a_in[4], 0],
    ])
    import_trades(client, csv_text)
    sid = new_session(client)
    q = client.get(f"/api/sessions/{sid}/queue").json()["items"]
    assert [i["entry_local"][11:16] for i in q] == ["10:00", "10:20"]


# 4 ─────────────────────────────────────────────────────────────────────────
def test_04_duplicate_timestamps_tiebreak(client, market):
    load_market(client, market)
    px = bar_at(market["one"], "2026-01-13 11:00")[1]
    head = "Trade ID,Seq,Symbol,Entry Time,Exit Time,Side,Qty,Entry Price,Exit Price,P&L"
    rows = [
        ["20", "", "NIFTY", "2026-01-13 11:00:00+05:30", "2026-01-13 11:10:00+05:30", "BUY", 75, px, px, 0],
        ["3", "", "NIFTY", "2026-01-13 11:00:00+05:30", "2026-01-13 11:10:00+05:30", "BUY", 75, px, px, 0],
        ["abc", "", "NIFTY", "2026-01-13 11:00:00+05:30", "2026-01-13 11:10:00+05:30", "BUY", 75, px, px, 0],
        ["99", "1", "NIFTY", "2026-01-13 11:00:00+05:30", "2026-01-13 11:10:00+05:30", "BUY", 75, px, px, 0],
    ]
    csv_text = "\n".join([head] + [",".join(map(str, r)) for r in rows]) + "\n"
    mapping = {"trade_ref": "Trade ID", "exec_seq": "Seq", "instrument": "Symbol", "entry_datetime": "Entry Time",
               "exit_datetime": "Exit Time", "direction": "Side", "quantity": "Qty", "entry_price": "Entry Price",
               "exit_price": "Exit Price", "pnl": "P&L"}
    _, preview = import_trades(client, csv_text, mapping=mapping)
    assert all(r["status"] == "DUPLICATE_TIMESTAMP" for r in preview["rows"])
    order = [r["trade_ref"] for r in sorted(preview["rows"], key=lambda r: r["chronological_position"])]
    # execution sequence first, then numeric trade IDs numerically, then text IDs
    assert order == ["99", "3", "20", "abc"]
    # Re-running resequencing gives the same order (consistency)
    sid = new_session(client)
    q1 = [i["trade_id"] for i in client.get(f"/api/sessions/{sid}/queue").json()["items"]]
    q2 = [i["trade_id"] for i in client.get(f"/api/sessions/{sid}/queue").json()["items"]]
    assert q1 == q2


# 5 ─────────────────────────────────────────────────────────────────────────
def test_05_missing_timestamp_flagged_and_excluded(client, market):
    sid, truth, preview = sample_setup(client, market)
    assert len(preview["unresolved"]) == 1
    assert preview["unresolved"][0]["status"] == "REQUIRES_TIMESTAMP_REVIEW"
    q = client.get(f"/api/sessions/{sid}/queue").json()
    assert len(q["items"]) == len(truth)
    assert len(q["unresolved"]) == 1 and q["unresolved"][0]["queue_state"] == "REQUIRES_TIMESTAMP_REVIEW"
    unresolved = client.get("/api/trades/unresolved").json()
    assert "P&L" not in json.dumps(unresolved) and "exit" not in json.dumps(unresolved).lower()
    tid = unresolved[0]["trade_id"]
    r = client.post(f"/api/trades/{tid}/resolve-timestamp", json={"value": "2026-01-12 09:20:00", "timezone": "Asia/Kolkata",
                                                                  "reason": "checked broker contract note"})
    assert r.status_code == 200, r.text
    q = client.get(f"/api/sessions/{sid}/queue").json()
    assert len(q["items"]) == len(truth) + 1 and not q["unresolved"]
    assert q["items"][0]["trade_id"] == tid  # 09:20 on the first day is the earliest


# 6 ─────────────────────────────────────────────────────────────────────────
def test_06_timezone_normalization(client, market):
    load_market(client, market)
    px = bar_at(market["one"], "2026-01-13 10:42")[1]
    rows = [
        ["1", "NIFTY", "2026-01-13T10:42:00+05:30", "2026-01-13T11:00:00+05:30", "BUY", 75, px, px, 0],
        ["2", "NIFTY", "2026-01-13T05:12:00Z", "2026-01-13T05:30:00Z", "BUY", 75, px, px, 0],
        ["3", "NIFTY", "13-Jan-2026 10:42:00 +0530", "13-Jan-2026 11:00:00 +0530", "BUY", 75, px, px, 0],
        ["4", "NIFTY", "2026-01-13 00:12:00-05:00", "2026-01-13 00:30:00-05:00", "BUY", 75, px, px, 0],
    ]
    _, preview = import_trades(client, simple_trades_csv(rows))
    locals_ = {r["trade_ref"]: r["normalized_entry"] for r in preview["rows"]}
    assert set(locals_.values()) == {"2026-01-13 10:42:00"}
    sources = {r["trade_ref"]: r["tz_source"] for r in preview["rows"]}
    assert sources["2"] == "utc" and sources["1"] == "offset:+05:30" and sources["4"] == "offset:-05:00"


def test_06b_naive_timestamps_require_timezone_confirmation(client, market):
    load_market(client, market)
    px = bar_at(market["one"], "2026-01-13 10:42")[1]
    rows = [["1", "NIFTY", "2026-01-13 10:42:00", "2026-01-13 11:00:00", "BUY", 75, px, px, 0],
            ["2", "NIFTY", "2026-01-13T05:12:00Z", "2026-01-13T05:30:00Z", "BUY", 75, px, px, 0]]
    bid, preview = import_trades(client, simple_trades_csv(rows), confirm=False)
    assert [b["code"] for b in preview["blocking_issues"]] == ["timezone_required"]
    assert client.post(f"/api/import/trades/{bid}/confirm", json={"acknowledge_order": True}).status_code == 409
    from conftest import TRADE_MAPPING
    r = client.post(f"/api/import/trades/{bid}/stage", json={"mapping": TRADE_MAPPING, "options": {"naive_timezone": "Asia/Kolkata"}})
    p = r.json()
    assert not p["blocking_issues"]
    assert {x["normalized_entry"] for x in p["rows"]} == {"2026-01-13 10:42:00"}
    assert {x["tz_source"] for x in p["rows"]} == {"assumed:Asia/Kolkata", "utc"}


def test_06c_ambiguous_dates_require_format(client, market):
    load_market(client, market)
    rows = [["1", "NIFTY", "03/04/2026 10:42:00 +05:30", "", "BUY", 75, 24000, 24010, 0]]
    bid, preview = import_trades(client, simple_trades_csv(rows), confirm=False)
    assert [b["code"] for b in preview["blocking_issues"]] == ["date_order_required"]
    from conftest import TRADE_MAPPING
    p = client.post(f"/api/import/trades/{bid}/stage", json={"mapping": TRADE_MAPPING, "options": {"date_order": "DMY"}}).json()
    assert p["rows"][0]["normalized_entry"] == "2026-04-03 10:42:00" and not p["blocking_issues"]


# 7 ─────────────────────────────────────────────────────────────────────────
def test_07_no_future_candles_reach_frontend(client, market):
    sid, truth, _ = sample_setup(client, market)
    for _ in range(3):
        cur = client.get(f"/api/sessions/{sid}/current").json()
        E = ep(cur["trade"]["entry_local"])
        rid = client.post(f"/api/sessions/{sid}/current/start").json()["id"]
        chart = client.get(f"/api/reviews/{rid}/chart").json()
        assert chart["mode"] == "BLIND"
        assert chart["candles"], "expected history"
        assert all(c["epoch"] + 900 <= E for c in chart["candles"])
        if chart["forming_candle"]:
            assert chart["forming_candle"]["epoch"] <= E < chart["forming_candle"]["epoch"] + 900
        assert all(p["epoch"] + 900 <= E for p in chart["ema"]["points"])
        assert "after_candles" not in chart and "exit" not in chart
        # No value from any bar that opens at/after the entry minute appears anywhere in the payload
        text = json.dumps(chart)
        future = [b for b in market["fifteen"] if int(b[0].replace(tzinfo=IST).timestamp()) > E][:6]
        for b in future:
            assert f'"local": "{b[0].strftime("%Y-%m-%d %H:%M:%S")}"' not in text
        complete_current(client, sid)


# 8 ─────────────────────────────────────────────────────────────────────────
def test_08_no_future_trade_leakage(client, market):
    sid, truth, _ = sample_setup(client, market)
    complete_current(client, sid)
    cur = client.get(f"/api/sessions/{sid}/current").json()
    text = json.dumps(cur) + json.dumps(client.get(f"/api/sessions/{sid}/queue").json())
    for key in ("exit_price", "exit_local", "exit_epoch", "pnl", "raw_row_json", "mfe", "mae"):
        assert f'"{key}"' not in text, key
    later = sorted(truth, key=lambda t: (t["entry"], int(t["trade_id"])))[1:]
    for t in later:
        assert str(t["pnl"]) not in text or t["pnl"] == 0
    assert cur["next"] == {"exists": True, "hidden": True}
    completed = client.get(f"/api/sessions/{sid}/completed").json()
    assert len(completed) == 1


# 9 ─────────────────────────────────────────────────────────────────────────
def test_09_pnl_unavailable_in_blind_mode(client, market):
    sid, _, _ = sample_setup(client, market)
    rid = client.post(f"/api/sessions/{sid}/current/start").json()["id"]
    for url in (f"/api/reviews/{rid}", f"/api/reviews/{rid}/chart", f"/api/sessions/{sid}/current"):
        body = client.get(url).text
        assert '"pnl"' not in body and "P&L" not in body
    assert client.get(f"/api/reviews/{rid}/outcome").status_code == 409
    client.post(f"/api/reviews/{rid}/lock", json={"answers": good_thesis(), "confirm": True})
    assert '"pnl"' not in client.get(f"/api/reviews/{rid}").text
    assert client.get(f"/api/reviews/{rid}/outcome").status_code == 409


# 10 ────────────────────────────────────────────────────────────────────────
def test_10_exit_unavailable_until_thesis_confirmed_and_revealed(client, market):
    sid, _, _ = sample_setup(client, market)
    rid = client.post(f"/api/sessions/{sid}/current/start").json()["id"]
    assert '"exit' not in client.get(f"/api/reviews/{rid}").text
    assert client.post(f"/api/reviews/{rid}/reveal").status_code == 409
    client.post(f"/api/reviews/{rid}/lock", json={"answers": good_thesis(), "confirm": True})
    assert '"exit' not in client.get(f"/api/reviews/{rid}/chart").text
    assert client.post(f"/api/reviews/{rid}/reveal").status_code == 200
    oc = client.get(f"/api/reviews/{rid}/outcome").json()
    assert oc["exit"]["price"] is not None
    chart = client.get(f"/api/reviews/{rid}/chart").json()
    assert chart["mode"] == "REVEALED" and chart["after_candles"] and chart["exit"]["epoch"]


# 11 ────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("with_1m", [True, False])
def test_11_intracandle_entry_does_not_expose_candle_outcome(client, market, with_1m):
    load_market(client, market, with_1m=with_1m)
    one = market["one"]
    entry_local = "2026-01-14 10:37:30"
    minute = bar_at(one, "2026-01-14 10:37")
    # entry price far enough from the index to be treated as a separate instrument (e.g. an option premium),
    # so the forming candle must be pure market data
    csv_text = simple_trades_csv([["1", "NIFTY", "2026-01-14 10:37:30+05:30", "2026-01-14 11:20:00+05:30", "BUY", 75, 150.0, 180.0, 2250]])
    import_trades(client, csv_text)
    sid = new_session(client)
    rid = client.post(f"/api/sessions/{sid}/current/start").json()["id"]
    chart = client.get(f"/api/reviews/{rid}/chart").json()
    f15 = next(b for b in market["fifteen"] if b[0] == datetime(2026, 1, 14, 10, 30))
    fc = chart["forming_candle"]
    assert fc is not None and fc["local"] == "2026-01-14 10:30:00"
    if with_1m:
        known = [b for b in one if datetime(2026, 1, 14, 10, 30) <= b[0] < datetime(2026, 1, 14, 10, 37)]
        assert fc["open"] == known[0][1]
        assert fc["high"] == max(b[2] for b in known)
        assert fc["low"] == min(b[3] for b in known)
        assert fc["close"] == known[-1][4]
    else:
        assert fc["open"] == f15[1] and fc["high"] == fc["low"] == fc["close"] == f15[1]
    # the full bar's high/low/close (which include minutes after entry) are not in the payload,
    # unless they happen to coincide with pre-entry values
    later = [b for b in one if datetime(2026, 1, 14, 10, 37) <= b[0] < datetime(2026, 1, 14, 10, 45)]
    pre = [b for b in one if datetime(2026, 1, 14, 10, 30) <= b[0] < datetime(2026, 1, 14, 10, 37)]
    if max(b[2] for b in later) > max(b[2] for b in pre):
        assert fc["high"] != f15[2]
    assert all(c["local"] != "2026-01-14 10:30:00" for c in chart["candles"])
    assert minute  # sanity


def test_11b_entry_price_on_chart_scale_is_last_print(client, market):
    load_market(client, market)
    pre = [b for b in market["one"] if datetime(2026, 1, 14, 10, 30) <= b[0] < datetime(2026, 1, 14, 10, 37)]
    px = pre[-1][4] + 1.5
    import_trades(client, simple_trades_csv([["1", "NIFTY", "2026-01-14 10:37:30+05:30", "2026-01-14 11:20:00+05:30", "BUY", 75, px, px + 5, 375]]))
    sid = new_session(client)
    rid = client.post(f"/api/sessions/{sid}/current/start").json()["id"]
    chart = client.get(f"/api/reviews/{rid}/chart").json()
    assert chart["entry"]["price_basis"] == "chart"
    assert chart["forming_candle"]["close"] == px


# 12 ────────────────────────────────────────────────────────────────────────
def test_12_ema_uses_only_available_candles(client, market, db_env):
    load_market(client, market)
    px = bar_at(market["one"], "2026-01-14 12:07")[1]
    import_trades(client, simple_trades_csv([["1", "NIFTY", "2026-01-14 12:07:10+05:30", "2026-01-14 13:00:00+05:30", "BUY", 75, px, px + 5, 375]]))
    sid = new_session(client)
    rid = client.post(f"/api/sessions/{sid}/current/start").json()["id"]
    E = ep("2026-01-14 12:07:10")
    before = client.get(f"/api/reviews/{rid}/chart").json()["ema"]["points"]
    assert len(before) > 20
    closes = [b[4] for b in market["fifteen"] if int(b[0].replace(tzinfo=IST).timestamp()) + 900 <= E]
    expected = mkt.ema(closes, 15)
    assert before[-1]["value"] == pytest.approx(expected[-1])
    # Corrupt every future bar: blind EMA must not change
    conn = sqlite3.connect(db_env / "test.sqlite3", isolation_level=None)
    conn.execute("UPDATE market_bars SET close = close * 3, high = high * 3 WHERE epoch + 900 > ?", (E,))
    conn.commit()
    after = client.get(f"/api/reviews/{rid}/chart").json()["ema"]["points"]
    assert dumps(before) == dumps(after)


def test_12b_ema_function_has_no_lookahead():
    vals = [float(i % 7) for i in range(100)]
    full = mkt.ema(vals, 15)
    for cut in (20, 50, 99):
        assert mkt.ema(vals[:cut], 15) == full[:cut]


# 13 ────────────────────────────────────────────────────────────────────────
def test_13_pivots_use_prior_completed_session_only(client, market, db_env):
    load_market(client, market)
    px = bar_at(market["one"], "2026-01-14 13:05")[1]
    import_trades(client, simple_trades_csv([["1", "NIFTY", "2026-01-14 13:05:00+05:30", "2026-01-14 13:40:00+05:30", "BUY", 75, px, px + 5, 375]]))
    sid = new_session(client)
    rid = client.post(f"/api/sessions/{sid}/current/start").json()["id"]
    chart = client.get(f"/api/reviews/{rid}/chart").json()
    ps = next(p for p in chart["pivots"]["sets"] if p["session_date"] == "2026-01-14")
    assert ps["from_session"] == "2026-01-13"
    prior = [b for b in market["fifteen"] if b[0].date().isoformat() == "2026-01-13"]
    H, L, C = max(b[2] for b in prior), min(b[3] for b in prior), prior[-1][4]
    P = (H + L + C) / 3
    assert ps["levels"]["P"] == pytest.approx(P)
    assert ps["levels"]["R1"] == pytest.approx(2 * P - L)
    assert ps["levels"]["S3"] == pytest.approx(L - 2 * (H - P))
    conn = sqlite3.connect(db_env / "test.sqlite3", isolation_level=None)
    conn.execute("UPDATE market_bars SET high = high + 500, low = low - 500 WHERE session_date = '2026-01-14'")
    conn.commit()
    ps2 = next(p for p in client.get(f"/api/reviews/{rid}/chart").json()["pivots"]["sets"] if p["session_date"] == "2026-01-14")
    assert ps2["levels"] == ps["levels"]


# 14 ────────────────────────────────────────────────────────────────────────
def test_14_thesis_is_immutable(client, market, db_env):
    sid, _, _ = sample_setup(client, market)
    rid = client.post(f"/api/sessions/{sid}/current/start").json()["id"]
    client.post(f"/api/reviews/{rid}/lock", json={"answers": good_thesis(), "confirm": True})
    original = client.get(f"/api/reviews/{rid}").json()["thesis"]
    assert client.put(f"/api/reviews/{rid}/draft", json=good_thesis(primary_reason="changed my mind later on this one")).status_code == 409
    assert client.post(f"/api/reviews/{rid}/lock", json={"answers": good_thesis(confidence=99), "confirm": True}).status_code == 409
    conn = sqlite3.connect(db_env / "test.sqlite3", isolation_level=None)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE thesis_snapshots SET payload_json='{}' WHERE review_id=?", (rid,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM thesis_snapshots WHERE review_id=?", (rid,))
    client.post(f"/api/reviews/{rid}/reveal")
    client.post(f"/api/reviews/{rid}/complete", json=good_post())
    r = client.post(f"/api/reviews/{rid}/revise", json={"section": "post_outcome", "reason": "second look",
                                                         "answers": good_post(reflection="On a second look the entry was actually fine and patient")})
    assert r.status_code == 200
    full = r.json()
    assert full["thesis"] == original
    assert full["post"]["answers"]["reflection"] == good_post()["reflection"]
    assert full["revisions"][0]["payload"]["reflection"].startswith("On a second look")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE post_outcome SET payload_json='{}' WHERE review_id=?", (rid,))


# 15 ────────────────────────────────────────────────────────────────────────
def test_15_blind_screen_identical_for_winner_and_loser(tmp_path, monkeypatch, market):
    from fastapi.testclient import TestClient
    from app.main import app
    px = bar_at(market["one"], "2026-01-14 11:10")[1]
    payloads = []
    for name, exit_px, pnl in (("winner", px + 400, 30000), ("loser", px - 400, -30000)):
        monkeypatch.setenv("TTM_DB_PATH", str(tmp_path / f"{name}.sqlite3"))
        with TestClient(app) as c:
            load_market(c, market)
            import_trades(c, simple_trades_csv([["7", "NIFTY", "2026-01-14 11:10:00+05:30", "2026-01-14 12:30:00+05:30", "BUY", 75, px, exit_px, pnl]]))
            sid = new_session(c)
            rid = c.post(f"/api/sessions/{sid}/current/start").json()["id"]
            cur = c.get(f"/api/sessions/{sid}/current").json()
            rev = c.get(f"/api/reviews/{rid}").json()
            chart = c.get(f"/api/reviews/{rid}/chart").json()
            for d in (cur["session"],):
                for k in ("created_at", "last_activity_at"):
                    d.pop(k)
            for d in (cur["review"], rev):
                d.pop("started_at")
            payloads.append(dumps({"current": cur, "review": rev, "chart": chart}))
    assert payloads[0] == payloads[1]


# 16 ────────────────────────────────────────────────────────────────────────
def test_16_resume_after_restart(tmp_path, monkeypatch, market):
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setenv("TTM_DB_PATH", str(tmp_path / "resume.sqlite3"))
    csv_text, _ = samplegen.sample_trades_csv(market["one"], n=10)
    with TestClient(app) as c:
        load_market(c, market)
        import_trades(c, csv_text)
        sid = new_session(c)
        complete_current(c, sid)
        rid = c.post(f"/api/sessions/{sid}/current/start").json()["id"]
        draft = good_thesis(primary_reason="Half-written reason about the EMA reclaim that I will finish later")
        assert c.put(f"/api/reviews/{rid}/draft", json=draft).status_code == 200
        pos = c.get(f"/api/sessions/{sid}/current").json()["position"]
    with TestClient(app) as c2:  # "reopen the application"
        sessions = c2.get("/api/sessions").json()
        assert sessions[0]["id"] == sid and sessions[0]["current_position"] == pos == 2
        cur = c2.get(f"/api/sessions/{sid}/current").json()
        assert cur["review"]["id"] == rid and cur["review"]["status"] == "IN_PROGRESS"
        assert cur["review"]["draft"]["primary_reason"] == draft["primary_reason"]
        assert c2.post(f"/api/reviews/{rid}/lock", json={"answers": draft, "confirm": True}).status_code == 200
    with TestClient(app) as c3:
        cur = c3.get(f"/api/sessions/{sid}/current").json()
        assert cur["review"]["status"] == "THESIS_LOCKED" and cur["position"] == 2
        # a new session does not overwrite the old one
        sid2 = new_session(c3)
        assert c3.get(f"/api/sessions/{sid2}/current").json()["position"] == 1
        assert c3.get(f"/api/sessions/{sid}/current").json()["position"] == 2


# 17 ────────────────────────────────────────────────────────────────────────
def test_17_override_logging(client, market):
    sid, _, _ = sample_setup(client, market)
    first = client.get(f"/api/sessions/{sid}/current").json()["trade"]
    r = client.post(f"/api/sessions/{sid}/current/skip", json={"reason": "duplicate of a broker row", "confirmation": "SKIP"})
    assert r.status_code == 409 and "disabled" in r.text
    client.put("/api/settings", json={"override_enabled": True})
    assert client.post(f"/api/sessions/{sid}/current/skip", json={"reason": "short", "confirmation": "SKIP"}).status_code == 409
    assert client.post(f"/api/sessions/{sid}/current/skip", json={"reason": "duplicate of a broker row", "confirmation": "yes"}).status_code == 409
    r = client.post(f"/api/sessions/{sid}/current/skip", json={"reason": "duplicate of a broker row", "confirmation": "SKIP"})
    assert r.status_code == 200
    ev = [e for e in client.get(f"/api/sessions/{sid}/events").json() if e["event_type"] == "OVERRIDE_SKIP"]
    assert len(ev) == 1
    e = ev[0]
    assert e["trade_id"] == first["id"] and e["at"] and e["detail"]["reason"] == "duplicate of a broker row"
    assert e["detail"]["user_action"] == "skip current trade" and e["detail"]["position"] == 1
    q = client.get(f"/api/sessions/{sid}/queue").json()["items"]
    assert q[0]["queue_state"] == "SKIPPED_WITH_OVERRIDE" and q[1]["queue_state"] == "CURRENT"
    assert client.get(f"/api/sessions/{sid}").status_code in (404, 405)  # no hidden endpoint
    assert client.get("/api/sessions").json()[0]["override_events"] == 1


# 18 ────────────────────────────────────────────────────────────────────────
def test_18_analytics_scope_excludes_future(client, market):
    sid, truth, _ = sample_setup(client, market)
    for _ in range(4):
        complete_current(client, sid)
    cur = client.get(f"/api/sessions/{sid}/current").json()
    a = client.get("/api/analytics", params={"session_id": sid}).json()
    assert a["scope"]["scope"] == "session_before_current"
    assert all(p < cur["position"] for p in a["scope"]["positions"])
    assert a["scope"]["trades"] + a["scope"]["excluded_open_at_current_entry"] == 4
    assert client.get("/api/analytics", params={"session_id": sid, "scope": "full_history"}).status_code == 409
    full = client.get("/api/analytics", params={"session_id": sid, "scope": "full_history", "acknowledge_full_history": True}).json()
    assert full["scope"]["trades"] == len(truth) and full["scope"]["journaled_trades"] == 4
    upto = client.get("/api/analytics", params={"session_id": sid, "scope": "session_up_to", "upto": 2}).json()
    assert upto["scope"]["positions"] == [1, 2]


def test_18b_overlapping_open_trade_excluded_from_current_scope(client, market):
    load_market(client, market)
    one = market["one"]
    a, b, c = bar_at(one, "2026-01-13 10:00"), bar_at(one, "2026-01-13 10:20"), bar_at(one, "2026-01-13 12:00")
    import_trades(client, simple_trades_csv([
        ["1", "NIFTY", "2026-01-13 10:00:00+05:30", "2026-01-13 11:30:00+05:30", "BUY", 75, a[1], a[4], 10],
        ["2", "NIFTY", "2026-01-13 10:20:00+05:30", "2026-01-13 10:40:00+05:30", "BUY", 75, b[1], b[4], 10],
        ["3", "NIFTY", "2026-01-13 12:00:00+05:30", "2026-01-13 12:40:00+05:30", "BUY", 75, c[1], c[4], 10],
    ]))
    sid = new_session(client)
    complete_current(client, sid)
    # trade 2 was entered while trade 1 was open: trade 1's outcome must not feed trade 2's analytics
    a2 = client.get("/api/analytics", params={"session_id": sid}).json()
    assert a2["scope"]["trades"] == 0 and a2["scope"]["excluded_open_at_current_entry"] == 1
    cur = client.get(f"/api/sessions/{sid}/current").json()
    assert cur["hindsight_cautions"]
    complete_current(client, sid)
    a3 = client.get("/api/analytics", params={"session_id": sid}).json()
    assert a3["scope"]["positions"] == [1, 2]


# Full workflow ─────────────────────────────────────────────────────────────
def test_full_workflow_random_order(client, market):
    sid, truth, _ = sample_setup(client, market, n=12)
    total = client.get(f"/api/sessions/{sid}/queue").json()["session"]["total"]
    seen = []
    last = None
    for i in range(total):
        cur = client.get(f"/api/sessions/{sid}/current").json()
        assert cur["position"] == i + 1
        seen.append(cur["trade"]["entry_local"])
        rid, done = complete_current(client, sid)
        full = client.get(f"/api/reviews/{rid}").json()
        assert full["status"] == "COMPLETED" and full["post"]["outcome"]["thesis_vs_reality"]
        for obs in done["observations"]:
            assert max(obs["evidence_positions"]) <= i + 1
        last = done
    assert seen == sorted(seen)
    assert last["finished"] and client.get(f"/api/sessions/{sid}/current").json()["finished"]
    a = client.get("/api/analytics", params={"session_id": sid}).json()
    assert a["scope"]["trades"] == total
    assert a["setups"] and a["calibration"]["confidence_buckets"]


def test_vague_answers_prompt_and_require_acknowledgement(client, market):
    sid, _, _ = sample_setup(client, market)
    rid = client.post(f"/api/sessions/{sid}/current/start").json()["id"]
    vague = good_thesis(primary_reason="I thought it would go up.")
    v = client.post(f"/api/reviews/{rid}/validate", json=vague).json()
    assert "primary_reason" in v["prompts"] and not v["can_lock"]
    assert "What specifically on the chart" in v["prompts"]["primary_reason"]
    assert client.post(f"/api/reviews/{rid}/lock", json={"answers": vague, "confirm": True}).status_code == 409
    vague["specificity_acknowledged"] = True
    assert client.post(f"/api/reviews/{rid}/lock", json={"answers": vague, "confirm": True}).status_code == 200
    ev = [e for e in client.get(f"/api/sessions/{sid}/events").json() if e["event_type"] == "THESIS_LOCKED"][0]
    assert ev["detail"]["vague_fields_acknowledged"] == ["primary_reason"]


def test_mapping_screen_does_not_expose_outcomes_by_default(client, market):
    csv_text, truth = samplegen.sample_trades_csv(market["one"], n=6)
    r = client.post("/api/import/trades/upload", files={"file": ("t.csv", csv_text.encode(), "text/csv")}).json()
    assert "sample_rows" not in r
    text = json.dumps(r["column_profiles"])
    for t in truth:
        assert str(t["exit_price"]) not in text and str(t["pnl"]) not in text


def _fills_import(client, rows, options=None, confirm=True):
    head = "trade_date,symbol,underlying,opt_type,trade_type,quantity,price,order_id,order_execution_time"
    csv_text = head + "\n" + "\n".join(rows) + "\n"
    r = client.post("/api/import/trades/upload", files={"file": ("zerodha.csv", csv_text.encode(), "text/csv")}).json()
    assert r["suggested_row_mode"] == "fills"
    mapping = {"entry_datetime": "order_execution_time", "instrument": "symbol", "underlying": "underlying", "option_type": "opt_type",
               "direction": "trade_type", "quantity": "quantity", "entry_price": "price", "trade_ref": "order_id"}
    p = client.post(f"/api/import/trades/{r['batch_id']}/stage",
                    json={"mapping": mapping, "options": {"row_mode": "fills", "naive_timezone": "Asia/Kolkata", **(options or {})}}).json()
    if confirm:
        assert client.post(f"/api/import/trades/{r['batch_id']}/confirm", json={"acknowledge_order": True}).status_code == 200
    return r["batch_id"], p


def test_fills_mode_rebuilds_round_trips(client, market):
    load_market(client, market)
    bid, p = _fills_import(client, [
        # CE: buy 75 @100 (order o1), add 75 @110 (o2), sell 100, sell 50 → one LONG trade
        "2026-01-13,NIFTY26JAN24000CE,NIFTY,CE,buy,75,100,o1,2026-01-13 10:00:05",
        "2026-01-13,NIFTY26JAN24000CE,NIFTY,CE,buy,75,110,o2,2026-01-13 10:05:00",
        "2026-01-13,NIFTY26JAN24000CE,NIFTY,CE,sell,100,120,o3,2026-01-13 10:30:00",
        "2026-01-13,NIFTY26JAN24000CE,NIFTY,CE,sell,50,90,o4,2026-01-13 10:40:00",
        # bad timestamp fill
        "2026-01-13,NIFTY26JAN24000CE,NIFTY,CE,buy,50,70,o7,not-a-time",
    ])
    assert p["total_rows"] == 1 and p["unresolved_total"] == 1
    ce = p["rows"][0]
    assert ce["direction"] == "LONG" and ce["entry_price"] == 105 and ce["normalized_entry"] == "2026-01-13 10:00:05"
    sid = new_session(client)
    cur = client.get(f"/api/sessions/{sid}/current").json()
    assert cur["trade"]["option_type"] == "CE" and cur["trade"]["chart_instrument"] == "NIFTY"
    # Blind view: the first order only (75 @ 100) — the later add is post-entry information
    assert cur["trade"]["entry_price"] == 100 and cur["trade"]["quantity"] == 75
    assert "110" not in json.dumps(cur["trade"]) and "105" not in json.dumps(cur)
    rid, _ = complete_current(client, sid)
    oc = client.get(f"/api/reviews/{rid}/outcome").json()
    # exit = weighted avg (100×120 + 50×90)/150 = 110; pnl = (110 − 105) × 150
    assert oc["exit"]["price"] == 110 and oc["pnl"] == 750 and oc["market_bias"] == "UP" and oc["price_basis"] == "separate"
    assert oc["entry"]["first_order_price"] == 100 and oc["entry"]["opening_fills"] == 2 and not oc["multi_entry"]
    unresolved = client.get("/api/trades/unresolved").json()
    r2 = client.post(f"/api/trades/{unresolved[0]['trade_id']}/resolve-timestamp", json={"value": "2026-01-13 11:30:00", "timezone": "Asia/Kolkata"})
    assert r2.status_code == 409


def test_fills_multi_entry_label(client, market):
    load_market(client, market)
    _, p = _fills_import(client, [
        "2026-01-14,NIFTY26JAN24100CE,NIFTY,CE,buy,100,50,a1,2026-01-14 10:00:00",
        "2026-01-14,NIFTY26JAN24100CE,NIFTY,CE,sell,50,60,a2,2026-01-14 10:10:00",   # partial exit
        "2026-01-14,NIFTY26JAN24100CE,NIFTY,CE,buy,50,55,a3,2026-01-14 10:20:00",    # re-add #1
        "2026-01-14,NIFTY26JAN24100CE,NIFTY,CE,sell,25,58,a4,2026-01-14 10:30:00",   # partial exit
        "2026-01-14,NIFTY26JAN24100CE,NIFTY,CE,buy,25,57,a5,2026-01-14 10:35:00",    # re-add #2
        "2026-01-14,NIFTY26JAN24100CE,NIFTY,CE,sell,100,65,a6,2026-01-14 11:00:00",
    ])
    assert p["total_rows"] == 1
    assert any("Multi-entry: added to the position 2 time(s)" in w for w in p["rows"][0]["warnings"])
    sid = new_session(client)
    cur = client.get(f"/api/sessions/{sid}/current").json()
    assert "Multi-entry" not in json.dumps(cur) and "re_add" not in json.dumps(cur)   # not shown before reveal
    rid, _ = complete_current(client, sid)
    oc = client.get(f"/api/reviews/{rid}/outcome").json()
    assert oc["multi_entry"] and oc["re_add_episodes"] == 2 and oc["entry"]["first_order_quantity"] == 100
    listing = client.get(f"/api/sessions/{sid}/completed").json()
    assert listing[0]["re_add_episodes"] == 2
    a = client.get("/api/analytics", params={"session_id": sid, "scope": "session_completed"}).json()
    cond = {c["condition"]: c for c in a["psychology"]["conditions"]}
    assert cond["Multi-entry (re-added after a partial exit)"]["n"] == 1


def test_fills_expiry_settlement(client, market):
    load_market(client, market)
    one = market["one"]
    close_13 = [b for b in one if b[0].date().isoformat() == "2026-01-13"][-1][4]
    k = round(close_13 - 50)          # CE 50 points in the money at expiry
    _, p = _fills_import(client, [
        # weekly contract expiring Tue 2026-01-13, bought, partly sold, remainder held to expiry
        f"2026-01-13,NIFTY26113{k}CE,NIFTY,CE,buy,150,40,e1,2026-01-13 11:00:00",
        f"2026-01-13,NIFTY26113{k}CE,NIFTY,CE,sell,50,45,e2,2026-01-13 12:00:00",
        # weekly PE expiring 2026-01-14, far out of the money → worthless
        "2026-01-14,NIFTY2611420000PE,NIFTY,PE,buy,75,2,e3,2026-01-14 10:00:00",
        # monthly contract (expires 2026-01-27): no index data that day → not settled
        "2026-01-14,NIFTY26JAN23000PE,NIFTY,PE,buy,75,5,e4,2026-01-14 10:05:00",
    ])
    rows = {r["instrument"]: r for r in p["rows"]}
    assert set(rows) == {f"NIFTY26113{k}CE", "NIFTY2611420000PE", "NIFTY26JAN23000PE"}
    ce = client.app  # silence linters
    from app.db import connect
    conn = connect()
    t = {r["instrument"]: dict(r) for r in conn.execute("SELECT * FROM trades")}
    ce = t[f"NIFTY26113{k}CE"]
    settle = json.loads(ce["settlement_json"])
    assert ce["exit_kind"] == "expiry_settlement" and settle["expiry_date"] == "2026-01-13"
    assert settle["settlement_price"] == pytest.approx(close_13 - k) and settle["settled_quantity"] == 100
    exp_exit = (50 * 45 + 100 * (close_13 - k)) / 150
    assert ce["exit_price"] == pytest.approx(exp_exit, abs=1e-3)
    assert ce["pnl"] == pytest.approx((exp_exit - 40) * 150, abs=0.02)
    assert ce["exit_local"] == "2026-01-13 15:30:00"
    pe = t["NIFTY2611420000PE"]
    assert pe["exit_kind"] == "expiry_settlement" and pe["exit_price"] == 0 and pe["pnl"] == pytest.approx(-150)
    mo = t["NIFTY26JAN23000PE"]
    assert mo["exit_epoch"] is None and mo["exit_kind"] is None
    assert any("not settled" in w for w in json.loads(mo["warnings_json"]))


def test_expiry_date_rules_and_holiday_shift():
    from datetime import date
    from app import expiry
    p = expiry.parse_symbol("NIFTY2061810000PE")
    assert p["kind"] == "weekly" and p["nominal_date"] == date(2020, 6, 18) and p["strike"] == 10000 and p["type"] == "PE"
    assert expiry.parse_symbol("NIFTY25D0226250CE")["nominal_date"] == date(2025, 12, 2)
    m = expiry.parse_symbol("BANKNIFTY20MAYFUT")
    assert m["kind"] == "monthly" and m["type"] == "FUT" and m["strike"] is None
    everyday = lambda d: d.weekday() < 5
    assert expiry.expiry_date(expiry.parse_symbol("NIFTY20JUN10300PE"), everyday)[0] == date(2020, 6, 25)       # last Thu
    assert expiry.expiry_date(expiry.parse_symbol("NIFTY25SEP25000CE"), everyday)[0] == date(2025, 9, 30)       # last Tue
    assert expiry.expiry_date(expiry.parse_symbol("BANKNIFTY24APR48000CE"), everyday)[0] == date(2024, 4, 24)   # last Wed
    holiday = lambda d: d.weekday() < 5 and d != date(2020, 6, 25)
    exp, method = expiry.expiry_date(expiry.parse_symbol("NIFTY20JUN10300PE"), holiday)
    assert exp == date(2020, 6, 24) and "moved to" in method
    assert expiry.parse_symbol("AXISBANK18MAY480PE") is None


def test_fills_contract_starting_with_sell_needs_confirmation(client, market):
    load_market(client, market)
    rows = [
        "2026-01-14,NIFTY26JAN24200CE,NIFTY,CE,sell,75,80,s1,2026-01-14 10:00:00",
        "2026-01-14,NIFTY26JAN24200CE,NIFTY,CE,buy,75,60,s2,2026-01-14 10:10:00",
        "2026-01-14,NIFTY26JAN24200CE,NIFTY,CE,sell,75,70,s3,2026-01-14 10:20:00",
        "2026-01-14,NIFTY26JAN24200CE,NIFTY,CE,buy,75,65,s4,2026-01-14 10:30:00",
        "2026-01-14,NIFTY26JAN24300CE,NIFTY,CE,buy,75,30,n1,2026-01-14 11:00:00",
        "2026-01-14,NIFTY26JAN24300CE,NIFTY,CE,sell,75,35,n2,2026-01-14 11:10:00",
    ]
    bid, p = _fills_import(client, rows)
    flagged = [u for u in p["unresolved"] if u["issue_kind"] == "contract_first_sell"]
    assert len(flagged) == 2 and {u["instrument"] for u in flagged} == {"NIFTY26JAN24200CE"}
    sid = new_session(client)
    assert client.get(f"/api/sessions/{sid}/queue").json()["session"]["total"] == 1
    un = client.get("/api/trades/unresolved").json()
    assert client.post(f"/api/trades/{un[0]['trade_id']}/resolve-timestamp",
                       json={"value": "2026-01-14 10:00:00", "timezone": "Asia/Kolkata"}).status_code == 409
    # "missing opening buy": first sell excluded, remaining fills re-paired as longs
    r = client.post(f"/api/import/trades/{bid}/resolve-contract", json={"symbol": "NIFTY26JAN24200CE", "decision": "missing_opening_buy"})
    assert r.status_code == 200, r.text
    assert r.json()["excluded_fill_rows"] == [1] and r.json()["trades"] == 2
    from app.db import connect
    conn = connect()
    t = [dict(x) for x in conn.execute("SELECT direction, entry_local, exit_local, chrono_status, exit_epoch FROM trades WHERE instrument='NIFTY26JAN24200CE' ORDER BY entry_epoch")]
    assert [x["direction"] for x in t] == ["LONG", "LONG"] and t[0]["entry_local"] == "2026-01-14 10:10:00"
    assert t[0]["exit_local"] == "2026-01-14 10:20:00" and t[1]["exit_epoch"] is None     # last buy still open (monthly, no expiry data)
    assert all(x["chrono_status"] == "OK" for x in t)
    assert client.get(f"/api/sessions/{sid}/queue").json()["session"]["total"] == 3
    ev = [e for e in conn.execute("SELECT event_type FROM events WHERE event_type='CONTRACT_RESOLVED'")]
    assert len(ev) == 1


def test_fills_contract_confirmed_real_short(client, market):
    load_market(client, market)
    bid, p = _fills_import(client, [
        "2026-01-15,NIFTY26JANFUT,NIFTY,,sell,75,24050,f1,2026-01-15 10:00:00",
        "2026-01-15,NIFTY26JANFUT,NIFTY,,buy,75,24000,f2,2026-01-15 10:30:00",
    ])
    assert p["unresolved_total"] == 1 and p["unresolved"][0]["issue_kind"] == "contract_first_sell"
    r = client.post(f"/api/import/trades/{bid}/resolve-contract", json={"symbol": "NIFTY26JANFUT", "decision": "real_short"})
    assert r.status_code == 200
    from app.db import connect
    x = dict(connect().execute("SELECT direction, pnl, chrono_status, chrono_seq FROM trades").fetchone())
    assert x["direction"] == "SHORT" and x["pnl"] == 3750 and x["chrono_status"] == "OK" and x["chrono_seq"] == 1


def test_pivots_skip_short_special_sessions(client, market, db_env):
    load_market(client, market)
    # Turn 2026-01-13 into a short special session (e.g. a Muhurat hour): keep only 4 bars
    conn = sqlite3.connect(db_env / "test.sqlite3", isolation_level=None)
    conn.execute("DELETE FROM market_bars WHERE session_date='2026-01-13' AND local > '2026-01-13 10:00:00'")
    px = bar_at(market["one"], "2026-01-14 13:05")[1]
    import_trades(client, simple_trades_csv([["1", "NIFTY", "2026-01-14 13:05:00+05:30", "2026-01-14 13:40:00+05:30", "BUY", 75, px, px + 5, 375]]))
    sid = new_session(client)
    rid = client.post(f"/api/sessions/{sid}/current/start").json()["id"]
    chart = client.get(f"/api/reviews/{rid}/chart").json()
    ps = next(p for p in chart["pivots"]["sets"] if p["session_date"] == "2026-01-14")
    assert ps["from_session"] == "2026-01-12"
    assert any("skipped short session" in l for l in chart["limitations"])


def test_day_pnl_context_uses_only_trades_closed_before_entry(client, market):
    load_market(client, market)
    one = market["one"]
    px = lambda t: bar_at(one, t)[1]
    import_trades(client, simple_trades_csv([
        ["1", "NIFTY", "2026-01-12 11:00:00+05:30", "2026-01-12 11:20:00+05:30", "BUY", 75, px("2026-01-12 11:00"), px("2026-01-12 11:20"), 4321],  # previous day
        ["2", "NIFTY", "2026-01-13 10:00:00+05:30", "2026-01-13 10:30:00+05:30", "BUY", 75, px("2026-01-13 10:00"), px("2026-01-13 10:30"), -1200],
        ["3", "NIFTY", "2026-01-13 10:40:00+05:30", "2026-01-13 10:50:00+05:30", "SELL", 75, px("2026-01-13 10:40"), px("2026-01-13 10:50"), -300],
        ["4", "NIFTY", "2026-01-13 10:15:00+05:30", "2026-01-13 11:30:00+05:30", "BUY", 75, px("2026-01-13 10:15"), px("2026-01-13 11:30"), 98765],  # still open at trade 5
        ["5", "NIFTY", "2026-01-13 11:00:00+05:30", "2026-01-13 11:40:00+05:30", "BUY", 75, px("2026-01-13 11:00"), px("2026-01-13 11:40"), 50],
    ]))
    sid = new_session(client)
    cur = client.get(f"/api/sessions/{sid}/current").json()
    assert cur["day_context"]["first_trade_of_day"] and cur["day_context"]["closed_before_entry"] == 0
    complete_current(client, sid)                      # trade 1 (previous day)
    cur = client.get(f"/api/sessions/{sid}/current").json()
    assert cur["day_context"]["first_trade_of_day"]    # trade 2: yesterday's P&L does not count
    for _ in range(3):                                 # trades 2, 4 (10:15), 3
        complete_current(client, sid)
    cur = client.get(f"/api/sessions/{sid}/current").json()
    assert cur["trade"]["entry_local"] == "2026-01-13 11:00:00"
    d = cur["day_context"]
    assert d["realised_day_pnl"] == -1500 and d["closed_before_entry"] == 2 and d["losses"] == 2
    assert d["consecutive_losses"] == 2 and d["open_at_entry"] == 1 and d["entered_earlier_today"] == 3
    assert "98765" not in json.dumps(cur)              # the still-open trade's result is not known yet
    rid = client.post(f"/api/sessions/{sid}/current/start").json()["id"]
    v = client.post(f"/api/reviews/{rid}/validate", json=good_thesis(day_pnl_influence="Yes — trying to recover a loss")).json()
    assert "Day P&L before this entry (fact):\n-1,500 realised" in v["snapshot_text"] or "-1500 realised" in v["snapshot_text"]
    complete_current(client, sid, thesis=good_thesis(day_pnl_influence="Yes — trying to recover a loss"))
    a = client.get("/api/analytics", params={"session_id": sid, "scope": "session_completed"}).json()
    cond = {c["condition"]: c for c in a["psychology"]["conditions"]}
    assert cond["Day P&L red before entry"]["positions"] == [4, 5]          # 10:40 entry followed the 10:30 loss
    assert cond["After 2+ losses in a row today"]["positions"] == [5]
    assert cond["Another position still open at entry"]["positions"] == [3, 4, 5]
    assert cond["Said day P&L was on mind: trying to recover a loss"]["n"] == 1
    assert set(cond["First trade of the day"]["positions"]) == {1, 2}


def test_expiry_settlement_prefers_official_daily_close(client, market, db_env):
    load_market(client, market)
    daily = db_env / "d1.csv"
    daily.write_text("timestamp,open,high,low,close,volume\n2026-01-13T00:00:00+05:30,1,99999,1,23900.5,0\n")
    r = client.post("/api/import/market/from-path", json={"path": str(daily), "mapping": MAPPING_M, "instrument": "NIFTY", "timeframe": "1d"})
    assert r.status_code == 200, r.text
    _fills_import(client, ["2026-01-13,NIFTY2611323800CE,NIFTY,CE,buy,75,40,d1,2026-01-13 11:00:00"])
    from app.db import connect
    t = dict(connect().execute("SELECT * FROM trades").fetchone())
    st = json.loads(t["settlement_json"])
    assert st["underlying_value"] == 23900.5 and st["settlement_price"] == pytest.approx(100.5)
    assert st["underlying_value_source"].startswith("official NIFTY closing value")
    # daily bars never reach the blind chart
    sid = new_session(client)
    rid = client.post(f"/api/sessions/{sid}/current/start").json()["id"]
    chart = client.get(f"/api/reviews/{rid}/chart").json()
    highs = [c["high"] for c in chart["candles"]] + ([chart["forming_candle"]["high"]] if chart["forming_candle"] else [])
    assert max(highs) < 99999 and all(c["local"][11:] != "00:00:00" for c in chart["candles"])


def test_contract_evidence_suggestion():
    from app.importer import contract_evidence
    # sell 75 then buy 75, repeated: flat as-is, leftover if first sell dropped → real short
    assert contract_evidence([-75, 75, -75, 75])["suggestion"] == "real_short"
    # a lone sell closing an earlier position: short forever as-is, flat if dropped → missing buy
    assert contract_evidence([-1200])["suggestion"] == "missing_opening_buy"
    ev = contract_evidence([-75, 75, 75])
    assert ev["suggestion"] == "unclear" and ev["as_is_end_position"] == 75


def test_exclude_trades_without_market_data_and_restore(client, market):
    load_market(client, market)
    one = market["one"]
    px = lambda t: bar_at(one, t)[1]
    import_trades(client, simple_trades_csv([
        ["1", "NIFTY", "2026-01-13 10:00:00+05:30", "2026-01-13 10:30:00+05:30", "BUY", 75, px("2026-01-13 10:00"), px("2026-01-13 10:30"), -500],
        ["2", "BANKNIFTY26JAN50000PE", "2026-01-13 10:40:00+05:30", "2026-01-13 11:00:00+05:30", "BUY", 30, 5, 6, 30],     # no BANKNIFTY data
        ["3", "BANKNIFTY26JAN50500CE", "2026-01-14 10:00:00+05:30", "2026-01-14 10:20:00+05:30", "BUY", 30, 4, 3, -1500],  # no BANKNIFTY data
        ["4", "NIFTY", "2026-01-14 11:00:00+05:30", "2026-01-14 11:30:00+05:30", "BUY", 75, px("2026-01-14 11:00"), px("2026-01-14 11:30"), 10],
    ]))
    sid = new_session(client)
    cov = client.get("/api/trades/coverage").json()
    assert cov["trades_without_data"] == 2 and cov["first_positions_without_data"] == [2, 3]
    assert [(g["instrument"], g["trades"]) for g in cov["groups"]] == [("BANKNIFTY", 2)]
    # reason is required and validated
    assert client.post("/api/trades/exclude", json={"no_market_data": True, "reason": "because"}).status_code == 400
    assert client.post("/api/trades/exclude", json={"no_market_data": True, "reason": "Other"}).status_code == 400
    ids = [i["trade_id"] for i in client.get(f"/api/sessions/{sid}/queue").json()["items"]]
    r = client.post("/api/trades/exclude", json={"trade_ids": [ids[2]], "reason": "No market data for this instrument"}).json()
    assert r == {"excluded": 1, "refused_reviewed": 0}
    r = client.post("/api/trades/exclude", json={"no_market_data": True, "instrument": "BANKNIFTY", "reason": "No market data for this instrument"}).json()
    assert r["excluded"] == 1
    q = client.get(f"/api/sessions/{sid}/queue").json()
    assert [i["entry_local"][11:16] for i in q["items"]] == ["10:00", "11:00"] and q["session"]["total"] == 2
    assert client.get("/api/trades/coverage").json()["trades_without_data"] == 0
    excluded = client.get("/api/trades/excluded").json()
    assert len(excluded) == 2 and "pnl" not in json.dumps(excluded) and excluded[0]["exclusion_reason"].startswith("No market data")
    # excluded trades still count toward what the trader knew about their day
    complete_current(client, sid)
    # day of trade 4 (2026-01-14): trade 3 (excluded, closed 10:20, −1500) is still part of the day context
    cur = client.get(f"/api/sessions/{sid}/current").json()
    assert cur["day_context"]["realised_day_pnl"] == -1500 and cur["market_data_available"]
    ev = [e for e in client.get(f"/api/sessions/{sid}/events").json()]
    # restore puts them back in chronological order (completed trade keeps its place)
    assert client.post("/api/trades/restore", json={"all": True}).json() == {"restored": 2}
    q = client.get(f"/api/sessions/{sid}/queue").json()
    assert q["session"]["total"] == 4 and [i["queue_state"] for i in q["items"]] == ["COMPLETED", "CURRENT", "LOCKED", "LOCKED"]


def test_exclude_current_trade_and_refuse_reviewed(client, market):
    load_market(client, market)
    one = market["one"]
    px = lambda t: bar_at(one, t)[1]
    import_trades(client, simple_trades_csv([
        ["1", "NIFTY", "2026-01-13 10:00:00+05:30", "2026-01-13 10:30:00+05:30", "BUY", 75, px("2026-01-13 10:00"), px("2026-01-13 10:30"), 5],
        ["2", "NIFTY", "2026-01-13 11:00:00+05:30", "2026-01-13 11:30:00+05:30", "BUY", 75, px("2026-01-13 11:00"), px("2026-01-13 11:30"), 5],
        ["3", "NIFTY", "2026-01-13 12:00:00+05:30", "2026-01-13 12:30:00+05:30", "BUY", 75, px("2026-01-13 12:00"), px("2026-01-13 12:30"), 5],
    ]))
    sid = new_session(client)
    rid = client.post(f"/api/sessions/{sid}/current/start").json()["id"]
    client.put(f"/api/reviews/{rid}/draft", json={"primary_reason": "draft in progress"})
    # an in-progress (not locked) current trade can be excluded, even with the override disabled
    r = client.post(f"/api/sessions/{sid}/current/exclude", json={"reason": "Data error in the trade record"})
    assert r.status_code == 200 and r.json()["excluded"] == 1
    cur = client.get(f"/api/sessions/{sid}/current").json()
    assert cur["position"] == 1 and cur["trade"]["entry_local"].endswith("11:00:00") and cur["total"] == 2
    # a trade with a locked thesis can't be excluded
    rid2 = client.post(f"/api/sessions/{sid}/current/start").json()["id"]
    client.post(f"/api/reviews/{rid2}/lock", json={"answers": good_thesis(), "confirm": True})
    r = client.post(f"/api/sessions/{sid}/current/exclude", json={"reason": "Data error in the trade record"}).json()
    assert r["excluded"] == 0 and r["refused_reviewed"] == 1
    assert client.get(f"/api/sessions/{sid}/current").json()["review"]["status"] == "THESIS_LOCKED"
    ev = [e["event_type"] for e in client.get(f"/api/sessions/{sid}/events").json()]
    assert "TRADES_EXCLUDED" in ev
    # restoring the first trade makes it current again (its draft survives)
    excluded = client.get("/api/trades/excluded").json()
    client.post("/api/trades/restore", json={"trade_ids": [excluded[0]["trade_id"]]})
    cur = client.get(f"/api/sessions/{sid}/current").json()
    assert cur["trade"]["entry_local"].endswith("10:00:00") and cur["review"]["draft"]["primary_reason"] == "draft in progress"


def test_chart_entry_position_swing_levels_and_vwap(client, market, db_env):
    load_market(client, market)
    px = lambda t: bar_at(market["one"], t)[1]
    import_trades(client, simple_trades_csv([
        ["1", "NIFTY", "2026-01-14 10:45:00+05:30", "2026-01-14 11:00:00+05:30", "BUY", 75, px("2026-01-14 10:45"), px("2026-01-14 11:00"), 5],
        ["2", "NIFTY", "2026-01-14 12:07:30+05:30", "2026-01-14 12:30:00+05:30", "BUY", 75, px("2026-01-14 12:07"), px("2026-01-14 12:30"), 5],
    ]))
    sid = new_session(client)
    rid = client.post(f"/api/sessions/{sid}/current/start").json()["id"]
    c1 = client.get(f"/api/reviews/{rid}/chart").json()
    assert c1["entry"]["position"]["at_candle_open"] and "at the open of the 10:45–11:00 candle" in c1["entry"]["position"]["description"]
    complete_current(client, sid)
    rid2 = client.post(f"/api/sessions/{sid}/current/start").json()["id"]
    c2 = client.get(f"/api/reviews/{rid2}/chart").json()
    pos = c2["entry"]["position"]
    assert pos["mid_candle"] and pos["seconds_into_candle"] == 7 * 60 + 30 and "12:00–12:15 candle" in pos["description"]
    # swing levels come only from completed candles, and each needs two completed candles after it
    last_completed = c2["candles"][-1]["local"]
    levels = c2["support_resistance"]["levels"]
    assert levels and all(l["local"] < c2["candles"][-2]["local"] for l in levels)
    assert all(l["local"] <= last_completed for l in levels)
    # index data has no volume → VWAP unavailable
    assert c2["vwap"]["available"] is False
    ctx = client.get(f"/api/reviews/{rid2}").json()["entry_context"]
    assert ctx["entered_mid_candle"] is True and ctx["trend_last_hour"] in ("rising", "falling", "mixed")


def test_vwap_and_swing_helpers():
    from app.market import session_vwap, swing_levels
    bars = [{"epoch": i, "local": f"2026-01-14 10:{i:02d}:00", "session_date": "2026-01-14",
             "high": h, "low": l, "close": c, "volume": v}
            for i, (h, l, c, v) in enumerate([(10, 8, 9, 100), (12, 9, 11, 300), (11, 7, 8, 100), (9, 6, 7, 100), (13, 9, 12, 0)])]
    ok, pts = session_vwap(bars)
    assert ok and pts[1]["value"] == pytest.approx((9 * 100 + (32 / 3) * 300) / 400)
    assert session_vwap([dict(b, volume=0) for b in bars]) == (False, [])
    lv = swing_levels(bars)
    assert {"kind": "resistance", "price": 12, "local": bars[1]["local"]} not in lv  # needs 2 bars on the left too
    lv = swing_levels(bars, lookaround=1)
    assert {"kind": "resistance", "price": 12, "local": bars[1]["local"]} in lv
    assert {"kind": "support", "price": 6, "local": bars[3]["local"]} in lv


def test_psychology_dashboard_confirmation_check_and_summary(client, market):
    load_market(client, market)
    px = lambda t: bar_at(market["one"], t)[1]
    import_trades(client, simple_trades_csv([
        ["1", "NIFTY", "2026-01-14 10:07:30+05:30", "2026-01-14 10:30:00+05:30", "BUY", 75, px("2026-01-14 10:07"), px("2026-01-14 10:30"), -5],
        ["2", "NIFTY", "2026-01-14 11:00:00+05:30", "2026-01-14 11:30:00+05:30", "BUY", 75, px("2026-01-14 11:00"), px("2026-01-14 11:30"), 5],
    ]))
    client.put("/api/settings", json={"declared_rules": [{"type": "require_confirmation", "value": None}]})
    sid = new_session(client)
    t1 = good_thesis(emotions=["FOMO", "Impatient"], rules_followed="Partially",
                     primary_reason="Waited for confirmation of the EMA reclaim with a strong close above the level",
                     confirmation_before_entry="Yes — it had completed")
    rid1, done = complete_current(client, sid, thesis=t1, post=good_post(psychology_influenced="Yes", psychology_flags=["Premature exit", "Increasing size"]))
    s = done["trade_summary"]
    assert s["status"].startswith("OBSERVATION — NOT YET A RULE")
    assert "Entered while the 15-minute candle was still forming" in s["facts"]
    assert any(x.startswith("Pre-trade state: FOMO") for x in s["stated"])
    complete_current(client, sid, thesis=good_thesis(emotions=["Calm"]))
    a = client.get("/api/analytics", params={"session_id": sid, "scope": "session_completed"}).json()
    dash = {i["item"]: i for i in a["psychology_dashboard"]["items"]}
    assert len(dash) == 12
    assert dash["FOMO"]["positions"] == [1] and dash["Impatience"]["positions"] == [1]
    assert dash["Rule violation"]["positions"] == [1] and dash["Premature exit"]["positions"] == [1]
    assert dash["Oversizing"]["positions"] == [1] and dash["Boredom"]["n"] == 0
    assert a["evidence_index"] == {"1": rid1, "2": a["evidence_index"]["2"]}
    titles = {c["title"]: c for c in a["contradictions"]}
    assert titles["Entered before the stated confirmation completed"]["evidence_positions"] == [1]
    rule = [c for c in a["contradictions"] if c["title"].startswith("Declared rule: Only enter after the confirming")]
    assert rule and rule[0]["evidence_positions"] == [1]


def test_patterns_after_wins_late_entries_and_structures():
    from app.analytics import patterns
    def rec(seq, minute, result, mfe, mae, emotions=(), ctx=None, exit_after=5):
        e = 1_768_300_000 + minute * 60
        return {"seq": seq, "review_id": seq, "entry_epoch": e, "exit_epoch": e + exit_after * 60, "date": "2026-01-14",
                "journaled": True, "result": result, "points": 1, "pnl": 1, "mfe": mfe, "mae": mae,
                "thesis": {"emotions": list(emotions)}, "post": {},
                "outcome": {"market_bias": "UP", "thesis_vs_reality": []},
                "ctx": ctx or {"above_ema": True, "trend_last_hour": "rising", "nearest_pivot": "R1", "pivot_side": "above",
                               "move_last_hour": 150, "reference_price": 24000}}
    recs = [rec(1, 0, "WIN", 40, -10), rec(2, 6, "LOSS", 5, -30, ["Overconfident"]),
            rec(3, 60, "WIN", 40, -10), rec(4, 66, "LOSS", 5, -30, ["Excited"]),
            rec(5, 120, "LOSS", 5, -40), rec(6, 200, "LOSS", 5, -40)]
    out = {p["title"]: p for p in patterns(recs, "test scope")}
    assert out["Confident / excited states after wins"]["evidence_positions"] == [2, 4]
    assert out["Losses on the trade after a win"]["evidence_positions"] == [2, 4]
    late = out["Possible late entries"]["evidence_positions"]
    assert late == [2, 4, 5, 6]
    struct = [p for p in out.values() if p["kind"] == "chart_structure"]
    assert struct and struct[0]["count"] == 6 and "above EMA · rising last hour · above R1" in struct[0]["title"]


def test_preview_splits_original_date_and_time(client, market):
    load_market(client, market)
    px = bar_at(market["one"], "2026-01-13 10:42")[1]
    _, p = import_trades(client, simple_trades_csv([["1", "NIFTY", "13-Jan-2026 10:42:00 +0530", "", "BUY", 75, px, px, 0]]), confirm=False)
    row = p["rows"][0]
    assert row["original_date"] == "13-Jan-2026" and row["original_time"] == "10:42:00 +0530"
