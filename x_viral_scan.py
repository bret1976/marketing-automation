"""X viral scan for 6Frame Studio Autopilot (Bret, 2026-11-13 t752u).

    "I want the viral scan to now do the search like it just did on X within the last 24 hours and then take the
     top 10 viral videos and decide which would be the easiest to recreate and then that's the video i want you
     to recreate. Only the videos, do not include the prompt-sharing ones."

Pipeline (no LLM, no paid generation; reading X needs an X API credential - see SOURCES):
  1. scan    - X posts with a NATIVE video, created in the last 24 h, in the AI-video / AI-film genre
               (genre evidence = an X News story about an AI video that clusters the post, or AI-video wording
               in the post itself). Prompt-sharing posts, prompt guides, tutorials, tool reviews / ads are dropped.
  2. rank    - by views (X impression_count), the same video re-shared by several accounts counted once,
               keep the top 10. Each item records url, author, created_at, views, likes, video duration.
  3. score   - "easiest to recreate" rubric (RUBRIC, every point logged). Real-person likeness, copyrighted
               show / film / game / brand IP and artists' music videos are NOT recreatable (hard block).
  4. pick    - highest score among the recreatable ones (ties -> more views). Nothing recreatable -> the slot
               is skipped (BrandGateSkip) with the reason; never a fallback topic.

SOURCES (pluggable, first that is configured wins):
  * XApiSource       - X API v2 with an app bearer token: env X_BEARER_TOKEN (or the app's saved
                       twitter_consumer_key/secret -> app-only token). News search + post lookup + recent search.
                       UNVERIFIED end-to-end: the live app has no X credential (2026-11-13).
  * SnapshotSource   - a JSON snapshot of the same X API v2 responses (env X_SCAN_SNAPSHOT=<path>, or
                       <STATE_DIR>/x_scan_snapshot.json uploaded with POST /api/x-scan/snapshot). Used only while
                       fresh (X_SNAPSHOT_MAX_AGE_HOURS, default 6 h) so metrics are never stale.
  * none             - XScanUnavailable (a BrandGateSkip): the slot is skipped and the missing credential is named.
"""
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import requests

import brand_gate

logger = logging.getLogger(__name__)
MARKER = "X_VIRAL_SCAN"
VERSION = "2026-11-13.x1"
WINDOW_HOURS = 24.0
TOP_N = 10
API = "https://api.x.com/2"
NEWS_QUERIES = ("AI video", "AI film")
SEARCH_QUERIES = (
    '("ai video" OR "ai film" OR "ai short film" OR "ai generated" OR "ai-generated" OR "made with ai" OR "ai movie" '
    'OR "ai animation") has:videos -is:retweet -is:reply',
    '(AI OR Veo OR Sora OR Kling OR Seedance OR "Grok Imagine" OR Higgsfield) (video OR film OR movie OR trailer OR short) '
    'has:videos -is:retweet -is:reply',
)
POST_FIELDS = "created_at,public_metrics,author_id,lang,attachments,text"
EXPANSIONS = "attachments.media_keys,author_id,attachments.media_source_tweet"
MEDIA_FIELDS = "duration_ms,type,public_metrics,media_key,width,height"
USER_FIELDS = "username,name,public_metrics,verified"


class XScanUnavailable(brand_gate.BrandGateSkip):
    """No X data source is configured (or it failed): skip the slot, name what is missing."""


# --------------------------------------------------------------------------- genre / exclusion rules
# a story (X News) or post that is ABOUT an AI-made video
STORY_GENRE = re.compile(r"\bai\b.{0,40}\b(video|film|movie|short|parody|trailer|music video|m/?v|clip|animation|thriller|memes?)\b|"
                         r"\b(video|film|movie|parody|trailer|clip)\b.{0,40}\b(ai|ai-generated)\b", re.I)
POST_GENRE = re.compile(
    brand_gate.GENRE_RE.pattern + r"|\b(grok ?imagine|higgsfield|midjourney video|pika|luma (dream|ray))\b|"
    r"\b(this|the) (video|clip|film) (is|was) (all|fully|entirely|100%)? ?(ai|a\.i\.)\b|\ball ai\b|@imagine\b|"
    r"\bai[- ]animated\b|\bai video generation\b", re.I)
# prompt-sharing: excluded outright (Bret: "do not include the prompt-sharing ones")
PROMPT_SHARING = re.compile(r"\bprompts?\b|\bprompting\b|\bprompted\b|\bprompt[- ]?(guide|share|sharing|engineering)\b|"
                            r"\bjson prompt\b|\bimage prompt\b", re.I)
# not a viral video: tutorials, tool reviews / ads, model comparisons, job posts, tier lists
NOT_A_VIDEO = re.compile(r"\b(tutorial|how to|how i made|step[- ]by[- ]step|guide|workflow|course|masterclass|review|tier list|"
                         r"vs\.?|versus|compared?|comparison|cheaper|pricing|price|discount|\d+% off|sign ?up|free trial|"
                         r"link in (bio|the reply)|hiring|job|role|subscribe to|fanvue|onlyfans|giveaway|airdrop)\b", re.I)

# --------------------------------------------------------------------------- recreate rubric
# Text-only checks over the post text + the X News story that clusters it. They do not look at the video, so
# Bret's approval of the finished video stays the real check (UNVERIFIED by pixels).
REAL_PEOPLE = re.compile(
    r"\b(jensen huang|elon musk|musk|trump|biden|obama|kamala|zuckerberg|bezos|sam altman|altman|taylor swift|beyonc[eé]|"
    r"drake|kanye|rihanna|tom cruise|keanu|will smith|the rock|dwayne johnson|mrbeast|pope|king charles|putin|zelensky|"
    r"mel gibson|danny glover|messi|ronaldo|lebron|moises arias|austin abrams|la toya jackson|ceo|president|"
    r"celebrity|celeb|deepfake|lookalike|look-alike|members?|idol|k-?pop|h1-?key|bts|blackpink|twice|newjeans)\b", re.I)
COPYRIGHT_IP = re.compile(
    r"\b(the office|dunder mifflin|lethal weapon|gta ?\d*|grand theft auto|vice city|marvel|avengers|spider-?man|batman|"
    r"superman|dc comics|disney|pixar|star wars|harry potter|pok[eé]mon|nintendo|mario|zelda|simpsons|friends|"
    r"game of thrones|stranger things|squid game|breaking bad|netflix|hbo|minecraft|fortnite|roblox|skyrim|"
    r"resident evil|lord of the rings|james bond|barbie|shrek|naruto|one piece|dragon ball|john wick|"
    r"m/v|music video|official mv|official video|digital single|album|lyrics)\b", re.I)
MULTI_SCENE = re.compile(r"\b(trailer|episode|ep\.? ?\d+|season|series|montage|compilation|day in the life|movie|"
                         r"full film|short film|saga|chapter|part \d|parody|music video|m/v)\b", re.I)
MANY_PEOPLE = re.compile(r"\b(crowd|army|parade|group|band|cast|family|team|girls|boys|people|everyone|members|"
                         r"audience|soldiers|hikers|dancers|office|coworkers|co-workers)\b", re.I)
RUBRIC = {
    "duration": "<=15 s: 3, <=35 s: 2, <=60 s: 1, longer/unknown: 0",
    "single_scene": "no trailer/episode/montage/parody/series markers: 2",
    "few_characters": "no crowd/group/cast markers: 2",
    "zero_cost_recipe": "a $0 recreate recipe exists for the subject (x_recreate_recipes): 3",
    "blocks": "real identifiable person / celebrity likeness, copyrighted show/film/game/brand IP, artists' music videos "
              "-> not recreatable (score 0)",
}


def _parse(s: Any) -> Optional[datetime]:
    return brand_gate._parse_iso(s)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- sources
class SnapshotSource:
    """Reads a JSON snapshot: {"fetched_at": iso, "fetched_by": str, "stories": [x news story...],
    "post_payloads": [x api v2 post responses with data/includes]}."""
    name = "snapshot"

    def __init__(self, path: str, max_age_hours: Optional[float] = None):
        self.path = path
        self.max_age_hours = float(max_age_hours if max_age_hours is not None
                                   else os.environ.get("X_SNAPSHOT_MAX_AGE_HOURS", 6))

    def fetch(self, now: Optional[datetime] = None) -> dict:
        now = now or _now()
        with open(self.path, encoding="utf-8") as fh:
            snap = json.load(fh)
        at = _parse(snap.get("fetched_at"))
        if not at:
            raise XScanUnavailable(f"X snapshot {self.path} has no fetched_at; refusing stale/unknown data")
        age = (now - at).total_seconds() / 3600
        if age > self.max_age_hours or age < -0.25:
            raise XScanUnavailable(f"X snapshot is {age:.1f} h old (max {self.max_age_hours:.0f} h); views would be stale")
        snap["source"] = {"kind": "snapshot", "path": self.path, "fetched_at": snap.get("fetched_at"),
                          "fetched_by": snap.get("fetched_by"), "age_hours": round(age, 2)}
        return snap


class XApiSource:
    """X API v2 with an app bearer token. UNVERIFIED end-to-end (no credential on the live app)."""
    name = "x_api"

    def __init__(self, bearer: str, get: Callable = requests.get):
        self.bearer = bearer
        self.get = get

    def _call(self, path: str, params: dict) -> dict:
        r = self.get(f"{API}{path}", headers={"Authorization": f"Bearer {self.bearer}"}, params=params, timeout=30)
        if getattr(r, "status_code", 200) != 200:
            raise XScanUnavailable(f"X API {path} HTTP {r.status_code}: {(r.text or '')[:160]}")
        return r.json()

    def fetch(self, now: Optional[datetime] = None) -> dict:
        now = now or _now()
        start = (now - timedelta(hours=WINDOW_HOURS) + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        stories, ids = [], []
        for q in NEWS_QUERIES:
            res = self._call("/news/search", {"query": q, "max_age_hours": int(WINDOW_HOURS), "max_results": 25,
                                              "news.fields": "name,summary,hook,updated_at,cluster_posts_results"})
            for s in res.get("data") or []:
                stories.append(s)
                ids += [c.get("post_id") for c in s.get("cluster_posts_results") or [] if c.get("post_id")]
        payloads = []
        ids = list(dict.fromkeys(ids))
        for i in range(0, len(ids), 100):
            payloads.append(self._call("/tweets", {"ids": ",".join(ids[i:i + 100]), "tweet.fields": POST_FIELDS,
                                                   "expansions": EXPANSIONS, "media.fields": MEDIA_FIELDS,
                                                   "user.fields": USER_FIELDS}))
        for q in SEARCH_QUERIES:
            payloads.append(self._call("/tweets/search/recent", {
                "query": q, "start_time": start, "max_results": 100, "sort_order": "relevancy",
                "tweet.fields": POST_FIELDS, "expansions": EXPANSIONS, "media.fields": MEDIA_FIELDS,
                "user.fields": USER_FIELDS}))
        return {"fetched_at": now.isoformat(), "stories": stories, "post_payloads": payloads,
                "source": {"kind": "x_api", "fetched_at": now.isoformat()}}


def snapshot_path() -> str:
    return os.environ.get("X_SCAN_SNAPSHOT") or os.path.join(
        os.environ.get("STATE_DIR") or os.environ.get("DATA_DIR") or ".", "x_scan_snapshot.json")


def source_from_env(settings: Optional[dict] = None, bearer_fetcher: Optional[Callable] = None):
    """First configured source, or raise XScanUnavailable naming what is missing. Never invents data."""
    bearer = (os.environ.get("X_BEARER_TOKEN") or os.environ.get("TWITTER_BEARER_TOKEN") or "").strip()
    if not bearer and settings and settings.get("twitter_consumer_key") and settings.get("twitter_consumer_secret") and bearer_fetcher:
        bearer = bearer_fetcher(settings) or ""
    if bearer:
        return XApiSource(bearer)
    path = snapshot_path()
    if os.path.exists(path):
        return SnapshotSource(path)
    raise XScanUnavailable(
        "X scan unavailable: the app has no X API credential (set X_BEARER_TOKEN, or save twitter_consumer_key/"
        "twitter_consumer_secret in Settings) and no fresh X scan snapshot was uploaded (POST /api/x-scan/snapshot). "
        "Not falling back to any other source; slot skipped.")


# --------------------------------------------------------------------------- normalise / filter / rank
def _story_index(stories: Iterable[dict]) -> Dict[str, List[dict]]:
    idx: Dict[str, List[dict]] = {}
    for s in stories or []:
        if not STORY_GENRE.search(f"{s.get('name') or ''}"):
            continue  # only stories that are about an AI-made video count as genre evidence
        for c in s.get("cluster_posts_results") or s.get("posts") or []:
            pid = c.get("post_id") if isinstance(c, dict) else c
            if pid:
                idx.setdefault(str(pid), []).append({"id": s.get("id"), "name": s.get("name"),
                                                     "hook": (s.get("hook") or "")[:300],
                                                     "summary": (s.get("summary") or "")[:600]})
    return idx


def normalise(snapshot: dict) -> List[dict]:
    """Every post in the snapshot -> one flat record (deduped by post id)."""
    stories = _story_index(snapshot.get("stories") or [])
    out: Dict[str, dict] = {}
    for payload in snapshot.get("post_payloads") or []:
        inc = payload.get("includes") or {}
        users = {u.get("id"): u for u in inc.get("users") or []}
        media = {m.get("media_key"): m for m in inc.get("media") or []}
        for p in payload.get("data") or []:
            pid = str(p.get("id"))
            ms = [media.get(k) or {} for k in (p.get("attachments") or {}).get("media_keys") or []]
            videos = [m for m in ms if m.get("type") == "video"]
            pm = p.get("public_metrics") or {}
            u = users.get(p.get("author_id")) or {}
            rec = {
                "id": pid, "platform": "X", "url": f"https://x.com/{u.get('username') or 'i'}/status/{pid}",
                "author": u.get("username"), "author_name": u.get("name"), "created_at": p.get("created_at"),
                "published_at": p.get("created_at"), "views": int(pm.get("impression_count") or 0),
                "likes": int(pm.get("like_count") or 0), "reposts": int(pm.get("retweet_count") or 0),
                "replies": int(pm.get("reply_count") or 0), "bookmarks": int(pm.get("bookmark_count") or 0),
                "has_video": bool(videos), "media_types": [m.get("type") for m in ms],
                "video_duration_s": round(videos[0]["duration_ms"] / 1000.0, 1) if videos and videos[0].get("duration_ms") else None,
                "video_key": videos[0].get("media_key") if videos else None,
                "text": (p.get("text") or "")[:600], "stories": stories.get(pid, []),
            }
            prev = out.get(pid)
            if not prev or rec["views"] > prev["views"]:
                out[pid] = rec
    return list(out.values())


def context_text(it: dict) -> str:
    """Post text + the X News stories that cluster it (what the video is about)."""
    return " ".join([it.get("text") or ""] + [f"{s.get('name')}. {s.get('hook')}" for s in it.get("stories") or []])


def check_item(it: dict, now: datetime, window: float = WINDOW_HOURS) -> Tuple[bool, str]:
    created = _parse(it.get("created_at"))
    if not created:
        return False, "no created_at"
    age = (now - created).total_seconds() / 3600
    it["age_hours"] = round(age, 2)
    if age < 0 or age > window:
        return False, f"outside the last {window:.0f} h ({age:.1f} h old)"
    if not it.get("has_video"):
        return False, f"no native video ({','.join(t or '?' for t in it.get('media_types') or []) or 'text only'})"
    text = brand_gate.title_text(it.get("text") or "")
    if PROMPT_SHARING.search(text):
        return False, "prompt-sharing post (excluded by Bret)"
    m = NOT_A_VIDEO.search(text)
    if m:
        return False, f"tutorial/tool/promo post, not a viral video ('{m.group(0)}')"
    ev = next((s["name"] for s in it.get("stories") or []), None)
    if ev:
        it["genre_evidence"] = f"X News story: {ev}"
    elif POST_GENRE.search(text):
        it["genre_evidence"] = f"post text: '{POST_GENRE.search(text).group(0)}'"
    else:
        return False, "not AI-video genre (no AI-video story and no AI-video wording)"
    if not it.get("views"):
        return False, "no views recorded"
    return True, ""


def _same_video_key(it: dict) -> Tuple:
    # the same clip re-shared by several accounts: same story + same duration (X keeps the source media length)
    story = (it.get("stories") or [{}])[0].get("id")
    return (story, it.get("video_duration_s")) if story and it.get("video_duration_s") else ("post", it["id"])


def rank(items: List[dict], now: datetime, top_n: int = TOP_N, log: Optional[List[str]] = None) -> List[dict]:
    log = log if log is not None else []
    kept: Dict[Tuple, dict] = {}
    for it in sorted(items, key=lambda x: -int(x.get("views") or 0)):
        ok, why = check_item(it, now)
        if not ok:
            log.append(f"drop @{it.get('author')} {it.get('id')} ({it.get('views')} views): {why}")
            continue
        k = _same_video_key(it)
        if k in kept:
            kept[k].setdefault("reshares", []).append({"url": it["url"], "views": it["views"]})
            log.append(f"dedupe @{it.get('author')} {it['id']}: same video as @{kept[k]['author']} {kept[k]['id']}")
            continue
        kept[k] = it
    top = sorted(kept.values(), key=lambda x: -int(x.get("views") or 0))[:top_n]
    for i, it in enumerate(top, 1):
        it["rank"] = i
    return top


# --------------------------------------------------------------------------- recreate score
def recreate_score(it: dict, recipes: Optional[List[dict]] = None) -> dict:
    import x_recreate_recipes
    recipes = recipes if recipes is not None else x_recreate_recipes.RECIPES
    ctx = context_text(it)
    pts: Dict[str, int] = {}
    why: List[str] = []
    blocks: List[str] = []
    m = REAL_PEOPLE.search(ctx)
    if m:
        blocks.append(f"real identifiable person / likeness ('{m.group(0)}')")
    m = COPYRIGHT_IP.search(ctx)
    if m:
        blocks.append(f"copyrighted show/film/game/brand/music IP ('{m.group(0)}')")
    d = it.get("video_duration_s")
    pts["duration"] = 3 if d and d <= 15 else 2 if d and d <= 35 else 1 if d and d <= 60 else 0
    why.append(f"duration {d}s -> {pts['duration']}")
    ms = MULTI_SCENE.search(ctx)
    pts["single_scene"] = 0 if ms else 2
    why.append(f"single scene -> {pts['single_scene']}" + (f" (multi-scene marker '{ms.group(0)}')" if ms else ""))
    mp = MANY_PEOPLE.search(ctx)
    pts["few_characters"] = 0 if mp else 2
    why.append(f"few characters -> {pts['few_characters']}" + (f" (marker '{mp.group(0)}')" if mp else ""))
    recipe = next((r for r in recipes if re.search(r["match"], ctx, re.I)), None)
    pts["zero_cost_recipe"] = 3 if recipe else 0
    why.append(f"$0 recipe -> {pts['zero_cost_recipe']}" + (f" ({recipe['key']})" if recipe else " (none matches)"))
    total = sum(pts.values())
    recreatable = not blocks and bool(recipe)
    if not recipe and not blocks:
        why.append("not recreatable at $0: no recipe the free pipeline can film")
    return {"score": total if recreatable else 0, "raw_points": total, "points": pts, "recreatable": recreatable,
            "blocks": blocks, "why": why, "recipe": recipe["key"] if recipe else None}


def pick(top: List[dict], recipes: Optional[List[dict]] = None, log: Optional[List[str]] = None) -> Optional[dict]:
    log = log if log is not None else []
    for it in top:
        it["recreate"] = recreate_score(it, recipes)
        log.append(f"#{it['rank']} @{it['author']} {it['views']} views {it['video_duration_s']}s -> score "
                   f"{it['recreate']['score']} {'' if it['recreate']['recreatable'] else 'NOT recreatable '}"
                   f"{it['recreate']['blocks'] or ''} {it['recreate']['why']}")
    ok = [it for it in top if it["recreate"]["recreatable"]]
    if not ok:
        return None
    best = sorted(ok, key=lambda x: (-x["recreate"]["score"], -x["views"]))[0]
    log.append(f"PICK #{best['rank']} @{best['author']} {best['url']}: highest recreate score "
               f"{best['recreate']['score']} among {len(ok)} recreatable of {len(top)}")
    return best


def provenance(it: dict) -> dict:
    """The record brand_gate.check_viral_item / publish_gate checks for an X pick."""
    return {"platform": "X", "url": it["url"], "title": (brand_gate.title_text(it.get("text") or "") or it["url"])[:160],
            "author": it.get("author"), "published_at": it.get("created_at"), "views": it.get("views"),
            "likes": it.get("likes"), "views_per_hour": round(it["views"] / max(it.get("age_hours") or 1, 1), 1),
            "video_duration_s": it.get("video_duration_s"), "genre_evidence": it.get("genre_evidence"),
            "age_hours_at_pick": it.get("age_hours"), "stories": [s.get("name") for s in it.get("stories") or []]}


def summary_row(it: dict) -> dict:
    r = it.get("recreate") or {}
    return {"rank": it.get("rank"), "url": it.get("url"), "author": it.get("author"), "created_at": it.get("created_at"),
            "age_hours": it.get("age_hours"), "views": it.get("views"), "likes": it.get("likes"),
            "video_duration_s": it.get("video_duration_s"), "genre_evidence": it.get("genre_evidence"),
            "text": (it.get("text") or "")[:140], "reshares": it.get("reshares", []),
            "recreate_score": r.get("score"), "recreatable": r.get("recreatable"), "blocks": r.get("blocks"),
            "points": r.get("points"), "why": r.get("why"), "recipe": r.get("recipe")}


def run_scan(source=None, settings: Optional[dict] = None, now: Optional[datetime] = None,
             bearer_fetcher: Optional[Callable] = None) -> dict:
    """Scan -> top 10 -> scores -> pick. Never raises for 'nothing qualifies' (pick=None); raises XScanUnavailable
    when there is no data source."""
    now = now or _now()
    source = source or source_from_env(settings, bearer_fetcher)
    snap = source.fetch(now)
    log: List[str] = []
    items = normalise(snap)
    top = rank(items, now, log=log)
    best = pick(top, log=log)
    res = {"version": VERSION, "scanned_at": now.isoformat(), "window_hours": WINDOW_HOURS, "source": snap.get("source"),
           "posts_seen": len(items), "rubric": RUBRIC, "top": [summary_row(it) for it in top],
           "pick": summary_row(best) if best else None, "pick_item": best, "log": log[-80:]}
    logger.info("%s %s posts, top=%s, pick=%s", MARKER, len(items), [(t['author'], t['views']) for t in top],
                best and best["url"])
    return res
