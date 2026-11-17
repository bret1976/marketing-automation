"""Morning analytics from already-collected PostProxy / Growth V2 data. No X API."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _in_window(ts: str | None, start: datetime, end: datetime) -> bool:
    dt = _parse(ts)
    return bool(dt and start <= dt < end)


def build_report(*, now: datetime | None = None) -> dict:
    """Last 24 hours of stored impressions, engagements, replies, and follower deltas."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    start = now - timedelta(hours=24)
    impressions = 0
    engagements = 0
    replies = 0
    posts_counted = 0
    posts_missing = 0
    samples: list[dict] = []
    metrics_available = False
    error = None

    try:
        import growth_v2

        summary = growth_v2.analytics_summary()
        metrics_available = True
        for row in summary.get("posts") or []:
            posted = row.get("posted_at")
            if not _in_window(posted, start, now):
                continue
            metrics = row.get("metrics") or {}
            views = metrics.get("views") if isinstance(metrics, dict) else row.get("views")
            eng = metrics.get("engagements") if isinstance(metrics, dict) else row.get("engagements")
            if views is None and eng is None and row.get("views") is None:
                posts_missing += 1
                continue
            posts_counted += 1
            v = int(views or row.get("views") or 0)
            e = int(eng or row.get("engagements") or 0)
            impressions += v
            engagements += e
            # Replies live in per-platform metrics when PostProxy provided them.
            reply_count = 0
            platforms = (metrics.get("platforms") or {}) if isinstance(metrics, dict) else {}
            if isinstance(platforms, dict):
                for payload in platforms.values():
                    if isinstance(payload, dict):
                        reply_count += int(payload.get("comments") or payload.get("replies") or 0)
            replies += reply_count
            samples.append({
                "id": row.get("id"),
                "title": row.get("campaign_title"),
                "posted_at": posted,
                "views": v,
                "engagements": e,
                "replies": reply_count,
            })
        followers = _follower_delta(growth_v2.load_state(), start, now)
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
        followers = {"error": error, "by_platform": {}, "deltas": {}}

    return {
        "window_start": start.isoformat(),
        "window_end": now.isoformat(),
        "window_label": "last 24 hours",
        "timezone_note": "America/Los_Angeles for the scheduled 7 AM run; timestamps are stored in UTC.",
        "impressions_or_views": impressions if posts_counted else None,
        "engagements": engagements if posts_counted else None,
        "replies": replies if posts_counted else None,
        "posts_with_metrics": posts_counted,
        "posts_without_metrics": posts_missing,
        "follower_deltas": followers.get("deltas") if isinstance(followers, dict) else {},
        "followers_latest": followers.get("latest") if isinstance(followers, dict) else {},
        "followers_previous": followers.get("previous") if isinstance(followers, dict) else {},
        "samples": samples[:25],
        "metrics_available": metrics_available,
        "missing_fields": _missing(posts_counted, impressions, replies, followers),
        "error": error,
        "source": "postproxy_stored_metrics",
        "x_api_used": False,
    }


def _follower_delta(state: dict, start: datetime, end: datetime) -> dict:
    current = (state or {}).get("followers") or {}
    latest = {}
    if isinstance(current.get("by_platform"), dict):
        latest = {k: (v.get("followers") if isinstance(v, dict) else v) for k, v in current["by_platform"].items()}
    history = list((state or {}).get("follower_history") or [])
    previous_row = None
    for row in history:
        at = _parse(row.get("at") if isinstance(row, dict) else None)
        if at and at <= start:
            previous_row = row
    # If nothing is at or before the window start, use the earliest row inside the window as baseline.
    if previous_row is None:
        for row in history:
            at = _parse(row.get("at") if isinstance(row, dict) else None)
            if at and start <= at < end:
                previous_row = row
                break
    previous = {}
    if isinstance(previous_row, dict):
        previous = {k: v for k, v in previous_row.items() if k != "at" and isinstance(v, int)}
    deltas = {}
    for key, value in latest.items():
        try:
            now_v = int(value)
        except (TypeError, ValueError):
            continue
        if key in previous:
            deltas[key] = now_v - int(previous[key])
        else:
            deltas[key] = None  # known now, no baseline
    return {"latest": latest, "previous": previous, "deltas": deltas, "current_at": current.get("at")}


def _missing(posts_counted: int, impressions: int, replies: int, followers: dict) -> list[str]:
    missing = []
    if not posts_counted:
        missing.append("no posts with stored metrics in the last 24 hours")
    if isinstance(followers, dict) and not followers.get("deltas"):
        missing.append("no follower baseline for a 24h delta (need two PostProxy snapshots)")
    return missing
