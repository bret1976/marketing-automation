"""Open 'last 24h viral' finder: a free replacement for the Gemini Google-Search trend scan.

Off by default. Turn on with VIRAL_SOURCE=open (Railway env). When off, nothing changes.

Sources (all $0):
  * YouTube Data API v3 (YOUTUBE_API_KEY, free quota): search.list publishedAfter=now-24h,
    order=viewCount, then videos.list for exact publishedAt + viewCount + duration.
    Cost: 100 quota units per query + 1 per 50 ids (default 10,000 units/day).
  * yt-dlp YouTube search ("Today" filter, sorted by views) - no key. Flat search has no upload
    time, so candidates are enriched with exact publishedAt/views via videos.list when a key
    exists, otherwise via a per-video yt-dlp probe (slower; may hit YouTube's bot wall on Railway).
  * Reddit top/day JSON for AI-video subreddits (public JSON, or app-only OAuth when
    REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET are set). Reddit exposes no view counts, so Reddit posts
    are ranked separately by upvotes per hour and never mixed into the views/hour list.
  * TikTok / Instagram: no key-free search exists (yt-dlp has no TikTok/Instagram search
    extractor and Instagram requires login), so they are reported as unavailable, not guessed.

Ranking: views per hour since upload; uploads older than VIRAL_OPEN_MAX_AGE_HOURS (24) are
dropped; duplicates (same video id, or same normalized title from the same channel) are merged.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, List, Optional
from urllib.parse import quote_plus

import requests

logger = logging.getLogger("open_viral")

YT_API = "https://www.googleapis.com/youtube/v3"
# YouTube results filter: Upload date = Today, Sort by = View count.
YT_TODAY_BY_VIEWS = "CAMSAggC"
DEFAULT_QUERIES = (
    "ai short film",
    "ai video",
    "veo 3",
    "ai generated video",
    "ai cinematic",
)
DEFAULT_SUBREDDITS = ("aivideo", "midjourney", "StableDiffusion", "singularity")
USER_AGENT = "Mozilla/5.0 (compatible; 6frame-marketing-automation/1.0; +https://6framestudio.com)"
_ISO_DUR = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")
NICHE_RE = re.compile(
    r"\bai[ -]?(short ?film|film|movie|video|animation|generated|cinema|cinematic|art)\b|"
    r"\b(veo ?3|seedance|kling|sora|runway|hailuo|midjourney|wan ?2|ltx|comfyui|stable diffusion)\b|"
    r"#ai(film|video|shortfilm|animation|art|generated)?\b", re.I)


def enabled() -> bool:
    return (os.environ.get("VIRAL_SOURCE") or "").strip().lower() == "open"


def _env_list(name: str, default: Iterable[str]) -> List[str]:
    raw = os.environ.get(name)
    if not raw:
        return list(default)
    return [x.strip() for x in raw.split(",") if x.strip()]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def iso_duration_seconds(s: str) -> Optional[int]:
    m = _ISO_DUR.fullmatch(s or "")
    if not m or not any(m.groups()):
        return None
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se


def _non_latin_share(t: str) -> float:
    letters = [c for c in (t or "") if c.isalpha()]
    return sum(1 for c in letters if ord(c) > 0x24F) / len(letters) if letters else 0.0


def norm_title(t: str) -> str:
    t = re.sub(r"#\w+", " ", (t or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


# ---------------------------------------------------------------- YouTube Data API
def _yt_key() -> str:
    return (os.environ.get("YOUTUBE_API_KEY") or "").strip()


def yt_videos(ids: List[str], key: str, get=requests.get) -> Dict[str, dict]:
    """videos.list for up to 50 ids per call (1 quota unit each call)."""
    out: Dict[str, dict] = {}
    for i in range(0, len(ids), 50):
        chunk = [x for x in ids[i:i + 50] if x]
        if not chunk:
            continue
        r = get(f"{YT_API}/videos", params={"part": "snippet,statistics,contentDetails", "id": ",".join(chunk),
                                            "key": key, "maxResults": 50}, timeout=20)
        data = r.json()
        if r.status_code != 200:
            raise RuntimeError(f"videos.list HTTP {r.status_code}: {(data.get('error') or {}).get('message', '')[:160]}")
        for it in data.get("items") or []:
            sn, st = it.get("snippet") or {}, it.get("statistics") or {}
            out[it["id"]] = {
                "platform": "YouTube", "id": it["id"],
                "url": f"https://www.youtube.com/watch?v={it['id']}",
                "title": sn.get("title") or "", "author": sn.get("channelTitle") or "",
                "description": (sn.get("description") or "")[:500],
                "published_at": sn.get("publishedAt"),
                "views": int(st.get("viewCount") or 0), "likes": int(st.get("likeCount") or 0) if st.get("likeCount") else None,
                "comments": int(st.get("commentCount") or 0) if st.get("commentCount") else None,
                "seconds": iso_duration_seconds((it.get("contentDetails") or {}).get("duration") or ""),
                "lang": sn.get("defaultAudioLanguage") or sn.get("defaultLanguage"),
            }
    return out


def yt_api_search(query: str, key: str, since: datetime, max_results: int = 25, get=requests.get) -> List[str]:
    r = get(f"{YT_API}/search", params={"part": "id", "type": "video", "order": "viewCount", "q": query,
                                        "publishedAfter": _iso(since), "maxResults": max_results, "key": key,
                                        "relevanceLanguage": os.environ.get("VIRAL_OPEN_LANG", "en")}, timeout=20)
    data = r.json()
    if r.status_code != 200:
        raise RuntimeError(f"search.list HTTP {r.status_code}: {(data.get('error') or {}).get('message', '')[:160]}")
    return [(it.get("id") or {}).get("videoId") for it in data.get("items") or [] if (it.get("id") or {}).get("videoId")]


# ---------------------------------------------------------------- yt-dlp (no key)
def _ytdlp_bin() -> Optional[str]:
    for p in (os.environ.get("YTDLP_BIN"), "/usr/local/bin/yt-dlp", shutil.which("yt-dlp")):
        if p and os.path.exists(p):
            return p
    return None


def ytdlp_search_ids(query: str, timeout: int = 60, runner=subprocess.run) -> List[dict]:
    exe = _ytdlp_bin()
    if not exe:
        raise RuntimeError("yt-dlp not available")
    url = f"https://www.youtube.com/results?search_query={quote_plus(query)}&sp={YT_TODAY_BY_VIEWS}"
    res = runner([exe, "--flat-playlist", "-J", "--no-warnings", url], capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or "")[-300:])
    out = []
    for e in (json.loads(res.stdout or "{}").get("entries") or []):
        if e.get("id") and re.search(r"/(watch|shorts)", e.get("url") or ""):
            out.append({"id": e["id"], "views": int(e.get("view_count") or 0), "title": e.get("title") or ""})
    return out


def ytdlp_probe(video_id: str, timeout: int = 60, runner=subprocess.run) -> Optional[dict]:
    """Exact upload time + views for one video without an API key (one page fetch)."""
    exe = _ytdlp_bin()
    if not exe:
        return None
    res = runner([exe, "-j", "--skip-download", "--no-warnings", f"https://www.youtube.com/watch?v={video_id}"],
                 capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        return None
    d = json.loads(res.stdout or "{}")
    ts = d.get("timestamp") or d.get("release_timestamp")
    if not ts:
        return None  # only a calendar day is known: too coarse for views/hour, skip rather than guess
    return {"platform": "YouTube", "id": video_id, "url": f"https://www.youtube.com/watch?v={video_id}",
            "title": d.get("title") or "", "author": d.get("channel") or d.get("uploader") or "",
            "description": (d.get("description") or "")[:500],
            "published_at": _iso(datetime.fromtimestamp(int(ts), timezone.utc)),
            "views": int(d.get("view_count") or 0), "likes": d.get("like_count"), "comments": d.get("comment_count"),
            "seconds": d.get("duration"), "lang": d.get("language")}


# ---------------------------------------------------------------- Reddit
def _reddit_token(post=requests.post) -> Optional[str]:
    cid, secret = os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET")
    if not (cid and secret):
        return None
    r = post("https://www.reddit.com/api/v1/access_token", auth=(cid, secret),
             data={"grant_type": "client_credentials"}, headers={"User-Agent": USER_AGENT}, timeout=20)
    return (r.json() or {}).get("access_token") if r.status_code == 200 else None


def reddit_top_day(sub: str, token: Optional[str] = None, get=requests.get) -> List[dict]:
    if token:
        url, headers = f"https://oauth.reddit.com/r/{sub}/top?t=day&limit=50", {"Authorization": f"bearer {token}"}
    else:
        url, headers = f"https://www.reddit.com/r/{sub}/top.json?t=day&limit=50", {}
    headers["User-Agent"] = USER_AGENT
    r = get(url, headers=headers, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"r/{sub} HTTP {r.status_code}" + ("" if token else " (public JSON; set REDDIT_CLIENT_ID/SECRET for OAuth)"))
    out = []
    for ch in ((r.json() or {}).get("data") or {}).get("children") or []:
        d = ch.get("data") or {}
        if not (d.get("is_video") or "v.redd.it" in (d.get("url") or "") or d.get("post_hint") in ("hosted:video", "rich:video")):
            continue
        out.append({"platform": "Reddit", "id": d.get("id"), "url": "https://www.reddit.com" + (d.get("permalink") or ""),
                    "title": d.get("title") or "", "author": "u/" + (d.get("author") or ""),
                    "published_at": _iso(datetime.fromtimestamp(float(d.get("created_utc") or 0), timezone.utc)),
                    "upvotes": int(d.get("score") or 0), "comments": int(d.get("num_comments") or 0), "views": None,
                    "subreddit": d.get("subreddit")})
    return out


# ---------------------------------------------------------------- pipeline
def _rank_fields(item: dict, now: datetime, metric: str) -> Optional[dict]:
    pub = _parse_iso(item.get("published_at") or "")
    if not pub:
        return None
    age_h = (now - pub).total_seconds() / 3600
    item["age_hours"] = round(age_h, 2)
    item[f"{metric}_per_hour"] = round((item.get(metric) or 0) / max(age_h, 0.25), 1)
    return item


def find_viral(queries: Optional[List[str]] = None, subreddits: Optional[List[str]] = None, top_n: int = 10,
               max_age_hours: Optional[float] = None, niche: Optional[bool] = None, max_seconds: Optional[int] = None,
               now: Optional[datetime] = None, yt_search=yt_api_search, yt_list=yt_videos,
               dlp_search=ytdlp_search_ids, dlp_probe=ytdlp_probe, reddit=reddit_top_day,
               reddit_token: Callable[[], Optional[str]] = _reddit_token) -> dict:
    """Never raises. Returns {"top": [...views/hour...], "reddit": [...upvotes/hour...], "sources": {...}, "errors": [...]}"""
    now = now or _now()
    queries = queries or _env_list("VIRAL_OPEN_QUERIES", DEFAULT_QUERIES)
    subreddits = subreddits if subreddits is not None else _env_list("VIRAL_OPEN_SUBREDDITS", DEFAULT_SUBREDDITS)
    max_age = float(max_age_hours if max_age_hours is not None else os.environ.get("VIRAL_OPEN_MAX_AGE_HOURS", 24))
    niche = niche if niche is not None else (os.environ.get("VIRAL_OPEN_NICHE", "1").strip() != "0")
    max_sec = int(max_seconds if max_seconds is not None else os.environ.get("VIRAL_OPEN_MAX_SECONDS", 180))
    latin_only = os.environ.get("VIRAL_OPEN_LATIN_ONLY", "1").strip() != "0"
    since = now - timedelta(hours=max_age)
    key = _yt_key()
    errors: List[str] = []
    sources = {"youtube_api": "no YOUTUBE_API_KEY" if not key else "ok", "youtube_ytdlp": "ok", "reddit": "ok",
               "tiktok": "unavailable: no key-free search (yt-dlp has no TikTok search extractor)",
               "instagram": "unavailable: search requires login"}
    ids: List[str] = []

    if key:
        for q in queries:
            try:
                ids += yt_search(q, key, since)
            except Exception as exc:
                errors.append(f"youtube_api {q}: {exc}"[:220])
        if errors and not ids:
            sources["youtube_api"] = "failed"
    dlp_ids: List[str] = []
    for q in queries:
        try:
            dlp_ids += [r["id"] for r in dlp_search(q)]
        except Exception as exc:
            errors.append(f"yt-dlp {q}: {exc}"[:220])
            sources["youtube_ytdlp"] = "failed"
    ids = list(dict.fromkeys(ids + dlp_ids))

    videos: Dict[str, dict] = {}
    if ids and key:
        try:
            videos = yt_list(ids, key)
        except Exception as exc:
            errors.append(f"videos.list: {exc}"[:220])
    if ids and not key:
        for vid in dlp_ids[: int(os.environ.get("VIRAL_OPEN_PROBE_LIMIT", 25))]:
            try:
                v = dlp_probe(vid)
                if v:
                    videos[vid] = v
            except Exception as exc:
                errors.append(f"yt-dlp probe {vid}: {exc}"[:220])

    top, seen_titles = [], set()
    for v in videos.values():
        v = _rank_fields(dict(v), now, "views")
        if not v or v["age_hours"] > max_age or v["age_hours"] < 0:
            continue
        if v.get("seconds") and v["seconds"] > max_sec:
            continue
        if niche and not NICHE_RE.search(v.get("title", "")):
            continue  # niche must show in the title itself (descriptions are tag-stuffed)
        if latin_only and _non_latin_share(v.get("title", "")) > 0.3:
            continue
        tkey = (v.get("author", "").lower(), norm_title(v.get("title", "")))
        if tkey in seen_titles:
            continue
        seen_titles.add(tkey)
        top.append(v)
    top.sort(key=lambda x: -x["views_per_hour"])

    red: List[dict] = []
    if subreddits:
        token = None
        try:
            token = reddit_token()
        except Exception as exc:
            errors.append(f"reddit oauth: {exc}"[:220])
        failed = 0
        for sub in subreddits:
            try:
                red += reddit(sub, token)
            except Exception as exc:
                failed += 1
                errors.append(f"reddit {exc}"[:220])
        if failed == len(subreddits):
            sources["reddit"] = "failed"
    rseen, rtop = set(), []
    for r in red:
        r = _rank_fields(dict(r), now, "upvotes")
        if not r or r["age_hours"] > max_age or r["url"] in rseen:
            continue
        rseen.add(r["url"])
        rtop.append(r)
    rtop.sort(key=lambda x: -x["upvotes_per_hour"])

    result = {"source": "open", "generated_at": _iso(now), "window_hours": max_age, "top": top[:top_n],
              "reddit": rtop[:top_n], "candidates": len(videos), "sources": sources, "errors": errors, "cost_usd": 0.0}
    logger.info("open viral: %s candidates, top=%s, errors=%s", len(videos),
                [(r["views_per_hour"], r["title"][:50]) for r in top[:3]], errors[:3])
    return result


def _fmt_int(n: Optional[int]) -> str:
    return f"{n:,}" if isinstance(n, int) else "n/a"


def to_trends(result: dict, brand: str = "6Frame Studio") -> List[dict]:
    """Shape open results like the Gemini trend scanner's ViralConcept dicts (no LLM, no invented copy)."""
    trends = []
    for r in (result.get("top") or []) + (result.get("reddit") or []):
        if r["platform"] == "Reddit":
            metrics = f'{_fmt_int(r.get("upvotes"))} upvotes, {_fmt_int(r.get("comments"))} comments in {r["age_hours"]:.1f}h ({r["upvotes_per_hour"]:,} upvotes/h)'
        else:
            metrics = f'{_fmt_int(r.get("views"))} views in {r["age_hours"]:.1f}h ({r["views_per_hour"]:,} views/h)'
        handle = r.get("author") or "the creator"
        title = r.get("title") or ""
        trends.append({
            "platform": r["platform"], "url": r["url"], "author": handle, "title": title,
            "viral_metrics": metrics, "published_at": r.get("published_at"),
            "views": r.get("views"), "views_per_hour": r.get("views_per_hour"), "age_hours": r.get("age_hours"),
            "original_concept": (r.get("description") or title)[:500],
            "studio_adaptation_concept": f"Original {brand} take on the idea behind \"{title[:120]}\" (no reuse of the creator's footage).",
            "recreated_video_prompt": f"Cinematic 8-second vertical shot inspired by: {title[:200]}",
            "recreated_linkedin_post": "", "recreated_twitter_thread": [], "recreated_instagram_caption": "",
            "original_post_text": title, "source": "open_viral",
        })
    return trends


if __name__ == "__main__":  # manual check: python open_viral.py
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(find_viral(), indent=2)[:20000])
