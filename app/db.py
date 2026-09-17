"""SQLite schema and connection handling.

Design notes
------------
* All instants are stored as UTC epoch seconds (``*_epoch``) so ordering never
  depends on string formats or timezones. Exchange-local strings (``*_local``)
  are stored alongside for display and session-date logic and are recomputed if
  the exchange timezone setting changes (see ``timeutil.renormalize_all``).
* The original text exactly as it appeared in the CSV is kept in ``*_original``.
* Locked theses and original post-outcome reflections are protected by triggers:
  UPDATE and DELETE raise, so they cannot be changed silently by any code path.
  Later edits go to ``journal_revisions``.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "journal.sqlite3"

SCHEMA = r"""
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_batches (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    kind             TEXT NOT NULL,              -- 'trades' | 'market'
    filename         TEXT,
    created_at       TEXT NOT NULL,
    status           TEXT NOT NULL,              -- UPLOADED | STAGED | CONFIRMED | CANCELLED
    headers_json     TEXT,
    raw_csv_path     TEXT,
    mapping_json     TEXT,
    options_json     TEXT,
    confirmed_at     TEXT,
    sort_method      TEXT,
    summary_json     TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id               INTEGER NOT NULL REFERENCES import_batches(id),
    import_row             INTEGER NOT NULL,     -- 1-based data row in the original CSV
    raw_row_json           TEXT NOT NULL,
    trade_ref              TEXT,                 -- user's own Trade ID, if any
    instrument             TEXT,                 -- as traded, e.g. "NIFTY 24500 CE"
    chart_instrument       TEXT NOT NULL,        -- market-data symbol for the chart, e.g. "NIFTY"
    option_type            TEXT,                 -- CE | PE | NULL
    entry_original         TEXT,
    entry_epoch            INTEGER,
    entry_local            TEXT,
    entry_tz_source        TEXT,                 -- e.g. 'offset:+05:30', 'assumed:Asia/Kolkata', 'column:UTC'
    exit_original          TEXT,
    exit_epoch             INTEGER,
    exit_local             TEXT,
    exit_tz_source         TEXT,
    exec_seq               INTEGER,
    entry_price            REAL,
    exit_price             REAL,
    direction              TEXT,                 -- LONG | SHORT | NULL
    quantity               REAL,
    pnl                    REAL,
    chrono_seq             INTEGER,              -- NULL while unresolved or unconfirmed
    chrono_status          TEXT NOT NULL,        -- OK | UNRESOLVED
    chrono_issue           TEXT,
    warnings_json          TEXT,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_trades_seq ON trades(chrono_seq);
CREATE INDEX IF NOT EXISTS ix_trades_batch ON trades(batch_id);

CREATE TABLE IF NOT EXISTS timestamp_corrections (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id      INTEGER NOT NULL REFERENCES trades(id),
    field         TEXT NOT NULL,                 -- entry | exit
    old_original  TEXT,
    old_epoch     INTEGER,
    new_input     TEXT NOT NULL,
    new_epoch     INTEGER NOT NULL,
    tz_used       TEXT NOT NULL,
    reason        TEXT,
    corrected_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_bars (
    instrument    TEXT NOT NULL,
    timeframe     TEXT NOT NULL,                 -- '15m' | '1m'
    epoch         INTEGER NOT NULL,              -- bar OPEN time, UTC epoch seconds
    local         TEXT NOT NULL,                 -- bar open, exchange-local 'YYYY-MM-DD HH:MM:SS'
    session_date  TEXT NOT NULL,                 -- exchange-local 'YYYY-MM-DD'
    open          REAL NOT NULL,
    high          REAL NOT NULL,
    low           REAL NOT NULL,
    close         REAL NOT NULL,
    volume        REAL,
    batch_id      INTEGER,
    PRIMARY KEY (instrument, timeframe, epoch)
);
CREATE INDEX IF NOT EXISTS ix_bars_session ON market_bars(instrument, timeframe, session_date);

CREATE TABLE IF NOT EXISTS review_sessions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT,
    created_at        TEXT NOT NULL,
    last_activity_at  TEXT NOT NULL,
    mode              TEXT NOT NULL DEFAULT 'BLIND_CHRONOLOGICAL',
    status            TEXT NOT NULL DEFAULT 'ACTIVE',     -- ACTIVE | ARCHIVED
    timezone          TEXT NOT NULL,
    sort_method       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id            INTEGER NOT NULL REFERENCES review_sessions(id),
    trade_id              INTEGER NOT NULL REFERENCES trades(id),
    status                TEXT NOT NULL,
    chrono_seq_at_start   INTEGER,
    started_at            TEXT,
    thesis_locked_at      TEXT,
    outcome_revealed_at   TEXT,
    completed_at          TEXT,
    skipped_override      INTEGER NOT NULL DEFAULT 0,
    override_reason       TEXT,
    out_of_sequence       INTEGER NOT NULL DEFAULT 0,
    draft_json            TEXT,                  -- editable pre-lock questionnaire answers
    post_draft_json       TEXT,                  -- editable post-outcome answers before completion
    entry_context_json    TEXT,                  -- chart facts at entry (pre-entry data only)
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    UNIQUE (session_id, trade_id)
);

CREATE TABLE IF NOT EXISTS thesis_snapshots (
    review_id     INTEGER PRIMARY KEY REFERENCES reviews(id),
    locked_at     TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    snapshot_text TEXT NOT NULL,
    sha256        TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS thesis_no_update BEFORE UPDATE ON thesis_snapshots
BEGIN SELECT RAISE(ABORT, 'Locked thesis is immutable'); END;
CREATE TRIGGER IF NOT EXISTS thesis_no_delete BEFORE DELETE ON thesis_snapshots
BEGIN SELECT RAISE(ABORT, 'Locked thesis is immutable'); END;

CREATE TABLE IF NOT EXISTS post_outcome (
    review_id     INTEGER PRIMARY KEY REFERENCES reviews(id),
    created_at    TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    outcome_json  TEXT NOT NULL,                 -- outcome metrics as computed at completion
    sha256        TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS post_no_update BEFORE UPDATE ON post_outcome
BEGIN SELECT RAISE(ABORT, 'Original post-outcome journal is immutable'); END;
CREATE TRIGGER IF NOT EXISTS post_no_delete BEFORE DELETE ON post_outcome
BEGIN SELECT RAISE(ABORT, 'Original post-outcome journal is immutable'); END;

CREATE TABLE IF NOT EXISTS journal_revisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id     INTEGER NOT NULL REFERENCES reviews(id),
    section       TEXT NOT NULL,                 -- 'post_outcome' | 'thesis_annotation'
    payload_json  TEXT NOT NULL,
    reason        TEXT,
    created_at    TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS rev_no_update BEFORE UPDATE ON journal_revisions
BEGIN SELECT RAISE(ABORT, 'Revisions are append-only'); END;

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    at           TEXT NOT NULL,
    session_id   INTEGER,
    trade_id     INTEGER,
    review_id    INTEGER,
    event_type   TEXT NOT NULL,
    detail_json  TEXT
);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'Event log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'Event log is append-only'); END;
"""

DEFAULT_SETTINGS = {
    "exchange_timezone": "Asia/Kolkata",
    "ema_length": 15,
    "pivot_method": "classic",            # classic | fibonacci | camarilla
    "lookback_sessions": 3,               # sessions of history shown before entry
    "show_next_timestamp": False,         # show next trade's entry time during blind review
    "process_context_enabled": False,     # show own earlier completed journals during review
    "override_enabled": False,            # administrative chronological override
    "session_open": "09:15",
    "session_close": "15:30",
    "context_threshold_pct": 0.15,        # "near EMA / pivot" threshold for contradiction checks (% of price)
    "declared_rules": [],                 # structured declared rules, see analytics.DECLARED_RULE_TYPES
    "show_prev_day_levels": False,
    "show_vwap": False,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_path() -> Path:
    return Path(os.environ.get("TTM_DB_PATH", DEFAULT_DB_PATH))


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path) if path else db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, check_same_thread=False, isolation_level=None, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


MIGRATIONS = {  # columns added after the first release: table -> [(column, type)]
    "trades": [("initial_entry_price", "REAL"), ("initial_quantity", "REAL"), ("entry_fills", "INTEGER"),
               ("re_add_episodes", "INTEGER NOT NULL DEFAULT 0"), ("exit_kind", "TEXT"), ("settlement_json", "TEXT"),
               ("issue_kind", "TEXT"), ("excluded", "INTEGER NOT NULL DEFAULT 0"), ("exclusion_reason", "TEXT"),
               ("excluded_at", "TEXT")],
}


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, typ in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
    for k, v in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, json.dumps(v)))


@contextmanager
def tx(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def get_settings(conn: sqlite3.Connection) -> dict:
    out = dict(DEFAULT_SETTINGS)
    for row in conn.execute("SELECT key, value FROM settings"):
        out[row["key"]] = json.loads(row["value"])
    return out


def set_setting(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )


def log_event(conn, event_type: str, *, session_id=None, trade_id=None, review_id=None, **detail) -> None:
    conn.execute(
        "INSERT INTO events(at, session_id, trade_id, review_id, event_type, detail_json) VALUES (?,?,?,?,?,?)",
        (now_iso(), session_id, trade_id, review_id, event_type, json.dumps(detail, default=str)),
    )
