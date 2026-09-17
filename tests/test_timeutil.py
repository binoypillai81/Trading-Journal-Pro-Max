from datetime import datetime, timezone

from app.timeutil import parse_timestamp, to_local

IST = "Asia/Kolkata"


def local(p):
    return to_local(p.epoch, IST)


def test_offset_variants_equal():
    a = parse_timestamp("2026-01-15T10:42:00+05:30")
    b = parse_timestamp("2026-01-15T05:12:00Z")
    c = parse_timestamp("15-Jan-2026 10:42:00 +0530")
    d = parse_timestamp("2026-01-15 10:42 IST")
    assert a.epoch == b.epoch == c.epoch == d.epoch
    assert d.warning and "India Standard Time" in d.warning


def test_naive_requires_timezone():
    p = parse_timestamp("2026-01-15 10:42:00")
    assert p.epoch is None and "no timezone" in p.issue
    q = parse_timestamp("2026-01-15 10:42:00", naive_tz=IST)
    assert local(q) == "2026-01-15 10:42:00" and q.source == "assumed:Asia/Kolkata"


def test_separate_date_time_and_ampm():
    p = parse_timestamp("15/01/2026", time_value="10:42:05 AM", naive_tz=IST)
    assert local(p) == "2026-01-15 10:42:05" and not p.ambiguous_date
    p = parse_timestamp("Jan 15, 2026", time_value="2:05 PM", naive_tz=IST)
    assert local(p) == "2026-01-15 14:05:00"


def test_ambiguous_numeric_date_flagged():
    p = parse_timestamp("03/04/2026 10:00", naive_tz=IST)
    assert p.ambiguous_date
    assert local(parse_timestamp("03/04/2026 10:00", naive_tz=IST, date_order="MDY")) == "2026-03-04 10:00:00"


def test_missing_and_date_only():
    assert parse_timestamp("").issue == "Missing entry timestamp"
    assert "No time component" in parse_timestamp("2026-01-15", naive_tz=IST).issue


def test_dst_gap_and_overlap_flagged():
    gap = parse_timestamp("2026-03-08 02:30:00", naive_tz="America/New_York")
    assert gap.epoch is None and "Non-existent" in gap.issue
    overlap = parse_timestamp("2026-11-01 01:30:00", naive_tz="America/New_York")
    assert overlap.epoch is None and "Ambiguous" in overlap.issue


def test_epoch_values():
    e = int(datetime(2026, 1, 15, 5, 12, tzinfo=timezone.utc).timestamp())
    assert parse_timestamp(str(e)).epoch == e
    assert parse_timestamp(str(e * 1000)).epoch == e


def test_trailing_date_not_read_as_offset():
    p = parse_timestamp("10:42 15-01-2026", naive_tz=IST)
    assert local(p) == "2026-01-15 10:42:00"
