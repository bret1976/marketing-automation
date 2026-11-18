"""DST-aware 8 AM / 5 PM PT slots."""
from datetime import datetime, timezone

import slot_schedule as ss


def _utc(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def test_default_hours_are_8_and_17_pt(monkeypatch):
    monkeypatch.delenv("AUTONOMOUS_HOURS", raising=False)
    monkeypatch.delenv("AUTONOMOUS_HOUR", raising=False)
    assert ss.scheduled_hours({}) == [8, 17]


def test_legacy_utc_pairs_map_to_local_8_17(monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_HOURS", "15,0")
    assert ss.scheduled_hours({}) == [8, 17]
    monkeypatch.setenv("AUTONOMOUS_HOURS", "16,1")
    assert ss.scheduled_hours({}) == [8, 17]
    monkeypatch.setenv("AUTONOMOUS_HOURS", "8,17")
    assert ss.scheduled_hours({}) == [8, 17]


def test_custom_local_hour_is_kept(monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_HOURS", "12")
    assert ss.scheduled_hours({}) == [12]


def test_pdt_morning_slot_is_8am_local():
    # 2026-10-07 is PDT (UTC-7). 15:05 UTC = 8:05 AM PT.
    now = _utc("2026-10-07T15:05:00")
    assert ss.as_local(now).hour == 8
    assert ss.due_slot_id(now, [8, 17]) == "2026-10-07-8"
    assert ss.next_slot(_utc("2026-10-07T13:00:00"), [8, 17]) == _utc("2026-10-07T15:00:00")


def test_pst_evening_slot_is_5pm_local():
    # 2026-11-18 01:05 UTC = 2026-11-17 5:05 PM PST.
    now = _utc("2026-11-18T01:05:00")
    assert ss.as_local(now).hour == 17
    assert ss.due_slot_id(now, [8, 17]) == "2026-11-17-17"
    assert ss.due_slot_id(_utc("2026-11-18T00:30:00"), [8, 17]) is None  # 4:30 PM PT, before the slot
    assert ss.next_slot(_utc("2026-11-17T20:00:00"), [8, 17]) == _utc("2026-11-18T01:00:00")


def test_naive_datetime_is_treated_as_utc():
    naive = datetime(2026, 11, 18, 1, 5)
    assert ss.due_slot_id(naive, [8, 17]) == "2026-11-17-17"


def test_expected_slots_use_local_ids_across_dst():
    # 13:00 UTC Oct 7 = 6 AM PDT. Lookback 48h.
    slots = ss.expected_slots(_utc("2026-10-07T13:00:00"), [8, 17], 48)
    assert [s["slot"] for s in slots] == ["2026-10-05-8", "2026-10-05-17", "2026-10-06-8", "2026-10-06-17"]
    # After the fall-back, 20:00 UTC Nov 17 = 12 PM PST.
    slots = ss.expected_slots(_utc("2026-11-17T20:00:00"), [8, 17], 24)
    assert [s["slot"] for s in slots] == ["2026-11-16-17", "2026-11-17-8"]


def test_march_dst_keeps_8am_pt():
    # Second Sunday of March 2027 is March 14. The following Monday 8 AM PDT = 15:00 UTC.
    now = _utc("2027-03-15T15:05:00")
    assert ss.as_local(now).hour == 8
    assert ss.due_slot_id(now, [8, 17]) == "2027-03-15-8"
