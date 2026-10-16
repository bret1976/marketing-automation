"""Free viral research for the stitched Shorts planner.

Before each slot: search YouTube (via yt-dlp, free, no API key) for the most-viewed recent
AI-film / AI-video Shorts, keep short-form results (<=90s) ranked by views, and hand the planner
the proven hook/title formats + recurring hashtags. Cost: $0 (no paid API calls).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from collections import Counter
from typing import List, Optional
from urllib.parse import quote_plus

logger = logging.getLogger("viral_research")

DEFAULT_QUERIES = (
    "ai short film #shorts",
    "veo 3 ai video #shorts",
    "seedance ai video #shorts",
    "ai generated video viral #shorts",
)
# YouTube relevance search (favours recent + popular); the "this month" filter returned low-view noise.
SEARCH_FILTER = ""
MIN_VIEWS = 10_000
NICHE_RE = re.compile(r"\bai[ -]?(short ?film|film|movie|video|animation|generated|cinema|cinematic)|"
                      r"\b(veo ?3|seedance|kling|sora|runway|hailuo|midjourney video)\b|#ai(film|video|shortfilm|animation|art)\b", re.I)
MAX_SECONDS = 90
_TAG_RE = re.compile(r"#\w+")


def _ytdlp_bin() -> Optional[str]:
    for p in (os.environ.get("YTDLP_BIN"), "/usr/local/bin/yt-dlp", shutil.which("yt-dlp")):
        if p and os.path.exists(p):
            return p
    return None


def search_youtube(query: str, timeout: int = 60, runner=subprocess.run) -> List[dict]:
    exe = _ytdlp_bin()
    if not exe:
        raise RuntimeError("yt-dlp not available")
    url = f"https://www.youtube.com/results?search_query={quote_plus(query)}" + (f"&sp={SEARCH_FILTER}" if SEARCH_FILTER else "")
    res = runner([exe, "--flat-playlist", "-J", "--no-warnings", url], capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or "")[-300:])
    out = []
    for e in (json.loads(res.stdout or "{}").get("entries") or []):
        vid = e.get("id") or ""
        out.append({"title": (e.get("title") or "").strip(), "views": int(e.get("view_count") or 0),
                    "seconds": e.get("duration"), "url": e.get("url") or f"https://www.youtube.com/shorts/{vid}",
                    "channel": e.get("channel") or e.get("uploader") or "", "query": query})
    return out


def research(queries=DEFAULT_QUERIES, top_n: int = 8, search=search_youtube) -> dict:
    """Return {"refs": [...top viral shorts...], "hashtags": [...], "errors": [...]}. Never raises."""
    seen, refs, errors = set(), [], []
    for q in queries:
        try:
            for r in search(q):
                if not r["title"] or r["url"] in seen:
                    continue
                if r["seconds"] and r["seconds"] > MAX_SECONDS:
                    continue
                if r["views"] < MIN_VIEWS or not re.search(r"/(watch|shorts)", r["url"]):
                    continue  # skip channels/playlists and low-view noise
                if not NICHE_RE.search(r["title"]):
                    continue  # stay in the AI-video niche (a bare "#ai" tag on a cartoon is not enough)
                seen.add(r["url"])
                refs.append(r)
        except Exception as exc:
            errors.append(f"{q}: {exc}"[:200])
    refs.sort(key=lambda r: -r["views"])
    refs = refs[:top_n]
    tags = Counter(t.lower() for r in refs for t in _TAG_RE.findall(r["title"]))
    generic = re.compile(r"^#(shorts?\w*|fyp|viral\w*|foryou\w*|trending|video|reels?|tiktok|ai)$")
    hashtags = [t for t, _ in tags.most_common(12) if not generic.match(t)][:6]
    logger.info("viral research: %s refs, top: %s", len(refs), [(r["views"], r["title"][:60]) for r in refs[:3]])
    return {"source": "youtube_search_via_ytdlp", "refs": refs, "hashtags": hashtags, "errors": errors, "cost_usd": 0.0}


def prompt_block(res: Optional[dict]) -> str:
    refs = (res or {}).get("refs") or []
    if not refs:
        return "No live viral references available this slot; use proven formats: number hook, reveal, 'which one is AI?' challenge."
    lines = [f'- "{r["title"][:90]}" ({r["views"]:,} views)' for r in refs]
    tags = " ".join((res or {}).get("hashtags") or [])
    return ("CURRENTLY VIRAL in this niche (most-viewed recent Shorts, live search this slot):\n" + "\n".join(lines) +
            (f"\nRecurring hashtags in them: {tags}" if tags else "") +
            "\nStudy WHY they hook (curiosity gap, number, challenge, reveal, emotion). Pick ONE proven hook format and "
            "adapt it to an ORIGINAL story (never copy a title, subject or character). Put the chosen format in "
            "\"viral_angle\" and the reference titles you based it on in \"viral_refs_used\".")
