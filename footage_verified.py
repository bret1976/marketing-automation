"""Footage mode v2 ("verified match"): the footage must show what the voiceover is saying.

Bret 2026-11-17: the viral scan found a topic, but the clips had nothing to do with the narration
(an AI fantasy short called "In Service of the Realm" was keyword-matched to the Realm *database*
Wikipedia page, then cut over a Tekken talk show). This module replaces the keyword pipeline:

  A. subject   - every trend title is resolved by the free Cloudflare text model to the real-world
                 subject it is ABOUT (or "none" for fiction / tutorials / product demos). The proposed
                 Wikipedia page is then verified by a second model call plus an embedding check. Low
                 confidence -> the trend is skipped. No subject qualifies -> no video (never a wrong one).
  B. per line  - the script is written from the Wikipedia text (facts only from it); every line carries
                 the shot it needs and its own search queries. Candidate clips are ranked by an embedding
                 match between the line and the file's title/description, then VERIFIED by a free vision
                 model on frames from the exact segment that would be used (subtitles / text / talk shows /
                 games / off-topic are rejected). A line without a verified clip is dropped; too few lines
                 left -> next topic. Per-line scores land in info["line_matches"].
  C. voice     - one natural voice, never speed-changed, words never clipped, even gaps (footage_tts.py).
  D. edit      - one shot per line, cut in the silence between sentences; captions bottom-anchored in
                 the safe area; credits only on the end card (never under a caption).
All calls are free (Cloudflare Workers AI free plan, public archives). No Gemini, no xAI, no paid API.
When Cloudflare returns 429 (neuron limit) or is unavailable, produce() logs 'ai fallback (cf 429)' and
builds from library or previously verified subjects with a template script and metadata-only clip matches.
A clip is kept only when its own title, description or caption explicitly depicts that line's specific
entity or event (not merely the subject category); otherwise the slot is skipped (never a mismatched video).
"""
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import cf_ai
import footage_mode as fm
import footage_tts

logger = logging.getLogger("footage_verified")
MARKER = "FOOTAGE_VERIFIED"
VERSION = "2026-11-17.4"

# Set only by a dry-run to exercise the no-Cloudflare path; the scheduler never sets this.
_force_fallback = False

SUBJECT_MIN_CONF = float(os.environ.get("FOOTAGE_SUBJECT_MIN_CONF", "0.75"))
SUBJECT_MIN_SIM = float(os.environ.get("FOOTAGE_SUBJECT_MIN_SIM", "0.55"))
CLIP_MIN_MATCH = int(os.environ.get("FOOTAGE_CLIP_MIN_MATCH", "6"))  # vision score 0-10
CLIP_GOOD_MATCH = 8
MAX_CHECKS_PER_LINE = int(os.environ.get("FOOTAGE_MAX_CHECKS_PER_LINE", "4"))
MIN_LINES = int(os.environ.get("FOOTAGE_MIN_LINES", "6"))
FALLBACK_MIN_LINES = int(os.environ.get("FOOTAGE_FALLBACK_MIN_LINES", "5"))
FILE_MENU = 45
MAX_VIDEO_SECONDS = float(os.environ.get("FOOTAGE_MAX_VIDEO_SECONDS", "900"))
LEAD = 0.35
GAP = 0.55          # even pause between sentences (Bret: even gaps, no rushing)
END_CARD = 2.6      # music + credits after the last line; no caption there
BAD_TYPES = {"talk_show", "interview", "video_game", "screen_recording", "animation_cartoon", "text_slide", "news_broadcast"}
GAME_TALK = re.compile(r"\b(tekken|gameplay|let'?s play|walkthrough|speedrun|playthrough|minecraft|fortnite|roblox|"
                       r"podcast|talk ?show|episode|ep\.? ?\d|round \d|stream|vlog|reaction|unboxing|review|tutorial|"
                       r"trailer|teaser|lecture|webinar|interview|panel|keynote|presentation|slides?)\b", re.I)


class _EchoLog(list):
    """A log list that also streams each entry to the service log (Railway logs show progress live)."""

    def append(self, item):  # noqa: D401
        logger.info("%s %s", MARKER, str(item)[:400])
        super().append(item)


# --------------------------------------------------------------------------- A. trend -> verified subject

# Bret 2026-11-17: no religious subjects for the brand (sites, deities, scriptures, religious figures,
# holidays/rituals of living religions). Checked on the Wikipedia page's categories + description (not the
# trend title's words) and again by the verify model. Archaeology of dead religions (Parthenon, pyramids,
# Stonehenge) and secular history (Holy Roman Empire) are not blocked.
RELIGIOUS_CAT = re.compile(
    r"\b(hindu|buddhist|jain|sikh|shinto|taoist|islamic|christian|catholic|orthodox|jewish|bah[aá]'?[ií])\s+"
    r"(gods?|goddess(es)?|deit(y|ies)|temples?|shrines?|holy (places|sites|cities)|pilgrimage\w*|festivals?|holidays?|"
    r"rituals?|scriptures?|texts|saints?|prophets?|clergy|religious leaders?|monaster(y|ies)|mosques?|churches)\b"
    r"|(?<!former )\b(mosques|churches|cathedrals|synagogues|gurdwaras|basilicas)\b"
    r"|\b(deities|gods|goddesses) of\b|\bforms of (vishnu|shiva|krishna|devi|buddha)\b|\bavatars of\b"
    r"|\b(hajj|umrah|religious (texts|scriptures|festivals|holidays|leaders|pilgrimages?)|sacred texts|"
    r"holy (places|sites|cities)|pilgrimage sites|places of worship|religious buildings and structures in (mecca|medina|jerusalem|varanasi|puri)|"
    r"popes|prophets of islam|bible|quran|qur'an|torah|vedas|islamic architecture)\b", re.I)
RELIGIOUS_DESC = re.compile(
    r"\b(deity|god|goddess|avatar|prophet|apostle|saint|pope|imam|guru|bishop|archbishop|scripture|holy book|sacred text|"
    r"religious (text|festival|holiday|ritual|figure|leader|site|building)|place of worship|pilgrimage|mosque|church|"
    r"cathedral|basilica|synagogue|gurdwara|masjid|islam\w*|hindu\w*|christian\w*|buddhis\w*|sikh\w*|jewish holiday|"
    r"(hindu|buddhist|jain|sikh) temple)\b", re.I)


def _wiki_categories(title: str, get=fm._get) -> List[str]:
    """Visible Wikipedia categories of a page ([] on any error)."""
    try:
        data = get(fm.WIKI_API, {"action": "query", "prop": "categories", "clshow": "!hidden", "cllimit": "max",
                                 "redirects": 1, "titles": title, "format": "json"}).json()
        page = next(iter(((data.get("query") or {}).get("pages") or {}).values()), {})
        return [str(c.get("title") or "").split(":", 1)[-1] for c in page.get("categories") or []]
    except Exception:  # noqa: BLE001
        return []


def religious_subject(subj: dict, categories: Optional[List[str]] = None) -> Optional[str]:
    """Return the evidence string if the Wikipedia subject is religious (category/description check), else None."""
    desc = subj.get("description") or ""
    m = RELIGIOUS_DESC.search(desc)
    if m:
        return f"description '{desc[:80]}'"
    for c in categories or []:
        if RELIGIOUS_CAT.search(c):
            return f"category '{c[:80]}'"
    return None


# trend-title pre-filter: name the real reason (the old single label said "live/sport/game/politics" for everything)
_SKIP_REASONS = [
    ("sport/wrestling", r"\bwwe\b|\baew\b|wrestl\w*|\bnba\b|\bnfl\b|\bufc\b"),
    ("livestream", r"\blive\b|\U0001F534|#?shortslive|\bstream(ing)?\b"),
    ("video game", r"\bgam(e|ing|er)\b|\bgta\b|roblox|minecraft|fortnite|free ?fire|pubg|\bbgmi\b"),
    ("music/dance", r"\bmashup\b|\bmusic\b|\bsong\b|\blyrics?\b|\bdance\b"),
    ("tutorial/how-to", r"\btutorial\b|\brecipe\b"),
    ("reaction/prank/vlog", r"\breaction\b|\bprank\b|\bunboxing\b|\basmr\b|\bvlog\b|\bpodcast\b"),
    ("review/trailer", r"\bmovie review\b|\btrailer\b"),
    ("series episode", r"\bepisode\b|\bep \d+|\bs\d+e\d+\b"),
]
_SKIP_REASONS = [(lab, re.compile(rx, re.I)) for lab, rx in _SKIP_REASONS]


def trend_skip_reason(title: str) -> Optional[str]:
    """Why a trend title is skipped before resolving, e.g. "tutorial/how-to: 'tutorial'"; None = resolve it.
    "film"/"movie" alone are NOT a skip in v2: AI short films OF real subjects are the niche, and the resolver
    rejects invented stories ("She Asked How Much Dinner Cost | AI Short Film" -> not real-world)."""
    for lab, rx in _SKIP_REASONS:
        m = rx.search(title or "")
        if m:
            return f"{lab}: '{m.group(0)}'"
    return None


RESOLVE_PROMPT = """A video is trending in the last 24 hours.
Title: "{title}"
{extra}
Question: is this video ABOUT a specific real-world, filmable subject (a real event, place, animal, machine,
spacecraft, natural phenomenon, historical episode) that documentary footage could show?
Most titles come from AI-filmmaking channels. An AI-made film, recreation or dramatization OF a real subject
counts as being about that subject ("TITANIC | AI Short Film made with Veo 3" is about the RMS Titanic;
"Voyager 1: The Loneliest Journey | AI Short Film" is about the Voyager 1 spacecraft).
Invented stories and characters, fantasy worlds, songs, memes, tutorials, product demos, tool reviews,
prompts and "how I made this" videos are NOT about a real-world subject, even if a word in the title is also
the name of something real ("In Service of the Realm | AI Fantasy Short Film" is invented fantasy, NOT the
Realm database; "Wanted | AI Action Short" is not the Bow Wow album).
Return JSON: {{"real_world": true|false, "subject": "<the subject in plain words or empty>",
"wikipedia_title": "<exact English Wikipedia article title or empty>", "confidence": <0.0-1.0>,
"why": "<one short sentence>"}}"""

VERIFY_PROMPT = """Trending video title: "{title}"
Candidate Wikipedia article: "{page}" ({desc})
Article categories: {cats}
Article opening: {lead}

1. Is the trending video about the subject of THIS article (same real-world thing, not just a shared word)?
2. Is the subject RELIGIOUS? Religious = a religious site or place of worship (e.g. the Kaaba, a mosque, church,
   Hindu temple), a deity or god (e.g. Jagannath), a scripture, a religious figure (prophet, saint, pope, guru),
   or a religious holiday, festival, pilgrimage or ritual of a living religion. NOT religious: secular history,
   empires and states, archaeology of ancient civilizations (Parthenon, pyramids, Stonehenge), science, nature.
Return JSON: {{"same_subject": true|false, "confidence": <0.0-1.0>, "religious": true|false,
"why": "<one short sentence>"}}"""


def resolve_trend(item: dict, get=fm._get, log: Optional[List[str]] = None) -> Optional[dict]:
    """Trend item -> verified Wikipedia subject dict (fm._wiki_page shape + 'verify'), or None."""
    log = log if log is not None else []
    title = (item.get("title") or "").strip()
    if not title:
        return None
    extra = ""
    if item.get("description"):
        extra = f'Description: "{str(item["description"])[:300]}"'
    ans = cf_ai.chat_json(RESOLVE_PROMPT.format(title=title[:200], extra=extra), max_tokens=1800)
    if not isinstance(ans, dict):
        log.append(f"subject skip (resolver gave no answer): {title[:80]}")
        return None
    conf = float(ans.get("confidence") or 0)
    if not ans.get("real_world") or not ans.get("wikipedia_title") or conf < SUBJECT_MIN_CONF:
        log.append(f"subject skip (not a real-world subject, conf {conf:.2f}: {str(ans.get('why'))[:90]}): {title[:70]}")
        return None
    try:
        subj = fm._wiki_page(str(ans["wikipedia_title"]), get)
    except Exception as exc:  # noqa: BLE001
        log.append(f"subject skip (wikipedia error {exc}): {title[:60]}"[:200])
        return None
    if not subj:
        log.append(f"subject skip (no such Wikipedia page '{ans['wikipedia_title']}'): {title[:60]}")
        return None
    ok, why = fm.subject_ok(subj)
    if not ok:
        log.append(f"subject skip ({why})")
        return None
    cats = _wiki_categories(subj["title"], get)
    rel = religious_subject(subj, cats)
    if rel:
        log.append(f"subject skip (religious: {rel}): {title[:60]} -> {subj['title']}"[:260])
        return None
    ver = cf_ai.chat_json(VERIFY_PROMPT.format(title=title[:200], page=subj["title"], desc=subj.get("description") or "",
                                               cats="; ".join(cats[:25]) or "(none)",
                                               lead=(subj.get("lead") or "")[:700]), max_tokens=1200)
    if isinstance(ver, dict) and ver.get("religious"):
        log.append(f"subject skip (religious: verify model: {str(ver.get('why'))[:90]}): {title[:60]} -> {subj['title']}"[:260])
        return None
    vconf = float((ver or {}).get("confidence") or 0)
    try:
        e = cf_ai.embed([f"{ans.get('subject')}: {title}", f"{subj['title']}. {subj.get('description')}. {(subj.get('lead') or '')[:400]}"])
        sim = cf_ai.cosine(e[0], e[1]) if len(e) == 2 else 0.0
    except Exception:  # noqa: BLE001
        sim = 0.0
    verify = {"resolver_conf": conf, "verify_conf": vconf, "same_subject": bool((ver or {}).get("same_subject")),
              "embed_sim": round(sim, 3), "why": (ver or {}).get("why") or ans.get("why"), "subject": ans.get("subject")}
    if not verify["same_subject"] or vconf < SUBJECT_MIN_CONF or sim < SUBJECT_MIN_SIM:
        log.append(f"subject skip (verify failed {verify}): {title[:60]} -> {subj['title']}"[:260])
        return None
    subj["verify"] = verify
    log.append(f"subject OK: '{title[:60]}' -> {subj['title']} (resolver {conf:.2f}, verify {vconf:.2f}, sim {sim:.2f})")
    return subj


# --------------------------------------------------------------------------- B1. script with per-line shots

SCRIPT_PROMPT = """Write the voiceover for a 40-50 second documentary Short about "{name}".
FACTS: use ONLY facts stated in the source text below; never add numbers, dates or claims that are not in it.
FOOTAGE: below is the list of real, licensed footage files we have for {name}. Write the script AROUND this
footage: every line must be something one of these files actually shows (judge by the file titles and
descriptions), so the picture always matches the words. Prefer video files (marked VIDEO) over photos.
Rules:
- 9 to 12 lines. Each line is ONE complete spoken sentence, 8 to 16 words, plain and vivid, no jargon.
- Line 1 names the subject and hooks the viewer. The last line is a memorable closing fact.
- No abstract lines (statistics, rankings, software features, legal matters) that no file can show.
- Each line: "file" = the number of the best file for it, "alt" = up to 2 other fitting file numbers,
  "visual" = what should be on screen (concrete nouns), "queries" = 2 short search phrases for more footage.
- Use each file at most twice.
Return JSON: {{"title_line": "<5-8 word title>", "hook": "<2-4 word on-screen hook, caps>",
"lines": [{{"say": "...", "file": 0, "alt": [1, 2], "visual": "...", "queries": ["...", "..."]}}]}}

FOOTAGE FILES:
{files}

SOURCE TEXT:
{text}"""

_NUM = re.compile(r"\d[\d,.]*")


def _grounded(line: str, source: str) -> bool:
    """Every number in the line must appear in the source text (cheap hallucination guard)."""
    src = source.replace(",", "")
    return all(n.replace(",", "").rstrip(".") in src for n in _NUM.findall(line))


def write_script(subj: dict, log: List[str], pool: Optional[List[dict]] = None) -> Optional[dict]:
    name = re.sub(r"\s*\([^)]*\)$", "", subj["title"]).strip()
    short = name if len(name.split()) <= 3 else " ".join(name.split()[:3])
    source = (subj.get("text") or subj.get("lead") or "")[:6000]
    pool = pool or []
    menu = pool[:FILE_MENU]
    files = "\n".join(f"{i}. [{'VIDEO' if c['kind'] == 'video' else 'photo'}] {str(c.get('title'))[:90]} - {str(c.get('description'))[:140]}"
                      for i, c in enumerate(menu)) or "(none listed)"
    data = cf_ai.chat_json(SCRIPT_PROMPT.format(name=name, text=source, files=files), max_tokens=4000, timeout=180)
    lines = []
    for ln in (data or {}).get("lines") or []:
        say = " ".join(str(ln.get("say") or "").split())
        if not say or len(say.split()) < 4 or len(say.split()) > 22:
            continue
        if not _grounded(say, source):
            log.append(f"script drop (number not in source): {say[:80]}")
            continue
        qs = [q for q in (ln.get("queries") or []) if isinstance(q, str) and q.strip()][:3]
        picks = []
        for k in [ln.get("file")] + list(ln.get("alt") or []):
            try:
                k = int(k)
            except (TypeError, ValueError):
                continue
            if 0 <= k < len(menu) and menu[k] not in picks:
                picks.append(menu[k])
        lines.append({"say": fm.spoken(say), "show": say, "parts": [say], "visual": str(ln.get("visual") or say)[:200],
                      "queries": qs or [name], "picks": picks})
    if len(lines) < MIN_LINES:
        log.append(f"script: LLM gave {len(lines)} usable lines for {name}; using the Wikipedia sentence fallback")
        fallback = fm.script_from_text(source, name=name, min_words=60, max_words=120)
        if len(fallback) < MIN_LINES:
            return None
        lines = [dict(l, visual=l["show"], queries=[name], picks=[]) for l in fallback]
        data = {}
    hook = str((data or {}).get("hook") or name).upper()[:24]
    return {"name": name, "short": short, "title_line": str((data or {}).get("title_line") or f"{name}: Real Footage")[:80],
            "hook": hook, "lines": lines}


# --------------------------------------------------------------------------- B2. footage pool + per-line verification

def _space(subj: dict) -> bool:
    return fm.subject_kind(subj) == "space" or bool(re.search(r"nasa|spacecraft|probe|rocket|orbit|planet|moon|apollo|voyager",
                                                                f"{subj['title']} {subj.get('description')}", re.I))


def gather_pool(subj: dict, script: Optional[dict], log: List[str], get=fm._get) -> List[dict]:
    name = script["name"] if script else re.sub(r"\s*\([^)]*\)$", "", subj["title"]).strip()
    space = _space(subj)
    jobs = []
    cat = subj.get("commons_category")
    if cat:
        jobs.append((f"commons cat video {cat}", lambda: [dict(c, curated=True) for c in fm.search_commons(f'incategory:"{cat}"', "video", get, limit=40)]))
        jobs.append((f"commons cat image {cat}", lambda: [dict(c, curated=True) for c in fm.search_commons(f'incategory:"{cat}"', "image", get, limit=40)]))
    queries = [name] + ([q for ln in script["lines"] for q in ln["queries"]] if script else
                        [f"{name} footage", f"{name} history"])
    seenq = set()
    for q in queries:
        k = q.lower().strip()
        if k in seenq:
            continue
        seenq.add(k)
        jobs.append((f"commons video {q}", lambda q=q: fm.search_commons(q, "video", get, limit=12)))
        if space:
            jobs.append((f"nasa video {q}", lambda q=q: fm.search_nasa(q, "video", get, limit=10)))
            jobs.append((f"nasa image {q}", lambda q=q: fm.search_nasa(q, "image", get, limit=8)))
        else:
            jobs.append((f"commons image {q}", lambda q=q: fm.search_commons(q, "image", get, limit=8)))
    jobs.append((f"archive {name}", lambda: fm.search_archive(name, get, limit=8)))
    from concurrent.futures import ThreadPoolExecutor
    pool: List[dict] = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = [(label, ex.submit(fn)) for label, fn in jobs]
        for label, fut in futs:
            try:
                pool += fut.result(timeout=120)
            except Exception as exc:  # noqa: BLE001
                log.append(f"search {label}: {exc}"[:160])
    seen, out = set(), []
    for c in pool:
        key = (c["provider"], c["id"])
        if key in seen:
            continue
        seen.add(key)
        text = f"{c.get('title')} {c.get('description')} {c.get('author')}"
        if fm.JUNK_FOOTAGE.search(text) or GAME_TALK.search(f"{c.get('title')}") or fm.TALKING_HEAD.search(c.get("title") or ""):
            continue
        if c["kind"] == "video" and c.get("duration") and not (3.5 <= float(c["duration"]) <= MAX_VIDEO_SECONDS):
            continue  # too short to cut, or a long lecture/talk (and remote seeks into long webm files take minutes)
        out.append(c)
    log.append(f"footage pool for {name}: {len(out)} candidates ({sum(c['kind'] == 'video' for c in out)} video) from {len(jobs)} searches")
    return out


def _resolve_url(c: dict, get=fm._get) -> Optional[str]:
    if c.get("_src"):
        return c["_src"]
    src = None
    if c["provider"] == "nasa":
        src = fm.resolve_nasa(c, get)
    elif c["provider"] == "internet_archive":
        src = fm.resolve_archive(c, get)
    else:
        src = c.get("url")
    c["_src"] = src
    return src


def _jpeg(src: str, t: float, crop: Optional[str], width: int = 448) -> bytes:
    vf = (f"crop={crop}," if crop else "") + f"scale={width}:-2"
    res = subprocess.run([fm._bin("ffmpeg"), "-v", "error", *fm._ua(src), "-ss", f"{max(0.0, t):.2f}", "-i", src,
                          "-frames:v", "1", "-vf", vf, "-f", "image2", "-c:v", "mjpeg", "-q:v", "4", "-"],
                         capture_output=True, timeout=120)
    return res.stdout or b""


def _pair(a: bytes, b: bytes, workdir: str) -> bytes:
    """Two frames side by side (start | end of the segment) so one vision call sees the whole shot."""
    pa, pb, out = (os.path.join(workdir, f"v_{uuid.uuid4().hex[:6]}_{k}.jpg") for k in "abo")
    open(pa, "wb").write(a)
    open(pb, "wb").write(b)
    subprocess.run([fm._bin("ffmpeg"), "-v", "error", "-y", "-i", pa, "-i", pb, "-filter_complex",
                    "[0:v]scale=448:-2[a];[1:v]scale=448:-2[b];[a][b]hstack=inputs=2", "-q:v", "4", out],
                   capture_output=True, timeout=60)
    data = open(out, "rb").read() if os.path.exists(out) else a
    for p in (pa, pb, out):
        try:
            os.remove(p)
        except OSError:
            pass
    return data


VISION_PROMPT = """You check b-roll for a documentary Short about {subject}.
Narration at this moment: "{say}"
The shot should show: {visual}
The image is {what} from the candidate footage.
Return JSON: {{"description": "<what is actually visible, one sentence>",
"match": <0-10, how well this footage shows what the narration says; 0 = unrelated, 5 = same general topic only, 8+ = clearly shows it>,
"on_topic": <true if it really shows {subject} or something directly part of it>,
"has_text": <true if there are burned-in subtitles, captions, title cards, logos/watermarks or large on-screen text>,
"type": "<one of: footage, photo, illustration, map_diagram, talk_show, interview, video_game, screen_recording, animation_cartoon, text_slide, news_broadcast>"}}"""


def vision_check(jpeg: bytes, subject: str, line: dict, what: str) -> dict:
    ans = cf_ai.vision_json(jpeg, VISION_PROMPT.format(subject=subject, say=line["show"], visual=line.get("visual") or line["show"], what=what))
    ans = ans or {}
    try:
        match = int(round(float(ans.get("match") or 0)))
    except (TypeError, ValueError):
        match = 0
    typ = str(ans.get("type") or "").strip().lower()
    ok = (match >= CLIP_MIN_MATCH and bool(ans.get("on_topic")) and not ans.get("has_text") and typ not in BAD_TYPES
          and not (typ == "map_diagram" and match < CLIP_GOOD_MATCH))
    return {"match": match, "on_topic": bool(ans.get("on_topic")), "has_text": bool(ans.get("has_text")), "type": typ,
            "description": str(ans.get("description") or "")[:200], "model": ans.get("_model"), "ok": ok}


def _probe_video(c: dict, src: str) -> Optional[dict]:
    if "_probe" in c:
        return c["_probe"]
    try:
        meta = fm.probe(src, timeout=60)
    except Exception:  # noqa: BLE001
        meta = None
    if meta and meta.get("duration") and meta.get("width"):
        if (meta["height"] or 0) < 360:
            meta = None
        else:
            meta["crop"] = fm.detect_crop(src, meta["duration"], meta["width"], meta["height"])
    c["_probe"] = meta
    return meta


def check_candidate(c: dict, line: dict, need: float, subject: str, used: Dict[str, List[float]], workdir: str,
                    get=fm._get) -> Optional[dict]:
    """Return a verified placement {cand, start, crop, vision} for this line, or None (with c['_last'] reason)."""
    src = _resolve_url(c, get)
    if not src:
        c["_last"] = "no file url"
        return None
    if c["kind"] == "image":
        if c.get("_img") is None:
            thumb = src
            if c["provider"] == "wikimedia_commons":
                thumb = "https://commons.wikimedia.org/wiki/Special:FilePath/" + requests_quote(c["id"].replace("File:", "")) + "?width=1600"
            try:
                data = fm._get(thumb, timeout=60).content
                c["_img"] = data
            except Exception as exc:  # noqa: BLE001
                c["_last"] = f"image download failed: {exc}"[:120]
                c["_img"] = b""
        if not c["_img"]:
            return None
        small = _shrink(c["_img"], workdir)
        if not small:
            c["_last"] = "unreadable image"
            return None
        if c["id"] in used:
            c["_last"] = "still already used"
            return None
        vis = vision_check(small, subject, line, "a still photo")
        return {"cand": c, "start": 0.0, "crop": None, "vision": vis}
    meta = _probe_video(c, src)
    if not meta:
        c["_last"] = "probe failed / too small"
        return None
    if meta["duration"] > MAX_VIDEO_SECONDS:
        c["_bad"] = True
        c["_last"] = f"too long ({meta['duration']:.0f}s)"
        return None
    segs = fm.choose_segments(src, meta["duration"], 3, need + 0.4, max_probes=5, crop=meta.get("crop"))
    segs = [s for s in segs if all(abs(s[0] - u) > max(6.0, need) for u in used.get(c["id"], []))]
    if not segs:
        c["_last"] = "no clean segment (cuts / black / frozen / already used)"
        return None
    best = None
    for start, _score in segs[:2]:  # vision-check up to two segments of this file
        a = _jpeg(src, start + need * 0.2, meta.get("crop"))
        b = _jpeg(src, start + need * 0.8, meta.get("crop"))
        if not a:
            continue
        vis = vision_check(_pair(a, b or a, workdir), subject, line, "two frames (start | end) of the exact video segment")
        cand = {"cand": c, "start": start, "crop": meta.get("crop"), "vision": vis}
        if best is None or (vis["ok"], vis["match"]) > (best["vision"]["ok"], best["vision"]["match"]):
            best = cand
        if vis["ok"] and vis["match"] >= CLIP_GOOD_MATCH:
            break
    if best is None:
        c["_last"] = "frame grab failed"
    return best


def requests_quote(s: str) -> str:
    import urllib.parse
    return urllib.parse.quote(s.replace(" ", "_"))


def _shrink(data: bytes, workdir: str) -> bytes:
    p = os.path.join(workdir, f"img_{uuid.uuid4().hex[:6]}")
    open(p, "wb").write(data)
    res = subprocess.run([fm._bin("ffmpeg"), "-v", "error", "-i", p, "-frames:v", "1", "-vf", "scale=640:-2",
                          "-f", "image2", "-c:v", "mjpeg", "-q:v", "4", "-"], capture_output=True, timeout=60)
    os.remove(p)
    return res.stdout or b""


def rank_for_line(pool: List[dict], line: dict, vecs: Dict[int, List[float]], line_vec: List[float], subject: str) -> List[Tuple[float, dict]]:
    words = {w for w in re.findall(r"[a-z]{4,}", (line.get("visual", "") + " " + line["show"]).lower())} - fm.GENERIC
    ranked = []
    for i, c in enumerate(pool):
        sim = cf_ai.cosine(vecs[i], line_vec) if i in vecs and line_vec else 0.0
        text = f"{c.get('title')} {c.get('description')}".lower()
        overlap = sum(1 for w in words if w in text)
        score = sim + 0.02 * min(overlap, 5) + (0.03 if c.get("curated") else 0) + (0.03 if c["kind"] == "video" else 0)
        ranked.append((round(score, 4), c))
    ranked.sort(key=lambda r: -r[0])
    return ranked


def match_lines(subj: dict, script: dict, durations: List[float], pool: List[dict], workdir: str, log: List[str]) -> List[Optional[dict]]:
    subject = script["name"]
    texts = [f"{c.get('title')}. {str(c.get('description'))[:300]}" for c in pool]
    lines = script["lines"]
    vecs_list = cf_ai.embed(texts + [f"{ln.get('visual')}. {ln['show']}" for ln in lines]) if pool else []
    vecs = {i: v for i, v in enumerate(vecs_list[:len(pool)])}
    line_vecs = vecs_list[len(pool):]
    used: Dict[str, List[float]] = {}
    uses: Dict[str, int] = {}
    out: List[Optional[dict]] = []
    for li, (line, need) in enumerate(zip(lines, durations)):
        ranked = rank_for_line(pool, line, vecs, line_vecs[li] if li < len(line_vecs) else [], subject)
        picks = line.get("picks") or []
        pick_ids = {id(c) for c in picks}
        ranked = [(1.0, c) for c in picks] + [r for r in ranked if id(r[1]) not in pick_ids]
        tried, best, rejects = 0, None, []
        for meta_score, c in ranked:
            if tried >= MAX_CHECKS_PER_LINE or meta_score < 0.45:
                break
            if uses.get(c["id"], 0) >= 2 or c.get("_bad"):
                continue
            try:
                got = check_candidate(c, line, need, subject, used, workdir)
            except cf_ai.CFQuota:
                raise
            except Exception as exc:  # noqa: BLE001
                c["_last"] = str(exc)[:120]
                got = None
            if got is None:
                continue
            tried += 1
            got["meta_score"] = meta_score
            v = got["vision"]
            if not v["on_topic"] or v["type"] in BAD_TYPES or v["match"] <= 1:
                c["_bad"] = True  # off-subject / talk show / game: never re-check this file for later lines
            if not v["ok"]:
                rejects.append(f"{c.get('title', '')[:50]} -> match {v['match']} type {v['type']} text {v['has_text']} ({v['description'][:60]})")
            elif best is None or v["match"] > best["vision"]["match"]:
                best = got
            if best and best["vision"]["match"] >= CLIP_GOOD_MATCH:
                break
        if best:
            c = best["cand"]
            used.setdefault(c["id"], []).append(best["start"])
            uses[c["id"]] = uses.get(c["id"], 0) + 1
            log.append(f"line {li + 1} MATCH {best['vision']['match']}/10 meta {best['meta_score']:.2f}: '{line['show'][:60]}' <- "
                       f"{c['provider']} '{c.get('title', '')[:60]}' @{best['start']:.1f}s | seen: {best['vision']['description'][:80]}")
        else:
            log.append(f"line {li + 1} NO VERIFIED CLIP (tried {tried}): '{line['show'][:70]}' rejects={rejects[:3]}"[:400])
        out.append(best)
    return out


# --------------------------------------------------------------------------- C/D. timing, localize, render

def timeline(durs: List[float]) -> Tuple[List[dict], List[float], float]:
    """VO starts per line with even gaps; shot boundaries in the middle of each gap (cut on silence)."""
    starts, t = [], LEAD
    for d in durs:
        starts.append(round(t, 3))
        t += d + GAP
    vo_end = starts[-1] + durs[-1]
    total = round(vo_end + END_CARD, 3)
    bounds = [0.0] + [round(starts[i] - GAP / 2, 3) for i in range(1, len(durs))] + [total]
    shot_durs = [round(bounds[i + 1] - bounds[i], 3) for i in range(len(durs))]
    caps = [{"text": None, "start": s, "end": round(s + d + 0.12, 3)} for s, d in zip(starts, durs)]
    return caps, shot_durs, total


def localize_shot(p: dict, dur: float, workdir: str, idx: int) -> dict:
    c = p["cand"]
    if c["kind"] == "image":
        raw = os.path.join(workdir, f"still_src_{idx}")
        open(raw, "wb").write(c["_img"])
        flat = os.path.join(workdir, f"still_flat_{idx}.png")
        fm._run([fm._bin("ffmpeg"), "-y", "-v", "error", "-i", raw, "-vf", "scale='min(3840,iw)':-2,format=rgb24", "-frames:v", "1", flat])
        return {"path": flat, "kind": "image", "start": 0.0, "dur": dur}
    cc = dict(c, crop=p.get("crop"))
    return fm.localize(cc, c["_src"], p["start"], dur, workdir, idx)


def produce(out_dir: str, viral: Optional[dict] = None, avoid_keys: Optional[set] = None,
            recent_topics: Optional[List[str]] = None, workdir: Optional[str] = None,
            name: Optional[str] = None) -> Tuple[str, str, dict, dict]:
    """Same contract as footage_mode.produce_footage: (vertical_mp4, landscape_mp4, concept, info). Raises when no
    topic can be built with verified, matching footage (no video beats a wrong video)."""
    t0 = time.time()
    forced = _force_fallback or (os.environ.get("FOOTAGE_FORCE_FALLBACK") or "").strip().lower() in {"1", "true", "yes"}
    fallback = False
    if forced or not cf_ai.ready():
        fallback = True
        # logged again below once `log` exists
    else:
        cf_ai.reset_usage()
    own_tmp = workdir is None
    workdir = workdir or tempfile.mkdtemp(prefix="footage_v2_")
    os.makedirs(workdir, exist_ok=True)
    log: List[str] = _EchoLog()
    avoid = avoid_keys or set()
    recent = " | ".join(recent_topics or []).lower()
    if fallback:
        if forced and cf_ai.ready():
            log.append("ai fallback (forced)")
            log.append("ai fallback reason: dry-run forced the no-Cloudflare path")
        else:
            log.append("ai fallback (cf 429)")
            if not cf_ai.ready():
                log.append("ai fallback reason: Cloudflare Workers AI is not configured")
    try:
        if viral is None:
            viral = fm.todays_viral()
        items = sorted((viral or {}).get("top") or [], key=lambda it: -float(it.get("views_per_hour") or it.get("score") or 0))
        import open_viral
        pool_mode = (os.environ.get("FOOTAGE_TOPIC_POOL") or "genre").lower()
        max_resolve = int(os.environ.get("FOOTAGE_MAX_RESOLVE", "12"))
        attempts = int(os.environ.get("FOOTAGE_TOPIC_ATTEMPTS", "3"))
        tried_subjects, resolved = set(), 0
        built, shortlist = None, []
        if not fallback:
            for it in items:
                if resolved >= max_resolve:
                    break
                title = it.get("title") or ""
                if pool_mode == "genre" and not open_viral.NICHE_RE.search(title):
                    log.append(f"trend skip (off-genre, not AI filmmaking): {title[:70]}")
                    continue
                why = trend_skip_reason(title)
                if why:
                    log.append(f"trend skip ({why}): {title[:70]}")
                    continue
                resolved += 1
                try:
                    subj = resolve_trend(it, log=log)
                except cf_ai.CFQuota as exc:
                    log.append("ai fallback (cf 429)")
                    log.append(f"ai fallback reason: {exc}"[:240])
                    fallback = True
                    break
                if not subj:
                    continue
                key = "v2-" + re.sub(r"[^a-z0-9]+", "-", subj["title"].lower()).strip("-")[:48]
                if subj["title"] in tried_subjects or key in avoid or subj["title"].lower() in recent:
                    log.append(f"subject skip (already tried / recently posted): {subj['title']}")
                    continue
                tried_subjects.add(subj["title"])
                supply = footage_supply(subj)
                if supply < MIN_SUPPLY:
                    log.append(f"subject skip (only {supply} licensed videos of {subj['title']}; need {MIN_SUPPLY}): prefer subjects with real footage")
                    continue
                log.append(f"footage supply for {subj['title']}: {supply} licensed videos")
                shortlist.append((subj, it, key, supply))
                if len(shortlist) >= SHORTLIST:
                    break
        # most real footage first (weighted by how viral the trend is): picture-rich subjects match best
        import math
        shortlist.sort(key=lambda r: -(min(r[3], 60) * (1 + 0.25 * math.log10(1 + float(r[1].get("views_per_hour") or 0)))))
        if not fallback:
            for subj, it, key, supply in shortlist[:attempts]:
                try:
                    built = build_topic(subj, it, key, workdir, log)
                except cf_ai.CFQuota as exc:
                    log.append("ai fallback (cf 429)")
                    log.append(f"ai fallback reason: {exc}"[:240])
                    fallback = True
                    built = None
                    break
                except Exception as exc:  # noqa: BLE001
                    log.append(f"topic {subj['title']} failed: {exc}"[:240])
                    built = None
                if built:
                    break
        if not built and fallback:
            built = run_fallback(avoid, recent, workdir, log)
        if not built:
            try:
                with open(os.path.join(out_dir, "footage_v2_last_log.json"), "w") as fh:
                    json.dump({"at": datetime.now(timezone.utc).isoformat(), "log": log, "cf_ai_calls": dict(cf_ai.USAGE)}, fh, indent=1)
            except OSError:
                pass
            if fallback:
                raise RuntimeError("ai fallback (cf 429): no library or previously verified subject has guaranteed "
                                   f"matching footage; skipping slot. log tail={log[-12:]}")
            raise RuntimeError("no trending subject today has verified, matching footage; not rendering a wrong video. "
                               f"log tail={log[-12:]}")
        return finish(built, out_dir, workdir, log, name, t0)
    finally:
        if own_tmp:
            shutil.rmtree(workdir, ignore_errors=True)


MIN_SUPPLY = int(os.environ.get("FOOTAGE_MIN_SUPPLY", "4"))
SHORTLIST = int(os.environ.get("FOOTAGE_SHORTLIST", "3"))


def footage_supply(subj: dict, get=fm._get) -> int:
    """Quick count of licensed VIDEO files for the subject (its Commons category, a name search, NASA for space)."""
    name = re.sub(r"\s*\([^)]*\)$", "", subj["title"]).strip()
    seen = set()
    searches = [lambda: fm.search_commons(name, "video", get, limit=20)]
    if subj.get("commons_category"):
        searches.append(lambda: fm.search_commons(f'deepcat:"{subj["commons_category"]}"', "video", get, limit=30))
    if _space(subj):
        searches.append(lambda: fm.search_nasa(name, "video", get, limit=20))
    for fn in searches:
        try:
            for c in fn():
                text = f"{c.get('title')} {c.get('description')}"
                if not fm.JUNK_FOOTAGE.search(text) and not GAME_TALK.search(c.get("title") or ""):
                    seen.add((c["provider"], c["id"]))
        except Exception:  # noqa: BLE001
            continue
    return len(seen)


def build_topic(subj: dict, item: dict, key: str, workdir: str, log: List[str]) -> Optional[dict]:
    # footage first: the script is written around the licensed files we actually have
    pool = gather_pool(subj, None, log)
    if len(pool) < 4:
        log.append(f"topic {subj['title']}: only {len(pool)} licensed candidates")
        return None
    pool.sort(key=lambda c: (c["kind"] != "video", not c.get("curated")))
    script = write_script(subj, log, pool)
    if not script:
        log.append(f"topic {subj['title']}: too little filmable material for a script")
        return None
    more = gather_pool(subj, script, log)  # plus each line's own search queries
    ids = {(c["provider"], c["id"]) for c in pool}
    pool += [c for c in more if (c["provider"], c["id"]) not in ids]
    # voice next: shot lengths come from the real line durations
    vo = footage_tts.synth_lines([ln["say"] for ln in script["lines"]], workdir)
    durs = vo["durations"]
    _, shot_durs, _ = timeline(durs)
    matches = match_lines(subj, script, [d + 0.2 for d in shot_durs], pool, workdir, log)
    keep = [i for i, m in enumerate(matches) if m]
    if len(keep) < MIN_LINES:
        log.append(f"topic {subj['title']}: only {len(keep)}/{len(matches)} lines have verified footage (need {MIN_LINES})")
        return None
    lines = [script["lines"][i] for i in keep]
    picks = [matches[i] for i in keep]
    clips = [vo["clips"][i] for i in keep]
    durs = [durs[i] for i in keep]
    caps, shot_durs, total = timeline(durs)
    if total < fm.MIN_SECONDS:
        log.append(f"topic {subj['title']}: verified lines only fill {total:.1f}s (< {fm.MIN_SECONDS:.0f}s)")
        return None
    if total > fm.MAX_SECONDS:  # drop trailing lines, never speed up
        while total > fm.MAX_SECONDS and len(lines) > MIN_LINES:
            lines, picks, clips, durs = lines[:-1], picks[:-1], clips[:-1], durs[:-1]
            caps, shot_durs, total = timeline(durs)
    for c, ln in zip(caps, lines):
        c["text"] = ln["show"]
    return {"subj": subj, "item": item, "key": key, "script": script, "lines": lines, "picks": picks, "clips": clips,
            "durs": durs, "caps": caps, "shot_durs": shot_durs, "total": total, "vo": vo}


def finish(b: dict, out_dir: str, workdir: str, log: List[str], name: Optional[str], t0: float) -> Tuple[str, str, dict, dict]:
    vo_wav = footage_tts.assemble(b["clips"], [c["start"] for c in b["caps"]], b["total"], b["vo"]["sr"], os.path.join(workdir, "vo.wav"))
    shots, sources = [], []
    for i, (p, d) in enumerate(zip(b["picks"], b["shot_durs"])):
        loc = localize_shot(p, d, workdir, i)
        loc["dur"] = d
        loc["focus"] = round(fm.focus_x(loc["path"], loc["kind"]), 3)
        shots.append(loc)
        c = p["cand"]
        sources.append({k: c.get(k) for k in ("provider", "kind", "title", "page", "author", "license", "license_url", "credit_short")}
                       | {"segment_start_s": round(p["start"], 2), "segment_seconds": d, "line": b["lines"][i]["show"],
                          "vision": p["vision"], "meta_score": p.get("meta_score")})
    music = fm.load_music("cinematic" if _space(b["subj"]) else "reveal")
    credits = sorted({s["credit_short"] for s in sources if s.get("credit_short")})
    credit = "Footage: " + "; ".join(credits)
    name = name or f"g2_footage_{uuid.uuid4().hex}"
    vertical = os.path.join(out_dir, f"{name}.mp4")
    landscape = os.path.join(out_dir, f"{name}_16x9.mp4")
    fm.ensure_render_space(out_dir, log)
    hook = b["script"]["hook"]
    win = (b["total"] - END_CARD + 0.15, b["total"])
    pv = fm.render(shots, vo_wav, b["caps"], music, hook, credit, vertical, "vertical", workdir, credit_window=win, layout="v2")
    pl = fm.render(shots, vo_wav, b["caps"], music, hook, credit, landscape, "landscape", workdir, credit_window=win, layout="v2")
    for label, meta in (("9:16", pv), ("16:9", pl)):
        if not fm.length_ok(meta.get("duration")):
            raise RuntimeError(f"{label} render is {meta.get('duration')}s, outside {fm.MIN_SECONDS:.0f}-{fm.MAX_SECONDS:.0f}s")
    subj, item, script = b["subj"], b["item"], b["script"]
    tags, question = fm.SUBJECT_STYLE[fm.subject_kind(subj)]
    tag = "#" + re.sub(r"[^A-Za-z0-9]", "", script["name"].title())[:28]
    line_matches = [{"line": s["line"], "clip": s["title"], "provider": s["provider"], "page": s["page"],
                     "segment_start_s": s["segment_start_s"], "vision_match": s["vision"]["match"],
                     "vision_seen": s["vision"]["description"], "type": s["vision"]["type"], "meta_score": s["meta_score"]}
                    for s in sources]
    concept = {
        "topic": script["name"], "topic_key": b["key"].replace("-", " "), "title_line": script["title_line"],
        "on_screen_hook": hook, "hook": b["lines"][0]["show"], "lines": [f"Real footage: {script['name']}.", credit],
        "hashtags": [tag] + tags, "linkedin_angle": "Real archival footage, every shot checked against the narration. No AI generation.",
        "comment_question": question, "vo": b["lines"],
        "concept_source": ("footage_v2 fallback (no Cloudflare, metadata-matched clips)" if b.get("fallback")
                           else "footage_v2 (Cloudflare Workers AI, free)"),
        "footage_credit": credit, "topic_source": ("library_fallback" if b.get("fallback") else "viral_scan"),
        "wikipedia": subj["url"], "text_credit": fm.TEXT_CREDIT,
        "viral_match": {"score": item.get("views_per_hour"), "reason": "verified_subject",
                        "matched": [{"title": (item.get("title") or "")[:120], "url": item.get("url"), "views": item.get("views"),
                                     "views_per_hour": item.get("views_per_hour")}]},
    }
    info = {
        "engine": "footage", "footage_mode": f"{fm.VERSION}+v2 {VERSION}", "media_source": "archival_footage", "format": "footage_short",
        "duration_s": round(float(pv["duration"]), 2), "length_rule_s": [fm.MIN_SECONDS, fm.MAX_SECONDS],
        "cost_usd": 0.0, "cost": {"cap": 0.0, "total": 0.0, "items": []}, "concept_source": concept["concept_source"],
        "topic_key": b["key"],
        "topic_source": "library_fallback" if b.get("fallback") else "viral_scan",
        "topic_reason": "metadata_guaranteed_fallback" if b.get("fallback") else "verified_subject",
        "ai_fallback": bool(b.get("fallback")),
        "subject_verify": subj.get("verify"),
        "wikipedia": subj["url"], "viral_match": concept["viral_match"], "footage_sources": sources, "line_matches": line_matches,
        "music": {k: music[k] for k in ("title", "author", "source", "license", "license_url", "bpm")},
        "voiceover": b["vo"]["info"], "captions": b["caps"], "cuts_s": [round(sum(b["shot_durs"][:i + 1]), 3) for i in range(len(b["shot_durs"]) - 1)],
        "vertical": pv, "landscape": pl, "landscape_path": landscape, "cf_ai_calls": dict(cf_ai.USAGE),
        "log": log[-60:], "render_seconds": round(time.time() - t0, 1), "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(os.path.join(out_dir, f"{name}.sources.json"), "w") as fh:
        json.dump({"concept": concept, "info": info}, fh, indent=1, default=str)
    remember_verified(subj, script, sources, fallback=bool(b.get("fallback")))
    logger.info("%s ready %s subject=%s lines=%s fallback=%s", MARKER, os.path.basename(vertical),
                script["name"], len(b["lines"]), bool(b.get("fallback")))
    return vertical, landscape, concept, info


# --------------------------------------------------------------------------- C. no-Cloudflare fallback (429 / unavailable)

_META_STOP = {
    "the", "a", "an", "of", "and", "in", "on", "at", "to", "for", "from", "with", "by", "this", "that",
    "was", "were", "are", "is", "its", "had", "has", "have", "been", "into", "over", "after", "before",
    "during", "through", "between", "about", "which", "when", "where", "what", "than", "then", "also",
    "used", "use", "using", "called", "known", "their", "they", "them", "his", "her", "she", "him",
    "who", "whom", "whose", "not", "but", "or", "as", "if", "so", "all", "any", "can", "may", "will",
    "would", "could", "should", "did", "does", "there", "here", "such", "these", "those", "each",
    "other", "more", "most", "some", "very", "just", "only", "both", "same", "own", "under", "above",
    "out", "down", "off", "again", "real", "footage",
}

# Words that only place a file in the same subject/category. They never prove a line's specific claim.
_GENERIC_SUBJECT = {
    "footage", "photograph", "photographer", "photo", "image", "film", "video", "still", "official",
    "archive", "archival", "commons", "wikimedia", "nasa", "public", "domain", "credit", "caption",
    "war", "world", "wartime", "wars", "military", "army", "navy", "forces", "force", "troops", "troop",
    "soldiers", "soldier", "men", "man", "allied", "allies", "enemy", "battle", "campaign", "operation",
    "invasion", "landing", "landings", "front", "coast", "beach", "beaches", "shore", "aboard",
    "second", "first", "history", "historical", "century", "american", "british", "french", "german",
    "nazi", "soviet", "russian", "chinese", "japanese", "national", "united", "states", "royal",
    "center", "centre", "university", "museum", "building", "portrait", "picture", "view", "near",
    "several", "many", "early", "later", "great", "south", "north", "east", "west", "northern",
    "southern", "eastern", "western",
}

_PLACE_WORDS = {
    "center", "centre", "peninsula", "beach", "island", "islands", "river", "ocean", "sea", "gulf",
    "bay", "mount", "mountain", "mountains", "valley", "desert", "forest", "park", "bridge", "harbour",
    "harbor", "port", "city", "town", "village", "county", "province", "district", "republic",
    "kingdom", "empire", "university", "museum", "church", "cathedral", "palace", "street", "road",
    "avenue", "station", "stations", "airport", "base", "camp", "field", "space",
}

_ORG_WORDS = {
    "force", "forces", "headquarters", "command", "army", "navy", "corps", "division", "regiment",
    "battalion", "fleet", "squadron", "committee", "commission", "council", "department", "office",
    "agency", "administration", "conference", "treaty", "alliance",
}

_PERSON_TITLES = {
    "president", "vice", "prime", "minister", "general", "major", "brigadier", "admiral", "colonel",
    "captain", "lieutenant", "commander", "sergeant", "doctor", "sir", "lady", "emperor", "empress",
    "king", "queen", "pope", "secretary", "chancellor", "chairman", "governor", "senator", "mayor",
    "professor", "judge", "justice", "tsar", "czar", "pharaoh", "premier",
}

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
    "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9,
    "sep": 9, "sept": 9, "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

_NAME_ALIASES = {
    "fdr": ("roosevelt", "franklin roosevelt", "franklin d roosevelt"),
    "roosevelt": ("fdr", "franklin roosevelt", "franklin d roosevelt"),
    "jfk": ("kennedy", "john kennedy", "john f kennedy"),
    "ike": ("eisenhower",),
    "eisenhower": ("ike",),
    "lbj": ("johnson", "lyndon johnson"),
    "mlk": ("king", "martin luther king"),
}


def _meta_words(text: str) -> List[str]:
    return [w for w in re.findall(r"[a-z0-9]{4,}", (text or "").lower()) if w not in _META_STOP]


def _clip_text(c: dict) -> str:
    return " ".join(
        str(c.get(k) or "") for k in ("title", "description", "caption", "object_name", "ImageDescription")
    )


def subject_name(subj: dict) -> str:
    return re.sub(r"\s*\([^)]*\)$", "", subj.get("title") or "").strip()


def subject_generic_words(subj: dict, extra: Optional[set] = None) -> set:
    """Tokens that only identify the subject or its category, plus a global generic list."""
    chunks = [
        subject_name(subj),
        subj.get("commons_category") or "",
        (subj.get("description") or "")[:280],
        (subj.get("lead") or "")[:280],
    ]
    words = set(_GENERIC_SUBJECT)
    for chunk in chunks:
        words.update(_meta_words(chunk))
        words.update(re.findall(r"[a-z0-9]{3,}", (chunk or "").lower()))
    if extra:
        words.update(extra)
    words -= {w for w in words if w in _MONTHS and w not in _GENERIC_SUBJECT}
    return words


def pool_generic_words(pool: List[dict], subj: dict, threshold: float = 0.3) -> set:
    texts = [_clip_text(c) for c in pool if clip_is_of_subject(c, subj)]
    if len(texts) < 4:
        return set()
    freq: Dict[str, int] = {}
    for text in texts:
        for w in set(_meta_words(text)):
            freq[w] = freq.get(w, 0) + 1
    n = len(texts)
    return {w for w, c in freq.items() if c / n >= threshold}


def _dates(text: str) -> List[tuple]:
    """Calendar dates and clock times found in text, as ('d', y, m, d) or ('t', h, m)."""
    raw = text or ""
    found: List[tuple] = []
    month = r"(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"
    for m in re.finditer(rf"\b(\d{{1,2}})\s+{month}\s+(\d{{4}})\b", raw, re.I):
        found.append(("d", int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1))))
    for m in re.finditer(rf"\b{month}\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", raw, re.I):
        found.append(("d", int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2))))
    for m in re.finditer(rf"\b{month}\s+(\d{{4}})\b", raw, re.I):
        found.append(("d", int(m.group(2)), _MONTHS[m.group(1).lower()], 0))
    for m in re.finditer(r"\b(\d{1,2}):(\d{2})\b", raw):
        found.append(("t", int(m.group(1)), int(m.group(2))))
    return found


def _date_in_text(token: tuple, text: str) -> bool:
    have = _dates(text)
    if token[0] == "t":
        return any(h[0] == "t" and h[1] == token[1] and h[2] == token[2] for h in have)
    y, mo, d = token[1], token[2], token[3]
    for h in have:
        if h[0] != "d" or h[1] != y or h[2] != mo:
            continue
        if d == 0 or h[3] == 0 or h[3] == d:
            return True
    # month name + year written without being parsed as a full date pattern already
    if y and mo:
        month_name = max((n for n, num in _MONTHS.items() if num == mo), key=len)
        if re.search(rf"\b{month_name}\b", text, re.I) and re.search(rf"\b{y}\b", text):
            if d == 0 or re.search(rf"\b{d}\b", text):
                return True
    return False


def _person_names(text: str) -> List[str]:
    """Person-like 'First (M.) Last' phrases, excluding places and organizations."""
    titles = (
        r"(?:(?:US|U\.S\.)\s+)?(?:President|Vice President|Prime Minister|General|"
        r"Major General|Brigadier(?:\s+General)?|Admiral|Colonel|Captain|Lieutenant|"
        r"Commander|Dr\.?|Sir|Lady|Emperor|Empress|King|Queen|Pope|Secretary|"
        r"Chancellor|Chairman|Governor|Senator|Mayor|Professor|Judge|Justice)\s+"
    )
    # Middle initial is optional and the period is optional: Franklin D Roosevelt.
    pat = re.compile(
        rf"\b(?:{titles})?([A-Z][a-z]+(?:\s+[A-Z]\.?)*(?:\s+[A-Z][a-z]+)+)\b"
    )
    names = []
    for m in pat.finditer(text or ""):
        name = re.sub(r"\s+", " ", m.group(1)).strip()
        toks = [w.lower().rstrip(".") for w in name.split()]
        if toks and toks[0] in {"the", "a", "an", "this", "that", "these", "those"}:
            continue
        if any(w in _PLACE_WORDS or w in _ORG_WORDS for w in toks):
            continue
        if all(w in _PERSON_TITLES or w in _META_STOP or w in _GENERIC_SUBJECT or len(w) <= 1 for w in toks):
            continue
        names.append(name)
    return names


def _name_in_text(name: str, text: str) -> bool:
    blob = " " + re.sub(r"[^a-z0-9]+", " ", (text or "").lower()) + " "
    toks = [w.lower().rstrip(".") for w in name.split() if w.lower().rstrip(".") not in _PERSON_TITLES]
    if not toks:
        return False
    phrase = " ".join(toks)
    if phrase and phrase in blob:
        return True
    compact = "".join(toks)
    if len(toks) >= 2 and len(compact) >= 3 and compact in blob.replace(" ", ""):
        return True  # fdr, jfk
    last = toks[-1]
    if len(last) >= 5 and last not in _GENERIC_SUBJECT and re.search(rf"\b{re.escape(last)}\b", blob):
        # last name only when the first name is absent is still a mention of that person
        return True
    for tok in toks:
        for alias in _NAME_ALIASES.get(tok, ()):
            if re.search(rf"\b{re.escape(alias)}\b", blob):
                return True
    return False


def line_specific_terms(line: dict, subj: dict, extra_generic: Optional[set] = None) -> dict:
    """Entities and dates that belong to this line, with generic subject words removed."""
    show = line.get("show") or line.get("say") or ""
    generic = subject_generic_words(subj, extra_generic)
    people = []
    for name in _person_names(show):
        if not set(_meta_words(name)) - generic or _name_in_text(name, subject_name(subj)):
            continue
        people.append(name)
    subj_dates = set(_dates(subject_name(subj) + " " + (subj.get("lead") or "")[:400] + " " + (subj.get("description") or "")[:200]))
    dates = [d for d in _dates(show) if d not in subj_dates]
    words = []
    for w in _meta_words(show):
        if w in generic or w in _META_STOP or w in _MONTHS:
            continue
        if any(w in n.lower() for n in people):
            continue
        words.append(w)
    return {"people": people, "dates": dates, "words": words}


def clip_is_of_subject(c: dict, subj: dict) -> bool:
    """True when metadata alone ties the file to this subject (Commons category or subject name)."""
    name = subject_name(subj)
    text = _clip_text(c).lower()
    name_in = bool(name) and bool(re.search(r"\b" + re.escape(name.lower()) + r"\b", text))
    if not name_in:
        toks = [w for w in _meta_words(name) if len(w) >= 4]
        name_in = bool(toks) and all(w in text for w in toks)
    curated = bool(c.get("curated") and subj.get("commons_category"))
    nasa = c.get("provider") == "nasa" and _space(subj)
    return bool((curated or nasa) and (name_in or curated))


def meta_line_match(c: dict, subj: dict, line: dict, extra_generic: Optional[set] = None) -> dict:
    """A clip may be used only when its own title/description/caption depicts this line's specific entity or event."""
    text = _clip_text(c)
    if not clip_is_of_subject(c, subj):
        return {"ok": False, "reason": "clip is not provenance-tied to the subject", "score": 0.0, "overlap": []}
    terms = line_specific_terms(line, subj, extra_generic)
    missing_people = [n for n in terms["people"] if not _name_in_text(n, text)]
    if missing_people:
        return {"ok": False, "reason": "missing line-specific name: " + ", ".join(missing_people),
                "score": 0.0, "overlap": []}
    missing_dates = [d for d in terms["dates"] if not _date_in_text(d, text)]
    if missing_dates:
        return {"ok": False, "reason": "missing line-specific date", "score": 0.0, "overlap": []}
    word_hits = [w for w in terms["words"] if re.search(r"\b" + re.escape(w) + r"\b", text.lower())]
    person_hits = [n for n in terms["people"] if _name_in_text(n, text)]
    date_hits = [d for d in terms["dates"] if _date_in_text(d, text)]
    # Subject-category overlap alone is not enough: need the line's own entities.
    if person_hits or date_hits:
        if word_hits or person_hits:
            overlap = [n.lower() for n in person_hits] + word_hits
            score = 0.9 + 0.02 * min(len(person_hits) + len(date_hits) + len(word_hits), 5)
            return {"ok": True, "reason": "line-specific entity in title/description",
                    "score": round(score, 3), "overlap": overlap}
        return {"ok": False, "reason": "name/date present but no line-specific detail",
                "score": 0.2, "overlap": [n.lower() for n in person_hits]}
    if len(word_hits) >= 2:
        return {"ok": True, "reason": "line-specific terms in title/description",
                "score": round(0.8 + 0.04 * min(len(word_hits), 5), 3), "overlap": word_hits}
    if len(word_hits) == 1 and (len(word_hits[0]) >= 6 or word_hits[0] not in _GENERIC_SUBJECT):
        # A single highly specific noun (radar, splashdown, heliopause) can carry the line.
        if len(word_hits[0]) >= 6:
            return {"ok": True, "reason": "line-specific term in title/description",
                    "score": 0.76, "overlap": word_hits}
    reason_bits = terms["words"][:6] or terms["people"] or ["(none)"]
    return {"ok": False, "reason": "no line-specific terms in title/description (need %s)" % ", ".join(map(str, reason_bits)),
            "score": round(0.08 * len(word_hits), 3), "overlap": word_hits}


def write_script_from_footage(subj: dict, pool: List[dict], log: List[str]) -> Optional[dict]:
    """Pick Wikipedia sentences that describe what the available images or clips actually show."""
    name = subject_name(subj)
    source = (subj.get("text") or subj.get("lead") or "")
    try:
        raw_sents = fm._sentences(source)
    except Exception:  # noqa: BLE001
        raw_sents = re.split(r"(?<=[.!?])\s+", source)
    provenanced = [c for c in pool if clip_is_of_subject(c, subj)]
    # Prefer real video over stills when both can be guaranteed.
    provenanced.sort(key=lambda c: (c.get("kind") != "video", not c.get("curated")))
    extra_generic = pool_generic_words(provenanced, subj)
    lines: List[dict] = []
    used, said = set(), set()
    for c in provenanced:
        best = None
        for raw in raw_sents:
            try:
                tightened = fm.tighten(raw) if hasattr(fm, "tighten") else raw
            except Exception:  # noqa: BLE001
                tightened = raw
            say = " ".join(fm.spoken(tightened).split())
            if not (4 <= len(say.split()) <= 22):
                continue
            if not _grounded(say, source):
                continue
            rec = {"say": say, "show": say if not hasattr(fm, "tighten") else tightened, "parts": [say],
                   "visual": say, "queries": [name], "picks": [c]}
            rec["show"] = tightened if isinstance(tightened, str) else say
            rec["say"] = fm.spoken(rec["show"])
            m = meta_line_match(c, subj, rec, extra_generic)
            if not m["ok"]:
                continue
            score = m["score"] + (0.05 if c.get("kind") == "video" else 0.0)
            if best is None or score > best[0]:
                best = (score, m, rec, c)
        if not best:
            continue
        key = (c.get("provider"), c.get("id"))
        say_key = best[2]["say"].lower()
        if key in used or say_key in said:
            continue
        if not lines:
            first = best[2]["show"].lower()
            if name.lower() not in first and not any(w in first for w in _meta_words(name)):
                continue
        rec = best[2]
        rec["picks"] = [c]
        rec["match_reason"] = best[1].get("reason")
        lines.append(rec)
        used.add(key)
        said.add(say_key)
        if len(lines) >= 12:
            break
    if len(lines) < FALLBACK_MIN_LINES:
        log.append(f"ai fallback script: only {len(lines)} guaranteed lines for {name}")
        return None
    short = name if len(name.split()) <= 3 else " ".join(name.split()[:3])
    return {"name": name, "short": short, "title_line": f"{name}: Real Footage"[:80],
            "hook": name.upper()[:24], "lines": lines}


def place_by_meta(c: dict, need: float, used: Dict[str, List[float]], workdir: str,
                  get=fm._get) -> Optional[dict]:
    """Pick a clean segment of a metadata-guaranteed clip. No vision call."""
    src = _resolve_url(c, get)
    if not src and c["kind"] != "image":
        c["_last"] = "no file url"
        return None
    if c["kind"] == "image":
        if c.get("id") in used:
            c["_last"] = "still already used"
            return None
        if c.get("_img") is None:
            thumb = src
            if c["provider"] == "wikimedia_commons":
                thumb = "https://commons.wikimedia.org/wiki/Special:FilePath/" + requests_quote(c["id"].replace("File:", "")) + "?width=1600"
            if not thumb:
                c["_last"] = "no file url"
                return None
            try:
                data = fm._get(thumb, timeout=60).content
                c["_img"] = data or b""
            except Exception as exc:  # noqa: BLE001
                c["_last"] = f"image download failed: {exc}"[:120]
                c["_img"] = b""
        if not c.get("_img"):
            return None
        vis = {"ok": True, "match": None, "on_topic": True, "has_text": False, "type": "photo",
               "description": "accepted by metadata guarantee (no vision)", "method": "metadata"}
        return {"cand": c, "start": 0.0, "crop": None, "vision": vis}
    meta = _probe_video(c, src)
    if not meta:
        c["_last"] = "probe failed / too small"
        return None
    if meta["duration"] > MAX_VIDEO_SECONDS:
        c["_bad"] = True
        c["_last"] = f"too long ({meta['duration']:.0f}s)"
        return None
    segs = fm.choose_segments(src, meta["duration"], 3, need + 0.4, max_probes=5, crop=meta.get("crop"))
    segs = [s for s in segs if all(abs(s[0] - u) > max(6.0, need) for u in used.get(c["id"], []))]
    if not segs:
        c["_last"] = "no clean segment (cuts / black / frozen / already used)"
        return None
    start = segs[0][0]
    vis = {"ok": True, "match": None, "on_topic": True, "has_text": False, "type": "footage",
           "description": "accepted by metadata guarantee (no vision)", "method": "metadata"}
    return {"cand": c, "start": start, "crop": meta.get("crop"), "vision": vis}


def match_lines_meta(subj: dict, script: dict, durations: List[float], pool: List[dict],
                     workdir: str, log: List[str]) -> List[Optional[dict]]:
    used: Dict[str, List[float]] = {}
    uses: Dict[str, int] = {}
    out: List[Optional[dict]] = []
    extra_generic = pool_generic_words(pool, subj)
    for line, need in zip(script["lines"], durations):
        ranked = []
        seen = set()
        for c in (line.get("picks") or []) + pool:
            ident = (c.get("provider"), c.get("id"))
            if ident in seen:
                continue
            seen.add(ident)
            m = meta_line_match(c, subj, line, extra_generic)
            if m["ok"]:
                ranked.append((m["score"], c, m))
        ranked.sort(key=lambda r: (r[1].get("kind") != "video", -r[0]))
        placed = None
        for score, c, m in ranked:
            if uses.get(c["id"], 0) >= 2 or c.get("_bad"):
                continue
            got = place_by_meta(c, need, used, workdir)
            if not got:
                continue
            got["meta_score"] = score
            got["vision"] = {**got["vision"], "reason": m["reason"], "overlap": m.get("overlap")}
            uses[c["id"]] = uses.get(c["id"], 0) + 1
            used.setdefault(c["id"], []).append(got["start"])
            placed = got
            break
        if placed is None:
            log.append(f"ai fallback: no guaranteed clip for line: {(line.get('show') or '')[:80]}")
        out.append(placed)
    return out


def build_fallback_topic(subj: dict, item: dict, key: str, workdir: str, log: List[str]) -> Optional[dict]:
    pool = gather_pool(subj, None, log)
    if len(pool) < 4:
        log.append(f"ai fallback {subj['title']}: only {len(pool)} licensed candidates")
        return None
    pool.sort(key=lambda c: (c["kind"] != "video", not c.get("curated")))
    script = write_script_from_footage(subj, pool, log)
    if not script:
        return None
    more = gather_pool(subj, script, log)
    ids = {(c["provider"], c["id"]) for c in pool}
    pool += [c for c in more if (c["provider"], c["id"]) not in ids]
    vo = footage_tts.synth_lines([ln["say"] for ln in script["lines"]], workdir)
    durs = vo["durations"]
    _, shot_durs, _ = timeline(durs)
    matches = match_lines_meta(subj, script, [d + 0.2 for d in shot_durs], pool, workdir, log)
    keep = [i for i, m in enumerate(matches) if m]
    if len(keep) < FALLBACK_MIN_LINES:
        log.append(f"ai fallback {subj['title']}: only {len(keep)}/{len(matches)} lines have guaranteed footage (need {FALLBACK_MIN_LINES})")
        return None
    lines = [script["lines"][i] for i in keep]
    picks = [matches[i] for i in keep]
    clips = [vo["clips"][i] for i in keep]
    durs = [durs[i] for i in keep]
    caps, shot_durs, total = timeline(durs)
    if total < fm.MIN_SECONDS:
        log.append(f"ai fallback {subj['title']}: guaranteed lines only fill {total:.1f}s (< {fm.MIN_SECONDS:.0f}s)")
        return None
    if total > fm.MAX_SECONDS:
        while total > fm.MAX_SECONDS and len(lines) > FALLBACK_MIN_LINES:
            lines, picks, clips, durs = lines[:-1], picks[:-1], clips[:-1], durs[:-1]
            caps, shot_durs, total = timeline(durs)
    for c, ln in zip(caps, lines):
        c["text"] = ln["show"]
    return {"subj": subj, "item": item, "key": key, "script": script, "lines": lines, "picks": picks,
            "clips": clips, "durs": durs, "caps": caps, "shot_durs": shot_durs, "total": total, "vo": vo,
            "fallback": True}


def _cache_path() -> str:
    root = os.environ.get("STATE_DIR") or os.environ.get("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(root, "footage_verified_cache.json")


def load_verified_cache() -> List[dict]:
    path = _cache_path()
    rows: List[dict] = []
    try:
        data = json.loads(open(path, encoding="utf-8").read())
        rows = data if isinstance(data, list) else list(data.get("subjects") or [])
    except Exception:  # noqa: BLE001
        rows = []
    gen = os.environ.get("STATE_DIR") or os.environ.get("DATA_DIR")
    if gen:
        generated = os.path.join(gen, "generated")
        try:
            for fn in os.listdir(generated):
                if not fn.endswith(".sources.json"):
                    continue
                try:
                    blob = json.loads(open(os.path.join(generated, fn), encoding="utf-8").read())
                except Exception:  # noqa: BLE001
                    continue
                info = (blob.get("info") or {}) if isinstance(blob, dict) else {}
                concept = (blob.get("concept") or {}) if isinstance(blob, dict) else {}
                matches = info.get("line_matches") or []
                visioned = [m for m in matches if isinstance((m or {}).get("vision_match"), (int, float))
                            and (m.get("vision_match") or 0) >= CLIP_MIN_MATCH]
                if len(visioned) < MIN_LINES:
                    continue
                rows.append({"title": concept.get("topic") or info.get("topic_key"),
                             "url": info.get("wikipedia") or concept.get("wikipedia"),
                             "from_sources": fn, "vision_verified": True, "lines": visioned})
        except OSError:
            pass
    return rows


def remember_verified(subj: dict, script: dict, sources: List[dict], fallback: bool = False) -> None:
    rec = {"title": subj.get("title"), "url": subj.get("url"), "commons_category": subj.get("commons_category"),
           "at": datetime.now(timezone.utc).isoformat(), "fallback": fallback,
           "lines": [{"line": s.get("line"), "provider": s.get("provider"), "title": s.get("title"),
                      "page": s.get("page"), "vision": s.get("vision")} for s in sources]}
    rows = []
    path = _cache_path()
    try:
        data = json.loads(open(path, encoding="utf-8").read())
        rows = data if isinstance(data, list) else list(data.get("subjects") or [])
    except Exception:  # noqa: BLE001
        rows = []
    rows = [r for r in rows if r.get("title") != rec["title"]]
    rows.append(rec)
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rows[-40:], fh, indent=1, default=str)
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("could not write verified-subject cache: %s", exc)


def library_fallback_subjects(avoid: set, recent: str, log: List[str], get=fm._get, limit: int = 8) -> List[tuple]:
    import footage_topics
    out = []
    for topic in footage_topics.TOPICS:
        if topic["key"] in avoid or (topic.get("topic") or "").lower() in recent:
            continue
        try:
            subj = fm.wiki_subject(topic["topic"], get) or fm._wiki_page(topic["topic"], get)
        except Exception as exc:  # noqa: BLE001
            log.append(f"ai fallback wiki {topic['key']}: {exc}"[:160])
            continue
        if not subj:
            log.append(f"ai fallback skip (no Wikipedia page): {topic['topic']}")
            continue
        ok, why = fm.subject_ok(subj)
        if not ok:
            log.append(f"ai fallback skip ({why}): {subj['title']}")
            continue
        try:
            cats = _wiki_categories(subj["title"], get)
        except Exception:  # noqa: BLE001
            cats = []
        rel = religious_subject(subj, cats)
        if rel:
            log.append(f"subject skip (religious: {rel}): library {topic['key']} -> {subj['title']}"[:260])
            continue
        key = "v2-" + re.sub(r"[^a-z0-9]+", "-", topic["key"]).strip("-") + "-fb"
        if key in avoid or subj["title"].lower() in recent:
            continue
        item = {"title": topic["topic"], "views_per_hour": 0, "source": "library"}
        out.append((subj, item, key))
        if len(out) >= limit:
            break
    return out


def cached_fallback_subjects(avoid: set, recent: str, log: List[str], get=fm._get) -> List[tuple]:
    out = []
    seen = set()
    for rec in load_verified_cache():
        title = (rec.get("title") or "").strip()
        if not title or title in seen or title.lower() in recent:
            continue
        if not rec.get("vision_verified") and rec.get("fallback"):
            continue  # don't chain metadata-only results as "already verified"
        try:
            subj = fm._wiki_page(title, get)
        except Exception as exc:  # noqa: BLE001
            log.append(f"ai fallback cache wiki {title}: {exc}"[:160])
            continue
        if not subj:
            continue
        ok, why = fm.subject_ok(subj)
        if not ok:
            continue
        try:
            cats = _wiki_categories(subj["title"], get)
        except Exception:  # noqa: BLE001
            cats = []
        rel = religious_subject(subj, cats)
        if rel:
            log.append(f"subject skip (religious: {rel}): cache {title} -> {subj['title']}"[:260])
            continue
        key = "v2-" + re.sub(r"[^a-z0-9]+", "-", subj["title"].lower()).strip("-")[:48]
        if key in avoid:
            continue
        seen.add(title)
        item = {"title": title, "views_per_hour": 0, "source": "verified_cache"}
        out.append((subj, item, key))
    return out


def run_fallback(avoid: set, recent: str, workdir: str, log: List[str]) -> Optional[dict]:
    """Build one topic without Cloudflare. Returns None (and logs) rather than a mismatched video."""
    if "ai fallback (cf 429)" not in log:
        log.append("ai fallback (cf 429)")
    candidates = cached_fallback_subjects(avoid, recent, log) + library_fallback_subjects(avoid, recent, log)
    if not candidates:
        log.append("ai fallback: no library or previously verified subjects available")
        return None
    for subj, item, key in candidates:
        try:
            built = build_fallback_topic(subj, item, key, workdir, log)
        except Exception as exc:  # noqa: BLE001
            log.append(f"ai fallback topic {subj.get('title')} failed: {exc}"[:240])
            built = None
        if built:
            log.append(f"ai fallback: built {subj['title']} with {len(built['lines'])} metadata-guaranteed lines")
            return built
    log.append("ai fallback: no topic with line-guaranteed footage; skipping slot")
    return None
