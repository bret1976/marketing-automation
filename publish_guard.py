"""Publish Guard Pack — one outbound publish per content fingerprint within a window.

Idea inspiration (no code copied):
- Matthew-Selvam/Open-Dispatch (MIT) — queue state machine so the same unit
  is not re-published while already queued/publishing/published.
- ShadowSlayer03/Post4U-Schedule-Social-Media-Posts (MIT) — smart retry:
  successful platforms never re-post.
- SoeRatch/disqueue — idempotency / dedupe lock concept (idea only).

Original marketing-automation Python: stable content fingerprints from
platform + normalized caption/thread + media/source, sliding publish ledger,
optional near-duplicate caption score. Soft-blocks duplicate schedule and
publish when PUBLISH_GUARD_BLOCK is on (default on). No UI/screens —
health marker + API only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

PACK = "publish-guard-v1"
DEFAULT_WINDOW_SEC = 24 * 60 * 60  # 24 hours
DEFAULT_MAX_LEDGER = 800
NEAR_DUPE_THRESHOLD = 0.88

_lock = threading.Lock()
_ROOT = Path(__file__).resolve().parent


def _state_dir() -> Path:
    raw = os.environ.get("STATE_DIR") or os.environ.get("DATA_DIR") or str(_ROOT)
    path = Path(raw)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _ledger_path() -> Path:
    override = os.environ.get("PUBLISH_GUARD_LEDGER")
    if override:
        return Path(override)
    return _state_dir() / "publish_guard_ledger.json"


def _truthy(name: str, default: str = "1") -> bool:
    raw = os.environ.get(name)
    if raw is None:
        raw = default
    return (raw or "").strip().lower() not in {"0", "false", "no", "off", ""}


def window_sec() -> int:
    try:
        return max(60, int(os.environ.get("PUBLISH_GUARD_WINDOW_SEC") or DEFAULT_WINDOW_SEC))
    except ValueError:
        return DEFAULT_WINDOW_SEC


def blocking_enabled() -> bool:
    """When true, schedule/publish soft-skips duplicates. Default on."""
    return _truthy("PUBLISH_GUARD_BLOCK", "1")


def _norm_token(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _norm_caption(post: dict[str, Any] | None) -> str:
    if not post:
        return ""
    thread = post.get("thread")
    if isinstance(thread, list) and thread:
        parts = [_norm_token(str(t)) for t in thread if t]
        return "\n".join(p for p in parts if p)
    return _norm_token(str(post.get("text") or post.get("caption") or ""))


def _platforms_key(post: dict[str, Any] | None) -> str:
    if not post:
        return "unknown"
    raw = post.get("platform") or post.get("platforms") or ""
    if isinstance(raw, list):
        parts = [str(p).strip().lower() for p in raw if str(p).strip()]
    else:
        parts = [p.strip().lower() for p in str(raw).replace("|", ",").split(",") if p.strip()]
    return ",".join(sorted(set(parts))) or "unknown"


def _media_key(post: dict[str, Any] | None) -> str:
    if not post:
        return ""
    for key in ("video_path", "vertical_video_path", "source_video_path", "media_url", "source_url"):
        val = post.get(key)
        if val:
            return _norm_token(str(Path(str(val)).name if "/" in str(val) or "\\" in str(val) else val))
    return ""


def content_fingerprint(post: dict[str, Any] | None) -> str | None:
    """Stable key for one outbound content unit (platform + caption + media)."""
    if not post:
        return None
    platforms = _platforms_key(post)
    caption = _norm_caption(post)
    media = _media_key(post)
    if not caption and not media:
        pid = _norm_token(str(post.get("id") or ""))
        if not pid:
            return None
        digest = hashlib.sha256(f"id:{pid}".encode("utf-8")).hexdigest()[:16]
        return f"{platforms}|id|{digest}"
    material = f"{platforms}\n{caption}\n{media}".encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()[:20]
    return f"{platforms}|{digest}"


def _tokenize(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9']+", _norm_token(text))
    return {w for w in words if len(w) > 1}


def caption_similarity(a: str, b: str) -> float:
    """Jaccard token overlap on normalized captions (0..1)."""
    ta, tb = _tokenize(a), _tokenize(b)
    if not ta and not tb:
        return 1.0 if _norm_token(a) == _norm_token(b) and a else 0.0
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


def _load_ledger() -> list[dict[str, Any]]:
    path = _ledger_path()
    try:
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
    except Exception:
        return []
    return []


def _save_ledger(rows: list[dict[str, Any]]) -> None:
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _prune(rows: list[dict[str, Any]], now: float | None = None) -> list[dict[str, Any]]:
    now = time.time() if now is None else now
    window = window_sec()
    kept = [r for r in rows if isinstance(r.get("ts"), (int, float)) and (now - float(r["ts"])) <= window]
    if len(kept) > DEFAULT_MAX_LEDGER:
        kept = kept[-DEFAULT_MAX_LEDGER:]
    return kept


def summary() -> dict[str, Any]:
    with _lock:
        rows = _prune(_load_ledger())
        _save_ledger(rows) if _ledger_path().exists() or rows else None
    return {
        "pack": PACK,
        "blocking": blocking_enabled(),
        "window_sec": window_sec(),
        "ledger_size": len(rows),
        "near_dupe_threshold": NEAR_DUPE_THRESHOLD,
        "ledger_path": str(_ledger_path()),
    }


def check_publish(
    post: dict[str, Any] | None,
    *,
    force: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
    """Return whether publishing this post would duplicate a recent send."""
    now = time.time() if now is None else now
    fp = content_fingerprint(post)
    caption = _norm_caption(post)
    platforms = _platforms_key(post)
    result: dict[str, Any] = {
        "pack": PACK,
        "fingerprint": fp,
        "platforms": platforms,
        "blocked": False,
        "force": bool(force),
        "blocking_enabled": blocking_enabled(),
        "reason": None,
        "match": None,
        "near_score": None,
    }
    if not fp:
        result["reason"] = "no_fingerprint"
        return result
    if force:
        result["reason"] = "forced"
        return result

    with _lock:
        rows = _prune(_load_ledger(), now=now)
        exact = next((r for r in reversed(rows) if r.get("fingerprint") == fp), None)
        if exact:
            result["match"] = {
                "fingerprint": exact.get("fingerprint"),
                "post_id": exact.get("post_id"),
                "ts": exact.get("ts"),
                "age_sec": round(now - float(exact["ts"]), 1),
            }
            if blocking_enabled():
                result["blocked"] = True
                result["reason"] = "duplicate_fingerprint"
            else:
                result["reason"] = "duplicate_fingerprint_soft"
            return result

        best_score = 0.0
        best_row = None
        for row in reversed(rows):
            if _platforms_key({"platform": row.get("platforms")}) != platforms and row.get("platforms") != platforms:
                # Compare only when platform sets overlap
                row_plats = set((row.get("platforms") or "").split(",")) - {""}
                post_plats = set(platforms.split(",")) - {""}
                if row_plats and post_plats and row_plats.isdisjoint(post_plats):
                    continue
            score = caption_similarity(caption, str(row.get("caption") or ""))
            if score > best_score:
                best_score = score
                best_row = row
        result["near_score"] = round(best_score, 4)
        if best_row and best_score >= NEAR_DUPE_THRESHOLD and caption:
            result["match"] = {
                "fingerprint": best_row.get("fingerprint"),
                "post_id": best_row.get("post_id"),
                "ts": best_row.get("ts"),
                "age_sec": round(now - float(best_row["ts"]), 1),
                "near_score": round(best_score, 4),
            }
            if blocking_enabled():
                result["blocked"] = True
                result["reason"] = "near_duplicate_caption"
            else:
                result["reason"] = "near_duplicate_caption_soft"
            return result

    result["reason"] = "ok"
    return result


def check_schedule(
    post: dict[str, Any] | None,
    existing: list[dict[str, Any]] | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Block scheduling when an identical PENDING/AWAITING item already exists, or ledger hit."""
    result = check_publish(post, force=force)
    if result.get("blocked") or force:
        return result
    fp = result.get("fingerprint")
    caption = _norm_caption(post)
    platforms = _platforms_key(post)
    for item in existing or []:
        status = str(item.get("status") or "").upper()
        if status not in {"PENDING", "AWAITING_APPROVAL", "PUBLISHING"}:
            continue
        if content_fingerprint(item) == fp and fp:
            result["blocked"] = blocking_enabled()
            result["reason"] = "duplicate_in_queue" if blocking_enabled() else "duplicate_in_queue_soft"
            result["match"] = {"post_id": item.get("id"), "status": status, "fingerprint": fp}
            return result
        if platforms == _platforms_key(item):
            score = caption_similarity(caption, _norm_caption(item))
            if score >= NEAR_DUPE_THRESHOLD and caption:
                result["near_score"] = round(score, 4)
                result["blocked"] = blocking_enabled()
                result["reason"] = "near_duplicate_in_queue" if blocking_enabled() else "near_duplicate_in_queue_soft"
                result["match"] = {
                    "post_id": item.get("id"),
                    "status": status,
                    "near_score": round(score, 4),
                }
                return result
    return result


def record_publish(post: dict[str, Any] | None, *, now: float | None = None) -> dict[str, Any]:
    """Append a successful publish fingerprint to the ledger."""
    now = time.time() if now is None else now
    fp = content_fingerprint(post)
    if not fp:
        return {"pack": PACK, "recorded": False, "reason": "no_fingerprint"}
    row = {
        "fingerprint": fp,
        "post_id": (post or {}).get("id"),
        "platforms": _platforms_key(post),
        "caption": _norm_caption(post)[:500],
        "media": _media_key(post),
        "ts": now,
    }
    with _lock:
        rows = _prune(_load_ledger(), now=now)
        rows.append(row)
        rows = _prune(rows, now=now)
        _save_ledger(rows)
    return {"pack": PACK, "recorded": True, "fingerprint": fp, "ledger_size": len(rows)}
