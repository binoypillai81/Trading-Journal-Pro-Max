"""FastAPI application: JSON API + static single-page frontend."""
from __future__ import annotations

import io
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import analytics, chronology, exclusions, importer, market, questionnaire, review
from .db import connect, get_settings, init_db, log_event, set_setting, tx
from .timeutil import renormalize_all, valid_timezone

STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Trade Time Machine", version="1.0")


@app.middleware("http")
async def _revalidate_static(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"  # always revalidate (ETag) so updates are picked up
    return response


def get_conn():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@app.on_event("startup")
def _startup():
    c = connect()
    init_db(c)
    c.close()


@app.exception_handler(review.LockedError)
def _locked(_: Request, exc: Exception):
    return JSONResponse(status_code=423, content={"detail": str(exc), "code": "CHRONOLOGICALLY_LOCKED"})


@app.exception_handler(review.StateError)
def _state(_: Request, exc: Exception):
    return JSONResponse(status_code=409, content={"detail": str(exc), "code": "INVALID_STATE"})


@app.exception_handler(PermissionError)
def _perm(_: Request, exc: Exception):
    return JSONResponse(status_code=409, content={"detail": str(exc), "code": "NOT_ALLOWED"})


@app.exception_handler(LookupError)
def _missing(_: Request, exc: Exception):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(ValueError)
def _bad(_: Request, exc: Exception):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(sqlite3.IntegrityError)
def _integrity(_: Request, exc: Exception):
    return JSONResponse(status_code=409, content={"detail": f"Integrity protection: {exc}"})


# ----------------------------------------------------------------------------- meta & settings

@app.get("/api/meta")
def meta(conn=Depends(get_conn)):
    return {"questionnaire": questionnaire.schema(), "settings": get_settings(conn), "states": review.STATES,
            "sort_method": chronology.SORT_METHOD, "pivot_formulas": market.PIVOT_DOC, "pivot_source": market.PIVOT_SOURCE_NOTE,
            "trade_fields": importer.TRADE_FIELDS, "fill_mode_labels": importer.FILL_MODE_LABELS, "declared_rule_types": analytics.DECLARED_RULE_TYPES,
            "analytics_scopes": analytics.SCOPES, "coverage": market.coverage(conn), "exclusion_reasons": exclusions.REASONS,
            "counts": {"trades_in_sequence": conn.execute("SELECT COUNT(*) FROM trades WHERE chrono_seq IS NOT NULL").fetchone()[0],
                       "unresolved": len(importer.unresolved_trades(conn)),
                       "without_market_data": exclusions.coverage(conn)["trades_without_data"],
                       "excluded": conn.execute("SELECT COUNT(*) FROM trades WHERE excluded=1").fetchone()[0]}}


EDITABLE = {"exchange_timezone", "ema_length", "atr_length", "pivot_method", "lookback_sessions", "show_next_timestamp",
            "process_context_enabled", "override_enabled", "context_threshold_pct", "declared_rules",
            "show_prev_day_levels", "show_vwap"}


@app.put("/api/settings")
def update_settings(changes: dict = Body(...), conn=Depends(get_conn)):
    bad = set(changes) - EDITABLE
    if bad:
        raise ValueError(f"Not editable: {sorted(bad)}")
    current = get_settings(conn)
    if "exchange_timezone" in changes and not valid_timezone(changes["exchange_timezone"]):
        raise ValueError("Unknown timezone")
    if "pivot_method" in changes and changes["pivot_method"] not in market.PIVOT_DOC:
        raise ValueError("pivot_method must be classic, fibonacci or camarilla")
    if "ema_length" in changes and not (2 <= int(changes["ema_length"]) <= 500):
        raise ValueError("ema_length must be 2..500")
    if "atr_length" in changes and not (2 <= int(changes["atr_length"]) <= 200):
        raise ValueError("atr_length must be 2..200")
    for r in changes.get("declared_rules") or []:
        if r.get("type") not in analytics.DECLARED_RULE_TYPES:
            raise ValueError(f"Unknown declared rule type {r.get('type')}")
    with tx(conn):
        for k, v in changes.items():
            if current.get(k) == v:
                continue
            set_setting(conn, k, v)
            if k == "override_enabled":
                log_event(conn, "OVERRIDE_SETTING_CHANGED", enabled=v)
            elif k == "exchange_timezone":
                renormalize_all(conn, v)
                log_event(conn, "TIMEZONE_CHANGED", old=current[k], new=v)
            else:
                log_event(conn, "SETTING_CHANGED", key=k, old=current.get(k), new=v)
    return get_settings(conn)


# ----------------------------------------------------------------------------- trade import

@app.post("/api/import/trades/upload")
async def upload_trades(file: UploadFile = File(...), conn=Depends(get_conn)):
    return importer.create_upload(conn, file.filename, await file.read())


@app.get("/api/import/trades/{bid}/samples")
def trade_samples(bid: int, conn=Depends(get_conn)):
    return importer.sample_rows(conn, bid)


@app.post("/api/import/trades/{bid}/stage")
def stage_trades(bid: int, body: dict = Body(...), conn=Depends(get_conn)):
    return importer.stage(conn, bid, body.get("mapping") or {}, body.get("options") or {})


@app.get("/api/import/trades/{bid}/preview")
def preview_trades(bid: int, conn=Depends(get_conn)):
    return importer.preview(conn, bid)


@app.post("/api/import/trades/{bid}/confirm")
def confirm_trades(bid: int, body: dict = Body(...), conn=Depends(get_conn)):
    if not body.get("acknowledge_order"):
        raise ValueError("Confirm: 'These trades will be reviewed in this chronological order.'")
    return importer.confirm(conn, bid)


@app.post("/api/import/trades/{bid}/cancel")
def cancel_trades(bid: int, conn=Depends(get_conn)):
    importer.cancel(conn, bid)
    return {"cancelled": True}


@app.get("/api/import/batches")
def batches(conn=Depends(get_conn)):
    return [dict(r) for r in conn.execute(
        "SELECT id, kind, filename, created_at, status, confirmed_at, summary_json FROM import_batches ORDER BY id DESC")]


@app.post("/api/import/trades/{bid}/resolve-contract")
def resolve_contract(bid: int, body: dict = Body(...), conn=Depends(get_conn)):
    return importer.resolve_contract(conn, bid, body.get("symbol", ""), body.get("decision", ""))


@app.get("/api/trades/coverage")
def trade_coverage(conn=Depends(get_conn)):
    return exclusions.coverage(conn)


@app.post("/api/trades/exclude")
def exclude_trades(body: dict = Body(...), conn=Depends(get_conn)):
    return exclusions.exclude(conn, reason=body.get("reason", ""), note=body.get("note"), trade_ids=body.get("trade_ids"),
                              no_market_data=bool(body.get("no_market_data")), instrument=body.get("instrument"))


@app.post("/api/trades/restore")
def restore_trades(body: dict = Body(...), conn=Depends(get_conn)):
    return exclusions.restore(conn, trade_ids=body.get("trade_ids"), all_trades=bool(body.get("all")))


@app.get("/api/trades/excluded")
def excluded_trades(conn=Depends(get_conn)):
    return exclusions.excluded_list(conn)


@app.get("/api/trades/unresolved")
def unresolved(conn=Depends(get_conn)):
    return importer.unresolved_trades(conn)


@app.post("/api/trades/{tid}/resolve-timestamp")
def resolve(tid: int, body: dict = Body(...), conn=Depends(get_conn)):
    return importer.resolve_timestamp(conn, tid, body.get("value", ""), body.get("timezone", ""), body.get("reason"),
                                      body.get("field", "entry"))


# ----------------------------------------------------------------------------- market data

@app.post("/api/import/market/preview")
async def market_preview(file: UploadFile = File(...)):
    return market.market_upload_preview(await file.read())


@app.post("/api/import/market/commit")
async def market_commit(file: UploadFile = File(...), mapping: str = Form(...), instrument: str = Form(...),
                        timeframe: str = Form("auto"), naive_timezone: str = Form(""), label: str = Form("open"),
                        conn=Depends(get_conn)):
    import json
    content = await file.read()
    return market.import_market(conn, io.BytesIO(content), mapping=json.loads(mapping), instrument=instrument,
                                timeframe=timeframe, naive_timezone=naive_timezone or None, label=label, filename=file.filename)


@app.post("/api/import/market/from-path")
def market_from_path(body: dict = Body(...), conn=Depends(get_conn)):
    p = Path(body["path"]).expanduser()
    if not p.is_file():
        raise ValueError(f"File not found: {p}")
    return market.import_market(conn, str(p), mapping=body["mapping"], instrument=body["instrument"],
                                timeframe=body.get("timeframe", "auto"), naive_timezone=body.get("naive_timezone") or None,
                                label=body.get("label", "open"), filename=p.name)


@app.get("/api/market/coverage")
def coverage(conn=Depends(get_conn)):
    return market.coverage(conn)


# ----------------------------------------------------------------------------- sessions & chronological review

@app.get("/api/sessions")
def sessions(conn=Depends(get_conn)):
    return review.list_sessions(conn)


@app.post("/api/sessions")
def new_session(body: dict = Body(default={}), conn=Depends(get_conn)):
    return review.create_session(conn, body.get("name"))


@app.post("/api/sessions/{sid}/resume")
def resume(sid: int, conn=Depends(get_conn)):
    with tx(conn):
        review.get_session(conn, sid)
        review.touch(conn, sid, "SESSION_RESUMED")
    return review.get_session(conn, sid)


@app.post("/api/sessions/{sid}/archive")
def archive(sid: int, conn=Depends(get_conn)):
    review.archive_session(conn, sid)
    return {"archived": True}


@app.get("/api/sessions/{sid}/queue")
def session_queue(sid: int, window: int | None = None, conn=Depends(get_conn)):
    return review.queue(conn, sid, window)


@app.get("/api/sessions/{sid}/current")
def session_current(sid: int, conn=Depends(get_conn)):
    return review.current(conn, sid)


@app.post("/api/sessions/{sid}/current/start")
def session_start(sid: int, conn=Depends(get_conn)):
    return review.start_current(conn, sid)


@app.post("/api/sessions/{sid}/current/skip")
def session_skip(sid: int, body: dict = Body(...), conn=Depends(get_conn)):
    return review.skip_with_override(conn, sid, body.get("reason", ""), body.get("confirmation", ""))


@app.post("/api/sessions/{sid}/current/exclude")
def session_exclude_current(sid: int, body: dict = Body(...), conn=Depends(get_conn)):
    prog = review.progress(conn, sid)
    if not prog["current_trade_id"]:
        raise review.StateError("No current trade")
    return {**exclusions.exclude(conn, reason=body.get("reason", ""), note=body.get("note"),
                                 trade_ids=[prog["current_trade_id"]], session_id=sid),
            "trade_id": prog["current_trade_id"]}


@app.get("/api/sessions/{sid}/completed")
def session_completed(sid: int, conn=Depends(get_conn)):
    return review.completed_list(conn, sid)


@app.get("/api/sessions/{sid}/events")
def session_events(sid: int, conn=Depends(get_conn)):
    return review.events(conn, sid)


@app.get("/api/reviews/{rid}")
def get_review(rid: int, conn=Depends(get_conn)):
    return review.get_review(conn, rid)


@app.get("/api/reviews/{rid}/chart")
def review_chart(rid: int, conn=Depends(get_conn)):
    return review.chart(conn, rid)


@app.put("/api/reviews/{rid}/draft")
def review_draft(rid: int, answers: dict = Body(...), conn=Depends(get_conn)):
    return review.save_draft(conn, rid, answers)


@app.post("/api/reviews/{rid}/validate")
def review_validate(rid: int, answers: dict = Body(...), conn=Depends(get_conn)):
    return review.validate_thesis(conn, rid, answers)


@app.post("/api/reviews/{rid}/lock")
def review_lock(rid: int, body: dict = Body(...), conn=Depends(get_conn)):
    return review.lock_thesis(conn, rid, body.get("answers") or {}, bool(body.get("confirm")))


@app.post("/api/reviews/{rid}/reveal")
def review_reveal(rid: int, conn=Depends(get_conn)):
    return review.reveal(conn, rid)


@app.get("/api/reviews/{rid}/outcome")
def review_outcome(rid: int, conn=Depends(get_conn)):
    return review.get_outcome(conn, rid)


@app.put("/api/reviews/{rid}/post-draft")
def review_post_draft(rid: int, answers: dict = Body(...), conn=Depends(get_conn)):
    return review.save_post_draft(conn, rid, answers)


@app.post("/api/reviews/{rid}/post-validate")
def review_post_validate(rid: int, answers: dict = Body(...), conn=Depends(get_conn)):
    review.access(conn, rid)
    return questionnaire.validate("post", answers)


@app.post("/api/reviews/{rid}/complete")
def review_complete(rid: int, answers: dict = Body(...), conn=Depends(get_conn)):
    return review.complete(conn, rid, answers)


@app.post("/api/reviews/{rid}/revise")
def review_revise(rid: int, body: dict = Body(...), conn=Depends(get_conn)):
    return review.revise(conn, rid, body.get("section", ""), body.get("answers") or {}, body.get("reason"))


# ----------------------------------------------------------------------------- analytics

@app.get("/api/analytics")
def get_analytics(session_id: int | None = None, scope: str = "session_before_current", upto: int | None = None,
                  acknowledge_full_history: bool = False, conn=Depends(get_conn)):
    return analytics.analytics(conn, session_id=session_id, scope=scope, upto=upto,
                               acknowledge_full_history=acknowledge_full_history)


# ----------------------------------------------------------------------------- frontend

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})
