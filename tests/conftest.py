import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import samplegen  # noqa: E402


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TTM_DB_PATH", str(tmp_path / "test.sqlite3"))
    return tmp_path


@pytest.fixture
def client(db_env):
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def market(db_env):
    one, fifteen = samplegen.synthetic_market(sessions=5)
    p1 = db_env / "m1.csv"
    p15 = db_env / "m15.csv"
    p1.write_text(samplegen.bars_csv(one))
    p15.write_text(samplegen.bars_csv(fifteen))
    return {"one": one, "fifteen": fifteen, "p1": p1, "p15": p15}


MAPPING_M = {"timestamp": "timestamp", "open": "open", "high": "high", "low": "low", "close": "close", "volume": "volume"}


def load_market(client, market, with_1m=True):
    r = client.post("/api/import/market/from-path", json={"path": str(market["p15"]), "mapping": MAPPING_M, "instrument": "NIFTY",
                                                          "timeframe": "15m", "naive_timezone": "Asia/Kolkata"})
    assert r.status_code == 200, r.text
    if with_1m:
        r = client.post("/api/import/market/from-path", json={"path": str(market["p1"]), "mapping": MAPPING_M, "instrument": "NIFTY",
                                                              "timeframe": "1m", "naive_timezone": "Asia/Kolkata"})
        assert r.status_code == 200, r.text


TRADE_MAPPING = {"trade_ref": "Trade ID", "instrument": "Symbol", "entry_datetime": "Entry Time", "exit_datetime": "Exit Time",
                 "direction": "Side", "quantity": "Qty", "entry_price": "Entry Price", "exit_price": "Exit Price", "pnl": "P&L"}


def import_trades(client, csv_text, mapping=None, options=None, confirm=True):
    r = client.post("/api/import/trades/upload", files={"file": ("trades.csv", csv_text.encode(), "text/csv")})
    assert r.status_code == 200, r.text
    bid = r.json()["batch_id"]
    r = client.post(f"/api/import/trades/{bid}/stage", json={"mapping": mapping or TRADE_MAPPING, "options": options or {}})
    assert r.status_code == 200, r.text
    preview = r.json()
    if confirm:
        r = client.post(f"/api/import/trades/{bid}/confirm", json={"acknowledge_order": True})
        assert r.status_code == 200, r.text
    return bid, preview


def good_thesis(direction="Long", **over):
    a = {
        "chart_observation": "Price reclaimed the 15 EMA after three lower candles and is pushing toward the pivot",
        "setups": ["EMA interaction", "Pullback"], "direction": direction,
        "primary_reason": "A 15 minute candle closed back above the EMA after a pullback into S1 support",
        "one_sentence": "Pullback to S1 held and the EMA reclaim suggests continuation toward the pivot",
        "expected_direction": "Up" if direction == "Long" else "Down", "expected_move_points": 30,
        "expected_timeframe": "30–60 minutes",
        "invalidation": "A 15 minute close back below the EMA and below the S1 level",
        "confidence": 65, "confidence_reason": "Clean reclaim but the broader trend is still unclear today",
        "setup_quality": 60, "emotions": ["Calm"], "pre_trade_thoughts": "Waiting for the candle close before clicking buy",
        "had_to_take": "No", "predefined_setup": "Yes", "rules_followed": "Mostly",
    }
    a.update(over)
    return a


def good_post(**over):
    a = {"reflection": "The reclaim was real but I entered before the candle actually closed above the EMA",
         "hindsight_assessment": "Partially visible", "psychology_influenced": "No"}
    a.update(over)
    return a


def new_session(client):
    r = client.post("/api/sessions", json={"name": "test"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def complete_current(client, sid, thesis=None, post=None):
    r = client.post(f"/api/sessions/{sid}/current/start")
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    cur = client.get(f"/api/sessions/{sid}/current").json()
    direction = {"LONG": "Long", "SHORT": "Short"}.get(cur["trade"]["direction"], "Long")
    r = client.post(f"/api/reviews/{rid}/lock", json={"answers": thesis or good_thesis(direction), "confirm": True})
    assert r.status_code == 200, r.text
    r = client.post(f"/api/reviews/{rid}/reveal")
    assert r.status_code == 200, r.text
    r = client.post(f"/api/reviews/{rid}/complete", json=post or good_post())
    assert r.status_code == 200, r.text
    return rid, r.json()


def simple_trades_csv(rows):
    head = "Trade ID,Symbol,Entry Time,Exit Time,Side,Qty,Entry Price,Exit Price,P&L"
    return "\n".join([head] + [",".join(str(x) for x in r) for r in rows]) + "\n"


def dumps(o):
    return json.dumps(o, sort_keys=True)
