"""HTTP routes for the Content Team. Drafts only. Token-gated ingest/reports for Grok routines."""

from __future__ import annotations

import hmac
import logging
import os
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from content_team import PACK, VERSION, health, scheduler, store
from content_team.roles import RUNNERS, run_role
from content_team.voices import load_voices, update_voice

logger = logging.getLogger("content_team.routes")

router = APIRouter(prefix="/api/content-team", tags=["content-team"])

INGEST_PAYLOAD_DOCS = {
    "kind": "trending | competitor | bookmark | qt_candidate | reply_target",
    "source": "grok_routine (default)",
    "items": [
        {
            "id": "optional stable id (defaults to url)",
            "url": "https://x.com/user/status/…",
            "author": "handle",
            "author_name": "Display Name",
            "text": "post body",
            "posted_at": "ISO-8601",
            "metrics": {
                "likes": 0,
                "replies": 0,
                "reposts": 0,
                "quotes": 0,
                "bookmarks": 0,
                "impressions": 0,
                "followers": 0,
            },
            "notes": "optional",
            "competitor": "handle — competitor items only",
            "list_name": "creators — competitor items only",
            "bookmarked_at": "ISO-8601 — bookmark items only",
            "why_qt": "optional — qt_candidate items only",
            "account_size": "large — reply_target items only",
            "why_reply": "optional — reply_target items only",
        }
    ],
}


def _token() -> str:
    return (os.environ.get("CONTENT_TEAM_TOKEN") or os.environ.get("CONTENT_TEAM_INGEST_TOKEN") or "").strip()


def token_ok(header_value: str | None) -> bool:
    expected = _token()
    if not expected or not header_value:
        return False
    return hmac.compare_digest(header_value, expected)


class IngestBody(BaseModel):
    kind: str
    source: str = "grok_routine"
    items: list[dict[str, Any]] = Field(default_factory=list)


class IdeaBody(BaseModel):
    text: str
    title: str = ""
    source: str = "dashboard"
    tags: list[str] = Field(default_factory=list)


class VoiceBody(BaseModel):
    label: Optional[str] = None
    voice: Optional[str] = None
    dos: Optional[str] = None
    donts: Optional[str] = None


class RunBody(BaseModel):
    idea: Optional[str] = None
    brief: Optional[str] = None
    subject: Optional[str] = None
    text: Optional[str] = None
    url: Optional[str] = None
    urls: Optional[list[str]] = None


class DraftStatusBody(BaseModel):
    status: str


@router.get("/health")
def content_team_health():
    return health()


@router.get("/docs/ingest")
def ingest_docs():
    return {
        "pack": PACK,
        "version": VERSION,
        "description": "Grok Bot routines with free X reads POST ingested posts here. This app never calls the X API.",
        "endpoints": {
            "POST /api/content-team/ingest": "kind + items",
            "POST /api/content-team/ingest/{kind}": "items only, kind in the path",
            "POST /api/content-team/ideas": "article tracker (dashboard or routine)",
        },
        "auth": "When ADMIN_PASSWORD is set, send header X-Content-Team-Token (CONTENT_TEAM_TOKEN) or a normal admin session.",
        "payload": INGEST_PAYLOAD_DOCS,
        "kinds": list(store.INGEST_KINDS),
    }


@router.post("/ingest")
def ingest(body: IngestBody):
    if body.kind not in store.INGEST_KINDS:
        raise HTTPException(status_code=400, detail=f"kind must be one of {list(store.INGEST_KINDS)}")
    items = [dict(it, source=it.get("source") or body.source) for it in body.items]
    added = store.append_ingest(body.kind, items)
    return {"ok": True, "kind": body.kind, "added": len(added), "total": len(store.load_ingest(body.kind))}


@router.post("/ingest/{kind}")
def ingest_kind(kind: str, body: dict[str, Any]):
    if kind not in store.INGEST_KINDS:
        raise HTTPException(status_code=400, detail=f"kind must be one of {list(store.INGEST_KINDS)}")
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="body must include an items array")
    source = body.get("source") or "grok_routine"
    added = store.append_ingest(kind, [dict(it, source=it.get("source") or source) for it in items])
    return {"ok": True, "kind": kind, "added": len(added), "total": len(store.load_ingest(kind))}


@router.get("/ingest/{kind}")
def get_ingest(kind: str):
    if kind not in store.INGEST_KINDS:
        raise HTTPException(status_code=400, detail=f"unknown kind {kind}")
    items = store.load_ingest(kind)
    return {"ok": True, "kind": kind, "items": items, "total": len(items)}


@router.post("/ideas")
def add_idea(body: IdeaBody):
    try:
        idea = store.add_idea(body.text, source=body.source, tags=body.tags, title=body.title)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "idea": idea, "total": len(store.load_ideas())}


@router.get("/ideas")
def list_ideas():
    items = store.load_ideas()
    return {"ok": True, "items": items, "total": len(items)}


@router.get("/voices")
def get_voices():
    return {"ok": True, "voices": load_voices()}


@router.put("/voices/{role}")
def put_voice(role: str, body: VoiceBody):
    try:
        voice = update_voice(role, body.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "voice": voice}


@router.get("/drafts")
def list_drafts(status: Optional[str] = None):
    items = store.load_drafts()
    if status:
        items = [d for d in items if d.get("status") == status.upper()]
    return {"ok": True, "items": items, "total": len(items)}


@router.post("/drafts/{draft_id}/review")
def review_draft(draft_id: str, body: DraftStatusBody):
    """Mark a draft approved or rejected. This never publishes or sends a reply."""
    try:
        found = store.set_draft_status(draft_id, body.status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not found:
        raise HTTPException(status_code=404, detail="draft not found")
    return {"ok": True, "draft": found, "published": False, "sent": False}


@router.get("/reports")
def all_reports():
    reports = {role: store.load_report(role) for role in store.ROLES}
    return {"ok": True, "reports": reports, "scheduler": scheduler.status()}


@router.get("/reports/{role}")
def get_report(role: str):
    if role not in store.ROLES:
        raise HTTPException(status_code=404, detail=f"unknown role {role}")
    report = store.load_report(role)
    return {"ok": True, "role": role, "report": report, "empty": not bool(report)}


@router.post("/run/{role}")
def post_run(role: str, body: Optional[RunBody] = None):
    if role not in RUNNERS:
        raise HTTPException(status_code=404, detail=f"unknown role {role}")
    payload = body.model_dump(exclude_none=True) if body else {}
    try:
        report = run_role(role, **payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "report": report}


@router.post("/run-morning")
def post_run_morning():
    return {"ok": True, "results": scheduler.run_morning()}


@router.post("/run-evening")
def post_run_evening():
    return {"ok": True, "results": scheduler.run_evening()}


@router.get("/scheduler")
def get_scheduler():
    return {"ok": True, **scheduler.status()}


# Convenience aliases so a Grok routine can fetch each report from a stable path.
@router.get("/researcher")
def alias_researcher():
    return get_report("researcher")


@router.get("/competitors")
def alias_competitors():
    return get_report("competitors")


@router.get("/bookmarks")
def alias_bookmarks():
    return get_report("bookmarks")


@router.get("/qt-scout")
def alias_qt():
    return get_report("qt_scout")


@router.get("/analytics")
def alias_analytics():
    return get_report("analytics")


@router.get("/chief")
def alias_chief():
    return get_report("chief")


@router.get("/sparring")
def alias_sparring():
    return get_report("sparring")


@router.get("/writer")
def alias_writer():
    return get_report("writer")


@router.get("/visuals")
def alias_visuals():
    return get_report("visuals")


@router.get("/factcheck")
def alias_factcheck():
    return get_report("factcheck")


@router.get("/tracker")
def alias_tracker():
    return list_ideas()


@router.get("/repurposer")
def alias_repurposer():
    return get_report("repurposer")


@router.get("/replies")
def alias_replies():
    return get_report("reply_scout")


@router.get("/sponsors")
def alias_sponsors():
    return get_report("sponsors")


@router.get("/collabs")
def alias_collabs():
    return get_report("collabs")


def request_has_content_team_token(request: Request) -> bool:
    return token_ok(request.headers.get("x-content-team-token"))
