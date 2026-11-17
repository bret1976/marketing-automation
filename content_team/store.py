"""JSON store under STATE_DIR/content_team/. Drafts never auto-publish."""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.RLock()

INGEST_KINDS = (
    "trending",
    "competitor",
    "bookmark",
    "qt_candidate",
    "reply_target",
)
ROLES = (
    "researcher",
    "competitors",
    "bookmarks",
    "qt_scout",
    "sparring",
    "writer",
    "visuals",
    "factcheck",
    "analytics",
    "tracker",
    "chief",
    "repurposer",
    "reply_scout",
    "sponsors",
    "collabs",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return _utcnow().isoformat()


def state_root() -> Path:
    override = os.environ.get("CONTENT_TEAM_STATE_DIR")
    if override:
        path = Path(override)
    else:
        base = os.environ.get("STATE_DIR") or os.environ.get("DATA_DIR") or os.getcwd()
        path = Path(base) / "content_team"
    path.mkdir(parents=True, exist_ok=True)
    (path / "ingest").mkdir(exist_ok=True)
    (path / "reports").mkdir(exist_ok=True)
    return path


def _lock(path: Path) -> threading.RLock:
    key = str(path)
    with _LOCKS_GUARD:
        if key not in _LOCKS:
            _LOCKS[key] = threading.RLock()
        return _LOCKS[key]


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    with _lock(path):
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)


def ingest_path(kind: str) -> Path:
    if kind not in INGEST_KINDS:
        raise ValueError(f"unknown ingest kind: {kind}")
    return state_root() / "ingest" / f"{kind}.json"


def load_ingest(kind: str) -> list[dict]:
    data = read_json(ingest_path(kind), [])
    return data if isinstance(data, list) else []


def save_ingest(kind: str, items: list[dict]) -> None:
    write_json(ingest_path(kind), items)


def append_ingest(kind: str, items: list[dict], *, limit: int = 500) -> list[dict]:
    existing = load_ingest(kind)
    by_id = {}
    for item in existing:
        by_id[str(item.get("id") or "")] = item
    added = []
    for raw in items:
        item = normalize_ingest_item(kind, raw)
        if not item:
            continue
        by_id[item["id"]] = item
        added.append(item)
    merged = list(by_id.values())
    merged.sort(key=lambda it: it.get("ingested_at") or it.get("posted_at") or "", reverse=True)
    save_ingest(kind, merged[:limit])
    return added


def normalize_ingest_item(kind: str, raw: dict) -> dict | None:
    if not isinstance(raw, dict):
        return None
    text = str(raw.get("text") or raw.get("body") or "").strip()
    url = str(raw.get("url") or "").strip()
    if not text and not url:
        return None
    item_id = str(raw.get("id") or "").strip() or (url or uuid.uuid4().hex)
    metrics = raw.get("metrics") if isinstance(raw.get("metrics"), dict) else {}
    item = {
        "id": item_id,
        "kind": kind,
        "url": url,
        "author": str(raw.get("author") or raw.get("handle") or "").lstrip("@"),
        "author_name": str(raw.get("author_name") or raw.get("name") or ""),
        "text": text,
        "posted_at": raw.get("posted_at") or raw.get("created_at") or "",
        "ingested_at": iso_now(),
        "source": str(raw.get("source") or "grok_routine"),
        "notes": str(raw.get("notes") or ""),
        "metrics": {
            "likes": _as_int(metrics.get("likes") or metrics.get("like_count")),
            "replies": _as_int(metrics.get("replies") or metrics.get("reply_count")),
            "reposts": _as_int(metrics.get("reposts") or metrics.get("retweet_count") or metrics.get("reposts")),
            "quotes": _as_int(metrics.get("quotes") or metrics.get("quote_count")),
            "bookmarks": _as_int(metrics.get("bookmarks") or metrics.get("bookmark_count")),
            "impressions": _as_int(metrics.get("impressions") or metrics.get("views") or metrics.get("impression_count")),
            "followers": _as_int(metrics.get("followers") or metrics.get("followers_count")),
        },
    }
    if kind == "competitor":
        item["competitor"] = str(raw.get("competitor") or item["author"])
        item["list_name"] = str(raw.get("list_name") or "default")
    elif kind == "bookmark":
        item["bookmarked_at"] = raw.get("bookmarked_at") or item["posted_at"] or iso_now()
    elif kind == "qt_candidate":
        item["why_qt"] = str(raw.get("why_qt") or raw.get("why") or "")
    elif kind == "reply_target":
        item["account_size"] = str(raw.get("account_size") or "large")
        item["why_reply"] = str(raw.get("why_reply") or raw.get("why") or "")
    return item


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def load_ideas() -> list[dict]:
    data = read_json(state_root() / "ideas.json", [])
    return data if isinstance(data, list) else []


def save_ideas(items: list[dict]) -> None:
    write_json(state_root() / "ideas.json", items)


def add_idea(text: str, *, source: str = "dashboard", tags: list[str] | None = None, title: str = "") -> dict:
    text = (text or "").strip()
    if not text:
        raise ValueError("idea text is required")
    idea = {
        "id": uuid.uuid4().hex[:12],
        "title": (title or text.split("\n", 1)[0])[:120],
        "text": text,
        "source": source,
        "tags": [t.strip() for t in (tags or []) if str(t).strip()],
        "status": "open",
        "created_at": iso_now(),
    }
    items = load_ideas()
    items.insert(0, idea)
    save_ideas(items)
    return idea


def load_drafts() -> list[dict]:
    data = read_json(state_root() / "drafts.json", [])
    return data if isinstance(data, list) else []


def save_drafts(items: list[dict]) -> None:
    write_json(state_root() / "drafts.json", items)


def add_draft(role: str, title: str, body: str, payload: dict, *, provider: str, llm_status: str) -> dict:
    draft = {
        "id": uuid.uuid4().hex[:12],
        "role": role,
        "title": title,
        "body": body,
        "payload": payload,
        "status": "AWAITING_REVIEW",
        "provider": provider,
        "llm_status": llm_status,  # real | stub | fallback | failed
        "created_at": iso_now(),
        "reviewed_at": None,
    }
    items = load_drafts()
    items.insert(0, draft)
    save_drafts(items[:1000])
    return draft


def set_draft_status(draft_id: str, status: str) -> dict | None:
    status = status.upper()
    if status not in {"AWAITING_REVIEW", "APPROVED", "REJECTED"}:
        raise ValueError("status must be AWAITING_REVIEW, APPROVED, or REJECTED")
    items = load_drafts()
    found = None
    for item in items:
        if item.get("id") == draft_id:
            item["status"] = status
            item["reviewed_at"] = iso_now()
            found = item
            break
    if found:
        save_drafts(items)
    return found


def report_path(role: str) -> Path:
    return state_root() / "reports" / f"{role}.json"


def load_report(role: str) -> dict:
    data = read_json(report_path(role), {})
    return data if isinstance(data, dict) else {}


def save_report(role: str, report: dict) -> dict:
    report = dict(report)
    report.setdefault("role", role)
    report.setdefault("generated_at", iso_now())
    write_json(report_path(role), report)
    return report


def scheduler_state() -> dict:
    data = read_json(state_root() / "scheduler.json", {})
    return data if isinstance(data, dict) else {}


def save_scheduler_state(state: dict) -> None:
    write_json(state_root() / "scheduler.json", state)


def status() -> dict:
    root = state_root()
    ingest_counts = {kind: len(load_ingest(kind)) for kind in INGEST_KINDS}
    return {
        "root": str(root),
        "ingest": ingest_counts,
        "ideas": len(load_ideas()),
        "drafts": len(load_drafts()),
        "drafts_awaiting": sum(1 for d in load_drafts() if d.get("status") == "AWAITING_REVIEW"),
    }
