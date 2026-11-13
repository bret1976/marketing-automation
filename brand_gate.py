"""Brand / genre / 24-hour / footage-match gate for every Autopilot publish (Bret, 2026-11-13).

Bret's rule for 6Frame Studio Autopilot, enforced on EVERY publish path (scheduled slot, manual
/api/trigger-autopilot, legacy hour trigger, dry-run), because they all go through
growth_v2.run_autopilot():

  1. Brand + genre come from the company website (6framestudio.com): AI filmmaking / AI video.
  2. The viral scan only counts uploads that are themselves AI-filmmaking content. A hashtag pile
     ("Wife slap husband #drama #shorts #aivideo #reels") does NOT make an upload in-genre: the
     AI-filmmaking marker has to be in the title text itself.
  3. The topic comes only from such an upload published within the last 24 hours; its platform, URL,
     title, channel, upload time, views and views/hour are recorded on the post.
  4. Every shot must be on the narrated subject: named for the subject in its own title AND (scanned
     subjects) sharing at least one concrete narration term; news / broadcaster / watermarked /
     talking-head material is rejected. (This check reads each clip's file title / description /
     category; it does not look at pixels - see check_clip.)
  5. Anything missing -> BrandGateSkip: the slot is skipped with the reason logged; never a fallback.

Free and local: no paid API, no LLM. One optional GET of the public website (cached).
"""
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)
GATE_VERSION = "2026-11-13.1"
MAX_AGE_HOURS = 24.0
# a render takes minutes; the item must be <= 24 h old when picked and still <= 24 h + this when published
PUBLISH_GRACE_HOURS = 1.0
BRAND_SITE = "https://6framestudio.com"
PINNED_BRAND = {"name": "6Frame Studio", "genre": "AI filmmaking / AI video", "site": BRAND_SITE}
SITE_GENRE_RE = re.compile(r"\bai[ -]?(short ?film|film|movie|video|production|assisted production|filmmaking|generated)\b|"
                           r"\b(veo|sora|kling|runway|seedance|generative video)\b", re.I)
# same vocabulary as open_viral.NICHE_RE, but matched on the title WITHOUT its hashtags
GENRE_RE = re.compile(
    r"\bai[ -]?(short ?film|film|movie|video|animation|generated|cinema|cinematic)\b|"
    r"\b(veo ?\d(\.\d)?|seedance|kling( ai)?|sora( ?2)?|runway( ml| gen-?\d)?|hailuo|wan ?2(\.\d)?|ltx ?video)\b|"
    r"\b(made|created|generated|filmed|shot)\s+(with|using|by|in)\s+(ai|a\.i\.)\b|\btext[ -]to[ -]video\b", re.I)
HASHTAG = re.compile(r"#[\w\u00c0-\uffff]+")
# in-genre titles that are not a film about anything: tools, tutorials, model comparisons, skits
NOT_A_FILM = re.compile(r"\b(tutorial|how to|guide|tips?|prompts?|workflow|review|vs\.?|versus|compar\w*|tested|"
                        r"free|course|install|update|released?|news|pricing|unlimited|hack|"
                        r"drama|prank|skit|wife|husband|girlfriend|boyfriend|mother-in-law|funny|comedy|"
                        r"reaction|meme|asmr|minecraft|roblox|gta)\b", re.I)


class BrandGateSkip(RuntimeError):
    """Raised when a slot must be skipped (not retried, never filled with something else)."""


def _parse_iso(s: Any) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- 1. brand from the website
_BRAND_CACHE: Dict[str, Any] = {}


def brand_profile(get=None, ttl: int = 6 * 3600) -> dict:
    """Reads 6framestudio.com (free, cached) and confirms the genre is AI filmmaking. If the site can't be
    read the pinned genre (same value, from Bret 2026-11-12) is used and that is recorded."""
    if _BRAND_CACHE.get("at") and time.time() - _BRAND_CACHE["at"] < ttl:
        return dict(_BRAND_CACHE["profile"])
    prof = dict(PINNED_BRAND)
    try:
        if get is None:
            import requests
            get = requests.get
        r = get(BRAND_SITE, timeout=15, headers={"User-Agent": "6frame-marketing-automation/1.0 (brand check)"})
        text = re.sub(r"<[^>]+>", " ", r.text or "")
        hits = sorted({m.group(0).lower() for m in SITE_GENRE_RE.finditer(text)})
        title = re.search(r"<title>(.*?)</title>", r.text or "", re.S | re.I)
        prof.update({"source": "website" if hits else "pinned (website names no AI-filmmaking terms)",
                     "site_title": " ".join((title.group(1) if title else "").split())[:120], "site_evidence": hits[:8],
                     "http_status": getattr(r, "status_code", None)})
    except Exception as exc:
        prof.update({"source": "pinned (website unreachable)", "site_error": str(exc)[:160]})
    _BRAND_CACHE.update({"at": time.time(), "profile": prof})
    return dict(prof)


# --------------------------------------------------------------------------- 2-3. viral item: in genre, <= 24 h
def title_text(title: str) -> str:
    """The title without hashtags or emoji: what the upload says it IS."""
    t = HASHTAG.sub(" ", title or "")
    t = re.sub(r"[\U0001F000-\U0001FFFF\u2600-\u27BF]", " ", t)
    return " ".join(t.split())


def check_viral_item(it: dict, now: Optional[datetime] = None, max_age: float = MAX_AGE_HOURS) -> Tuple[bool, str, dict]:
    """(ok, reason, provenance). The upload must be AI-filmmaking content (marker in the title text, not only in
    hashtags), be a film rather than a tool/tutorial/skit, and have a real upload time within max_age hours."""
    now = now or _now()
    title = it.get("title") or ""
    prov = {"platform": it.get("platform") or "YouTube", "url": it.get("url"), "title": title[:160],
            "author": it.get("author"), "published_at": it.get("published_at"), "views": it.get("views"),
            "views_per_hour": it.get("views_per_hour"), "likes": it.get("likes"), "comments": it.get("comments")}
    body = title_text(title)
    if str(it.get("platform") or "").upper() == "X":  # X viral scan (x_viral_scan.py, Bret 2026-11-13 t752u)
        return _check_x_item(it, prov, body, now, max_age)
    if not GENRE_RE.search(body):
        why = "AI-filmmaking tag only in hashtags" if GENRE_RE.search(title) or re.search(r"#ai\w*", title, re.I) else "off-genre"
        return False, f"{why}: {title[:80]}", prov
    if NOT_A_FILM.search(body):
        return False, f"in-genre but not a film (tool/tutorial/skit): {title[:80]}", prov
    pub = _parse_iso(it.get("published_at"))
    if not pub:
        return False, f"no upload time: {title[:80]}", prov
    age = (now - pub).total_seconds() / 3600.0
    prov["age_hours_at_pick"] = round(age, 2)
    if age < 0 or age > max_age:
        return False, f"not within {max_age:.0f}h ({age:.1f}h old): {title[:80]}", prov
    if not it.get("url") or not int(it.get("views") or 0):
        return False, f"no url/engagement recorded: {title[:80]}", prov
    return True, "", prov


X_PROMPT_SHARING = re.compile(r"\bprompts?\b|\bprompting\b|\bprompted\b", re.I)
X_GENRE_EVIDENCE = re.compile(r"\bai\b|\bai-generated\b|@imagine|grok ?imagine|veo|sora|kling|seedance|runway|higgsfield", re.I)


def _check_x_item(it: dict, prov: dict, body: str, now: datetime, max_age: float) -> Tuple[bool, str, dict]:
    """An X post picked by the X viral scan: native video, genre evidence recorded (an X News story about an AI
    video that clusters the post, or AI-video wording in the post), not prompt-sharing, <= max_age hours, url +
    views recorded."""
    prov.update(platform="X", genre_evidence=it.get("genre_evidence"), video_duration_s=it.get("video_duration_s"))
    ev = it.get("genre_evidence") or ""
    if not X_GENRE_EVIDENCE.search(ev):
        return False, f"X post has no AI-video genre evidence: {it.get('url')}", prov
    if X_PROMPT_SHARING.search(body):
        return False, f"X prompt-sharing post (excluded): {it.get('url')}", prov
    if not it.get("video_duration_s"):
        return False, f"X post has no native video recorded: {it.get('url')}", prov
    pub = _parse_iso(it.get("published_at"))
    if not pub:
        return False, f"no post time: {it.get('url')}", prov
    age = (now - pub).total_seconds() / 3600.0
    prov["age_hours_at_check"] = round(age, 2)
    if age < 0 or age > max_age:
        return False, f"not within {max_age:.0f}h ({age:.1f}h old): {it.get('url')}", prov
    if not it.get("url") or not int(it.get("views") or 0):
        return False, f"no url/views recorded: {it.get('url')}", prov
    return True, "", prov


# --------------------------------------------------------------------------- subject
MEDIA_SUBJECT = re.compile(r"\b(novel|novella|book|poem|poetry|play|short story|opera|magazine|newspaper|literary|"
                           r"fiction(al)?|painting|sculpture|artwork|film|movie|television|tv|series|song|album|"
                           r"musical|comic|video game|board game|software|website|brand|company)\b", re.I)
FILMABLE_KINDS = ("history", "space", "nature", "wildlife", "tech")


def check_subject(subj: dict, viral_title: str, kind: str) -> Tuple[bool, str]:
    """A scanned Wikipedia subject must be the thing the viral film is about, and something a camera can film."""
    title = subj.get("title") or ""
    desc = subj.get("description") or ""
    paren = re.search(r"\(([^)]*)\)\s*$", title)
    if paren and not re.search(r"\b" + re.escape(paren.group(1).lower()) + r"\b", (viral_title or "").lower()):
        return False, f"homonym ({paren.group(1)}) not named in the viral title: {title}"
    if MEDIA_SUBJECT.search(desc):
        return False, f"subject is a work/media, not filmable footage: {title} | {desc[:80]}"
    if kind not in FILMABLE_KINDS:
        return False, f"subject kind '{kind}' has no real-world footage to match: {title} | {desc[:80]}"
    return True, ""


# --------------------------------------------------------------------------- 4. per-clip match
# checked on the clip's file title + author (all topics) and, for scanned subjects, its description too.
# "newsreel" is archival and allowed; modern TV news, broadcasters, watermarks and talking heads are not.
JUNK = re.compile(r"\bvoa\b|voice of america|\bbbc\b|\bcnn\b|\bnbc\b|\bcbs\b|\babc news\b|al ?jazeera|\breuters\b|"
                  r"associated press|\bap news\b|euronews|dw news|\bcgtn\b|\brt news\b|\bnews\b|newscast|bulletin|headlines|"
                  r"anchor(man|woman)?\b|correspondent|reporters?\b|watermark\w*|\blogo\b|"
                  r"screen ?(recording|capture|shot)|\bvlog\b|selfie|talks? about|interview\w*|press conference|"
                  r"\bspeech\b|lecture|webinar|podcast|trailer|gameplay|open alpha|\bgame\b", re.I)
WEB_MARK = re.compile(r"\b\w+\.(com|net|tv)\b", re.I)  # a site name burned into a file TITLE = watermark/screen grab


STOP = set("""the a an of and in on at to for from with by is are was were be been being it its this that these those
as or but not no into onto over under about after before during while when where which who whom whose than then there
their they them his her he she we our you your i me my one two three four five six seven eight nine ten first second
third also more most many much some such very can could would should will may might has have had do does did
real footage video file film clip webm ogv mp4 jpg jpeg png photo image picture view part new old year years today
called known named people time times day days made make like just only even same other another each every both
""".split())


def _words(s: str) -> List[str]:
    return re.findall(r"[a-z0-9]+", (s or "").lower())


def narration_terms(topic: dict) -> set:
    """Concrete words of the narration (and subject description): names, places, years, things."""
    text = " ".join(f"{v.get('say', '')} {v.get('show', '')}" for v in topic.get("vo") or [])
    text += " " + " ".join(topic.get("lines") or []) + " " + (topic.get("subject_description") or "")
    name = set(_words(topic.get("topic") or ""))
    req = re.compile(topic.get("require") or r"$^", re.I)
    out = set()
    for w in _words(text):
        if w in STOP or w in name or req.fullmatch(w) or any(w.startswith(n) for n in name if len(n) >= 4):
            continue  # the subject's own name (and "slaps", "moods") is not independent evidence
        if re.fullmatch(r"1[5-9]\d\d|20[0-4]\d", w) or (len(w) >= 5 and not w.isdigit()):
            out.add(w)
    return out


def check_clip(c: dict, topic: dict, pinned_titles: Iterable[str] = ()) -> dict:
    """One candidate clip against the topic's narration. Returns {"ok", "reason", "evidence"}.
    Metadata check (file title, description, author, Commons category): it does NOT inspect frames, so a
    clip whose metadata lies would pass; it rejects homonyms ("Slap" = Slovene for waterfall), stills that
    only share a word, news/broadcaster/watermarked material and talking heads."""
    title = c.get("title") or ""
    desc = c.get("description") or ""
    text = f"{title} {desc}"
    res = {"clip": title[:100], "provider": c.get("provider"), "ok": False, "reason": "", "evidence": []}
    junk_text = f"{title} {c.get('author') or ''}" + (f" {desc}" if topic.get("title_only") else "")
    junk = JUNK.search(junk_text) or WEB_MARK.search(title)
    if junk:
        res["reason"] = f"news/broadcaster/watermark/talking-head material ('{junk.group(0)}')"
        return res
    if topic.get("exclude") and re.search(topic["exclude"], title, re.I):
        res["reason"] = "excluded for this topic (film/artwork/sport/politics homonym)"
        return res
    if title in set(pinned_titles):
        res.update(ok=True, reason="hand-verified library film", evidence=["pinned"])
        return res
    if not topic.get("title_only"):  # hand-written library topic: its own require regex must hit the file TITLE
        m = re.search(topic.get("require") or r"$^", title, re.I)
        if not m:
            res["reason"] = "library topic: file title does not name the subject"
            return res
        res.update(ok=True, reason="file title names the library subject", evidence=[m.group(0)])
        return res
    phrase = topic.get("match") or r"$^"
    req = topic.get("require") or phrase
    m = re.search(r"\b(?:" + phrase + r")\b", title, re.I) or re.search(req, title, re.I)
    cat = ((topic.get("queries") or {}).get("commons_cat") or [None])[0]
    if c.get("curated"):  # filed in the subject's own Commons category: must still name it (or the category)
        mc = m or re.search(req, text, re.I) or (re.search(r"\b" + re.escape(cat) + r"\b", text, re.I) if cat else None)
        if not mc:
            res["reason"] = f"in the category tree but does not name '{topic.get('topic')}'"
            return res
        res.update(ok=True, reason="filed in the subject's own Commons category and named for it", evidence=[mc.group(0)])
        return res
    if not m:
        res["reason"] = f"file title does not name '{topic.get('topic')}'"
        return res
    shared = sorted(narration_terms(topic) & set(_words(text)))
    if not shared:
        res["reason"] = (f"names '{m.group(0)}' but shares no narration term (homonym or unrelated, "
                         f"e.g. a waterfall/chalk/photo that only shares the word)")
        return res
    res.update(ok=True, reason="named for the subject and shares narration terms", evidence=[m.group(0)] + shared[:6])
    return res


def shot_narration(shots: List[dict], captions: List[dict], cuts: List[float], durs: List[float]) -> List[dict]:
    """Attach to each shot the narration text spoken while it is on screen."""
    out = []
    t = 0.0
    for i, s in enumerate(shots):
        start = cuts[i] if i < len(cuts) else t
        end = start + (durs[i] if i < len(durs) else 0.0)
        t = end
        said = [c.get("text") for c in captions or [] if c.get("start", 0) < end and c.get("end", 0) > start]
        out.append(dict(s, shot=i, start_s=round(start, 2), end_s=round(end, 2), narration=said))
    return out


# --------------------------------------------------------------------------- 5. final gate before publishing
def publish_gate(concept: dict, info: dict, now: Optional[datetime] = None, min_shots: int = 3) -> dict:
    """Last check before ANY Autopilot publish. Raises BrandGateSkip with the reason; returns the gate record."""
    now = now or _now()
    bg = (info or {}).get("brand_gate") or {}
    if not bg:
        raise BrandGateSkip(f"brand gate: engine '{(info or {}).get('engine') or (info or {}).get('media_source')}' "
                            f"produced no in-genre 24h viral provenance; skipping, not publishing")
    item = bg.get("viral_item") or {}
    ok, why, _ = check_viral_item(item, now=now, max_age=MAX_AGE_HOURS + PUBLISH_GRACE_HOURS)
    if not ok:
        raise BrandGateSkip(f"brand gate: viral source fails at publish time: {why}")
    clips = bg.get("clips") or []
    bad = [c for c in clips if not c.get("ok")]
    if bad:
        raise BrandGateSkip(f"brand gate: {len(bad)} shot(s) do not match the narration: "
                            + "; ".join(f"{c.get('clip')}: {c.get('reason')}" for c in bad[:4]))
    if len(clips) < min_shots:
        raise BrandGateSkip(f"brand gate: only {len(clips)} checked shots (need {min_shots})")
    if (bg.get("brand") or {}).get("genre") != PINNED_BRAND["genre"]:
        raise BrandGateSkip("brand gate: brand genre missing")
    return dict(bg, publish_checked_at=now.isoformat(), gate_version=GATE_VERSION)
