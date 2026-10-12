"""Spend Guard Pack (spend-guard-v1) — caps on the hub's paid/outbound API routes.

The marketing hub runs without a login wall (REQUIRE_ADMIN_AUTH is opt-in), so
the routes that spend money (Veo/fal video renders, Gemini analysis/scans),
fire the full Autopilot, or post to social accounts are reachable by anyone who
finds the URL. This pack adds a "denial-of-wallet" backstop in front of them:

- per-client sliding window (default 1 hour) per route class, in memory;
- global per-UTC-day ceiling per route class, persisted to STATE_DIR so a
  restart does not reset the daily total;
- reserve-before / refund-after: a request is counted when it starts and
  refunded if the route answers with an error (validation, 4xx/5xx), so only
  accepted work burns budget, and concurrent floods cannot slip past the check.

Blocked requests get HTTP 429 + Retry-After and never reach the route, so no
provider call, render, or post happens. Scheduled Autopilot slots run inside the
process (not over HTTP) and are never counted or blocked.

Env:
  SPEND_GUARD=0                              kill switch (pass everything)
  SPEND_GUARD_WINDOW_SEC                     per-client window (default 3600)
  SPEND_GUARD_<CLASS>_PER_CLIENT             per-client limit per window
  SPEND_GUARD_<CLASS>_DAILY                  global limit per UTC day
     (CLASS = VIDEO_GEN | AI_CALL | AUTOPILOT | PUBLISH | UPLOAD; 0 = no cap)
  SPEND_GUARD_BYPASS_TOKEN                   optional; X-Spend-Guard-Bypass header
                                             equal to it skips the guard

Idea inspiration (no code copied):
- laurentS/slowapi (MIT) — per-route limits keyed by client address, 429 + Retry-After.
- m24927605/agentic-spendguard (Apache-2.0) — reserve budget before the provider
  call, settle/refund after.
- jonahberg/the-blue-board PR #235 — sliding window plus a daily cap so a
  resetting window cannot become an unbounded daily total; 0 = kill switch.
- OWASP LLM Top 10 (LLM10 Unbounded Consumption / denial of wallet).
"""
from __future__ import annotations

import hmac
import json
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, Optional, Tuple

PACK = "spend-guard-v1"

# class -> (per-client per window, global per UTC day)
DEFAULT_LIMITS: Dict[str, Tuple[int, int]] = {
    "video_gen": (6, 40),     # Veo / fal / multi-scene / variant renders
    "ai_call": (40, 400),     # Gemini analysis, trend scans, scoring, refreshes
    "autopilot": (4, 16),     # manual "run Autopilot now" (full pipeline + post)
    "publish": (30, 150),     # outbound posts / replies / approvals
    "upload": (30, 200),      # video uploads (disk on the Railway volume)
}

_ROUTES = [
    ("POST", r"/api/generate-video", "video_gen"),
    ("POST", r"/api/generate-video-variants", "video_gen"),
    ("POST", r"/api/growth-os/multiscene-video", "video_gen"),
    ("POST", r"/api/apply-template", "video_gen"),
    ("POST", r"/api/analyze", "ai_call"),
    ("POST", r"/api/viral-search", "ai_call"),
    ("POST", r"/api/virality-score", "ai_call"),
    ("POST", r"/api/repurpose-video-link", "ai_call"),
    ("POST", r"/api/load-original-video", "ai_call"),
    ("POST", r"/api/growth-os/campaign-plan", "ai_call"),
    ("POST", r"/api/growth-os/live-refresh", "ai_call"),
    ("POST", r"/api/growth-os/report", "ai_call"),
    ("POST", r"/api/analytics/refresh", "ai_call"),
    ("POST", r"/api/engagement-queue/refresh", "ai_call"),
    ("POST", r"/api/trigger-autopilot", "autopilot"),
    ("POST", r"/api/publish/twitter", "publish"),
    ("POST", r"/api/publish/linkedin", "publish"),
    ("POST", r"/api/postproxy/test-publish", "publish"),
    ("POST", r"/api/approval-queue/[^/]+/approve", "publish"),
    ("POST", r"/api/engagement-queue/[^/]+/send", "publish"),
    ("POST", r"/api/upload", "upload"),
]
ROUTES = [(m, re.compile("^" + p + "/?$"), c) for m, p, c in _ROUTES]

_lock = threading.RLock()
_clients: Dict[Tuple[str, str], Deque[float]] = {}
_ledger_cache: Optional[Dict[str, Any]] = None
_recent_blocks: Deque[Dict[str, Any]] = deque(maxlen=25)


def enabled() -> bool:
    return (os.environ.get("SPEND_GUARD", "1").strip().lower() not in ("0", "false", "off", "no"))


def window_sec() -> int:
    try:
        return max(60, int(os.environ.get("SPEND_GUARD_WINDOW_SEC", "3600")))
    except ValueError:
        return 3600


def limits(cls: str) -> Tuple[int, int]:
    per_client, daily = DEFAULT_LIMITS[cls]
    key = cls.upper()
    try:
        per_client = int(os.environ.get(f"SPEND_GUARD_{key}_PER_CLIENT", per_client))
    except ValueError:
        pass
    try:
        daily = int(os.environ.get(f"SPEND_GUARD_{key}_DAILY", daily))
    except ValueError:
        pass
    return max(0, per_client), max(0, daily)


def classify(method: str, path: str) -> Optional[str]:
    method = (method or "").upper()
    for m, rx, cls in ROUTES:
        if m == method and rx.match(path or ""):
            return cls
    return None


def client_key(headers: Any, peer: Optional[str]) -> str:
    """Railway's edge sets X-Real-IP; fall back to first X-Forwarded-For hop."""
    get = getattr(headers, "get", None)
    real = (get("x-real-ip") if get else None) or ""
    if real.strip():
        return real.strip()[:64]
    xff = (get("x-forwarded-for") if get else None) or ""
    if xff.strip():
        return xff.split(",")[0].strip()[:64]
    return (peer or "unknown")[:64]


def bypass_ok(token: Optional[str]) -> bool:
    expected = os.environ.get("SPEND_GUARD_BYPASS_TOKEN", "")
    return bool(expected) and bool(token) and hmac.compare_digest(str(token), expected)


def _state_dir() -> Path:
    base = os.environ.get("STATE_DIR") or os.environ.get("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
    return Path(base)


def ledger_path() -> Path:
    return _state_dir() / "spend_guard_ledger.json"


def _utc_day(now: Optional[float] = None) -> str:
    return datetime.fromtimestamp(now if now is not None else time.time(), tz=timezone.utc).strftime("%Y-%m-%d")


def _empty_ledger(day: str) -> Dict[str, Any]:
    return {"day": day, "used": {}, "blocked": {}}


def _load_ledger(now: Optional[float] = None) -> Dict[str, Any]:
    global _ledger_cache
    day = _utc_day(now)
    if _ledger_cache is None:
        try:
            data = json.loads(ledger_path().read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("bad ledger")
            _ledger_cache = data
        except Exception:
            _ledger_cache = _empty_ledger(day)
    if _ledger_cache.get("day") != day:
        _ledger_cache = _empty_ledger(day)
    _ledger_cache.setdefault("used", {})
    _ledger_cache.setdefault("blocked", {})
    return _ledger_cache


def _save_ledger() -> None:
    if _ledger_cache is None:
        return
    path = ledger_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(_ledger_cache), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass  # guard must never break a request because the volume is busy


def _seconds_to_utc_midnight(now: float) -> int:
    dt = datetime.fromtimestamp(now, tz=timezone.utc)
    return max(1, 86400 - (dt.hour * 3600 + dt.minute * 60 + dt.second))


def reserve(cls: str, client: str, now: Optional[float] = None) -> Dict[str, Any]:
    """Count one request. Returns {"ok": bool, ...}; on block, nothing is counted."""
    now = time.time() if now is None else now
    per_client, daily = limits(cls)
    win = window_sec()
    with _lock:
        ledger = _load_ledger(now)
        q = _clients.setdefault((cls, client), deque())
        while q and q[0] <= now - win:
            q.popleft()
        used = int(ledger["used"].get(cls, 0))
        reason = None
        retry_after = 0
        if daily and used >= daily:
            reason = "daily_cap"
            retry_after = _seconds_to_utc_midnight(now)
        elif per_client and len(q) >= per_client:
            reason = "client_window"
            retry_after = max(1, int(q[0] + win - now) + 1)
        if reason:
            ledger["blocked"][cls] = int(ledger["blocked"].get(cls, 0)) + 1
            _recent_blocks.append({
                "at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(timespec="seconds"),
                "class": cls,
                "reason": reason,
                "client": _mask(client),
            })
            _save_ledger()
            return {"ok": False, "class": cls, "reason": reason, "retry_after": retry_after,
                    "per_client_limit": per_client, "daily_limit": daily, "window_sec": win}
        q.append(now)
        ledger["used"][cls] = used + 1
        _save_ledger()
        return {"ok": True, "class": cls, "ts": now}


def refund(cls: str, client: str, ts: float, now: Optional[float] = None) -> None:
    """Give back a reservation when the route rejected / failed the request."""
    with _lock:
        ledger = _load_ledger(now)
        q = _clients.get((cls, client))
        if q:
            try:
                q.remove(ts)
            except ValueError:
                pass
        if _utc_day(ts) == ledger.get("day"):
            ledger["used"][cls] = max(0, int(ledger["used"].get(cls, 0)) - 1)
        _save_ledger()


def _mask(client: str) -> str:
    if ":" in client:  # IPv6
        parts = client.split(":")
        return ":".join(parts[:3]) + ":…"
    parts = client.split(".")
    if len(parts) == 4:
        return ".".join(parts[:2]) + ".x.x"
    return client[:4] + "…"


def summary(now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else now
    with _lock:
        ledger = _load_ledger(now)
        classes = {}
        for cls in DEFAULT_LIMITS:
            per_client, daily = limits(cls)
            used = int(ledger["used"].get(cls, 0))
            classes[cls] = {
                "per_client_limit": per_client,
                "daily_limit": daily,
                "used_today": used,
                "remaining_today": (max(0, daily - used) if daily else None),
                "blocked_today": int(ledger["blocked"].get(cls, 0)),
            }
        return {
            "pack": PACK,
            "enabled": enabled(),
            "day_utc": ledger.get("day"),
            "window_sec": window_sec(),
            "bypass_token_set": bool(os.environ.get("SPEND_GUARD_BYPASS_TOKEN")),
            "classes": classes,
            "routes": [{"method": m, "path": p, "class": c} for m, p, c in _ROUTES],
            "recent_blocks": list(_recent_blocks)[-10:],
            "ledger_path": str(ledger_path()),
        }


def compact(now: Optional[float] = None) -> Dict[str, Any]:
    s = summary(now)
    return {
        "pack": PACK,
        "enabled": s["enabled"],
        "day_utc": s["day_utc"],
        "used_today": {k: v["used_today"] for k, v in s["classes"].items()},
        "blocked_today": sum(v["blocked_today"] for v in s["classes"].values()),
    }


def reset_for_tests() -> None:
    global _ledger_cache
    with _lock:
        _clients.clear()
        _recent_blocks.clear()
        _ledger_cache = None
