"""X (Twitter) safety policy - x-safe-v1.

Why: @6framestudios was getting spam-filtered (Oct 2026). X's automation and
authenticity rules name automated replies, reply bursts, hashtag footers,
link-only posts, duplicates and delete-and-repost. This module is the one gate
every X publish from this app goes through. It only touches the X path; the
other networks keep their behaviour.

Rules (all on unless X_SAFE_MODE=0):
  * No automated replies/comments on X (mention-reply drafting and the
    engagement-queue X send are off). X_REPLIES_ENABLED=1 re-enables.
  * No X threads (self-reply chains): one post per publish.
  * At most X_MAX_POSTS_PER_DAY (2) native-video originals per rolling 24h,
    at least X_MIN_GAP_HOURS (4) apart, scheduled at varied minutes.
  * Caption: hook first line, <= 280 chars, 0-1 hashtag, no links, never
    a link-only or text-only (no video) post.
  * Never posts the same caption or the same video to X twice (permanent
    ledger), so delete-and-repost or retry double-posts are refused.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("x_policy")

PACK = "x-safe-v1"
X_CHAR_LIMIT = 280
_URL_RE = re.compile(r"(https?://\S+|www\.\S+|\b[\w-]+\.(?:com|app|io|ly|co|net|org|dev|me)/\S*)", re.I)
_HASHTAG_RE = re.compile(r"(?<![\w&])#\w+")
_MENTION_RE = re.compile(r"(?<![\w])@\w+")
_LOCK = threading.Lock()
X_NAMES = {"twitter", "x"}


def _truthy(v: Any) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _falsy(v: Any) -> bool:
    return str(v).strip().lower() in {"0", "false", "no", "off"}


def enabled() -> bool:
    return not _falsy(os.environ.get("X_SAFE_MODE", "1"))


def replies_enabled() -> bool:
    """Automated X replies/comments. Off whenever X_SAFE_MODE is on, unless X_REPLIES_ENABLED=1."""
    if not enabled():
        return True
    return _truthy(os.environ.get("X_REPLIES_ENABLED", "0"))


def max_posts_per_day() -> int:
    try:
        return max(0, min(2, int(os.environ.get("X_MAX_POSTS_PER_DAY", "2"))))
    except ValueError:
        return 2


def min_gap_hours() -> float:
    try:
        return max(4.0, float(os.environ.get("X_MIN_GAP_HOURS", "4")))
    except ValueError:
        return 4.0


def is_x(platform: Any) -> bool:
    return str(platform or "").strip().lower() in X_NAMES


# ---------------------------------------------------------------- ledger

def _state_dir() -> str:
    d = os.environ.get("STATE_DIR") or os.environ.get("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def ledger_path() -> str:
    return os.path.join(_state_dir(), "x_post_ledger.json")


def _load() -> dict:
    try:
        with open(ledger_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data: dict) -> None:
    path = ledger_path()
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(path + ".tmp", path)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(v: Any) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(v))
    except Exception:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _norm_text(text: str) -> str:
    t = _URL_RE.sub(" ", text or "")
    t = _HASHTAG_RE.sub(" ", t)
    t = _MENTION_RE.sub(" ", t)
    return re.sub(r"[^\w]+", " ", t.lower()).strip()


def caption_fingerprint(text: str) -> str:
    return hashlib.sha256(_norm_text(text).encode()).hexdigest()[:20]


def media_fingerprint(post: dict) -> str:
    for key in ("vertical_video_path", "video_path", "source_video_path"):
        v = post.get(key)
        if v:
            name = os.path.basename(str(v).split("?")[0])
            return hashlib.sha256(name.encode()).hexdigest()[:20]
    return ""


def recent_posts(hours: float = 24, now: Optional[datetime] = None) -> List[dict]:
    now = now or _now()
    out = []
    for row in _load().get("posts") or []:
        at = _parse(row.get("at"))
        if at and (now - at).total_seconds() < hours * 3600:
            out.append(row)
    return out


def last_post_at() -> Optional[datetime]:
    times = [_parse(r.get("at")) for r in (_load().get("posts") or [])]
    times = [t for t in times if t]
    return max(times) if times else None


def next_allowed_at(now: Optional[datetime] = None) -> datetime:
    """Earliest time the next X post may go out under the cadence rules."""
    now = now or _now()
    rows = sorted((r for r in (_load().get("posts") or []) if _parse(r.get("at"))), key=lambda r: _parse(r["at"]))
    candidates = [now]
    if rows:
        candidates.append(_parse(rows[-1]["at"]) + timedelta(hours=min_gap_hours()))
    window = [r for r in rows if (now - _parse(r["at"])).total_seconds() < 86400]
    cap = max_posts_per_day()
    if cap and len(window) >= cap:
        candidates.append(_parse(window[-cap]["at"]) + timedelta(hours=24))
    return max(candidates)


def jitter_minutes() -> int:
    """Varied minute offset so X posts never land on the same clock minute."""
    return random.randint(7, 53)


def schedule_time(planned_local: datetime) -> datetime:
    """Push a planned (naive, server-local) X time past the cadence window and add minute jitter."""
    offset = datetime.now() - datetime.utcnow()
    planned_utc = (planned_local - offset).replace(tzinfo=timezone.utc)
    allowed = next_allowed_at()
    base = max(planned_utc, allowed)
    base = base + timedelta(minutes=jitter_minutes(), seconds=random.randint(0, 59))
    return (base.replace(tzinfo=None) + offset).replace(microsecond=0)


def _dup_reason(post: dict, caption: str) -> Optional[str]:
    data = _load()
    cfp = caption_fingerprint(caption)
    mfp = media_fingerprint(post)
    pid = str(post.get("id") or "")
    for row in data.get("posts") or []:
        if pid and row.get("post_id") == pid:
            return "this post was already published to X"
        if cfp and row.get("caption_fp") == cfp:
            return "same caption was already posted to X"
        if mfp and row.get("media_fp") == mfp:
            return "same video was already posted to X"
    return None


def record(post: dict, caption: str) -> None:
    with _LOCK:
        data = _load()
        rows = data.setdefault("posts", [])
        rows.append({
            "at": _now().isoformat(),
            "post_id": post.get("id"),
            "caption_fp": caption_fingerprint(caption),
            "media_fp": media_fingerprint(post),
            "chars": len(caption),
            "hashtags": len(_HASHTAG_RE.findall(caption)),
        })
        data["posts"] = rows[-2000:]  # permanent enough: years at 2/day
        _save(data)
    logger.info("X_POLICY recorded X post %s; next X post allowed after %s UTC", post.get("id"),
                next_allowed_at().isoformat(timespec="minutes"))


# ---------------------------------------------------------------- captions

_WEAK_OPENER = re.compile(r"^(so|and|but|how|why|what|when|ever|did you|have you|do you|can you|which|who)\b", re.I)


def _clamp(text: str, n: int) -> str:
    text = text.strip()
    if len(text) <= n:
        return text
    cut = text[: n - 1]
    sp = cut.rfind(" ")
    if sp > n * 0.6:
        cut = cut[:sp]
    return cut.rstrip(" ,;:-") + "\u2026"


def sanitize_caption(text: str, hook: str = "") -> str:
    """X caption: spectacle hook first line, no links, 0-1 hashtag, <= 280 chars, one post."""
    raw = str(text or "")
    tags = _HASHTAG_RE.findall(raw)
    body = _URL_RE.sub("", raw)
    body = _HASHTAG_RE.sub("", body)
    lines = [re.sub(r"[ \t]+", " ", l).strip(" \t") for l in body.splitlines()]
    lines = [l for l in lines if l and l.strip(" .,:;-|")]
    hook = _URL_RE.sub("", _HASHTAG_RE.sub("", str(hook or ""))).strip()
    first = lines[0] if lines else ""
    if hook and (not first or first.endswith("?") or _WEAK_OPENER.match(first)) and hook.lower() not in first.lower():
        lines.insert(0, hook)
    tag = ""
    for t in tags:
        if t.lower() not in {"#6framestudio", "#6framestudios"}:
            tag = t
            break
    room = X_CHAR_LIMIT - (len(tag) + 2 if tag else 0)
    out = ""
    for l in lines:
        cand = (out + "\n" + l) if out else l
        if len(cand) <= room:
            out = cand
        elif not out:
            out = _clamp(l, room)
            break
        else:
            break
    out = out.strip()
    if tag and out:
        out = f"{out}\n\n{tag}"
    return out[:X_CHAR_LIMIT]


def check(post: dict, caption: str) -> Tuple[bool, str, Optional[datetime]]:
    """(allowed, reason, retry_at_utc). retry_at is set only for cadence holds."""
    if not enabled():
        return True, "", None
    if not post.get("video_path") and not post.get("vertical_video_path"):
        return False, "X posts must carry native video (no text-only or link-only posts)", None
    if not _norm_text(caption):
        return False, "X caption is empty after removing links/hashtags (link-only post refused)", None
    dup = _dup_reason(post, caption)
    if dup:
        return False, dup, None
    allowed = next_allowed_at()
    if allowed > _now() + timedelta(seconds=30):
        return False, (f"X cadence: max {max_posts_per_day()}/24h, >= {min_gap_hours():g}h apart; "
                       f"next slot {allowed.isoformat(timespec='minutes')} UTC"), allowed
    return True, "", None


def summary() -> dict:
    last = last_post_at()
    return {
        "pack": PACK,
        "enabled": enabled(),
        "replies_enabled": replies_enabled(),
        "threads_enabled": not enabled(),
        "max_posts_per_day": max_posts_per_day(),
        "min_gap_hours": min_gap_hours(),
        "max_hashtags": 1,
        "max_chars": X_CHAR_LIMIT,
        "links_allowed": False,
        "video_required": True,
        "posts_last_24h": len(recent_posts(24)),
        "last_x_post_utc": last.isoformat(timespec="minutes") if last else None,
        "next_x_post_allowed_utc": next_allowed_at().isoformat(timespec="minutes"),
        "ledger_path": ledger_path(),
    }


def bootstrap_from_posts(posts: List[dict]) -> int:
    """First boot only: seed the ledger with X posts this app already published, so the
    cadence and duplicate rules count them (scheduled_posts.json, status SUCCESS/PARTIAL)."""
    with _LOCK:
        data = _load()
        if data.get("bootstrapped"):
            return 0
        rows = data.setdefault("posts", [])
        n = 0
        for p in posts or []:
            plats = [s.strip().lower() for s in str(p.get("platform") or "").split(",")]
            if not any(is_x(x) for x in plats) or p.get("status") not in {"SUCCESS", "PARTIAL_SUCCESS"}:
                continue
            err = str(p.get("error_message") or "").lower()
            if p.get("status") == "PARTIAL_SUCCESS" and ("twitter" in err.split("errors:")[-1] or "x_policy" in err):
                continue
            at = _parse(p.get("posted_at"))
            if not at:
                continue
            cap = ((p.get("platform_captions") or {}).get("twitter")) or p.get("text") or ""
            rows.append({"at": at.isoformat(), "post_id": p.get("id"), "caption_fp": caption_fingerprint(cap),
                         "media_fp": media_fingerprint(p), "chars": len(cap), "hashtags": len(_HASHTAG_RE.findall(cap)),
                         "bootstrap": True})
            n += 1
        rows.sort(key=lambda r: r.get("at") or "")
        data["posts"] = rows[-2000:]
        data["bootstrapped"] = _now().isoformat()
        _save(data)
    return n


def log_boot() -> None:
    s = summary()
    logger.info("X_POLICY %s enabled=%s replies=%s threads=%s schedule=max %s/24h, >=%sh apart, minute jitter 7-53; "
                "captions <=%s chars, <=1 hashtag, no links, video required; next X slot after %s UTC",
                PACK, s["enabled"], "on" if s["replies_enabled"] else "off", "on" if s["threads_enabled"] else "off",
                s["max_posts_per_day"], s["min_gap_hours"], s["max_chars"], s["next_x_post_allowed_utc"])
