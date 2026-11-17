"""One function per Content Team role. Every artifact is a draft. Nothing publishes."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from content_team import analytics as analytics_mod
from content_team import store
from content_team.llm import complete_json
from content_team.voices import prompt_block
from content_team.web import fetch_text

FetchFn = Callable[[str], dict]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compact_posts(items: list[dict], limit: int = 40) -> list[dict]:
    out = []
    for item in items[:limit]:
        out.append({
            "id": item.get("id"),
            "author": item.get("author"),
            "text": (item.get("text") or "")[:500],
            "url": item.get("url"),
            "posted_at": item.get("posted_at"),
            "metrics": item.get("metrics") or {},
            "competitor": item.get("competitor"),
            "why_qt": item.get("why_qt"),
            "why_reply": item.get("why_reply"),
            "notes": item.get("notes"),
        })
    return out


def _record(role: str, title: str, body: str, payload: dict, llm: dict) -> dict:
    draft = store.add_draft(
        role,
        title,
        body,
        payload,
        provider=llm.get("provider") or "fallback",
        llm_status=llm.get("llm_status") or "fallback",
    )
    report = store.save_report(role, {
        "role": role,
        "generated_at": _now(),
        "title": title,
        "body": body,
        "payload": payload,
        "draft_id": draft["id"],
        "provider": draft["provider"],
        "llm_status": draft["llm_status"],
        "llm_error": llm.get("error"),
        "status": "AWAITING_REVIEW",
    })
    report["draft"] = draft
    return report


# ---------------------------------------------------------------------------
# Scheduled / ingest-driven roles
# ---------------------------------------------------------------------------

def run_researcher() -> dict:
    posts = store.load_ingest("trending")
    fallback_ideas = []
    for i, post in enumerate(posts[:30], start=1):
        hook = (post.get("text") or "").split("\n", 1)[0][:120]
        fallback_ideas.append({
            "title": hook or f"Idea from @{post.get('author') or 'unknown'}",
            "hook": hook,
            "angle": "riff on this ingested post in 6Frame's cinematic lane",
            "why": "extracted from ingested trending post (LLM unavailable)",
            "source_id": post.get("id"),
            "source_url": post.get("url"),
        })
    while len(fallback_ideas) < 30:
        n = len(fallback_ideas) + 1
        fallback_ideas.append({
            "title": f"Placeholder idea {n} — needs a fresh trending ingest",
            "hook": "",
            "angle": "waiting for Grok routine ingest",
            "why": "fewer than 30 ingested posts were available",
            "source_id": "",
            "source_url": "",
        })
    llm = complete_json(
        "X Researcher task: from these ingested trending posts, produce 30 content ideas "
        "for 6Frame Studio. Return JSON {\"ideas\":[{\"title\",\"hook\",\"angle\",\"why\",\"source_id\"}]}.\n"
        + json.dumps(_compact_posts(posts), ensure_ascii=False),
        fallback={"ideas": fallback_ideas},
        system=prompt_block("researcher"), role="researcher",
    )
    data = llm.get("data") or {}
    ideas = list(data.get("ideas") or fallback_ideas)[:30]
    while len(ideas) < 30:
        ideas.append(fallback_ideas[len(ideas)])
    body = "\n".join(f"{i}. {it.get('title') or it.get('hook')}" for i, it in enumerate(ideas, 1))
    return _record("researcher", "30 morning content ideas", body, {"ideas": ideas, "ingested": len(posts)}, llm)


def run_competitors() -> dict:
    posts = store.load_ingest("competitor")
    fallback = {
        "summary": "Competitor digest from ingested posts only." if posts else "No competitor posts ingested yet.",
        "highlights": [
            {"author": p.get("author"), "takeaway": (p.get("text") or "")[:200], "url": p.get("url")}
            for p in posts[:12]
        ],
    }
    llm = complete_json(
        "Competitor Watch: write a daily digest of these ingested competitor posts. "
        "Return JSON {\"summary\": str, \"highlights\":[{\"author\",\"takeaway\",\"url\"}]}.\n"
        + json.dumps(_compact_posts(posts), ensure_ascii=False),
        fallback=fallback,
        system=prompt_block("competitors"), role="competitors",
    )
    data = llm.get("data") or fallback
    body = str(data.get("summary") or fallback["summary"])
    return _record("competitors", "Daily competitor digest", body, data, llm)


def run_bookmarks() -> dict:
    posts = store.load_ingest("bookmark")
    fallback = {
        "summary": f"{len(posts)} bookmarked posts logged." if posts else "No bookmarks ingested yet.",
        "themes": [],
        "keep": [{"author": p.get("author"), "text": (p.get("text") or "")[:200], "url": p.get("url")} for p in posts[:20]],
    }
    llm = complete_json(
        "Bookmarks Brain: write a 7 PM summary of these ingested bookmarked posts. "
        "Return JSON {\"summary\": str, \"themes\":[str], \"keep\":[{\"author\",\"text\",\"url\"}]}.\n"
        + json.dumps(_compact_posts(posts, 60), ensure_ascii=False),
        fallback=fallback,
        system=prompt_block("bookmarks"), role="bookmarks",
    )
    data = llm.get("data") or fallback
    return _record("bookmarks", "7 PM bookmarks summary", str(data.get("summary") or ""), data, llm)


def run_qt_scout() -> dict:
    posts = store.load_ingest("qt_candidate") or store.load_ingest("trending")
    fallback_cands = []
    for post in posts[:3]:
        snippet = (post.get("text") or "")[:80]
        fallback_cands.append({
            "source_id": post.get("id"),
            "author": post.get("author"),
            "url": post.get("url"),
            "quote": f"The craft choice here is the real story: {snippet}",
            "why": post.get("why_qt") or "ingested as a quote-tweet candidate",
        })
    while len(fallback_cands) < 3:
        fallback_cands.append({
            "source_id": "",
            "author": "",
            "url": "",
            "quote": "Needs a fresh QT ingest from the Grok routine.",
            "why": "fewer than 3 candidates were ingested",
        })
    llm = complete_json(
        "QT Scout: pick exactly 3 posts worth quote-tweeting and draft a QT for each. "
        "Return JSON {\"candidates\":[{\"source_id\",\"author\",\"url\",\"quote\",\"why\"}]}.\n"
        + json.dumps(_compact_posts(posts), ensure_ascii=False),
        fallback={"candidates": fallback_cands},
        system=prompt_block("qt_scout"), role="qt_scout",
    )
    data = llm.get("data") or {}
    cands = list(data.get("candidates") or fallback_cands)[:3]
    while len(cands) < 3:
        cands.append(fallback_cands[len(cands)])
    body = "\n\n".join(f"QT {i} (@{c.get('author') or 'unknown'}):\n{c.get('quote')}" for i, c in enumerate(cands, 1))
    return _record("qt_scout", "3 quote-tweet drafts", body, {"candidates": cands}, llm)


def run_analytics(now: datetime | None = None) -> dict:
    payload = analytics_mod.build_report(now=now)
    # Analytics is numbers-only; do not let an LLM invent metrics.
    impressions = payload.get("impressions_or_views")
    engagements = payload.get("engagements")
    replies = payload.get("replies")
    deltas = payload.get("follower_deltas") or {}
    delta_bits = ", ".join(f"{k}: {v if v is not None else 'n/a'}" for k, v in list(deltas.items())[:8]) or "n/a"
    body = (
        f"Last 24h (stored PostProxy metrics only).\n"
        f"Impressions/views: {impressions if impressions is not None else 'unavailable'}\n"
        f"Engagements: {engagements if engagements is not None else 'unavailable'}\n"
        f"Replies/comments: {replies if replies is not None else 'unavailable'}\n"
        f"Follower deltas: {delta_bits}\n"
        f"Posts with metrics: {payload.get('posts_with_metrics')}\n"
    )
    if payload.get("missing_fields"):
        body += "Missing: " + "; ".join(payload["missing_fields"]) + "\n"
    llm = {"provider": "none", "llm_status": "real" if payload.get("metrics_available") else "fallback", "error": payload.get("error"), "data": payload}
    # llm_status "real" here means real stored metrics, not an LLM call.
    return _record("analytics", "Morning 24h analytics", body, payload, llm)


def run_chief() -> dict:
    snapshot = {role: store.load_report(role) for role in store.ROLES if role != "chief"}
    awaiting = [d for d in store.load_drafts() if d.get("status") == "AWAITING_REVIEW"]
    digest = []
    for role, report in snapshot.items():
        if not report:
            digest.append({"role": role, "status": "no report yet", "llm_status": None, "title": ""})
            continue
        digest.append({
            "role": role,
            "status": report.get("status"),
            "llm_status": report.get("llm_status"),
            "title": report.get("title"),
            "generated_at": report.get("generated_at"),
            "llm_error": report.get("llm_error"),
        })
    fallback = {
        "summary": f"End of day: {len(awaiting)} drafts awaiting review. "
                   + ", ".join(f"{d['role']}={'ready' if d['title'] else 'idle'}" for d in digest),
        "highlights": [d for d in digest if d.get("title")],
        "needs_review": len(awaiting),
    }
    llm = complete_json(
        "Chief of Staff end-of-day rollup. Summarize what each role produced today, note LLM failures, "
        "and list what Bret should review. Return JSON {\"summary\": str, \"highlights\":[object], \"needs_review\": int}.\n"
        + json.dumps({"digest": digest, "awaiting": len(awaiting)}, ensure_ascii=False),
        fallback=fallback,
        system=prompt_block("chief"), role="chief",
    )
    data = llm.get("data") or fallback
    data["digest"] = digest
    data["needs_review"] = data.get("needs_review", len(awaiting))
    return _record("chief", "End-of-day Content Team rollup", str(data.get("summary") or fallback["summary"]), data, llm)


# ---------------------------------------------------------------------------
# On-demand create / hunt roles
# ---------------------------------------------------------------------------

def run_sparring(idea: str) -> dict:
    idea = (idea or "").strip()
    if not idea:
        raise ValueError("idea is required")
    fallback = {
        "titles": [f"{idea[:60]} — option {i}" for i in range(1, 6)],
        "packaging": {"hook": idea[:120], "visual": "specify the key image", "cta": "show the process"},
    }
    llm = complete_json(
        "Ideas sparring: titles and packaging for this idea. "
        "Return JSON {\"titles\":[str], \"packaging\":{\"hook\",\"visual\",\"cta\"}}.\nIDEA:\n" + idea,
        fallback=fallback,
        system=prompt_block("sparring"), role="sparring",
    )
    data = llm.get("data") or fallback
    data["idea"] = idea
    titles = data.get("titles") or fallback["titles"]
    body = "Titles:\n" + "\n".join(f"- {t}" for t in titles)
    return _record("sparring", "Title and packaging options", body, data, llm)


def run_writer(brief: str) -> dict:
    brief = (brief or "").strip()
    if not brief:
        raise ValueError("brief is required")
    fallback = {
        "post": brief[:260],
        "thread": [brief[:260]],
    }
    llm = complete_json(
        "X Writer: draft one X post and an optional short thread from this brief. "
        "Return JSON {\"post\": str, \"thread\":[str]}. Hook first, <=280 chars, 0-1 hashtag, no links.\nBRIEF:\n" + brief,
        fallback=fallback,
        system=prompt_block("writer"), role="writer",
    )
    data = llm.get("data") or fallback
    data["brief"] = brief
    body = str(data.get("post") or fallback["post"])
    return _record("writer", "X post draft", body, data, llm)


def run_visuals(subject: str) -> dict:
    subject = (subject or "").strip()
    if not subject:
        raise ValueError("subject is required")
    fallback = {
        "prompts": [{"title": "key art", "prompt": f"Cinematic still of {subject}, anamorphic lens, textured practical light, no watermark."}],
        "negative": "no watermarks, no logos, no typography unless asked",
    }
    llm = complete_json(
        "Banner & Visuals: write image-generation prompts for this title or post. "
        "Return JSON {\"prompts\":[{\"title\",\"prompt\"}], \"negative\": str}.\nSUBJECT:\n" + subject,
        fallback=fallback,
        system=prompt_block("visuals"), role="visuals",
    )
    data = llm.get("data") or fallback
    data["subject"] = subject
    lines = [p.get("prompt") if isinstance(p, dict) else str(p) for p in (data.get("prompts") or [])]
    return _record("visuals", "Image prompts", "\n\n".join(lines), data, llm)


def run_factcheck(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        raise ValueError("text is required")
    import re
    raw_claims = re.findall(r".{0,40}\d[\d,\.]*\s*(%|percent|million|billion|views|impressions|followers)?.{0,40}", text, flags=re.I)
    fallback_claims = [{"claim": c.strip(), "verdict": "unverified", "sources": [], "notes": "LLM unavailable; number flagged for manual check."} for c in raw_claims[:12]]
    if not fallback_claims:
        fallback_claims = [{"claim": text[:200], "verdict": "unverified", "sources": [], "notes": "No numeric claims auto-detected; review the whole draft."}]
    llm = complete_json(
        "Fact Checker: extract every statistic and factual claim, and flag each one. "
        "Return JSON {\"claims\":[{\"claim\",\"verdict\",\"sources\",\"notes\"}]}. Verdict is supported, unverified, or doubtful.\nDRAFT:\n" + text,
        fallback={"claims": fallback_claims},
        system=prompt_block("factcheck"), role="factcheck",
    )
    data = llm.get("data") or {"claims": fallback_claims}
    data["source_text"] = text
    claims = data.get("claims") or fallback_claims
    body = "\n".join(f"- [{c.get('verdict')}] {c.get('claim')}" for c in claims)
    return _record("factcheck", "Fact check", body, data, llm)


def run_repurposer(text: str, url: str = "") -> dict:
    text = (text or "").strip()
    if not text:
        raise ValueError("text is required")
    fallback = {"linkedin": text, "newsletter": text[:400], "source_url": url}
    llm = complete_json(
        "Repurposer: turn this X post into a LinkedIn post and a newsletter blurb. "
        "Return JSON {\"linkedin\": str, \"newsletter\": str}.\nPOST:\n" + text + (f"\nURL: {url}" if url else ""),
        fallback=fallback,
        system=prompt_block("repurposer"), role="repurposer",
    )
    data = llm.get("data") or fallback
    data["source_text"] = text
    data["source_url"] = url
    body = "LINKEDIN\n" + str(data.get("linkedin") or "") + "\n\nNEWSLETTER\n" + str(data.get("newsletter") or "")
    return _record("repurposer", "LinkedIn + newsletter drafts", body, data, llm)


def run_reply_scout() -> dict:
    posts = store.load_ingest("reply_target")
    fallback_replies = []
    for post in posts[:8]:
        fallback_replies.append({
            "source_id": post.get("id"),
            "author": post.get("author"),
            "url": post.get("url"),
            "reply": "Draft a specific observation about the craft in this post. (LLM fallback — review before anything is sent. Nothing is sent automatically.)",
        })
    llm = complete_json(
        "Reply Scout: draft replies to these ingested posts from larger accounts. Do not send them. "
        "Return JSON {\"replies\":[{\"source_id\",\"author\",\"url\",\"reply\"}]}.\n"
        + json.dumps(_compact_posts(posts), ensure_ascii=False),
        fallback={"replies": fallback_replies},
        system=prompt_block("reply_scout"), role="reply_scout",
    )
    data = llm.get("data") or {"replies": fallback_replies}
    data["never_send"] = True
    replies = data.get("replies") or fallback_replies
    body = "\n\n".join(f"@{r.get('author')}: {r.get('reply')}" for r in replies) or "No reply targets ingested."
    return _record("reply_scout", "Reply drafts (not sent)", body, data, llm)


def run_sponsors(urls: list[str] | None = None, fetch: FetchFn | None = None) -> dict:
    return _research_list("sponsors", _default_research_urls() if urls is None else urls, fetch)


def run_collabs(urls: list[str] | None = None, fetch: FetchFn | None = None) -> dict:
    return _research_list("collabs", _default_research_urls() if urls is None else urls, fetch)


def _default_research_urls() -> list[str]:
    return [
        "https://6framestudio.com/",
    ]


def _research_list(role: str, urls: list[str], fetch: FetchFn | None) -> dict:
    fetch = fetch or fetch_text
    pages = []
    for url in urls[:8]:
        pages.append(fetch(url))
    ok_pages = [p for p in pages if p.get("ok") and p.get("text")]
    fallback = {
        "leads": [],
        "fetch": [{"url": p.get("url"), "ok": p.get("ok"), "error": p.get("error")} for p in pages],
        "note": "No leads yet. Public pages were fetched (or failed) and are listed in fetch.",
    }
    if ok_pages:
        fallback["note"] = "LLM unavailable; review the fetched public pages and add leads manually."
    prompt = (
        f"{'Sponsor Hunter' if role == 'sponsors' else 'Collab Finder'}: from these public pages, "
        "list plausible leads with a reason. Return JSON {\"leads\":[{\"name\",\"why\",\"url\"}]}.\n"
        + json.dumps([{"url": p["url"], "text": (p.get("text") or "")[:1500]} for p in ok_pages], ensure_ascii=False)
    )
    llm = complete_json(prompt, fallback=fallback, system=prompt_block(role), role=role)
    data = llm.get("data") or fallback
    data["fetch"] = fallback["fetch"]
    leads = data.get("leads") or []
    body = "\n".join(f"- {lead.get('name')}: {lead.get('why')}" for lead in leads) or fallback["note"]
    return _record(role, "Sponsor leads" if role == "sponsors" else "Collab leads", body, data, llm)


RUNNERS = {
    "researcher": lambda **kw: run_researcher(),
    "competitors": lambda **kw: run_competitors(),
    "bookmarks": lambda **kw: run_bookmarks(),
    "qt_scout": lambda **kw: run_qt_scout(),
    "analytics": lambda **kw: run_analytics(),
    "chief": lambda **kw: run_chief(),
    "sparring": lambda **kw: run_sparring(kw.get("idea") or kw.get("text") or ""),
    "writer": lambda **kw: run_writer(kw.get("brief") or kw.get("text") or ""),
    "visuals": lambda **kw: run_visuals(kw.get("subject") or kw.get("text") or ""),
    "factcheck": lambda **kw: run_factcheck(kw.get("text") or ""),
    "repurposer": lambda **kw: run_repurposer(kw.get("text") or "", kw.get("url") or ""),
    "reply_scout": lambda **kw: run_reply_scout(),
    "sponsors": lambda **kw: run_sponsors(kw.get("urls")),
    "collabs": lambda **kw: run_collabs(kw.get("urls")),
}


def run_role(role: str, **kwargs) -> dict:
    if role not in RUNNERS:
        raise ValueError(f"unknown role: {role}")
    return RUNNERS[role](**kwargs)
