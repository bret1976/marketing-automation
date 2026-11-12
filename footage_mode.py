"""VIDEO_ENGINE=footage: a 31-55 second Short (target ~45 s) with NO generation API (no Gemini, Veo, Omni, Runway, FAL).

Pipeline (all free, all local except the public archive searches):
  1. topic   - today's most-watched uploads from the keyless open finder (open_viral.py, yt-dlp),
               matched to a hand-written, fact-checked template library (footage_topics.py).
  2. script  - template hook / on-screen title / ~8 unhurried VO lines / captions. No LLM.
  3. footage - real, free, legally reusable clips matched to the topic: NASA Image & Video Library,
               Wikimedia Commons (PD / CC0 / CC BY only), Internet Archive (public-domain / Prelinger).
               Pexels / Pixabay only when PEXELS_API_KEY / PIXABAY_API_KEY are set. Every clip's
               source page, author and license is recorded in info["footage_sources"].
  4. edit    - ~8-12 shots (2.2-6.5 s each) cut on the music beat (CC0 Freesound beds, pre-analysed beat grid),
               denoise + sharpen + desaturated cinematic grade + grain, Ken Burns only on stills,
               Kokoro (local ONNX) voiceover, burned-in hook + VO captions + source credit.
  5. output  - 1080x1920 (YouTube Shorts / Instagram / TikTok / Facebook) and 1920x1080 (LinkedIn / X),
               H.264 Main 4.0, yuv420p, 30 fps, AAC 44.1 kHz stereo, +faststart.

Length rule (Bret, 2026-11-12): every rendered clip, 9:16 AND 16:9, is >= MIN_SECONDS (31) and
<= MAX_SECONDS (55), measured with ffprobe on the output. The length comes from the voiceover and the
footage actually available, never from the app's autonomous_video_duration setting (that is the old
8-second generation formula). Not enough on-topic footage for 31 s -> the render fails; it is never
padded with black or shipped short. The voiceover is never sped up to fit.

Off unless VIDEO_ENGINE=footage. Cost: $0.
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
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

import footage_topics

logger = logging.getLogger(__name__)
MARKER = "FOOTAGE_MODE"
VERSION = "2026-11-12.1"
MIN_SECONDS = 31.0  # hard floor for every output file (Bret 2026-11-12)
MAX_SECONDS = 55.0  # hard ceiling for every output file (still a Short everywhere)
TARGET_SECONDS = 45.0  # aim here when enough good on-topic footage exists
MIN_SHOT_SECONDS = 2.2
MAX_SHOT_SECONDS = 6.5  # longest single shot; more shots are used rather than holding one too long
SHOT_SECONDS = 4.2  # typical shot length the plan aims for
SEGMENT_SECONDS = MAX_SHOT_SECONDS + 0.5  # every shot is cut this long so beat-snapped lengths always fit
VO_SPEED = 0.95  # natural, unhurried read; never raised to squeeze lines in
VO_LEAD = 0.35
VO_GAP = 0.6  # unhurried pause between lines
VO_TAIL = 1.6  # music + picture after the last line
HOOK_SECONDS = 2.8
FPS = 30
UA = "6frame-marketing-automation/1.0 (footage mode; https://6framestudio.com)"
HERE = os.path.dirname(os.path.abspath(__file__))
MUSIC_DIR = os.path.join(HERE, "footage_assets", "music")
TALKING_HEAD = re.compile(r"\b(interview|briefing|press conference|fireside|reflect\w*|lecture|chat|podcast|panel|"
                          r"q ?& ?a|conference|news|webinar|town hall|remarks|speech|explained|reaction|trailer|animation|animated|"
                          r"game|minecraft|simulator|simulation|model|toy|lego|recreation|cgi|ai|ceremony|anniversary|commemorat\w*|"
                          r"cemetery|museum|address|social|honou?rs?|memorial|tribute|wikimania|engineering facts|remembrance|reporters?|"
                          r"white house|oval office|president trump|trump|biden|obama|putin|zelensk\w*|iran|gaza|ukraine)\b", re.I)
TITLE_YEAR = re.compile(r"(?<!\d)(1[89]\d\d|20[0-4]\d)(?!\d)")
MODERN_MARK = re.compile(r"\b(hd|4k|8k|uhd|drone|gopro|dji)\b", re.I)


def off_era(title: str, era: Optional[int]) -> bool:
    """True when a clip is clearly from long after the event (a later year in the title, or modern-camera markers)."""
    if not era:
        return False
    if any(int(y) >= era + 10 for y in TITLE_YEAR.findall(title or "")):
        return True
    return era < 1990 and bool(MODERN_MARK.search(title or ""))
VIDEO_EXT = (".mp4", ".webm", ".ogv", ".mov", ".m4v", ".mpeg", ".mpg")
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".tif", ".tiff")


def status() -> dict:
    """Read-only health block: engine flag and whether the Kokoro files exist (no synthesis, no network)."""
    model, voices = os.environ.get("KOKORO_MODEL") or "", os.environ.get("KOKORO_VOICES") or ""
    return {"enabled": enabled(), "version": VERSION,
            "kokoro_model": bool(model and os.path.exists(model)), "kokoro_voices": bool(voices and os.path.exists(voices)),
            "viral_source": (os.environ.get("VIRAL_SOURCE") or "").strip().lower() or "default"}


def enabled() -> bool:
    return (os.environ.get("VIDEO_ENGINE") or "").strip().lower() == "footage"


def target_seconds() -> float:
    """FOOTAGE_TARGET_SECONDS (default 45), always clamped into [MIN_SECONDS, MAX_SECONDS].
    Deliberately NOT autonomous_video_duration: a stale 8 s setting must never shrink a footage clip."""
    try:
        t = float(os.environ.get("FOOTAGE_TARGET_SECONDS") or TARGET_SECONDS)
    except ValueError:
        t = TARGET_SECONDS
    return min(max(t, MIN_SECONDS), MAX_SECONDS)


def length_ok(seconds: float) -> bool:
    return MIN_SECONDS <= float(seconds or 0.0) <= MAX_SECONDS


def plan_total(vo_seconds: float, target: Optional[float] = None) -> float:
    """Planned clip length: at least the target (~45 s), long enough for the whole VO plus a tail,
    never outside [MIN_SECONDS, MAX_SECONDS]."""
    t = target_seconds() if target is None else min(max(target, MIN_SECONDS), MAX_SECONDS)
    return round(min(max(t, (vo_seconds or 0.0) + VO_TAIL, MIN_SECONDS), MAX_SECONDS), 3)


def shot_count(total: float) -> int:
    """How many shots a clip of `total` seconds is planned with (every shot <= MAX_SHOT_SECONDS)."""
    import math
    return max(math.ceil(total / MAX_SHOT_SECONDS), int(round(total / SHOT_SECONDS)))


def min_shots() -> int:
    import math
    return math.ceil(MIN_SECONDS / MAX_SHOT_SECONDS)


def fit_total(planned: float, shots_available: int) -> float:
    """Clip length the shots we actually got can carry. Raises when that is under MIN_SECONDS."""
    total = min(planned, shots_available * MAX_SHOT_SECONDS, MAX_SECONDS)
    if total < MIN_SECONDS:
        raise RuntimeError(f"only {shots_available} usable on-topic shots: {total:.1f}s < {MIN_SECONDS:.0f}s minimum; "
                           f"not padding or shipping a short clip")
    return round(total, 3)


# --------------------------------------------------------------------------- helpers

def _bin(name: str) -> str:
    return shutil.which(name) or name


def _run(cmd: List[str], timeout: int = 300) -> subprocess.CompletedProcess:
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(f"{os.path.basename(cmd[0])} failed: {res.stderr[-600:]}")
    return res


def _get(url: str, params: Optional[dict] = None, timeout: int = 25, headers: Optional[dict] = None) -> requests.Response:
    h = {"User-Agent": UA}
    h.update(headers or {})
    for attempt in range(3):  # Wikipedia/Commons drop connections or 429 under load: brief retries, then raise
        try:
            r = requests.get(url, params=params, timeout=timeout, headers=h)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 + 3 * attempt)
                continue
            r.raise_for_status()
            return r
        except (requests.ConnectionError, requests.Timeout):
            if attempt == 2:
                raise
            time.sleep(2 + 3 * attempt)
    raise RuntimeError("unreachable")


def _strip_html(s: Any) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", str(s or "")).split())


def _ua(src: str) -> List[str]:
    """-user_agent only for network inputs (ffmpeg rejects it for local files)."""
    return ["-user_agent", UA] if str(src).startswith(("http://", "https://")) else []


def probe(src: str, timeout: int = 60) -> dict:
    res = _run([_bin("ffprobe"), "-v", "error", *_ua(src), "-show_entries",
                "format=duration:stream=codec_type,width,height,codec_name,profile,level,pix_fmt,r_frame_rate,sample_rate,channels",
                "-of", "json", src], timeout=timeout)
    data = json.loads(res.stdout or "{}")
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})
    try:
        dur = float((data.get("format") or {}).get("duration") or 0)
    except ValueError:
        dur = 0.0
    return {"duration": dur, "width": int(v.get("width") or 0), "height": int(v.get("height") or 0),
            "vcodec": v.get("codec_name"), "profile": v.get("profile"), "level": v.get("level"), "pix_fmt": v.get("pix_fmt"),
            "fps": v.get("r_frame_rate"), "acodec": a.get("codec_name"), "sample_rate": a.get("sample_rate"),
            "channels": a.get("channels")}


def _font(bold: bool = True) -> str:
    names = (["DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "Arial Bold.ttf"] if bold
             else ["DejaVuSans.ttf", "LiberationSans-Regular.ttf", "Arial.ttf"])
    for root in ("/usr/share/fonts", "/usr/local/share/fonts", "/System/Library/Fonts", "/Library/Fonts"):
        for dirpath, _, files in os.walk(root):
            for n in names:
                if n in files:
                    return os.path.join(dirpath, n)
    return ""


# --------------------------------------------------------------------------- 1. topic

def score_topics(viral_items: List[dict], topics: Optional[List[dict]] = None) -> List[dict]:
    """Rank template topics by today's viral demand: sum of views/hour of matching uploads
    (strong match x1.0, related-only x0.25). Returns [{topic, score, matched:[...]}], best first."""
    topics = topics or footage_topics.TOPICS
    out = []
    for t in topics:
        strong, weak = re.compile(t["match"], re.I), re.compile(t.get("related") or r"$^", re.I)
        score, matched = 0.0, []
        for it in viral_items or []:
            title = f"{it.get('title') or ''} {it.get('description') or ''}"
            vph = float(it.get("views_per_hour") or it.get("score") or 0) or 0.0
            if strong.search(title):
                w = 1.0
            elif weak.search(title):
                w = 0.25
            else:
                continue
            score += w * max(vph, 1.0)
            matched.append({"title": (it.get("title") or "")[:120], "url": it.get("url"), "views": it.get("views"),
                            "views_per_hour": it.get("views_per_hour"), "weight": w})
        out.append({"topic": t, "score": round(score, 1), "matched": matched})
    out.sort(key=lambda r: r["score"], reverse=True)
    return out


def pick_topic(viral_items: List[dict], avoid_keys: Optional[set] = None, recent_topics: Optional[List[str]] = None,
               forced: Optional[str] = None) -> dict:
    if forced:
        t = footage_topics.BY_KEY[forced]
        return {"topic": t, "score": 0.0, "matched": [], "reason": "forced"}
    avoid_keys = avoid_keys or set()
    recent = " | ".join(recent_topics or []).lower()
    ranked = score_topics(viral_items)
    fresh = [r for r in ranked if r["topic"]["key"] not in avoid_keys and r["topic"]["topic"].lower() not in recent]
    pool = fresh or ranked
    best = pool[0]
    best["reason"] = "viral_match" if best["score"] > 0 else "no viral match today; next unused library topic"
    return best


def todays_viral(queries: Optional[List[str]] = None) -> dict:
    """Keyless (yt-dlp) last-24h finder from PR #23 over a broad 'what's going viral today' query set
    (FOOTAGE_VIRAL_QUERIES overrides). Only titles/view counts are read; no viral video is downloaded."""
    import open_viral
    qs = queries or [q.strip() for q in (os.environ.get("FOOTAGE_VIRAL_QUERIES") or "|".join(BROAD_VIRAL_QUERIES)).split("|")
                     if q.strip()]
    os.environ.setdefault("VIRAL_OPEN_PROBE_LIMIT", os.environ.get("FOOTAGE_VIRAL_PROBE_LIMIT", "80"))
    return open_viral.find_viral(queries=qs, subreddits=[], top_n=60, niche=False, max_seconds=3600)


# --------------------------------------------------------------------------- 1b. viral-first topic selection
#
# Bret 2026-11-12: the last-24h viral scan DETERMINES the video. The finder runs first over a broad
# "what's going viral today" query set; each viral upload (ranked by views/hour) is resolved to the
# real-world subject it is about (a Wikipedia article named in the title), and footage mode then looks
# for public-domain / CC archival footage of THAT subject. The viral videos themselves are never
# downloaded or recut. A subject without enough licensed on-topic footage for 31 s is skipped and the
# next viral subject is tried; there is no fallback to a canned library topic.
# The hand-written library (footage_topics.py) is only used as a better, fact-checked script when the
# viral subject IS one of its topics.

BROAD_VIRAL_QUERIES = ("#shorts", "viral", "trending", "documentary", "history", "space", "science", "nature",
                       "wildlife", "disaster", "engineering", "footage", "ocean", "volcano", "earthquake", "storm",
                       "tornado", "weather", "animals", "shark", "aviation", "aircraft", "train", "military",
                       "rocket launch", "nasa", "ww2", "ancient", "archaeology", "shipwreck")
WIKI_API = "https://en.wikipedia.org/w/api.php"
TEXT_CREDIT = "Narration adapted from Wikipedia (CC BY-SA 4.0)"
# subjects we can't (or shouldn't) build from archival footage: people in the news, media/IP, brands, politics
SKIP_SUBJECT = re.compile(
    r"\b(singer|rapper|actor|actress|comedian|musician|songwriter|dj|politician|youtuber|streamer|influencer|"
    r"tiktoker|footballer|basketball|baseball|soccer|cricketer|athlete|player|wrestl\w*|boxer|fighter|ufc|model|"
    r"businessman|businesswoman|entrepreneur|billionaire|television|tv series|sitcom|reality|film|movie|song|single|"
    r"album|band|video game|game|franchise|anime|manga|cartoon|character|fictional|company|corporation|brand|"
    r"website|app|software|podcast|election|political|politics|party|president|prime minister|senator|"
    r"congress|parliament|club|team|league|season|episode|esports|toy|lego|minecraft|roblox|fortnite|meme|"
    r"internet|social media|news|journalist|presenter|chef|restaurant|celebrity)\b", re.I)
# living people (Wikipedia leads say "born ..."), plus countries/cities: far too broad to build one Short around
SKIP_TEXT = re.compile(r"\bborn\b", re.I)
SKIP_PLACE = re.compile(r"^(country|sovereign state|island country|transcontinental country|city|town|village|"
                        r"municipality|capital|state of|province|region|continent|county)\b|\b(country|city) in\b", re.I)
# abstract concepts / broad classes ("Snow", "Bird", "Evolution"): no specific thing to film
SKIP_ABSTRACT = re.compile(r"^(change|process|concept|theory|form|state|class|type|kind|natural|precipitation|field|"
                           r"study|branch|term|group|set|act|practice|phenomenon|property|quality|period|unit|measure|"
                           r"list|aspect|method|system|means|activity|art|style|genre|condition|feeling|emotion)\b", re.I)
# live streams, gaming, music, compilations: their view counts are real but there is no real-world subject to film
SKIP_VIRAL = re.compile(r"\blive\b|\U0001F534|#?shortslive|\bstream(ing)?\b|\bgam(e|ing|er)\b|\bgta\b|roblox|minecraft|"
                        r"fortnite|free ?fire|pubg|\bbgmi\b|\bmashup\b|\bmusic\b|\bsong\b|\blyrics?\b|\bdance\b|"
                        r"\breaction\b|\bprank\b|\bunboxing\b|\basmr\b|\btutorial\b|\brecipe\b|\bvlog\b|\bmovie review\b|"
                        r"\btrailer\b|\bpodcast\b|\bfilm\b|\bmovie\b|\bepisode\b|\bep \d+|\bs\d+e\d+\b", re.I)
_TITLE_NOISE = re.compile(r"#\w+|\[[^\]]*\]|\([^)]*\)|[\U0001F000-\U0001FFFF\u2600-\u27BF]|"
                          r"\b(real|rare|footage|viral|shorts?|official|video|full|hd|4k|live|new|today|watch|amazing|"
                          r"incredible|insane|crazy|you won'?t believe|must see|part \d+|ep(isode)? ?\d+)\b", re.I)
# scanned subjects match on a name, so homonyms need an extra guard (e.g. "Wolf" -> a wrestler "Chief Little Wolf")
DYNAMIC_EXCLUDE = (r"\bvs\.?\b|versus|wrestl\w*|boxing|\bbout\b|\bmatch\b|football|rugby|soccer|cricket|\bteam\b|"
                   r"\bclub\b|stadium|\bchief\b|\bmr\.?\b|\bmrs\.?\b|logo|mascot|\btoy\b|cartoon|comic|anime|\bfilm\b|"
                   r"movie|trailer|\bsong\b|music|concert|album|parade|costume|statue|sculpture|painting|drawing|"
                   r"illustration|stamp|coin|\bmap\b|diagram|\bchart\b|\bslide\b|lecture|conference|interview|"
                   r"question\w*|hearing|testimony|testif\w*|committee|senat\w*|congress\w*|secretary|governor|mayor|"
                   r"campaign|election|harris|pelosi|desantis|vance|rally|protest|press|briefing|statement|remarks|"
                   r"silent|western|feature|starring|directed|actor|actress|episode|television|\btv\b|serial|"
                   r"advert\w*|commercial")
GENERIC = {"the", "a", "an", "of", "and", "in", "on", "at", "to", "for", "from", "with", "by", "disaster", "launch",
           "attack", "battle", "eruption", "sinking", "landing", "mission", "flight", "first", "great", "big", "history",
           "world", "war", "day", "night", "event", "explosion", "crash", "storm", "fire", "bridge", "dam", "ship",
           "rocket", "space", "test", "new", "old", "most", "best", "last", "how", "why", "what", "this", "that"}
_YEAR = re.compile(r"(?<![\d,.])(1[1-9]\d\d|20\d\d)(?!\d|,\d)")
_ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def _two(n: int) -> str:
    if n < 20:
        return _ONES[n]
    return _TENS[n // 10] + ("" if n % 10 == 0 else "-" + _ONES[n % 10])


def year_words(y: int) -> str:
    """1912 -> 'nineteen twelve', 1903 -> 'nineteen oh three', 2000 -> 'two thousand', 2022 -> 'twenty twenty-two'."""
    hi, lo = divmod(y, 100)
    if y % 1000 == 0:
        return _ONES[y // 1000] + " thousand"
    if 2000 < y < 2010:
        return "two thousand " + _ONES[lo]
    if lo == 0:
        return _two(hi) + " hundred"
    return _two(hi) + (" oh " + _ONES[lo] if lo < 10 else " " + _two(lo))


def spoken(text: str) -> str:
    return _YEAR.sub(lambda m: year_words(int(m.group(1))), text)


def clean_viral_title(title: str) -> str:
    t = (title or "").split("|")[0]
    t = _TITLE_NOISE.sub(" ", t)
    t = re.sub(r"[^\w\s'.-]", " ", t)
    return " ".join(t.split())


def _tokens(s: str) -> List[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w]


def named_in(article: str, title: str) -> bool:
    """The article's subject is literally named in the viral title (all its significant words appear)."""
    stop = {"the", "a", "an", "of", "and", "in", "on", "at", "to", "for", "from", "with", "by"}
    words = [w for w in _tokens(re.sub(r"\([^)]*\)", "", article)) if w not in stop]
    stem = lambda w: w[:-1] if len(w) > 4 and w.endswith("s") else w
    have = {stem(w) for w in _tokens(title)}
    words = [stem(w) for w in words]
    distinct = [w for w in words if w not in GENERIC]
    return bool(distinct) and all(w in have for w in words) and len("".join(distinct)) >= 4


def _sentences(text: str) -> List[str]:
    # plain-format section headings sit on their own line with no end punctuation ("Flight", "Behavior and diet")
    text = "\n".join(ln for ln in (text or "").split("\n") if re.search(r"[.!?\"')]\s*$", ln.strip()))
    text = re.sub(r"\b([ap])\.m\.", r"\1m", text)  # "4:00 p.m." is not a sentence end
    text = re.sub(r"\b([A-Z])\.\s(?=[A-Z])", r"\1 ", text)  # initials: "W. Douglas Burden"
    text = re.sub(r"\b(St|Dr|Mr|Mrs|Ms|Jr|Sr|Mt|Gen|Col|Lt|Capt|No|vs|c|ca)\.\s", r"\1 ", text)
    text = re.sub(r"\([^()]*\)", "", text)
    text = re.sub(r"\[[^\]]*\]", "", text)
    text = re.sub(r"\s+([,.;:])", r"\1", " ".join(text.split()))
    out = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])", text)
    return [s.strip() for s in out if len(s.split()) >= 4 and "==" not in s]


def _halve(c: str, limit: int) -> List[str]:
    if len(c) <= limit:
        return [c]
    words = c.split()
    cut = min(range(1, len(words)), key=lambda i: abs(len(" ".join(words[:i])) - len(c) / 2))
    return _halve(" ".join(words[:cut]), limit) + _halve(" ".join(words[cut:]), limit)


def _chunks(sentence: str, limit: int = 56) -> List[str]:
    """Caption-sized pieces (<= limit chars): clauses (commas/semicolons, never inside a date like 'May 6, 1937')
    packed greedily, then anything still too long halved at the most central word gap."""
    parts, out = re.split(r"(?<=[,;:])\s+(?!\d)", sentence), []
    cur = ""
    for p in parts:
        if cur and len(cur) + 1 + len(p) > limit:
            out.append(cur)
            cur = p
        else:
            cur = f"{cur} {p}".strip()
    if cur:
        out.append(cur)
    return [x for c in out for x in _halve(c, limit) if x]


_APPOSITIVE = re.compile(r",\s+(?:also|commonly|sometimes|often|formerly|officially)\s+(?:known as|called|named)\b[^,]*,", re.I)
_TRAILING = re.compile(r"(?:,|\s+-|;)\s*(?:including|though|although|as well as|such as|as demonstrated by|consisting of|"
                       r"which|whereas|while|especially|particularly|mainly|primarily|with|making|resulting in)\b[^.]*", re.I)
CAPTION_MAX_CHARS = 84  # one complete sentence per caption: <= 3 lines at the 9:16 wrap width


def tighten(sentence: str) -> str:
    """A Wikipedia sentence cut to its main clause (no appositives or trailing asides), still a full sentence."""
    s = _APPOSITIVE.sub("", sentence)
    s = re.sub(r";.*$", ".", s)
    s = _TRAILING.sub("", s)
    s = re.sub(r"\s+([,.;:])", r"\1", " ".join(s.split())).rstrip(",;: ")
    if not s.endswith((".", "!", "?")):
        s = s.rstrip(".") + "."
    return s


# etymology / naming trivia reads badly as narration over footage
_NAME_LORE = re.compile(r"\b(etymolog\w*|derived from|stems from|epithet|vernacular|latin|greek|old english|named by|"
                        r"the (?:generic |common |scientific )?name|binomial|taxonom\w*|nomenclature)\b", re.I)
_DANGLING = re.compile(r"^(it|its|they|their|them|this|these|those|that|such|he|she|his|her|both|also|however|"
                       r"under their|in addition|as a result)\b", re.I)


def script_from_text(text: str, min_words: int = 60, max_words: int = 100, name: str = "") -> List[dict]:
    """A short documentary VO from a Wikipedia article (lead first): each line is one tightened, complete sentence
    short enough to sit on screen whole (no mid-sentence captions or pauses). The first line names the subject,
    and a line opening with a pronoun is used only right after the sentence it refers to. Years are spoken out.
    [] if too thin for a 31 s clip."""
    lines, words, prev_kept = [], 0, False
    name_re = re.compile(r"\b" + re.escape(name.split()[0][:5]), re.I) if name else None
    for raw in _sentences(text):
        s = tighten(raw)
        n = len(s.split())
        ok = (5 <= n <= 17 and len(s) <= CAPTION_MAX_CHARS and not s.count('"') % 2
              and not re.search(r"[^\x00-\u024F\u2010-\u2030]", s) and not _NAME_LORE.search(s))
        ok = ok and not (_DANGLING.search(s) and (not prev_kept or not lines))
        ok = ok and (bool(lines) or name_re is None or bool(name_re.search(s)))
        prev_kept = False
        if not ok:
            continue
        if words and words + n > max_words:
            break
        lines.append({"say": spoken(s), "show": s, "parts": [s]})
        words, prev_kept = words + n, True
    return lines if words >= min_words else []


def wiki_subjects(title: str, get: Callable = _get, limit: int = 2) -> List[dict]:
    """Up to `limit` Wikipedia subjects named in a viral title ("Lynx attacks wolf" -> Lynx, Wolf)."""
    q = clean_viral_title(title)
    if len(q) < 4:
        return []
    hits = (get(WIKI_API, {"action": "query", "list": "search", "srsearch": q, "srlimit": 8, "format": "json",
                           "srnamespace": 0}).json().get("query") or {}).get("search", [])
    named = [h["title"] for h in hits if named_in(h["title"], q)]
    # shorter, distinct names from the title words themselves (search ranks long article titles first)
    # "A vs B" / "A attacks B" titles name two subjects; take them from the title words themselves
    duel = re.search(r"([A-Z][a-z]{3,})\s+(?:vs\.?|versus|attacks?|hunts?|fights?|meets?|against)\s+([A-Z][a-z]{3,})", q)
    for w in (list(duel.groups()) if duel else []):
        if w not in named:
            named.append(w)
    out = []
    for pick in named:
        if len(out) >= limit:
            break
        subj = _wiki_page(pick, get)
        if subj and named_in(subj["title"], q):
            out.append(subj)
    return out


def wiki_subject(title: str, get: Callable = _get) -> Optional[dict]:
    """The Wikipedia article a viral title is about (named in it), with its plain-text lead. None if none fits."""
    subs = wiki_subjects(title, get, limit=1)
    return subs[0] if subs else None


def _wiki_page(pick: str, get: Callable = _get) -> Optional[dict]:
    data = get(WIKI_API, {"action": "query", "prop": "extracts|pageprops|description", "explaintext": 1,
                          "exsectionformat": "plain", "redirects": 1, "titles": pick, "format": "json"}).json()
    page = next(iter(((data.get("query") or {}).get("pages") or {}).values()), {})
    if not page or "missing" in page or "disambiguation" in (page.get("pageprops") or {}):
        return None
    full = page.get("extract") or ""
    lead = full.split("\n\n\n")[0]
    cat = ""
    qid = (page.get("pageprops") or {}).get("wikibase_item")
    if qid:  # the subject's curated Wikimedia Commons category (Wikidata P373): far better relevance than a name search
        try:
            claims = get("https://www.wikidata.org/w/api.php", {"action": "wbgetclaims", "entity": qid, "property": "P373",
                                                                 "format": "json"}).json().get("claims", {}).get("P373", [])
            cat = ((claims[0].get("mainsnak") or {}).get("datavalue") or {}).get("value") or "" if claims else ""
        except Exception:
            cat = ""
    return {"title": page.get("title") or pick, "description": page.get("description") or "", "lead": lead,
            "commons_category": cat,
            "text": full[:15000], "url": f"https://en.wikipedia.org/wiki/{(page.get('title') or pick).replace(' ', '_')}"}


def subject_ok(subj: dict) -> Tuple[bool, str]:
    """Skip people in the news, media/IP, brands, politics: not legally/plausibly sourceable as archival footage."""
    head = f"{subj.get('title')} | {subj.get('description')}"
    if SKIP_SUBJECT.search(subj.get("description") or "") or TALKING_HEAD.search(subj.get("title") or ""):
        return False, f"subject type: {head[:120]}"
    if SKIP_TEXT.search((subj.get("lead") or "")[:300]) or SKIP_TEXT.search(subj.get("description") or ""):
        return False, f"living person: {head[:120]}"
    if SKIP_ABSTRACT.search(subj.get("description") or ""):
        return False, f"too abstract: {head[:120]}"
    if SKIP_PLACE.search(subj.get("description") or ""):
        return False, f"too broad (country/city): {head[:120]}"
    return True, ""


# hashtags + comment question by subject kind (scanned subjects are not all history: wolves, cheetahs, volcanoes...)
SUBJECT_STYLE = {
    "wildlife": (["#Wildlife", "#Nature", "#RealFootage"], "Which animal should we make a film about next?"),
    "nature": (["#Nature", "#Earth", "#RealFootage"], "Which natural wonder should we cover next?"),
    "space": (["#Space", "#Science", "#RealFootage"], "Which mission or discovery should we cover next?"),
    "tech": (["#Engineering", "#Science", "#RealFootage"], "Which machine or invention should we cover next?"),
    "history": (["#History", "#ArchivalFootage", "#RealFootage"], "Which moment in history should we cut next?"),
    "general": (["#Documentary", "#RealFootage", "#DidYouKnow"], "What should we make a film about next?"),
}


def subject_kind(subj: dict) -> str:
    d = f"{subj.get('description', '')} {(subj.get('lead') or '')[:300]}".lower()
    if re.search(r"\b(species|genus|family of|subfamily|mammal|bird|reptile|amphibian|fish|insect|spider|shark|whale|"
                 r"predator|carnivor|herbivor|canine|feline|felid|rodent|primate|animal|cat|dog|bear|snake)s?\b", d):
        return "wildlife"
    if re.search(r"\b(spacecraft|space|rocket|planet|moon|star|galaxy|nasa|astronaut|orbit|comet|asteroid|telescope)\b", d):
        return "space"
    if re.search(r"\b(battle|war|disaster|attack|expedition|siege|revolution|empire|dynasty|ancient|century|historic)\b", d):
        return "history"
    if re.search(r"\b(volcano|mountain|river|waterfall|glacier|desert|forest|ocean|storm|hurricane|tornado|earthquake|"
                 r"weather|reef|cave|canyon|aurora|lake)s?\b", d):
        return "nature"
    if re.search(r"\b(aircraft|airship|locomotive|ship|engine|machine|vehicle|car|bridge|tower|invention|robot)s?\b", d):
        return "tech"
    return "general"


def topic_from_subject(subj: dict) -> Optional[dict]:
    """A footage topic (same shape as footage_topics.TOPICS) for a viral subject, scripted from its Wikipedia lead."""
    title = re.sub(r"\s*\([^)]*\)$", "", subj["title"]).strip()
    name = re.sub(r"\s*\([^)]*\)$", "", subj["title"]).strip()
    vo = script_from_text(subj.get("text") or subj.get("lead") or "", name=name)
    if not vo:
        return None
    years = [int(y) for y in _YEAR.findall(" ".join(v["show"] for v in vo[:3])) if int(y) <= datetime.now().year]
    year = years[0] if years else None
    words = [w for w in _tokens(title) if w not in GENERIC] or _tokens(title)
    phrase = r"\W+".join(re.escape(w) for w in _tokens(title))
    def plural(w: str) -> str:
        return rf"\b{re.escape(w)}(s|es)?\b" + (rf"|\b{re.escape(w[:-1])}ves\b" if w.endswith("f") else "")
    require = "|".join([phrase] + [plural(w) for w in words if len(w) >= 4])
    hook_title = title.upper() if len(title) <= 22 else " ".join(title.split()[:3]).upper()
    lower = (title + " " + subj.get("description", "")).lower()
    mood = ("dark_build" if re.search(r"disaster|attack|war|battle|eruption|explosion|crash|sinking|storm|fire", lower)
            else "cinematic" if re.search(r"space|rocket|launch|nasa|moon|mars|orbit|satellite|flight|aircraft", lower)
            else "reveal")
    tag = "#" + re.sub(r"[^A-Za-z0-9]", "", title.title())[:28]
    kind = subject_kind(subj)
    tags, question = SUBJECT_STYLE[kind]
    space = bool(re.search(r"nasa|space|rocket|moon|mars|apollo|shuttle|satellite|orbit|launch|astronaut|telescope", lower))
    return {
        "key": "viral-" + re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48],
        "topic": title, "title_line": f"{title}: Real Footage",
        # a year only for dated subjects (events, missions, machines): "CHEETAH · 2022" would mislead
        "on_screen_hook": f"{hook_title} · {year}" if year and kind in ("history", "space", "tech") else hook_title,
        "hook": vo[0]["show"], "vo": vo,
        "lines": [f"Real archival footage: {title}."],
        "hashtags": [tag] + tags, "comment_question": question, "kind": kind, "mood": mood, "match": phrase, "related": None,
        "require": require, "exclude": DYNAMIC_EXCLUDE,
        # with a curated Commons category, search only inside it (a name search drags in homonyms:
        # "Wolf Creek Falls Trail", "Wolf Lowry (1917)"); otherwise fall back to a strict name search
        "queries": ({"commons": [], "archive": [], "nasa": [title] if space else [],
                     "commons_cat": [subj["commons_category"]]} if subj.get("commons_category") else
                    {"commons": [title, f"{title} footage"], "archive": [title], "nasa": [title] if space else []}),
        "stills": [f'incategory:"{subj["commons_category"]}"'] if subj.get("commons_category") else [title], "source": "viral_scan", "title_only": True, "max_per_source": 2, "wikipedia": subj["url"], "text_credit": TEXT_CREDIT,
    }


def viral_topics(viral_items: List[dict], avoid_keys: Optional[set] = None, recent_topics: Optional[List[str]] = None,
                 limit: int = 6, get: Callable = _get, log: Optional[List[str]] = None) -> List[dict]:
    """Viral uploads (best views/hour first) -> distinct footage topics, in viral order.
    A viral title naming a library subject uses the hand-checked library script; anything else is resolved
    to its Wikipedia subject. Never returns a topic that no viral title asked for."""
    log = log if log is not None else []
    avoid = avoid_keys or set()
    recent = " | ".join(recent_topics or []).lower()
    out, seen = [], set()
    items = sorted(viral_items or [], key=lambda it: -(float(it.get("views_per_hour") or it.get("score") or 0)))
    for it in items:
        if len(out) >= limit:
            break
        title = it.get("title") or ""
        vph = float(it.get("views_per_hour") or it.get("score") or 0)
        matched = {"title": title[:120], "url": it.get("url"), "views": it.get("views"), "views_per_hour": it.get("views_per_hour")}
        if SKIP_VIRAL.search(title) or (TALKING_HEAD.search(title) and
                                         not any(re.search(t["match"], title, re.I) for t in footage_topics.TOPICS)):
            log.append(f"viral skip (news/politics/talk/live/game/music): {title[:80]}")
            continue
        topic = next((t for t in footage_topics.TOPICS if re.search(t["match"], title, re.I)), None)
        reason = "viral_match_library_script"
        if topic is None:
            try:
                subs = wiki_subjects(title, get)
            except Exception as exc:
                log.append(f"viral resolve error {title[:60]}: {exc}"[:200])
                continue
            if not subs:
                log.append(f"viral skip (no named real-world subject): {title[:80]}")
                continue
            topics = []
            for subj in subs:
                ok, why = subject_ok(subj)
                if not ok:
                    log.append(f"viral skip ({why})")
                    continue
                t = topic_from_subject(subj)
                if not t:
                    log.append(f"viral skip (too little to say for 31 s): {subj['title']}")
                    continue
                topics.append(t)
            reason = "viral_subject"
        else:
            topics = [topic]
        for topic in topics:
            if topic["key"] in seen or topic["key"] in avoid or topic["topic"].lower() in recent:
                continue
            seen.add(topic["key"])
            out.append({"topic": topic, "score": round(vph, 1), "matched": [matched], "reason": reason})
    return out[:limit]


# --------------------------------------------------------------------------- 2. script / concept (templates, no LLM)

def build_concept(topic: dict, sources: List[dict], viral_pick: Optional[dict] = None) -> dict:
    orgs = []
    for s in sources:
        label = s.get("credit_short") or s.get("provider")
        if label and label not in orgs:
            orgs.append(label)
    credit = "Footage: " + "; ".join(orgs) if orgs else ""
    # posted captions keep 2 lines of <=110 chars (growth_v2.build_captions): the short Wikipedia text credit rides
    # on line 1 so it is never clipped off by a long footage credit (the article URL is in info["wikipedia"])
    first = list(topic.get("lines") or [])[:1]
    if topic.get("text_credit"):
        first = [" ".join(x.rstrip(".") + "." for x in first + [topic["text_credit"]])]
    lines = first + ([credit] if credit else [])
    base = [] if topic.get("source") == "viral_scan" else footage_topics.FOOTAGE_HASHTAGS
    tags = list(dict.fromkeys(topic.get("hashtags", []) + base))[:5]
    return {
        "topic": topic["topic"], "topic_key": topic["key"].replace("-", " "),
        "title_line": topic["title_line"], "on_screen_hook": topic["on_screen_hook"], "hook": topic["hook"],
        "lines": lines, "hashtags": tags,
        "linkedin_angle": "Real archival footage, re-edited for 9:16 and 16:9. No AI generation, no paid APIs.",
        "comment_question": topic.get("comment_question") or "Which moment in history should we cut next?",
        "vo": topic["vo"], "concept_source": "footage_template", "footage_credit": credit,
        "topic_source": topic.get("source") or "library", "wikipedia": topic.get("wikipedia"),
        "text_credit": topic.get("text_credit"),
        "viral_match": {"score": (viral_pick or {}).get("score"), "reason": (viral_pick or {}).get("reason"),
                        "matched": (viral_pick or {}).get("matched", [])[:5]},
    }


# --------------------------------------------------------------------------- 3. footage search (keyless archives)

_OK_LICENSE = re.compile(r"^(public domain|pd\b|pd-|cc0|cc[- ]by[- ]?\d|cc by \d|no restrictions)", re.I)
_BAD_LICENSE = re.compile(r"\b(sa|nc|nd)\b|share ?alike|noncommercial|no ?deriv|fair use|copyright", re.I)


def license_ok(name: str) -> bool:
    n = (name or "").strip()
    return bool(n and _OK_LICENSE.search(n) and not _BAD_LICENSE.search(n.replace("public domain", "")))


def search_nasa(query: str, media: str = "video", get: Callable = _get, limit: int = 12) -> List[dict]:
    data = get("https://images-api.nasa.gov/search", {"q": query, "media_type": media}).json()
    out = []
    for item in (data.get("collection") or {}).get("items", [])[:limit]:
        d = (item.get("data") or [{}])[0]
        nid = d.get("nasa_id")
        if not nid:
            continue
        out.append({"provider": "nasa", "kind": "video" if media == "video" else "image", "id": nid,
                    "title": d.get("title") or nid, "description": _strip_html(d.get("description"))[:400],
                    "page": f"https://images.nasa.gov/details/{nid}", "collection": item.get("href"),
                    "author": d.get("photographer") or d.get("center") or "NASA",
                    "license": "Public domain (NASA media; not copyrighted in the US, no endorsement implied)",
                    "license_url": "https://www.nasa.gov/nasa-brand-center/images-and-media/",
                    "credit_short": "NASA (public domain)", "date": d.get("date_created")})
    return out


def resolve_nasa(c: dict, get: Callable = _get) -> Optional[str]:
    files = get(c["collection"].replace(" ", "%20")).json()
    files = [f.replace("http://", "https://") for f in files]
    if c["kind"] == "video":
        for suffix in ("~large.mp4", "~orig.mp4", "~medium.mp4", "~mobile.mp4"):
            hit = [f for f in files if f.endswith(suffix)]
            if hit:
                return hit[0]
    else:
        for suffix in ("~orig.jpg", "~large.jpg", "~orig.png", "~medium.jpg"):
            hit = [f for f in files if f.endswith(suffix)]
            if hit:
                return hit[0]
    return None


def search_commons(query: str, media: str = "video", get: Callable = _get, limit: int = 15) -> List[dict]:
    kind = "filetype:video" if media == "video" else "filetype:bitmap"
    data = get("https://commons.wikimedia.org/w/api.php", {
        "action": "query", "format": "json", "generator": "search", "gsrsearch": f"{kind} {query}",
        "gsrnamespace": 6, "gsrlimit": limit, "prop": "imageinfo",
        "iiprop": "url|size|mediatype|extmetadata",
        "iiextmetadatafilter": "LicenseShortName|LicenseUrl|Artist|ImageDescription|ObjectName|Credit"}).json()
    out = []
    for p in sorted((data.get("query") or {}).get("pages", {}).values(), key=lambda p: p.get("index", 0)):
        ii = (p.get("imageinfo") or [{}])[0]
        m = ii.get("extmetadata") or {}
        lic = (m.get("LicenseShortName") or {}).get("value") or ""
        if not license_ok(lic):
            continue
        artist = _strip_html((m.get("Artist") or {}).get("value")) or _strip_html((m.get("Credit") or {}).get("value")) or "Unknown"
        url = (ii.get("url") or "").split("?")[0]
        is_video = url.lower().endswith(VIDEO_EXT)
        if (media == "video") != is_video:
            continue
        cc_by = lic.lower().startswith("cc")
        out.append({"provider": "wikimedia_commons", "kind": "video" if is_video else "image", "id": p.get("title"),
                    "title": p.get("title", "").replace("File:", ""),
                    "description": _strip_html((m.get("ImageDescription") or {}).get("value"))[:400],
                    "page": ii.get("descriptionurl") or f"https://commons.wikimedia.org/wiki/{p.get('title', '').replace(' ', '_')}",
                    "url": url, "width": ii.get("width"), "height": ii.get("height"), "duration": ii.get("duration"),
                    "author": artist[:120], "license": lic,
                    "license_url": (m.get("LicenseUrl") or {}).get("value") or "",
                    "credit_short": (f"{artist[:40]} / Wikimedia Commons ({lic})" if cc_by else "Wikimedia Commons (public domain)")})
    return out


# Internet Archive collections whose films are public domain: Prelinger (per the Archive), Universal Newsreels
# (donated to the US National Archives, public domain), FedFlix (US government works, public domain).
PD_ARCHIVE_COLLECTIONS = {"prelinger": "Prelinger Archives", "universal_newsreels": "Universal Newsreels via National Archives",
                          "fedflix": "FedFlix, US government work"}


def search_archive(query: str, get: Callable = _get, limit: int = 8) -> List[dict]:
    q = (f'(title:({query}) OR subject:({query}) OR description:({query})) AND mediatype:movies AND '
         f'collection:({" OR ".join(PD_ARCHIVE_COLLECTIONS)})')
    data = get("https://archive.org/advancedsearch.php", {"q": q, "fl[]": ["identifier", "title", "licenseurl", "collection", "description"],
                                                         "rows": limit, "output": "json"}).json()
    out = []
    for d in (data.get("response") or {}).get("docs", []):
        ident = d.get("identifier")
        coll = d.get("collection") or []
        coll = coll if isinstance(coll, list) else [coll]
        lic_url = d.get("licenseurl") or ""
        # Uploader-asserted licenseurl values are NOT trusted (users mark studio films 'public domain');
        # only curated public-domain collections are used.
        pd_coll = [c for c in coll if c.lower() in PD_ARCHIVE_COLLECTIONS]
        if not pd_coll:
            continue
        lic = f"Public domain ({PD_ARCHIVE_COLLECTIONS[pd_coll[0].lower()]})"
        desc = d.get("description")
        desc = " ".join(desc) if isinstance(desc, list) else (desc or "")
        out.append({"provider": "internet_archive", "kind": "video", "id": ident, "title": d.get("title") or ident,
                    "description": _strip_html(desc)[:400], "page": f"https://archive.org/details/{ident}",
                    "author": "Internet Archive" + (f" / {PD_ARCHIVE_COLLECTIONS[pd_coll[0].lower()]}" if pd_coll else ""),
                    "license": lic, "license_url": lic_url or "https://archive.org/details/prelinger",
                    "credit_short": "Internet Archive (public domain)"})
    return out


def resolve_archive(c: dict, get: Callable = _get) -> Optional[str]:
    meta = get(f"https://archive.org/metadata/{c['id']}").json()
    best = None
    for f in meta.get("files", []):
        name, fmt = f.get("name", ""), (f.get("format") or "").lower()
        if not name.lower().endswith((".mp4", ".mpeg", ".mpg", ".ogv", ".mov")):
            continue
        h = int(f.get("height") or 0)
        size = int(f.get("size") or 0)
        rank = (("h.264" in fmt and "hd" in fmt) * 3 + ("h.264" in fmt) * 2 + ("mpeg4" in fmt) + h / 2000.0
                - (size > 2_500_000_000) * 5)
        if best is None or rank > best[0]:
            best = (rank, name, h)
    if not best:
        return None
    c["height"] = best[2] or c.get("height")
    return f"https://archive.org/download/{c['id']}/{requests.utils.quote(best[1])}"


def search_pexels(query: str, get: Callable = _get) -> List[dict]:  # UNVERIFIED: needs PEXELS_API_KEY
    key = (os.environ.get("PEXELS_API_KEY") or "").strip()
    if not key:
        return []
    data = get("https://api.pexels.com/videos/search", {"query": query, "orientation": "landscape", "per_page": 10},
               headers={"Authorization": key}).json()
    out = []
    for v in data.get("videos", []):
        files = sorted([f for f in v.get("video_files", []) if (f.get("height") or 0) <= 1080],
                       key=lambda f: f.get("height") or 0, reverse=True)
        if files:
            out.append({"provider": "pexels", "kind": "video", "id": v.get("id"), "title": v.get("url", ""),
                        "description": "", "page": v.get("url"), "url": files[0]["link"], "width": files[0].get("width"),
                        "height": files[0].get("height"), "duration": v.get("duration"),
                        "author": (v.get("user") or {}).get("name") or "Pexels", "license": "Pexels License",
                        "license_url": "https://www.pexels.com/license/", "credit_short": "Pexels"})
    return out


def search_pixabay(query: str, get: Callable = _get) -> List[dict]:  # UNVERIFIED: needs PIXABAY_API_KEY
    key = (os.environ.get("PIXABAY_API_KEY") or "").strip()
    if not key:
        return []
    data = get("https://pixabay.com/api/videos/", {"key": key, "q": query, "per_page": 10}).json()
    out = []
    for v in data.get("hits", []):
        f = (v.get("videos") or {}).get("large") or (v.get("videos") or {}).get("medium") or {}
        if f.get("url"):
            out.append({"provider": "pixabay", "kind": "video", "id": v.get("id"), "title": v.get("tags", ""),
                        "description": "", "page": v.get("pageURL"), "url": f["url"], "width": f.get("width"),
                        "height": f.get("height"), "duration": v.get("duration"), "author": v.get("user") or "Pixabay",
                        "license": "Pixabay Content License", "license_url": "https://pixabay.com/service/license-summary/",
                        "credit_short": "Pixabay"})
    return out


def topic_year(topic: dict) -> Optional[int]:
    """Year the topic is about (from its on-screen hook), so modern clips can't stand in for archival footage."""
    m = re.search(r"\b(1[89]\d\d|20\d\d)\b", topic.get("on_screen_hook") or topic.get("hook") or "")
    return int(m.group(1)) if m else None


def screen_credit(credit: str, width: int, size: int) -> Tuple[str, int]:
    """On-screen credit that always fits the frame: merge repeated '(public domain)' and wrap/shrink like captions."""
    if not credit:
        return "", size
    body = credit[len("Footage: "):] if credit.startswith("Footage: ") else credit
    parts = [x.strip() for x in body.split(";") if x.strip()]
    pd = [x[:-len(" (public domain)")] for x in parts if x.endswith(" (public domain)")]
    other = [x for x in parts if not x.endswith(" (public domain)")]
    segs = ([", ".join(pd) + " (public domain)"] if pd else []) + other
    return fit_caption("Footage: " + "; ".join(segs), width, size)


def gather_candidates(topic: dict, get: Callable = _get) -> Tuple[List[dict], List[str]]:
    """Search every free source for the topic. Never raises; returns (candidates, errors)."""
    qs = topic.get("queries") or {}
    cands: List[dict] = []
    errors: List[str] = []
    jobs: List[Tuple[str, Callable[[], List[dict]]]] = []
    for q in qs.get("nasa", []):
        jobs.append((f"nasa video {q}", lambda q=q: search_nasa(q, "video", get)))
    for q in qs.get("commons", []):
        jobs.append((f"commons video {q}", lambda q=q: search_commons(q, "video", get)))
    for q in qs.get("archive", []):
        jobs.append((f"archive {q}", lambda q=q: search_archive(q, get)))
    for cat in qs.get("commons_cat", []):  # files filed directly in the subject's Commons category are on-topic
        jobs.append((f"commons category {cat}", lambda cat=cat: [dict(c, curated=True) for c in
                                                                 search_commons(f'incategory:"{cat}"', "video", get)]))
        jobs.append((f"commons deepcat {cat}", lambda cat=cat: search_commons(f'deepcat:"{cat}"', "video", get, limit=40)))
    for q in (qs.get("nasa") or qs.get("commons") or [topic["topic"]])[:1]:
        jobs.append((f"pexels {q}", lambda q=q: search_pexels(q, get)))
        jobs.append((f"pixabay {q}", lambda q=q: search_pixabay(q, get)))
    # stills (Ken Burns) as a fallback pool
    for q in (topic.get("stills") or qs.get("commons") or [])[:2]:
        jobs.append((f"commons image {q}", lambda q=q: search_commons(q, "image", get)))
    for q in (qs.get("nasa") or [])[:1]:
        jobs.append((f"nasa image {q}", lambda q=q: search_nasa(q, "image", get)))
    for label, fn in jobs:
        try:
            cands += fn()
        except Exception as exc:
            errors.append(f"{label}: {exc}"[:200])
    req = re.compile(topic.get("require") or ".", re.I)
    excl = re.compile(topic["exclude"], re.I) if topic.get("exclude") else None
    era = topic_year(topic)
    seen, seen_titles, kept = set(), set(), []
    for c in cands:
        k = (c["provider"], c["id"])
        base = re.sub(r"\((?:cropped|edit|clean|hi-?res)[^)]*\)|\b(cropped|edit|clean|hires|enhanced)\b|\.\w{3,4}$", "",
                      (c.get("title") or "").lower())
        tkey = re.sub(r"[^a-z0-9]", "", base)[:28]
        if k in seen or tkey in seen_titles:
            continue
        seen.add(k)
        if excl and excl.search(c.get("title") or ""):
            continue
        if topic.get("title_only") and not c.get("curated") and re.search(r"\(\d{4}\)|\bby [A-Z][a-z]+ [A-Z]\.?", c.get("title") or ""):
            continue  # "Wolf Lowry (1917) by William S. Hart": a film or artwork that only shares the name
        if excl and topic.get("title_only") and excl.search(c.get("description") or ""):
            continue  # scanned subjects: the file page's description must not say it's a film, match, speech...
        seen_titles.add(tkey)
        text = f"{c.get('title')} {c.get('description')}"
        c["relevant"] = bool(c.get("curated") or req.search(text))
        c["title_match"] = bool(c.get("curated") or req.search(c.get("title") or ""))
        c["talking_head"] = bool(TALKING_HEAD.search(c.get("title") or ""))
        c["off_era"] = off_era(c.get("title") or "", era)
        if topic.get("title_only") and not c["title_match"]:  # scanned subjects: the clip itself must be named for it
            continue
        if c["relevant"] and not c["talking_head"] and not c["off_era"]:
            kept.append(c)
    if sum(1 for c in kept if c["title_match"]) >= 2:  # enough on-topic titles: drop description-only matches
        kept = [c for c in kept if c["title_match"]]
    kept.sort(key=lambda c: (c["kind"] != "video", not c["title_match"], not str(c.get("license", "")).lower().startswith("public"),
                             c["provider"] not in ("nasa", "pexels", "pixabay", "wikimedia_commons"), -(c.get("height") or 0)))
    return kept, errors


# --------------------------------------------------------------------------- 3b. segment selection

FRAME_W, FRAME_H = 160, 90


def _gray_frames(src: str, start: float, seconds: float = 2.4, fps: float = 3.0, w: int = FRAME_W, h: int = FRAME_H,
                 crop: Optional[str] = None) -> List[bytes]:
    vf = (f"crop={crop}," if crop else "") + f"fps={fps},scale={w}:{h},format=gray"
    data = b""
    for attempt in range(2):
        res = subprocess.run([_bin("ffmpeg"), "-v", "error", *_ua(src), "-ss", f"{start:.2f}", "-i", src, "-t", f"{seconds:.2f}",
                              "-vf", vf, "-f", "rawvideo", "-"], capture_output=True, timeout=120)
        data = res.stdout or b""
        if data or b"429" not in (res.stderr or b""):
            break
        time.sleep(4)
    n = w * h
    return [data[i:i + n] for i in range(0, len(data) - n + 1, n)]


def segment_score(frames: List[bytes], w: int = FRAME_W, h: int = FRAME_H) -> Optional[float]:
    """Higher = better b-roll. None = reject: black/flat/slate, a hard cut inside the shot, frozen,
    soft/out-of-focus, or dominated by a dark blob (silhouette close-ups)."""
    import numpy as np
    if len(frames) < 4:
        return None
    arr = np.stack([np.frombuffer(f, dtype=np.uint8).reshape(h, w).astype(np.float32) for f in frames])
    means = arr.mean(axis=(1, 2))
    stds = arr.std(axis=(1, 2))
    diffs = np.abs(np.diff(arr, axis=0)).mean(axis=(1, 2))
    sharp = (np.abs(np.diff(arr, axis=2)).mean(axis=(1, 2)) + np.abs(np.diff(arr, axis=1)).mean(axis=(1, 2))) / 2
    dark = (arr < 30).mean(axis=(1, 2))
    clipped = ((arr > 238).mean(axis=(1, 2)) + (arr < 14).mean(axis=(1, 2)))
    if means.min() < 32 or means.max() > 220 or stds.min() < 24 or clipped.max() > 0.30:
        return None
    if diffs.max() > 26:  # a hard scene cut inside the shot
        return None
    motion = float(diffs.mean())
    if motion < 0.6:  # frozen frame / title card
        return None
    if sharp.mean() < 3.2 or dark.max() > 0.42:
        return None
    hb, wb = h // 10, w // 10
    blocks = arr[:, :hb * 10, :wb * 10].reshape(len(arr), hb, 10, wb, 10).std(axis=(2, 4))
    flat = float((blocks < 5.0).mean())  # empty sky / sea / walls
    mid = float(((arr > 50) & (arr < 200)).mean())  # tonal range: soot-and-chalk dupes score low
    if mid < 0.45:
        return None
    return round(float(stds.mean()) / 8.0 + min(motion, 12.0) + float(sharp.mean()) * 1.5 - max(0.0, motion - 20.0)
                 - float(clipped.mean()) * 20.0 + (mid - 0.75) * 20.0 - max(0.0, flat - 0.30) * 30.0, 2)


def bars_from_frames(arr, thr: float = 48.0) -> Tuple[float, float, float, float]:
    """Fractions (left, right, top, bottom) of near-black, static bars baked into the picture."""
    import numpy as np
    col = arr.mean(axis=(0, 1))
    col_sd = arr.std(axis=(0, 1))
    row = arr.mean(axis=(0, 2))
    row_sd = arr.std(axis=(0, 2))

    def run(values, sds):
        n = 0
        for v, sd in zip(values, sds):
            if v < thr and sd < 14:
                n += 1
            else:
                break
        return n
    w, h = arr.shape[2], arr.shape[1]
    return (run(col, col_sd) / w, run(col[::-1], col_sd[::-1]) / w, run(row, row_sd) / h, run(row[::-1], row_sd[::-1]) / h)


def detect_crop(src: str, duration: float, width: int, height: int, samples: int = 7) -> Optional[str]:
    """Remove baked-in letterbox/pillarbox bars (common in 4:3 archival masters stored as 16:9).
    Measured from frames spread across the file, so dark scenes don't fool it."""
    import numpy as np
    if not width or not height or duration < 3:
        return None
    frames = []
    for k in range(samples):
        t = duration * (0.15 + 0.7 * k / max(1, samples - 1))
        frames += _gray_frames(src, t, seconds=0.4, fps=5.0, w=320, h=180)[:1]
    if len(frames) < 3:
        return None
    arr = np.stack([np.frombuffer(f, dtype=np.uint8).reshape(180, 320).astype(np.float32) for f in frames])
    left, right, top, bottom = bars_from_frames(arr)
    if max(left, right, top, bottom) < 0.02:
        return None
    pad = 0.012  # eat soft bar edges
    x0 = int(width * (left + pad if left else 0))
    x1 = int(width * (1 - (right + pad if right else 0)))
    y0 = int(height * (top + pad if top else 0))
    y1 = int(height * (1 - (bottom + pad if bottom else 0)))
    w, h = (x1 - x0) // 2 * 2, (y1 - y0) // 2 * 2
    if w < width * 0.4 or h < height * 0.4:
        return None
    return f"{w}:{h}:{x0}:{y0}"


def choose_segments(src: str, duration: float, need: int, shot_len: float, max_probes: int = 8,
                    crop: Optional[str] = None) -> List[Tuple[float, float]]:
    if duration < shot_len + 0.5:
        return []
    lo, hi = (duration * 0.08, duration * 0.92 - shot_len) if duration > 40 else (0.2, duration - shot_len - 0.1)
    if hi <= lo:
        lo, hi = 0.0, max(0.0, duration - shot_len - 0.05)
    n = max(need, min(max(max_probes, need + 4), int((hi - lo) / max(shot_len, 1)) + 1))
    points = [lo + (hi - lo) * (i + 0.5) / n for i in range(n)]
    scored = []
    for t in points:
        s = segment_score(_gray_frames(src, t, seconds=min(shot_len, 2.6), crop=crop))
        if s is not None:
            scored.append((s, t))
    scored.sort(reverse=True)
    picked: List[Tuple[float, float]] = []
    for s, t in scored:
        if all(abs(t - p[1]) > max(6.0, shot_len * 1.2) for p in picked):
            picked.append((s, t))
        if len(picked) >= need:
            break
    return [(t, s) for s, t in picked]


# --------------------------------------------------------------------------- 4. music / beats

def load_music(mood: Optional[str] = None) -> dict:
    with open(os.path.join(MUSIC_DIR, "manifest.json")) as fh:
        tracks = json.load(fh)
    pick = next((t for t in tracks if t["mood"] == mood), tracks[0])
    pick = dict(pick)
    pick["path"] = os.path.join(MUSIC_DIR, pick["file"])
    return pick


def loop_seconds(music: dict) -> float:
    """Length of the bar-aligned loop unit (a whole number of 4-beat bars) used to extend a bed."""
    beats = music.get("beats") or []
    n4 = (len(beats) - 1) // 4 * 4
    if n4 >= 4:
        return float(beats[n4])
    return float(music.get("seconds") or 12.0)


def music_beats(music: dict, total: float) -> List[float]:
    """Beat grid covering `total` seconds. Long beds use their own analysed grid; short excerpts are looped
    on a bar boundary, so the grid repeats every loop_seconds()."""
    beats = [float(b) for b in music.get("beats") or []]
    if not beats or beats[-1] >= total - 0.5 or music.get("loop") is False:
        return beats
    unit = loop_seconds(music)
    base = [b for b in beats if b < unit - 1e-6]
    out: List[float] = []
    k = 0
    while k * unit < total:
        out += [round(b + k * unit, 3) for b in base]
        k += 1
    return [b for b in out if b <= total]


def beat_cuts(beats: List[float], shots: int = 3, total: float = 8.0, min_len: float = 1.8,
              max_len: Optional[float] = None) -> List[float]:
    """Cut points snapped to the nearest beat around even divisions of the clip (no shot shorter than
    min_len or, when given, longer than max_len)."""
    cuts: List[float] = []
    for i in range(1, shots):
        target = total * i / shots
        prev = cuts[-1] if cuts else 0.0
        options = [b for b in beats if b - prev >= min_len and total - b >= min_len * (shots - i)
                   and (max_len is None or (b - prev <= max_len and total - b <= max_len * (shots - i)))]
        cut = min(options, key=lambda b: abs(b - target)) if options else target
        cuts.append(round(cut, 3))
    return cuts


def shot_durations(beats: List[float], shots: int, total: float) -> Tuple[List[float], List[float]]:
    """(cuts, durations) for `shots` beat-snapped shots that sum exactly to `total`."""
    cuts = beat_cuts(beats, shots, total, min_len=MIN_SHOT_SECONDS, max_len=MAX_SHOT_SECONDS)
    bounds = [0.0] + cuts + [total]
    return cuts, [round(bounds[i + 1] - bounds[i], 3) for i in range(len(bounds) - 1)]


# --------------------------------------------------------------------------- 4b. voiceover (Kokoro, local)

_KOKORO = None


def _kokoro_files() -> Tuple[str, str]:
    model = os.environ.get("KOKORO_MODEL") or ""
    voices = os.environ.get("KOKORO_VOICES") or ""
    if model and voices and os.path.exists(model) and os.path.exists(voices):
        return model, voices
    base = os.path.join(os.environ.get("STATE_DIR") or os.environ.get("DATA_DIR") or tempfile.gettempdir(), "kokoro")
    os.makedirs(base, exist_ok=True)
    rel = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
    files = {"kokoro-v1.0.int8.onnx": os.path.join(base, "kokoro-v1.0.int8.onnx"),
             "voices-v1.0.bin": os.path.join(base, "voices-v1.0.bin")}
    for name, dest in files.items():
        if not os.path.exists(dest) or os.path.getsize(dest) < 1_000_000:
            logger.info("%s downloading free Kokoro model file %s", MARKER, name)
            with requests.get(rel + name, stream=True, timeout=300) as r:
                r.raise_for_status()
                tmp = dest + ".part"
                with open(tmp, "wb") as fh:
                    for chunk in r.iter_content(1 << 20):
                        fh.write(chunk)
                os.replace(tmp, dest)
    return files["kokoro-v1.0.int8.onnx"], files["voices-v1.0.bin"]


def _kokoro():
    global _KOKORO
    if _KOKORO is None:
        from kokoro_onnx import Kokoro  # free, local ONNX TTS (Apache-2.0 model)
        model, voices = _kokoro_files()
        _KOKORO = Kokoro(model, voices)
    return _KOKORO


def synth_vo(lines: List[dict], workdir: str, max_total: float = MAX_SECONDS - VO_TAIL) -> Tuple[Optional[str], List[dict], dict]:
    """Returns (wav_path or None, timed captions [{text,start,end}], info). Never raises.
    One natural, unhurried speed (VO_SPEED): if the script runs past max_total, trailing lines are dropped,
    the read is never sped up."""
    try:
        import wave
        import numpy as np
        k = _kokoro()
        voice = os.environ.get("FOOTAGE_VOICE") or "am_michael:0.6,am_onyx:0.4"
        style = None
        for part in voice.split(","):
            name, _, w = part.partition(":")
            vec = k.get_voice_style(name.strip()) * float(w or 1.0)
            style = vec if style is None else style + vec
        speed = VO_SPEED
        clips, sr = [], 24000
        for ln in lines:
            audio, sr = k.create(ln["say"], voice=style, speed=speed, lang="en-us")
            idx = np.where(np.abs(audio) > 0.01)[0]
            if len(idx):
                audio = audio[max(0, idx[0] - int(0.02 * sr)): idx[-1] + int(0.06 * sr)]
            clips.append(audio)
        gap = VO_GAP
        start = VO_LEAD
        # drop trailing lines rather than rush the read
        while len(clips) > 1 and start + sum(len(c) for c in clips) / sr + gap * (len(clips) - 1) > max_total:
            clips.pop()
            lines = lines[:len(clips)]
        caps, parts = [], []
        silence = np.zeros(int(gap * sr), dtype=np.float32)
        lead = np.zeros(int(start * sr), dtype=np.float32)
        parts.append(lead)
        t = start
        for ln, c in zip(lines, clips):
            d = len(c) / sr
            pieces = ln.get("parts") or [ln["show"]]
            total_chars = sum(len(x) for x in pieces) or 1
            pt = t
            for j, piece in enumerate(pieces):  # long sentences: caption pieces timed by length
                pd = d * len(piece) / total_chars
                caps.append({"text": piece, "start": round(pt, 3),
                             "end": round(pt + pd + (0.1 if j == len(pieces) - 1 else 0.0), 3)})
                pt += pd
            parts += [c.astype(np.float32), silence]
            t += d + gap
        wav = os.path.join(workdir, "vo.wav")
        pcm = (np.clip(np.concatenate(parts), -1.0, 1.0) * 32767).astype("<i2").tobytes()
        with wave.open(wav, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(pcm)
        spoken_end = round(t - gap, 2)
        return wav, caps, {"tts": "kokoro-onnx", "voice": voice, "speed": speed, "seconds": spoken_end,
                           "lines_used": len(lines)}
    except Exception as exc:
        logger.warning("%s voiceover unavailable (%s); captions only", MARKER, exc)
        caps, t = [], 0.4
        for ln in lines:
            if t + 4.2 > max_total:
                break
            caps.append({"text": ln["show"], "start": round(t, 2), "end": round(t + 4.0, 2)})
            t += 4.4
        return None, caps, {"tts": "none", "error": str(exc)[:200], "seconds": round(t, 2)}


# --------------------------------------------------------------------------- 5. edit / render

GRADE = ("eq=contrast=1.07:saturation=0.80:gamma=1.03:brightness=0.01,"
         "colorbalance=rs=-0.03:gs=0.0:bs=0.035:rh=0.03:gh=0.01:bh=-0.025,"
         "vignette=angle=PI/7")


def focus_x(path: str, kind: str, out_aspect: float = 9 / 16, samples: int = 6) -> float:
    """Horizontal centre (0..1) of the action for a portrait crop: the crop-width window with the most
    edge + motion energy, measured on frames across the shot. 0.5 if it can't be measured."""
    import numpy as np
    try:
        meta = probe(path)
        w, h = meta["width"], meta["height"]
        if not w or not h:
            return 0.5
        dur = max(0.5, float(meta["duration"] or 1.0)) if kind != "image" else 0.5
        frames = _gray_frames(path, 0.0, seconds=dur, fps=samples / dur, w=FRAME_W, h=FRAME_H)
        if not frames:
            return 0.5
        arr = np.stack([np.frombuffer(f, dtype=np.uint8).reshape(FRAME_H, FRAME_W).astype(np.float32) for f in frames])
        edge = np.abs(np.diff(arr, axis=2)).mean(axis=(0, 1))
        edge = np.append(edge, edge[-1])
        motion = np.abs(np.diff(arr, axis=0)).mean(axis=(0, 1)) if len(arr) > 1 else np.zeros(FRAME_W)
        energy = edge + 1.5 * motion
        win = max(4, int(round(FRAME_W * min(1.0, (h * out_aspect) / w))))
        if win >= FRAME_W:
            return 0.5
        sums = np.convolve(energy, np.ones(win), mode="valid")
        centre = (int(np.argmax(sums)) + win / 2) / FRAME_W
        return float(min(max(0.5 + (centre - 0.5) * 0.85, 0.0), 1.0))  # bias slightly toward the middle
    except Exception:
        return 0.5


def _shot_filter(kind: str, w: int, h: int, dur: float, idx: int, focus: float = 0.5) -> str:
    frames = int(round(dur * FPS))
    portrait = h > w
    if kind == "image":  # Ken Burns ONLY on stills: slow push-in, alternating drift
        z = "min(1.0+0.0009*on,1.14)"
        cx = f"(iw*{focus:.3f})" if portrait else "(iw/2)"
        x = f"max(0,min(iw-iw/zoom,{cx}-(iw/zoom/2)" + ("+on*0.6" if idx % 2 else "-on*0.6") + "))"
        fill = (f"scale=-2:{h * 3}:flags=lanczos" if portrait else
                f"scale={w * 3}:{h * 3}:force_original_aspect_ratio=increase:flags=lanczos,crop={w * 3}:{h * 3}")
        if portrait:  # crop the still to a wide-enough strip around the focus, then zoompan inside it
            fill += f",crop='min(iw,{w * 3 * 1.6:.0f})':{h * 3}:'max(0,min(iw-ow,iw*{focus:.3f}-ow/2))':0"
            x = "iw/2-(iw/zoom/2)" + ("+on*0.6" if idx % 2 else "-on*0.6")
        return f"{fill},zoompan=z='{z}':x='{x}':y='ih/2-(ih/zoom/2)':d={frames}:s={w}x{h}:fps={FPS},setsar=1"
    clean = "hqdn3d=1.5:1.5:4:4,unsharp=5:5:0.55:5:5:0.0"
    if not portrait:
        return (f"fps={FPS},scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h},setsar=1,{clean}")
    # Full-bleed 9:16 pan-and-scan: denoise at source size, fill the height with 8% overscan, then a slow
    # pan (alternating direction) across the action's centre. No blurred bars, no letterbox.
    over = int(round(h * 1.08 / 2) * 2)
    pan = 0.10  # fraction of the scaled width travelled over the shot
    d = 1 if idx % 2 == 0 else -1
    x = (f"max(0,min(iw-ow,iw*{focus:.3f}-ow/2+{d}*iw*{pan}*(t/{max(dur, 0.1):.3f}-0.5)))")
    return (f"fps={FPS},{clean},scale=-2:{over}:flags=lanczos,crop={w}:{h}:x='{x}':y='(ih-oh)/2',"
            f"unsharp=5:5:0.35:5:5:0.0,setsar=1")


def _textfile(workdir: str, text: str) -> str:
    path = os.path.join(workdir, f"t_{uuid.uuid4().hex[:8]}.txt")
    with open(path, "w") as fh:
        fh.write(text)
    return path


def _wrap(text: str, width: int) -> str:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return "\n".join(lines)


def fit_caption(text: str, width: int, size: int) -> Tuple[str, int]:
    """One line when it nearly fits (font shrinks up to ~22%); otherwise two balanced lines, never an orphan word."""
    text = " ".join(text.split())
    if len(text) <= width:
        return text, size
    if len(text) <= int(width * 1.28):
        return text, int(size * width / len(text))
    words = text.split()
    best = None
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        score = max(len(a), len(b))
        if best is None or score < best[0]:
            best = (score, f"{a}\n{b}")
    if best[0] > width * 1.15 and len(words) >= 6:  # a long complete sentence: three balanced lines, larger type
        for i in range(1, len(words) - 1):
            for j in range(i + 1, len(words)):
                a, b, c = " ".join(words[:i]), " ".join(words[i:j]), " ".join(words[j:])
                score = max(len(a), len(b), len(c))
                if score < best[0]:
                    best = (score, f"{a}\n{b}\n{c}")
    return best[1], size if best[0] <= width else int(size * width / best[0])


def _draw(workdir: str, text: str, font: str, size: int, y: str, start: float, end: float, alpha: float = 1.0,
          border: int = 4, fade: float = 0.18, plate: bool = False) -> str:
    tf = _textfile(workdir, text).replace(":", r"\:")
    a = (f"if(lt(t,{start}),0,if(lt(t,{start + fade}),(t-{start})/{fade},if(lt(t,{end - fade}),1,"
         f"if(lt(t,{end}),({end}-t)/{fade},0))))")
    return (f"drawtext=fontfile='{font}':textfile='{tf}':fontsize={size}:fontcolor=white:line_spacing={int(size * 0.18)}:"
            f"borderw={border}:bordercolor=black@0.85:shadowx=0:shadowy=3:shadowcolor=black@0.6:"
            + (f"box=1:boxcolor=black@0.38:boxborderw={int(size * 0.32)}:" if plate else "")
            + f"x=(w-text_w)/2:y={y}:alpha='{a}*{alpha}':enable='between(t,{start},{end})'")


def render(shots: List[dict], vo_wav: Optional[str], captions: List[dict], music: dict, hook: str, credit: str,
           out_path: str, orientation: str, workdir: str) -> dict:
    """shots: [{path, kind, start, dur}] already local. Writes a CapCut/Meta-safe MP4 exactly sum(dur) long.
    Short music beds are looped on a bar boundary so the bed covers the whole clip."""
    total = round(sum(float(s["dur"]) for s in shots), 3)
    font = _font(True)
    vertical = orientation == "vertical"
    W, H = (1080, 1920) if vertical else (1920, 1080)
    bw, bh = W, H  # full-bleed in both orientations (9:16 is a pan-and-scan of the action, never padded)
    cmd = [_bin("ffmpeg"), "-y", "-v", "error"]
    fc = []
    for i, s in enumerate(shots):
        if s["kind"] == "image":
            cmd += ["-loop", "1", "-t", f"{s['dur'] + 0.2:.3f}", "-i", s["path"]]
        else:
            cmd += ["-ss", f"{s['start']:.3f}", "-t", f"{s['dur'] + 0.2:.3f}", "-i", s["path"]]
        focus = s.get("focus", 0.5) if vertical else 0.5
        # exact integer frame budget per shot (cumulative rounding), padded by cloning so a short source can't drop frames
        f0, f1 = round(sum(x["dur"] for x in shots[:i]) * FPS), round(sum(x["dur"] for x in shots[:i + 1]) * FPS)
        if i == len(shots) - 1:
            f1 = int(round(total * FPS))
        fc.append(f"[{i}:v]{_shot_filter(s['kind'], bw, bh, s['dur'], i, focus)},setpts=PTS-STARTPTS,"
                  f"tpad=stop_mode=clone:stop_duration=1,trim=end_frame={f1 - f0},setpts=N/({FPS}*TB)[s{i}]")
    n = len(shots)
    fc.append("".join(f"[s{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0,{GRADE},noise=alls=5:allf=t+u,format=yuv420p[prog]")
    fc.append("[prog]null[base]")
    if vertical:
        # inside TikTok/Reels/Shorts safe area: top ~10% and bottom ~25% carry platform UI
        hook_y, cap_y, cred_y = "210", "1250", "1420"
        hook_size, cap_size, cred_size, wrap_hook, wrap_cap = 74, 56, 26, 20, 26
    else:
        hook_y, cap_y, cred_y = "90", "h-310", "h-58"  # ~150 px clear between caption plate and credit
        hook_size, cap_size, cred_size, wrap_hook, wrap_cap = 78, 58, 24, 34, 44
    draws = [_draw(workdir, _wrap(hook, wrap_hook), font, hook_size, hook_y, 0.0, HOOK_SECONDS, border=5, fade=0.12)]
    for c in captions:
        text, size = fit_caption(c["text"], wrap_cap, cap_size)
        draws.append(_draw(workdir, text, font, size, cap_y, c["start"], min(c["end"], total), border=3, plate=True))
    if credit:
        ctext, csize = screen_credit(credit, int((1000 if vertical else 1800) / (cred_size * 0.52)), cred_size)
        if "\n" in ctext and not vertical:
            cred_y = f"h-{58 + int(csize * 1.2)}"
        draws.append(_draw(workdir, ctext, _font(False) or font, csize, cred_y, 0.0, total,
                           alpha=0.7, border=2, fade=0.01))
    fc.append(f"[base]{','.join(draws)},fade=t=out:st={total - 0.35}:d=0.35[v]")
    # audio: CC0 music bed (starts on a downbeat), ducked under the Kokoro VO, loudness-normalised
    mi = n
    cmd += ["-i", music["path"]]
    music_chain = f"[{mi}:a]aresample=44100,"
    if music.get("loop") is not False and (music.get("seconds") or 12.0) < total:
        unit = loop_seconds(music)
        music_chain += (f"atrim=0:{unit:.3f},asetpts=PTS-STARTPTS,afade=t=in:d=0.01,afade=t=out:st={unit - 0.015:.3f}:d=0.015,"
                        f"aloop=loop=-1:size={int(round(unit * 44100))},")
    music_chain += (f"atrim=0:{total},asetpts=PTS-STARTPTS,volume=0.9,"
                    f"afade=t=out:st={total - 1.2:.3f}:d=1.2")
    if vo_wav:
        cmd += ["-i", vo_wav]
        fc.append(f"[{mi + 1}:a]aresample=44100,highpass=f=70,acompressor=threshold=0.1:ratio=3:attack=5:release=80,"
                  f"volume=1.6,apad=whole_dur={total}[vo]")
        fc.append("[vo]asplit=2[vo1][vosc]")
        fc.append(f"{music_chain}[mus]")
        fc.append("[mus][vosc]sidechaincompress=threshold=0.04:ratio=7:attack=15:release=350:makeup=1[duck]")
        fc.append("[duck][vo1]amix=inputs=2:duration=first:normalize=0,loudnorm=I=-14:TP=-1.5:LRA=9,"
                  f"aresample=44100,atrim=0:{total}[a]")
    else:
        fc.append(f"{music_chain},loudnorm=I=-14:TP=-1.5:LRA=9,aresample=44100,atrim=0:{total}[a]")
    cmd += ["-filter_complex", ";".join(fc), "-map", "[v]", "-map", "[a]", "-t", f"{total}",
            "-r", str(FPS), "-c:v", "libx264", "-profile:v", "main", "-level:v", "4.0", "-pix_fmt", "yuv420p",
            "-preset", os.environ.get("FOOTAGE_X264_PRESET", "slow"), "-crf", "18", "-maxrate", "12M", "-bufsize", "24M",
            "-g", str(FPS * 2), "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
            "-movflags", "+faststart", out_path]
    _run(cmd, timeout=900)
    return probe(out_path)


def localize(c: dict, src: str, start: float, dur: float, workdir: str, idx: int) -> dict:
    """Cut just the needed seconds (HTTP range reads) into a local mezzanine; stills are downloaded."""
    if c["kind"] == "image":
        ext = os.path.splitext(src.split("?")[0])[1] or ".jpg"
        dest = os.path.join(workdir, f"still{idx}{ext}")
        with _get(src, timeout=120) as r, open(dest, "wb") as fh:
            fh.write(r.content)
        flat = os.path.join(workdir, f"still{idx}.png")
        _run([_bin("ffmpeg"), "-y", "-v", "error", "-i", dest, "-vf", "scale='min(3840,iw)':-2,format=rgb24", "-frames:v", "1", flat])
        return {"path": flat, "kind": "image", "start": 0.0, "dur": dur}
    dest = os.path.join(workdir, f"shot{idx}.mp4")
    vf = ["-vf", f"crop={c['crop']}"] if c.get("crop") else []
    err = None
    for attempt in range(3):  # Wikimedia/Archive rate-limit (429) or drop long range reads: back off and retry
        try:
            _run([_bin("ffmpeg"), "-y", "-v", "error", *_ua(src), "-ss", f"{start:.3f}", "-i", src, "-t", f"{dur + 0.4:.3f}",
                  *vf, "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "12", "-pix_fmt", "yuv420p", dest], timeout=600)
            got = probe(dest).get("duration") or 0.0
            if got >= dur - 0.05:
                return {"path": dest, "kind": "video", "start": 0.0, "dur": dur}
            err = RuntimeError(f"cut too short ({got:.2f}s < {dur:.2f}s)")
        except Exception as exc:
            err = exc
        time.sleep(3 * (attempt + 1))
    raise err


def pinned_segments(src: str, duration: float, near: List[float], shot_len: float, crop: Optional[str]) -> List[Tuple[float, float]]:
    """Hand-picked moments from the topic library: refine each within +/-3 s with the same quality gate."""
    out = []
    for t0 in near:
        best = None
        for t in (t0, t0 - 3.0, t0 + 3.0):
            if t < 0 or t + shot_len > duration:
                continue
            sc = segment_score(_gray_frames(src, t, seconds=min(shot_len, 2.6), crop=crop))
            if sc is not None and (best is None or sc > best[1]):
                best = (t, sc)
        if best is None and t0 + shot_len <= duration:
            best = (t0, 0.0)  # hand-checked moment: keep it even if a probe was rate-limited or the gate is strict
        if best:
            out.append(best)
    return out


def plan_shots(cands: List[dict], need: int, workdir: str, log: List[str],
               resolve_nasa_fn=resolve_nasa, resolve_archive_fn=resolve_archive,
               segment_fn=None, probe_fn=None, crop_fn=None, pins: Optional[Dict[str, List[float]]] = None,
               seg_len: float = SEGMENT_SECONDS, min_need: Optional[int] = None,
               localize_fn=None, max_per_source: Optional[int] = None) -> List[Tuple[dict, dict]]:
    """Probe up to FOOTAGE_MAX_SOURCES relevant videos, score every candidate segment (exposure, contrast,
    motion, sharpness, no cuts inside), weight by source resolution, then take the best segments globally
    (max 2 per source while others are available, then more per source). Stills (Ken Burns) only fill what
    video can't. Every shot is cut `seg_len` long (the edit trims it to its beat-snapped length).
    Returns up to `need` shots; raises if fewer than `min_need` (default: need) are usable."""
    segment_fn = segment_fn or choose_segments
    probe_fn = probe_fn or probe
    crop_fn = crop_fn or detect_crop
    localize_fn = localize_fn or localize
    min_need = need if min_need is None else min_need
    durs = [seg_len] * need
    pool: List[Tuple[float, dict, str, float]] = []  # (score, cand, src, start)
    stills: List[Tuple[dict, str]] = []
    probed = 0
    max_sources = int(os.environ.get("FOOTAGE_MAX_SOURCES", 6))
    for c in cands:
        if probed >= max_sources and (pool or len(stills) >= need):
            break
        try:
            if c["kind"] == "image":
                if len(stills) < need * 2:
                    src = resolve_nasa_fn(c) if c["provider"] == "nasa" else c.get("url")
                    if src:
                        c["file_url"] = src
                        stills.append((c, src))
                continue
            if probed >= max_sources:
                continue
            src = c.get("url")
            if c["provider"] == "nasa":
                src = resolve_nasa_fn(c)
            elif c["provider"] == "internet_archive":
                src = resolve_archive_fn(c)
            if not src:
                continue
            probed += 1
            c["file_url"] = src
            meta = probe_fn(src)
            c.update({"width": meta["width"] or c.get("width"), "height": meta["height"] or c.get("height"),
                      "duration": meta["duration"] or c.get("duration")})
            height = int(c.get("height") or 0)
            if height < 300:
                log.append(f"skip low-res {c['provider']} {c['title'][:60]} ({height}p)")
                continue
            dur_s = float(c.get("duration") or 0)
            c["crop"] = crop_fn(src, dur_s, int(c.get("width") or 0), height)
            pinned = (pins or {}).get(c.get("title") or "")
            if pinned:
                segs = pinned_segments(src, dur_s, pinned, max(durs), c["crop"])
                # keep the library's narrative order: first pin = hook shot
                segs = [(t, 1000.0 - k) for k, (t, _) in enumerate(segs)]
                # a 31-55 s clip needs more than the hand-picked moments: add gated segments from the same film
                extra = segment_fn(src, dur_s, need, max(durs), crop=c["crop"])
                segs += [(t, sc) for t, sc in extra if all(abs(t - p) > max(durs) for p, _ in segs)]
            else:
                segs = segment_fn(src, dur_s, need, max(durs), crop=c["crop"])
            res_w = 1.0 if pinned else min(1.0, (height / 1080.0)) ** 0.5
            log.append(f"{c['provider']} {c['title'][:60]} {c.get('width')}x{height} {dur_s:.0f}s -> "
                       f"segments {[(round(t, 1), s) for t, s in segs]}")
            for t, sc in segs:
                pool.append((sc * res_w, c, src, t))
        except Exception as exc:
            log.append(f"source error {c.get('provider')} {str(c.get('title'))[:50]}: {exc}"[:220])
    pool.sort(key=lambda x: x[0], reverse=True)
    ordered: List[Tuple[dict, str, float]] = []
    per_src: Dict[str, int] = {}
    caps = ((need,) if any(sc >= 900 for sc, *_ in pool) else (max_per_source,) if max_per_source else (2, need))
    for cap in caps:  # diversity first unless pinned; stills last
        for sc, c, src, t in pool:
            if any(x[1] == src and abs(x[2] - t) < 1e-6 for x in ordered) or per_src.get(src, 0) >= cap:
                continue
            ordered.append((c, src, t))
            per_src[src] = per_src.get(src, 0) + 1
    ordered += [(c, src, 0.0) for c, src in stills]
    shots = []
    for c, src, start in ordered:
        if len(shots) >= need:
            break
        i = len(shots)
        dur = durs[i]
        try:
            local = localize_fn(c, src, start, dur, workdir, i)
        except Exception as exc:
            log.append(f"localize failed {c['provider']} {c['title'][:50]} @{start:.1f}s: {exc}"[:220])
            continue
        rec = {k: c.get(k) for k in ("provider", "kind", "title", "page", "author", "license", "license_url", "credit_short",
                                     "width", "height", "duration", "crop")}
        rec.update({"file_url": src, "segment_start_s": round(start, 2), "segment_seconds": round(dur, 2),
                    "ken_burns": c["kind"] == "image"})
        shots.append((local, rec))
    if len(shots) < min_need:
        raise RuntimeError(f"only {len(shots)} usable on-topic shots (need {min_need} for {MIN_SECONDS:.0f}s)")
    return shots


def produce_footage(out_dir: str, viral: Optional[dict] = None, avoid_keys: Optional[set] = None,
                    recent_topics: Optional[List[str]] = None, topic_key: Optional[str] = None,
                    workdir: Optional[str] = None, name: Optional[str] = None) -> Tuple[str, str, dict, dict]:
    """Returns (vertical_mp4, landscape_mp4, concept, info). Raises if no usable footage."""
    t0 = time.time()
    own_tmp = workdir is None
    workdir = workdir or tempfile.mkdtemp(prefix="footage_")
    os.makedirs(workdir, exist_ok=True)
    log: List[str] = []
    try:
        if viral is None and not topic_key:
            viral = todays_viral()
        items = (viral or {}).get("top") or []
        attempts = int(os.environ.get("FOOTAGE_TOPIC_ATTEMPTS", 4))
        # viral-first: the scan picks the subject; no canned library topic fills the slot when nothing qualifies
        order = ([pick_topic(items, forced=topic_key)] if topic_key else
                 viral_topics(items, avoid_keys=avoid_keys, recent_topics=recent_topics, limit=attempts, log=log))
        log.append(f"viral scan: {len(items)} uploads -> topics {[(p['topic']['topic'], p['score']) for p in order]}")
        if not order:
            raise RuntimeError(f"no viral subject today that can be built from licensed archival footage; "
                               f"not falling back to a library topic; log={log[-8:]}")
        last_err = None
        for attempt, pick in enumerate(order[:attempts]):
            topic = pick["topic"]
            try:
                cands, errs = gather_candidates(topic)
                log += errs
                log.append(f"topic {topic['key']} (viral score {pick.get('score')}): {len(cands)} relevant candidates")
                music = load_music(topic.get("mood"))
                # length comes from the unhurried VO + footage, never from autonomous_video_duration
                vo_wav, captions, vo_info = synth_vo(topic["vo"], workdir)
                planned = plan_total(float(vo_info.get("seconds") or 0.0))
                pins = {p["title"]: p["near"] for p in topic.get("pinned", [])}
                cands.sort(key=lambda c: (c.get("title") not in pins,))
                shots = plan_shots(cands, shot_count(planned), workdir, log, pins=pins, min_need=min_shots(),
                                   max_per_source=topic.get("max_per_source"))
                total = fit_total(planned, len(shots))
                n = shot_count(total) if len(shots) >= shot_count(total) else len(shots)
                shots = shots[:n]
                if float(vo_info.get("seconds") or 0.0) + VO_TAIL > total:  # less footage than VO: drop trailing lines
                    vo_wav, captions, vo_info = synth_vo(topic["vo"], workdir, max_total=total - VO_TAIL)
                cuts, durs = shot_durations(music_beats(music, total), n, total)
                for (loc, rec), d in zip(shots, durs):
                    loc["dur"] = d
                    rec["segment_seconds"] = d
                log.append(f"length planned {planned:.1f}s, final {total:.1f}s, {n} shots, VO {vo_info.get('seconds')}s")
                break
            except Exception as exc:
                last_err = exc
                log.append(f"topic {topic['key']} failed: {exc}"[:220])
        else:
            raise RuntimeError(f"no usable footage for any topic: {last_err}; log={log[-6:]}")
        sources = [rec for _, rec in shots]
        concept = build_concept(topic, sources, pick)
        name = name or f"g2_footage_{uuid.uuid4().hex}"
        vertical = os.path.join(out_dir, f"{name}.mp4")
        landscape = os.path.join(out_dir, f"{name}_16x9.mp4")
        locals_ = [s for s, _ in shots]
        for (loc, rec) in shots:
            loc["focus"] = rec["portrait_focus_x"] = round(focus_x(loc["path"], loc["kind"]), 3)
        credit = concept["footage_credit"]
        pv = render(locals_, vo_wav, captions, music, topic["on_screen_hook"], credit, vertical, "vertical", workdir)
        pl = render(locals_, vo_wav, captions, music, topic["on_screen_hook"], credit, landscape, "landscape", workdir)
        for label, meta in (("9:16", pv), ("16:9", pl)):  # hard gate on the real files
            if not length_ok(meta.get("duration")):
                raise RuntimeError(f"{label} render is {meta.get('duration')}s, outside {MIN_SECONDS:.0f}-{MAX_SECONDS:.0f}s; not shipping")
        info = {
            "engine": "footage", "footage_mode": VERSION, "media_source": "archival_footage", "format": "footage_short",
            "duration_s": round(float(pv["duration"]), 2), "length_rule_s": [MIN_SECONDS, MAX_SECONDS],
            "cost_usd": 0.0, "cost": {"cap": 0.0, "total": 0.0, "items": []},
            "concept_source": "footage_template (no LLM)", "topic_key": topic["key"],
            "topic_source": topic.get("source") or "library", "topic_reason": pick.get("reason"),
            "wikipedia": topic.get("wikipedia"),
            "viral_match": concept["viral_match"], "footage_sources": sources,
            "music": {k: music[k] for k in ("title", "author", "source", "license", "license_url", "bpm")},
            "cuts_s": cuts, "voiceover": vo_info, "captions": captions,
            "vertical": pv, "landscape": pl, "landscape_path": landscape, "log": log[-40:],
            "render_seconds": round(time.time() - t0, 1),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        with open(os.path.join(out_dir, f"{name}.sources.json"), "w") as fh:
            json.dump({"concept": concept, "info": info}, fh, indent=1, default=str)
        logger.info("%s ready %s topic=%s sources=%s", MARKER, os.path.basename(vertical), topic["key"],
                    [s["provider"] for s in sources])
        return vertical, landscape, concept, info
    finally:
        if own_tmp:
            shutil.rmtree(workdir, ignore_errors=True)
