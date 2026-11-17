"""PT schedules: 7 AM researcher + analytics (+ daily research), 7 PM bookmarks + chief.

Hooks into the existing in-process asyncio scheduler style. Does not unpause Autopilot.
Does not publish.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from content_team import store
from content_team.roles import run_analytics, run_bookmarks, run_chief, run_competitors, run_qt_scout, run_researcher

logger = logging.getLogger("content_team.scheduler")

PT = ZoneInfo("America/Los_Angeles")
MORNING_HOUR = 7
EVENING_HOUR = 19
TICK_SECONDS = 30

scheduler_started = False


def now_pt(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(PT)
    if now.tzinfo is None:
        return now.replace(tzinfo=PT)
    return now.astimezone(PT)


def status() -> dict:
    state = store.scheduler_state()
    return {
        "started": scheduler_started,
        "timezone": "America/Los_Angeles",
        "morning_hour": MORNING_HOUR,
        "evening_hour": EVENING_HOUR,
        "last_morning_date": state.get("last_morning_date"),
        "last_evening_date": state.get("last_evening_date"),
        "last_error": state.get("last_error"),
    }


def due_jobs(now: datetime | None = None) -> list[str]:
    here = now_pt(now)
    today = here.date().isoformat()
    state = store.scheduler_state()
    jobs: list[str] = []
    if here.hour == MORNING_HOUR and state.get("last_morning_date") != today:
        jobs.append("morning")
    if here.hour == EVENING_HOUR and state.get("last_evening_date") != today:
        jobs.append("evening")
    return jobs


def run_morning() -> dict:
    results = {}
    for name, fn in (
        ("researcher", run_researcher),
        ("analytics", run_analytics),
        ("competitors", run_competitors),
        ("qt_scout", run_qt_scout),
    ):
        try:
            results[name] = {"ok": True, "draft_id": fn().get("draft_id")}
        except Exception as exc:  # noqa: BLE001
            logger.exception("content team morning job %s failed", name)
            results[name] = {"ok": False, "error": str(exc)}
    return results


def run_evening() -> dict:
    results = {}
    for name, fn in (("bookmarks", run_bookmarks), ("chief", run_chief)):
        try:
            results[name] = {"ok": True, "draft_id": fn().get("draft_id")}
        except Exception as exc:  # noqa: BLE001
            logger.exception("content team evening job %s failed", name)
            results[name] = {"ok": False, "error": str(exc)}
    return results


def tick(now: datetime | None = None, *, morning: Callable = run_morning, evening: Callable = run_evening) -> list[str]:
    here = now_pt(now)
    today = here.date().isoformat()
    ran: list[str] = []
    state = store.scheduler_state()
    for job in due_jobs(here):
        try:
            if job == "morning":
                morning()
                state["last_morning_date"] = today
            elif job == "evening":
                evening()
                state["last_evening_date"] = today
            ran.append(job)
            state["last_error"] = None
        except Exception as exc:  # noqa: BLE001
            state["last_error"] = str(exc)
            logger.exception("content team %s tick failed", job)
        store.save_scheduler_state(state)
    return ran


async def scheduler_loop(stop: asyncio.Event | None = None) -> None:
    global scheduler_started
    scheduler_started = True
    logger.info("Content Team scheduler started (America/Los_Angeles, 7 AM / 7 PM)")
    while stop is None or not stop.is_set():
        try:
            tick()
        except Exception:  # noqa: BLE001
            logger.exception("content team scheduler tick failed")
        if stop is None:
            await asyncio.sleep(TICK_SECONDS)
            continue
        try:
            await asyncio.wait_for(stop.wait(), timeout=TICK_SECONDS)
        except asyncio.TimeoutError:
            continue
