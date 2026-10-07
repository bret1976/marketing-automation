"""Autopilot Watch Pack — dead-man's switch for the 8 AM / 5 PM autopost slots.

Why: on 2026-10-05 Autopilot silently missed three slots in a row (Gemini
prepaid credits ran out, YouTube bot-walled yt-dlp, TikTok discovery empty)
and nobody noticed for ~1.5 days. This pack makes that visible at one URL.

Idea inspiration (pattern only, no code copied):
- healthchecks/healthchecks (BSD-3-Clause) — OK / LATE / DOWN states from an
  expected schedule plus a grace period.
- jjdoor/deadcron (MIT) — "overdue = no success within period + grace" and
  consecutive-miss counting.
- missedrun/missedrun-selfhosted (AGPL-3.0) — status vocabulary only; nothing
  vendored.

Original marketing-automation Python. Read-only: it never schedules, posts,
retries, or calls any outside service. It reads the slot ledger the live
Autopilot overlay already writes (STATE_DIR/autonomous_slot.json), the
scheduled-posts log, and a passive log tap that classifies known upstream
failure signatures (Gemini 402 credits, YouTube bot wall, Facebook page_id...).
Kill switch: AUTOPILOT_WATCH=0. No UI — /health marker + JSON API only.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

PACK = "autopilot-watch-v1"
DEFAULT_GRACE_MIN = 75          # overlay retries inside the slot hour; past this the slot is gone
DEFAULT_LOOKBACK_HOURS = 72
MAX_SAMPLE_CHARS = 240
PERSIST_EVERY_SEC = 30

_ROOT = Path(__file__).resolve().parent
_lock = threading.Lock()
_tap: Dict[str, Any] = {"signatures": {}, "slot_attempts": [], "slot_published": [], "started_at": None}
_handler: Optional[logging.Handler] = None
_last_persist = 0.0

# (id, matcher, human hint). Matchers are lowercase substring groups: every
# string in a group must appear; any group matching counts.
SIGNATURES = (
    ("gemini_credits_depleted", (("resource_exhausted", "prepay"), ("resource_exhausted", "credits are depleted")),
     "Gemini prepaid credits are empty - top up at AI Studio (ai.studio/projects) to restore fresh viral scans."),
    ("gemini_rate_limited", (("429", "resource_exhausted"),),
     "Gemini is rate limiting - scans will retry next slot."),
    ("youtube_bot_wall", (("confirm you", "not a bot"),),
     "YouTube is bot-walling yt-dlp downloads - add cookies or rely on TikTok / hosted clips."),
    ("tiktok_discovery_empty", (("tiktok discovery returned 0",),),
     "TikTok discovery found no unused originals this slot."),
    ("no_original_video", (("no original video could be downloaded",),),
     "No source video downloaded - Autopilot falls back to hosted last-resort clips."),
    ("facebook_page_id_missing", (("missing required page_id",),),
     "Facebook skipped - refresh PostProxy placements / reconnect the Facebook page."),
    ("postproxy_placements_failed", (("postproxy placements", "failed"), ("could not fetch postproxy placements",)),
     "PostProxy could not load platform placements - reconnect the affected account in PostProxy."),
)

_REDACT_RE = re.compile(r"(?i)((?:api[_-]?key|key|token|secret|signature|sig|password)=)[^&\s'\"]+")
_SLOT_REACHED_RE = re.compile(r"Autonomous autoposting slot (\S+) reached")
_SLOT_DONE_RE = re.compile(r"Autonomous slot (\S+) published and persisted")


def enabled() -> bool:
    return (os.environ.get("AUTOPILOT_WATCH", "1").strip().lower() not in {"0", "false", "off", "no"})


def _state_dir() -> Path:
    raw = os.environ.get("STATE_DIR") or os.environ.get("DATA_DIR") or str(_ROOT)
    return Path(raw)


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if dt else None


def _pt(dt: datetime) -> Optional[str]:
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%a %b %d %I:%M %p PT").replace(" 0", " ")
    except Exception:
        return None


def redact(text: str) -> str:
    return _REDACT_RE.sub(r"\1[redacted]", text or "")[:MAX_SAMPLE_CHARS]


def classify(text: str) -> List[str]:
    low = (text or "").lower()
    hits = []
    for sig_id, groups, _hint in SIGNATURES:
        if any(all(part in low for part in group) for group in groups):
            hits.append(sig_id)
    # Credits-depleted is the precise reason; don't double count it as rate limiting.
    if "gemini_credits_depleted" in hits and "gemini_rate_limited" in hits:
        hits.remove("gemini_rate_limited")
    return hits


def hint_for(sig_id: str) -> str:
    for s_id, _groups, hint in SIGNATURES:
        if s_id == sig_id:
            return hint
    return ""


# --------------------------------------------------------------------------
# Schedule math
# --------------------------------------------------------------------------

def parse_hours(raw: Any) -> List[int]:
    """Same accepted shapes as the overlay: JSON list, '15,0', or a single int."""
    if raw is None or raw == "":
        return []
    if isinstance(raw, (list, tuple)):
        values = list(raw)
    else:
        text = str(raw).strip()
        try:
            loaded = json.loads(text)
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


def scheduled_hours(settings: Optional[dict] = None) -> List[int]:
    settings = settings or {}
    hours = parse_hours(os.environ.get("AUTONOMOUS_HOURS") or settings.get("autonomous_hours"))
    if not hours:
        hours = parse_hours(os.environ.get("AUTONOMOUS_HOUR") or settings.get("autonomous_hour") or 9)
    return sorted(hours)


def autoposting_on(settings: Optional[dict] = None) -> bool:
    """Mirror the overlay: settings['autonomous_posting'] (main.load_settings already folds env in)."""
    if settings and "autonomous_posting" in settings:
        raw = settings.get("autonomous_posting")
    else:
        raw = os.environ.get("AUTONOMOUS_POSTING", "true")
    return str(raw).strip().lower() not in {"0", "false", "off", "no", "none", ""}


def expected_slots(now: datetime, hours: Iterable[int], lookback_hours: int) -> List[Dict[str, Any]]:
    """Slots whose start time is within (now - lookback, now], oldest first.

    Slot ids match the overlay: f"{date}-{hour}" in container time (UTC on Railway).
    """
    now = now.astimezone(timezone.utc)
    start = now - timedelta(hours=lookback_hours)
    hours = sorted(set(int(h) % 24 for h in hours))
    out = []
    day = start.date()
    while day <= now.date():
        for hour in hours:
            at = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
            if start < at <= now:
                out.append({"slot": f"{day.isoformat()}-{hour}", "at": at})
        day += timedelta(days=1)
    return out


def next_slot(now: datetime, hours: Iterable[int]) -> Optional[datetime]:
    hours = sorted(set(int(h) % 24 for h in hours))
    if not hours:
        return None
    now = now.astimezone(timezone.utc)
    for offset in range(0, 3):
        day = (now + timedelta(days=offset)).date()
        for hour in hours:
            at = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
            if at > now:
                return at
    return None


def load_slot_ledger(state_dir: Optional[Path] = None) -> dict:
    path = (state_dir or _state_dir()) / "autonomous_slot.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def evaluate_slots(now: datetime, hours: List[int], ledger: dict, grace_min: int, lookback_hours: int,
                   attempted: Iterable[str] = ()) -> Dict[str, Any]:
    successes = set(s for s in (ledger.get("success_slots") or []) if s)
    if ledger.get("last_success_slot"):
        successes.add(ledger["last_success_slot"])
    attempted = set(attempted or ())
    rows = []
    for item in expected_slots(now, hours, lookback_hours):
        at = item["at"]
        slot = item["slot"]
        if slot in successes:
            state = "ok"
        elif now < at + timedelta(minutes=grace_min):
            state = "running" if slot in attempted else "due"
        else:
            state = "missed"
        rows.append({"slot": slot, "at_utc": _iso(at), "at_pt": _pt(at), "state": state})
    consecutive = 0
    for row in reversed(rows):
        if row["state"] in ("running", "due"):
            continue
        if row["state"] == "missed":
            consecutive += 1
            continue
        break
    missed = [r["slot"] for r in rows if r["state"] == "missed"]
    if consecutive >= 2:
        status = "down"
    elif consecutive == 1:
        status = "late"
    else:
        status = "ok"
    return {"status": status, "consecutive_missed": consecutive, "missed_slots": missed, "slots": rows}


# --------------------------------------------------------------------------
# Scheduled-posts log scan (FAILED rows + per-platform errors)
# --------------------------------------------------------------------------

def _post_time(post: dict) -> Optional[datetime]:
    for key in ("posted_at", "scheduled_time", "created_at"):
        raw = post.get(key)
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def _post_error_texts(post: dict) -> List[str]:
    texts = []
    if post.get("error_message"):
        texts.append(str(post["error_message"]))
    result = post.get("postproxy_result") or post.get("publish_result") or {}
    if isinstance(result, dict):
        for err in result.get("errors") or []:
            texts.append(str(err))
        for plat in result.get("platforms") or []:
            if isinstance(plat, dict) and (plat.get("error") or plat.get("error_message")):
                texts.append(f"{plat.get('platform', '')}: {plat.get('error') or plat.get('error_message')}")
    return texts


def scan_posts(posts: List[dict], now: datetime, lookback_hours: int) -> Dict[str, Any]:
    since = now - timedelta(hours=lookback_hours)
    failed = 0
    issues: Dict[str, Dict[str, Any]] = {}
    for post in posts or []:
        if not isinstance(post, dict):
            continue
        at = _post_time(post)
        if at is None or at < since:
            continue
        if str(post.get("status", "")).upper() == "FAILED":
            failed += 1
        for text in _post_error_texts(post):
            for sig in classify(text):
                row = issues.setdefault(sig, {"count": 0, "last_seen_utc": None, "sample": ""})
                row["count"] += 1
                if row["last_seen_utc"] is None or _iso(at) > row["last_seen_utc"]:
                    row["last_seen_utc"] = _iso(at)
                    row["sample"] = redact(text)
    return {"failed_rows": failed, "issues": issues}


# --------------------------------------------------------------------------
# Passive log tap
# --------------------------------------------------------------------------

def _tap_file() -> Path:
    return _state_dir() / "autopilot_watch.json"


def _persist(force: bool = False) -> None:
    global _last_persist
    now = time.time()
    if not force and now - _last_persist < PERSIST_EVERY_SEC:
        return
    _last_persist = now
    try:
        path = _tap_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(_tap, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass


def _load_tap() -> None:
    try:
        data = json.loads(_tap_file().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _tap["signatures"] = data.get("signatures") or {}
            _tap["slot_attempts"] = (data.get("slot_attempts") or [])[-40:]
            _tap["slot_published"] = (data.get("slot_published") or [])[-40:]
    except Exception:
        pass


def observe(message: str, when: Optional[datetime] = None) -> List[str]:
    """Record one log line. Returns matched signature ids. Never raises."""
    try:
        text = message or ""
        when_iso = _iso(when or _now())
        hits = classify(text)
        reached = _SLOT_REACHED_RE.search(text)
        done = _SLOT_DONE_RE.search(text)
        if not hits and not reached and not done:
            return []
        with _lock:
            for sig in hits:
                row = _tap["signatures"].setdefault(sig, {"count": 0, "first_seen_utc": when_iso})
                row["count"] = int(row.get("count", 0)) + 1
                row["last_seen_utc"] = when_iso
                row["sample"] = redact(text)
            if reached:
                _tap["slot_attempts"] = (_tap["slot_attempts"] + [{"slot": reached.group(1), "at_utc": when_iso}])[-40:]
            if done:
                _tap["slot_published"] = (_tap["slot_published"] + [{"slot": done.group(1), "at_utc": when_iso}])[-40:]
            _persist()
        return hits
    except Exception:
        return []


class _TapHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - thin wrapper
        try:
            observe(record.getMessage())
        except Exception:
            pass


def install() -> bool:
    """Attach the passive log tap to the root logger once. No-op when disabled."""
    global _handler
    if not enabled() or _handler is not None:
        return False
    _load_tap()
    _tap["started_at"] = _iso(_now())
    _handler = _TapHandler(level=logging.INFO)
    logging.getLogger().addHandler(_handler)
    return True


def reset_for_tests() -> None:
    global _handler, _last_persist
    if _handler is not None:
        logging.getLogger().removeHandler(_handler)
    _handler = None
    _last_persist = 0.0
    _tap.update({"signatures": {}, "slot_attempts": [], "slot_published": [], "started_at": None})


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------

def _active_issues(post_issues: Dict[str, Any], now: datetime, lookback_hours: int) -> List[Dict[str, Any]]:
    since_iso = _iso(now - timedelta(hours=lookback_hours))
    merged: Dict[str, Dict[str, Any]] = {}
    for sig, row in (_tap.get("signatures") or {}).items():
        if (row.get("last_seen_utc") or "") >= since_iso:
            merged[sig] = {"id": sig, "log_count": row.get("count", 0), "post_count": 0,
                           "last_seen_utc": row.get("last_seen_utc"), "sample": row.get("sample", "")}
    for sig, row in (post_issues or {}).items():
        cur = merged.setdefault(sig, {"id": sig, "log_count": 0, "post_count": 0,
                                      "last_seen_utc": row.get("last_seen_utc"), "sample": row.get("sample", "")})
        cur["post_count"] = row.get("count", 0)
        if (row.get("last_seen_utc") or "") > (cur.get("last_seen_utc") or ""):
            cur["last_seen_utc"] = row.get("last_seen_utc")
            cur["sample"] = row.get("sample", "")
    out = []
    for sig, row in merged.items():
        row["hint"] = hint_for(sig)
        out.append(row)
    out.sort(key=lambda r: r.get("last_seen_utc") or "", reverse=True)
    return out


def report(settings: Optional[dict] = None, posts: Optional[List[dict]] = None,
           now: Optional[datetime] = None, state_dir: Optional[Path] = None) -> Dict[str, Any]:
    if not enabled():
        return {"pack": PACK, "enabled": False}
    now = (now or _now()).astimezone(timezone.utc)
    grace = _int_env("AUTOPILOT_WATCH_GRACE_MIN", DEFAULT_GRACE_MIN, 10, 600)
    lookback = _int_env("AUTOPILOT_WATCH_LOOKBACK_HOURS", DEFAULT_LOOKBACK_HOURS, 12, 24 * 14)
    hours = scheduled_hours(settings)
    ledger = load_slot_ledger(state_dir)
    attempted = [a.get("slot") for a in _tap.get("slot_attempts") or []]
    if autoposting_on(settings):
        slots = evaluate_slots(now, hours, ledger, grace, lookback, attempted)
    else:
        slots = {"status": "disabled", "consecutive_missed": 0, "missed_slots": [], "slots": []}
    scanned = scan_posts(posts or [], now, lookback) if posts is not None else {"failed_rows": None, "issues": {}}
    issues = _active_issues(scanned["issues"], now, lookback)
    last_success_at = ledger.get("last_success_at")
    age_hours = None
    if last_success_at:
        try:
            dt = datetime.fromisoformat(str(last_success_at).replace("Z", "+00:00"))
            dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            age_hours = round((now - dt).total_seconds() / 3600, 2)
        except Exception:
            age_hours = None
    nxt = next_slot(now, hours)
    return {
        "pack": PACK,
        "enabled": True,
        "checked_at_utc": _iso(now),
        "status": slots["status"],
        "schedule": {"hours_utc": hours, "grace_min": grace, "lookback_hours": lookback,
                     "next_slot_utc": _iso(nxt), "next_slot_pt": _pt(nxt) if nxt else None,
                     "autoposting_on": autoposting_on(settings)},
        "last_success_slot": ledger.get("last_success_slot"),
        "last_success_at_utc": last_success_at,
        "last_success_age_hours": age_hours,
        "consecutive_missed": slots["consecutive_missed"],
        "missed_slots": slots["missed_slots"],
        "slots": slots["slots"],
        "failed_rows": scanned["failed_rows"],
        "issues": issues,
        "tap": {"installed": _handler is not None, "started_at_utc": _tap.get("started_at"),
                "recent_attempts": (_tap.get("slot_attempts") or [])[-6:],
                "recent_published": (_tap.get("slot_published") or [])[-6:]},
        "read_only": True,
    }


def compact(settings: Optional[dict] = None) -> Dict[str, Any]:
    """Small block for /health (no post-log scan, cheap)."""
    try:
        full = report(settings=settings, posts=None)
        if not full.get("enabled"):
            return full
        return {
            "status": full["status"],
            "last_success_slot": full["last_success_slot"],
            "last_success_age_hours": full["last_success_age_hours"],
            "consecutive_missed": full["consecutive_missed"],
            "missed_slots": len(full["missed_slots"]),
            "next_slot_utc": full["schedule"]["next_slot_utc"],
            "top_issue": (full["issues"][0]["id"] if full["issues"] else None),
        }
    except Exception as exc:  # health must never fail because of this pack
        return {"status": "unknown", "error": str(exc)[:120]}
