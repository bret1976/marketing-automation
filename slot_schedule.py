"""Autopilot slot hours in America/Los_Angeles.

The advertised slots are 8 AM and 5 PM Pacific. Hours are defined in that
timezone so they stay on those local clock times through DST (they used to be
hard-coded as UTC 15:00 / 00:00, which is 8 AM / 5 PM only during PDT).

Slot ids are `{local_date}-{local_hour}`, e.g. `2026-11-17-17` for 5 PM PT
on Nov 17. AUTONOMOUS_HOURS is read as local hours in AUTONOMOUS_TZ
(default America/Los_Angeles). The legacy UTC pairs 15,0 (PDT) and 16,1 (PST)
are treated as 8,17 local so an old env value does not shift the slots.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("slot_schedule")

PACK_TZ = "America/Los_Angeles"
DEFAULT_HOURS = [8, 17]
# Old AUTONOMOUS_HOURS values that meant "8 AM / 5 PM PT" as UTC hours.
_LEGACY_UTC_PAIRS = {frozenset({15, 0}), frozenset({16, 1})}


def slot_tz_name() -> str:
    return (os.environ.get("AUTONOMOUS_TZ") or PACK_TZ).strip() or PACK_TZ


def slot_zone() -> ZoneInfo:
    try:
        return ZoneInfo(slot_tz_name())
    except Exception:
        return ZoneInfo(PACK_TZ)


def parse_hours(raw: Any) -> List[int]:
    """JSON list, '8,17', or a single int. Does not apply defaults or legacy mapping."""
    if raw is None or raw == "":
        return []
    if isinstance(raw, (list, tuple)):
        values = list(raw)
    else:
        text = str(raw).strip()
        try:
            loaded = __import__("json").loads(text)
            values = loaded if isinstance(loaded, list) else [loaded]
        except Exception:
            values = [p.strip() for p in text.split(",") if p.strip()]
    out: List[int] = []
    for value in values:
        try:
            hour = int(value) % 24
        except (TypeError, ValueError):
            continue
        if hour not in out:
            out.append(hour)
    return out


def normalize_hours(hours: List[int]) -> List[int]:
    """Default 8 and 17 PT; map the old UTC 8 AM / 5 PM pairs to local hours."""
    if not hours:
        return list(DEFAULT_HOURS)
    if frozenset(hours) in _LEGACY_UTC_PAIRS:
        logger.info("AUTONOMOUS_HOURS %s is a legacy UTC 8 AM / 5 PM pair; using local %s instead",
                    hours, DEFAULT_HOURS)
        return list(DEFAULT_HOURS)
    return list(hours)


def scheduled_hours(settings: Optional[dict] = None) -> List[int]:
    settings = settings or {}
    hours = parse_hours(os.environ.get("AUTONOMOUS_HOURS") or settings.get("autonomous_hours"))
    if not hours:
        hours = parse_hours(os.environ.get("AUTONOMOUS_HOUR") or settings.get("autonomous_hour"))
    return normalize_hours(hours)


def as_utc(now: datetime) -> datetime:
    """Naive datetimes are treated as UTC (Railway's container clock)."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def as_local(now: datetime) -> datetime:
    return as_utc(now).astimezone(slot_zone())


def slot_id(now: datetime) -> str:
    local = as_local(now)
    return f"{local.date().isoformat()}-{local.hour}"


def local_slot_at(day, hour: int) -> datetime:
    """Timezone-aware local datetime for `hour` on `day`, as UTC."""
    at = datetime(day.year, day.month, day.day, int(hour) % 24, tzinfo=slot_zone())
    return at.astimezone(timezone.utc)


def due_slot_id(now: datetime, hours: Iterable[int]) -> Optional[str]:
    local = as_local(now)
    hours = list(hours)
    if local.hour not in hours:
        return None
    return f"{local.date().isoformat()}-{local.hour}"


def expected_slots(now: datetime, hours: Iterable[int], lookback_hours: int) -> List[Dict[str, Any]]:
    """Slots whose start is within (now - lookback, now], oldest first.

    Slot ids are `{local_date}-{local_hour}` in AUTONOMOUS_TZ. `at` is UTC.
    """
    now = as_utc(now)
    start = now - timedelta(hours=lookback_hours)
    hours = sorted(set(int(h) % 24 for h in hours))
    tz = slot_zone()
    out: List[Dict[str, Any]] = []
    day = start.astimezone(tz).date()
    last = now.astimezone(tz).date()
    while day <= last:
        for hour in hours:
            at = datetime(day.year, day.month, day.day, hour, tzinfo=tz).astimezone(timezone.utc)
            if start < at <= now:
                out.append({"slot": f"{day.isoformat()}-{hour}", "at": at})
        day += timedelta(days=1)
    return out


def next_slot(now: datetime, hours: Iterable[int]) -> Optional[datetime]:
    hours = sorted(set(int(h) % 24 for h in hours))
    if not hours:
        return None
    now = as_utc(now)
    tz = slot_zone()
    local = now.astimezone(tz)
    for offset in range(0, 4):
        day = (local + timedelta(days=offset)).date()
        for hour in hours:
            at = datetime(day.year, day.month, day.day, hour, tzinfo=tz).astimezone(timezone.utc)
            if at > now:
                return at
    return None


def utc_hours_today(now: Optional[datetime] = None, hours: Optional[Iterable[int]] = None) -> List[int]:
    """UTC hours of today's (and yesterday's overlapping) local slots, for display only."""
    now = as_utc(now or datetime.now(timezone.utc))
    hours = list(hours) if hours is not None else scheduled_hours()
    seen: List[int] = []
    local = now.astimezone(slot_zone())
    for offset in (-1, 0, 1):
        day = (local + timedelta(days=offset)).date()
        for hour in hours:
            at = datetime(day.year, day.month, day.day, hour, tzinfo=slot_zone()).astimezone(timezone.utc)
            h = at.hour
            if h not in seen and abs((at - now).total_seconds()) < 36 * 3600:
                seen.append(h)
    return seen
