"""Growth V2 Autopilot - original videos, hook-first captions, real metrics.

Why (audit 2026-10-14, real data): every Autopilot post was another creator's
TikTok/Reddit clip downloaded and re-uploaded. YouTube (reused content),
TikTok (FYF "unoriginal content") and Instagram (aggregator rule) all suppress
that. YouTube titles shipped as "Autonomous: Public TikTok original".

This module:
  * stops the legacy boot monkeypatch layers (cockpit_overlay / V9 / V10 /
    V10_ISOLATE) so the repo is the single source of truth for Autopilot;
  * generates an ORIGINAL 8s science/engineering clip (Veo, FAL fallback),
    reframes to 9:16 and burns a hook into the first ~2.8 seconds;
  * optional fallback: a downloaded clip is only used when TRANSFORMED
    (<=15s, hook + on-screen commentary + creator credit). Never as-is;
  * hook-first captions (<=60 char hook, 1-3 short lines, 3-5 topic hashtags,
    no "At 6Frame Studio" opener, no links on TikTok/YouTube/Instagram);
  * 14-day per-creator cooldown and exact-repeat block;
  * max 2 attempts per slot, then stop + alert;
  * real engagement (PostProxy insights, TikTok/YouTube public pages, X API
    when creds exist) feeding best-hour logic; "none" confidence without data;
  * alerts for YouTube deleted/unavailable uploads and X quota exhaustion.

Kill switches (env):
  GROWTH_V2=0                     -> legacy pipeline + legacy boot patches
  AUTOPILOT_PAUSE=1               -> Autopilot slots do nothing
  AUTOPILOT_TRANSFORM_FALLBACK=0  -> never use a downloaded clip, even transformed
  GROWTH_ALERT_EMAIL=0            -> alerts stay in dashboard/logs only
"""
from __future__ import annotations

import asyncio
import importlib.abc
import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import stitched_shorts
import viral_research
import x_policy

logger = logging.getLogger("growth_v2")

MARKER = "GROWTH_V2"
VERSION = "2026-10-19.1"
LEGACY_MODULES = {"cockpit_overlay", "cockpit_v9", "cockpit_v10", "cockpit_v10_isolate"}
MAX_SLOT_ATTEMPTS_DEFAULT = 2
SLOT_RETRY_SECONDS = 600
CREATOR_COOLDOWN_DAYS = 14
REPEAT_WINDOW_DAYS = 90
METRICS_INTERVAL_SECONDS = 3 * 3600
METRICS_MAX_AGE_DAYS = 30
MAX_HOOK_CHARS = 60
SHORT_VIDEO_PLATFORMS = ("tiktok", "youtube", "instagram")
VIDEO_PLATFORMS_ORDER = ("instagram", "tiktok", "youtube", "linkedin", "twitter")
BRAND_TAGS = {"#6framestudio", "#aifilmmaking", "#aicinema"}
JUNK_TITLES = {"public tiktok original", "viral clip", "forced original", "untitled post", "latest scanned trend", ""}
_URL_RE = re.compile(r"(https?://\S+|www\.\S+)", re.I)
_HASHTAG_RE = re.compile(r"#\w+")
_OPENER_RE = re.compile(r"^\s*(at\s+6\s*frame\s+studios?\b[^.!?\n]*[.!?]?\s*)", re.I)


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _falsy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"0", "false", "no", "off"}


def enabled() -> bool:
    return not _falsy(os.environ.get("GROWTH_V2", "1"))


def paused() -> bool:
    return _truthy(os.environ.get("AUTOPILOT_PAUSE"))


def transform_fallback_enabled() -> bool:
    """Off by default (Bret, 2026-10-14): no downloaded third-party clip is ever published
    unless AUTOPILOT_TRANSFORM_FALLBACK=1 is set explicitly."""
    return _truthy(os.environ.get("AUTOPILOT_TRANSFORM_FALLBACK", "0"))


def max_slot_attempts() -> int:
    try:
        return max(1, min(5, int(os.environ.get("GROWTH_MAX_SLOT_ATTEMPTS") or MAX_SLOT_ATTEMPTS_DEFAULT)))
    except ValueError:
        return MAX_SLOT_ATTEMPTS_DEFAULT


def legacy_patches_allowed() -> bool:
    return (not enabled()) or _truthy(os.environ.get("LEGACY_BOOT_PATCHES"))


class _LegacyPatchStub(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Railway's start command appends `import cockpit_*; cockpit_*.apply()`
    to main.py at boot. Those layers replace Autopilot with repost-only
    closures. Resolve those imports to a stub whose apply() is a no-op."""

    skipped: List[str] = []

    def find_spec(self, fullname, path=None, target=None):
        if fullname in LEGACY_MODULES or fullname.startswith("cockpit_"):
            return importlib.util.spec_from_loader(fullname, self)
        return None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        name = module.__name__

        def apply(*_args, **_kwargs):
            if name not in _LegacyPatchStub.skipped:
                _LegacyPatchStub.skipped.append(name)
            logger.warning("%s: skipped legacy boot patch %s (LEGACY_BOOT_PATCHES=1 or GROWTH_V2=0 re-enables)", MARKER, name)

        module.apply = apply
        module.STUBBED_BY = MARKER


_STUB = _LegacyPatchStub()


def stub_legacy_boot_patches() -> bool:
    if legacy_patches_allowed():
        return False
    if not any(isinstance(f, _LegacyPatchStub) for f in sys.meta_path):
        sys.meta_path.insert(0, _STUB)
    for name in list(sys.modules):
        if (name in LEGACY_MODULES or name.startswith("cockpit_")) and getattr(sys.modules[name], "STUBBED_BY", "") != MARKER:
            sys.modules.pop(name, None)
    return True


def skipped_legacy_modules() -> List[str]:
    return list(_LegacyPatchStub.skipped)


def _main():
    mod = sys.modules.get("main")
    if mod is None:
        import main as mod  # type: ignore
    return mod


def _state_dir() -> str:
    try:
        return _main().STATE_DIR
    except Exception:
        return os.environ.get("STATE_DIR") or os.path.dirname(os.path.abspath(__file__))


def state_path() -> str:
    return os.path.join(_state_dir(), "growth_v2_state.json")


def load_state() -> dict:
    try:
        with open(state_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
            if isinstance(data, dict):
                data.setdefault("history", [])
                data.setdefault("alerts", [])
                data.setdefault("slots", {})
                return data
    except Exception:
        pass
    return {"history": [], "alerts": [], "slots": {}}


def save_state(state: dict) -> None:
    path = state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    state["history"] = state.get("history", [])[-400:]
    state["alerts"] = state.get("alerts", [])[-60:]
    slots = state.get("slots", {})
    if len(slots) > 120:
        for key in sorted(slots)[:-120]:
            slots.pop(key, None)
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, path)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()  # naive timestamps are server-local time
    return dt.astimezone(timezone.utc)


# --------------------------------------------------------------------------
# alerts
# --------------------------------------------------------------------------

def add_alert(kind: str, message: str, post_id: Optional[str] = None, state: Optional[dict] = None, email: bool = True) -> dict:
    own = state is None
    state = state if state is not None else load_state()
    for existing in state.get("alerts", []):
        if existing.get("kind") == kind and existing.get("post_id") == post_id and existing.get("message") == message:
            return existing
    alert = {"id": uuid.uuid4().hex[:12], "kind": kind, "message": message, "post_id": post_id, "at": _utcnow().isoformat()}
    state.setdefault("alerts", []).append(alert)
    logger.error("GROWTH_ALERT %s: %s%s", kind, message, f" (post {post_id})" if post_id else "")
    if email:
        try:
            _maybe_email_alert(alert, state)
        except Exception as exc:
            logger.warning("Alert email failed: %s", exc)
    if own:
        save_state(state)
    return alert


def _maybe_email_alert(alert: dict, state: dict) -> None:
    """At most one email per alert kind per 24h, to the existing report
    address via the already-configured Resend account. GROWTH_ALERT_EMAIL=0 off."""
    if _falsy(os.environ.get("GROWTH_ALERT_EMAIL", "1")):
        return
    sent = state.setdefault("alert_emails", {})
    last = _parse_dt(sent.get(alert["kind"]))
    if last and _utcnow() - last < timedelta(hours=24):
        return
    cfg = _main().report_email_config()
    if cfg.get("provider") != "resend" or not (cfg.get("resend_api_key") and cfg.get("to_addr")):
        return
    import requests

    res = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": "Bearer " + cfg["resend_api_key"], "Content-Type": "application/json"},
        json={
            "from": cfg.get("resend_from") or cfg.get("from_addr"),
            "to": [cfg["to_addr"]],
            "subject": "[6Frame Autopilot] " + alert["kind"].replace("_", " "),
            "text": f"{alert['message']}\n\nPost: {alert.get('post_id') or '-'}\nTime (UTC): {alert['at']}\n\nDetails: /api/growth-v2/status",
        },
        timeout=20,
    )
    if res.status_code < 300:
        sent[alert["kind"]] = _utcnow().isoformat()


# --------------------------------------------------------------------------
# text helpers (captions, hooks, titles)
# --------------------------------------------------------------------------

def strip_links(text: str) -> str:
    out = _URL_RE.sub("", text or "")
    out = re.sub(r"[ \t]{2,}", " ", out)
    return "\n".join(line.rstrip() for line in out.splitlines()).strip()


def unescape_caption_escapes(text: Any) -> str:
    """Port of VegasCryptoAgent/marketing-automation#18: Gemini/JSON copy sometimes
    carries the literal two-character escapes \\n, \\r, \\t, \\# so LinkedIn showed
    '\\n\\n#tag'. Turn them into real characters."""
    raw = str(text or "")
    if "\\" not in raw:
        return raw
    out = raw.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")
    return out.replace("\\t", "\t").replace("\\#", "#")


def _clean_line(text: Any) -> str:
    text = strip_links(unescape_caption_escapes(text))
    text = _OPENER_RE.sub("", text)
    text = _HASHTAG_RE.sub("", text)
    return " ".join(text.split()).strip(" -\u2013\u2014|")


def clamp_words(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:-\u2013\u2014|")


def clean_hook(text: Any) -> str:
    hook = _clean_line(text).strip("\"'\u201c\u201d")
    words = hook.split()
    if len(words) > 12:
        hook = " ".join(words[:12])
    return clamp_words(hook, MAX_HOOK_CHARS)


def normalize_hashtags(tags: Any, fallback: Optional[List[str]] = None) -> List[str]:
    if isinstance(tags, str):
        tags = re.split(r"[\s,]+", unescape_caption_escapes(tags))
    elif tags:
        tags = [t for tag in tags for t in re.split(r"[\s,]+", unescape_caption_escapes(tag))]
    out: List[str] = []
    for tag in tags or []:
        word = re.sub(r"[^\w]", "", str(tag).strip().lstrip("\\#"))
        if not word or word.isdigit():
            continue
        candidate = f"#{word}"
        if candidate.lower() in BRAND_TAGS:
            continue
        if candidate.lower() not in {t.lower() for t in out}:
            out.append(candidate)
    for tag in fallback or ["#Science", "#Engineering", "#Physics", "#AIVideo"]:
        if len(out) >= 3:
            break
        if tag.lower() not in {t.lower() for t in out}:
            out.append(tag)
    return out[:5]


def youtube_title_for(post: dict, body: str = "") -> str:
    """AI hook <= 60 chars. Never 'Autonomous: Public TikTok original'."""
    candidates = [post.get("youtube_title"), post.get("hook")]
    raw_campaign = re.sub(r"^(autonomous( catch-up)?|autopilot)\s*:\s*", "", (post.get("campaign_title") or "").strip(), flags=re.I)
    candidates.append(raw_campaign)
    for line in (body or post.get("text") or "").splitlines():
        cleaned = _clean_line(line)
        if cleaned:
            candidates.append(cleaned)
            break
    for cand in candidates:
        title = clean_hook(cand or "")
        if title and title.lower() not in JUNK_TITLES:
            return title
    return "6Frame Studio"


def build_captions(concept: dict, credit: Optional[dict] = None) -> Dict[str, str]:
    hook = clean_hook(concept.get("hook") or "")
    lines = [clamp_words(_clean_line(l), 110) for l in (concept.get("lines") or []) if _clean_line(l)][:2]
    tags = normalize_hashtags(concept.get("hashtags"))
    tag_line = " ".join(tags)
    credit_short = credit_long = ""
    if credit and credit.get("creator"):
        credit_short = f"Original clip: @{credit['creator'].lstrip('@')}. Commentary + edit: 6Frame Studio."
        credit_long = credit_short + (f" Source: {credit['url']}" if credit.get("url") else "")
    question = _clean_question(concept.get("comment_question")) if not credit else ""
    # Per-post title line (unique per run, shared by every platform); reposts carry the credit instead.
    series = "" if credit else clean_title(concept.get("title_line"))
    short_body = "\n".join(([series] if series else []) + [hook] + lines + ([credit_short] if credit_short else []) + ([question] if question else []))
    short = strip_links(f"{short_body}\n\n{tag_line}")
    angle = _clean_line(concept.get("linkedin_angle") or "")
    li_parts = ([series] if series else []) + [hook] + lines + ([angle] if angle else []) + ([credit_long] if credit_long else []) + ([question] if question else [])
    linkedin = "\n\n".join(p for p in li_parts if p) + f"\n\n{tag_line}"
    tweet_tags = " ".join(tags[:1])  # x-safe-v1: X gets 0-1 hashtag
    base = f"{hook} {lines[0]}" if lines else hook
    if series and len(series) + len(base) + len(tweet_tags) + 6 <= 280:
        base = f"{series}: {base}"
    if question and len(base) + len(question) + len(tweet_tags) + 4 <= 280:
        base = f"{base} {question}"
    tweet = clamp_words(base, 280 - len(tweet_tags) - 2)
    twitter = strip_links(f"{tweet}\n\n{tweet_tags}")
    return {"tiktok": short, "youtube": short, "instagram": short, "linkedin": linkedin, "twitter": twitter}


# --------------------------------------------------------------------------
# concepts (topic steering: short science/engineering "wow")
# --------------------------------------------------------------------------

FALLBACK_CONCEPTS: List[dict] = [
    {"topic": "Ferrofluid spikes", "topic_key": "ferrofluid magnet spikes",
     "veo_prompt": "Extreme macro of black ferrofluid on a glass dish; a hidden magnet rises and the liquid instantly erupts into sharp mirror-like spikes that ripple and pulse; studio lighting, centered subject, photoreal, slow push-in.",
     "on_screen_hook": "This liquid grows spikes", "hook": "This liquid turns into spikes near a magnet",
     "lines": ["Ferrofluid is oil full of nanoscale iron particles.", "The spikes trace the magnetic field lines."],
     "hashtags": ["#Science", "#Physics", "#Ferrofluid", "#Magnetism"], "linkedin_angle": "Generated end-to-end with an AI video model, no stock footage."},
    {"topic": "Rocket shock diamonds", "topic_key": "rocket shock diamonds",
     "veo_prompt": "Close side view of a rocket engine test firing at night; bright repeating diamond-shaped shock patterns appear in the exhaust plume; heat shimmer, sparks, centered composition, photoreal, steady camera.",
     "on_screen_hook": "Why rocket fire has diamonds", "hook": "Why rocket exhaust forms glowing diamonds",
     "lines": ["Those are shock diamonds: standing shock waves in the plume.", "They form when exhaust pressure doesn't match the outside air."],
     "hashtags": ["#Engineering", "#Rocket", "#Physics", "#Space"], "linkedin_angle": "Fully AI-generated test-stand shot, directed for vertical."},
    {"topic": "Prince Rupert's drop", "topic_key": "prince ruperts drop glass",
     "veo_prompt": "Macro shot of a tadpole-shaped glass drop held in pliers; a hammer strikes the bulb and nothing happens; then the thin tail is snipped and the whole drop bursts into powder in slow motion; dark background, centered, photoreal.",
     "on_screen_hook": "Hammer-proof glass?", "hook": "This glass survives a hammer but not a snip",
     "lines": ["Rapid cooling locks huge compressive stress into the head.", "Break the tail and that stress releases at about 1 km/s."],
     "hashtags": ["#Science", "#Physics", "#Glass", "#SlowMotion"], "linkedin_angle": "Physics-accurate slow motion, generated with AI video."},
    {"topic": "Tesla turbine", "topic_key": "tesla bladeless turbine",
     "veo_prompt": "Cutaway view of a bladeless Tesla turbine: thin stacked metal discs spinning at blurring speed as glowing air spirals between them; machined aluminum, workshop lighting, centered, photoreal macro.",
     "on_screen_hook": "A turbine with no blades", "hook": "A turbine with zero blades that still spins fast",
     "lines": ["Nikola Tesla's design uses smooth discs instead of blades.", "Fluid drag on the disc surfaces does all the work."],
     "hashtags": ["#Engineering", "#Tesla", "#Mechanics", "#Innovation"], "linkedin_angle": "AI-generated engineering visual, built for 9:16."},
    {"topic": "Oobleck on a speaker", "topic_key": "oobleck speaker vibration",
     "veo_prompt": "Top-down close-up of white cornstarch fluid on a vibrating speaker cone; the fluid rises into writhing finger-like shapes that dance with the bass; bright lighting, centered, photoreal macro.",
     "on_screen_hook": "Sound makes this goo dance", "hook": "Sound waves make this goo come alive",
     "lines": ["Cornstarch and water stiffens when hit and flows when left alone.", "The speaker hits it fast enough to make it stand up."],
     "hashtags": ["#Science", "#Physics", "#Oobleck", "#Sound"], "linkedin_angle": "Generated with AI video; no stock or reposted footage."},
    {"topic": "Thermite rail weld", "topic_key": "thermite rail weld",
     "veo_prompt": "A ceramic crucible above a gap between two steel rails ignites with blinding white light and molten iron pours down into the mould, sparks spraying; outdoor rail yard at dusk, centered, photoreal.",
     "on_screen_hook": "How train rails get welded", "hook": "Train tracks are welded with liquid iron",
     "lines": ["Thermite is aluminum powder plus iron oxide.", "The reaction tops 2,000 \u00b0C and fuses the rails into one."],
     "hashtags": ["#Engineering", "#Chemistry", "#Railway", "#Science"], "linkedin_angle": "AI-rendered engineering shot, directed frame by frame."},
    {"topic": "Hydraulic press vs diamond", "topic_key": "hydraulic press diamond",
     "veo_prompt": "Industrial hydraulic press slowly lowering onto a small clear diamond on a steel anvil; pressure gauge needle climbs; the diamond shatters into glittering dust; dramatic lighting, centered, photoreal.",
     "on_screen_hook": "Hardest is not toughest", "hook": "The hardest material can still shatter",
     "lines": ["Diamond is the hardest natural material, but it's brittle.", "Hardness resists scratches; toughness resists breaking."],
     "hashtags": ["#Engineering", "#Materials", "#Science", "#Diamond"], "linkedin_angle": "AI-generated materials-science visual."},
    {"topic": "Kelvin water dropper", "topic_key": "kelvin water dropper spark",
     "veo_prompt": "Two streams of water dripping through metal rings into cans; tiny droplets start bending sideways, then a bright blue spark jumps between two metal spheres; dark lab, centered, photoreal macro.",
     "on_screen_hook": "Lightning from dripping water", "hook": "Dripping water can make its own lightning",
     "lines": ["Lord Kelvin's 1867 dropper builds up thousands of volts.", "Tiny charge differences amplify until they spark."],
     "hashtags": ["#Physics", "#Science", "#Electricity", "#Experiment"], "linkedin_angle": "Generated with AI video, physics-checked script."},
]


def concept_key(concept: dict) -> str:
    raw = (concept.get("topic_key") or concept.get("topic") or concept.get("hook") or "").lower()
    return " ".join(sorted(set(re.findall(r"[a-z0-9]+", raw))))[:120]


def _recent_history(state: dict, days: int) -> List[dict]:
    cutoff = _utcnow() - timedelta(days=days)
    return [h for h in state.get("history", []) if (_parse_dt(h.get("at")) or cutoff - timedelta(1)) >= cutoff]


def used_keys(state: dict) -> set:
    return {h.get("topic_key") for h in _recent_history(state, REPEAT_WINDOW_DAYS) if h.get("topic_key")}


def creator_from_url(url: str) -> str:
    url = url or ""
    for pattern in (r"tiktok\.com/@([\w.]+)", r"instagram\.com/([\w.]+)/", r"(?:x|twitter)\.com/(\w+)/status",
                    r"reddit\.com/(?:user|u)/([\w-]+)"):
        match = re.search(pattern, url, re.I)
        if match:
            return match.group(1).lower()
    return ""


def used_source_urls(state: dict) -> set:
    urls = {h.get("source_url") for h in state.get("history", []) if h.get("source_url")}
    try:
        for post in _main().load_scheduled_posts():
            if post.get("source_url") and post.get("status") in {"SUCCESS", "PARTIAL_SUCCESS"}:
                urls.add(post["source_url"].strip())
    except Exception:
        pass
    return urls


def creators_on_cooldown(state: dict) -> set:
    creators = {str(h.get("creator") or "").lower().lstrip("@") for h in _recent_history(state, CREATOR_COOLDOWN_DAYS) if h.get("creator")}
    try:
        cutoff = _utcnow() - timedelta(days=CREATOR_COOLDOWN_DAYS)
        for post in _main().load_scheduled_posts():
            at = _parse_dt(post.get("posted_at"))
            if at and at >= cutoff and post.get("source_url"):
                creators.add(post.get("source_creator") or creator_from_url(post["source_url"]))
    except Exception:
        pass
    creators.discard("")
    return creators


CONCEPT_PROMPT = """You are the short-form producer for 6Frame Studio, an AI filmmaking studio.
Invent ONE original 8-second vertical video concept in the "science & engineering wow" lane:
real physics, extreme machines, materials, chemistry, space, or nature's engineering.
The whole clip will be generated by an AI video model, so: no real people's faces, no logos,
no brands, no copyrighted characters, no text inside the video. One continuous shot,
native vertical 9:16 framing, subject centered and filling the frame.
It must be factually correct.
Each clip shows one extreme, measurable fact.
FIRST SECOND (0-1s beat): the subject is already moving AND there is a distinct, describable sound effect
(e.g. a sharp crack, hiss, whoosh, clang, snap). Name that sound in veo_prompt. No slow fade-in, no static opener.
LOOP: the last half-second returns to a composition closely matching the opening frame so the clip loops cleanly;
say this explicitly at the end of veo_prompt.

Return ONLY JSON with these keys:
  topic: short name
  topic_key: 3-6 lowercase words identifying the subject
  title_line: the caption's top title, 2-6 words, punchy and show-like, written for THIS video's specific subject
        and action: a concrete visual title in Title Case naming the object and what visibly happens to it, in the
        style of "Trapped Lightning In Acrylic", "Frozen Disc Glides On Magnets", "Wire Lowers Metal Block"
        (style only, never reuse these); no numbering, no "#", no series or brand name, no hashtags or emoji.
        It must be clearly different from every one of these earlier titles: {avoid_titles}
  shot_list: array of exactly 3 beats covering the 8 seconds, each an object
        {{"t": "0-1s" | "1-5s" | "5-8s", "shows": concrete visible action in plain words}}; the 0-1s beat also names the sound effect,
        the 5-8s beat ends back near the opening composition.
        Each beat must be something a camera can literally see (no abstractions like "stress" or "tension").
  veo_prompt: 50-110 words, photoreal, written as that same shot list in order (camera, subject,
        exact action per beat, how the clip ends), vertical 9:16 framing, centered subject
  on_screen_hook: max 6 words, shown on screen in the first 2 seconds; MUST contain one concrete, factually
        correct number written in digits (e.g. "3,000 mph crack", "1,000°C in 2 seconds")
  comment_question: one short question (max 80 characters, ends with "?") that invites a comment about the fact,
        no "like and subscribe", no "comment below"
  hook: the caption's first line, about 8-10 words, max 60 characters, curiosity or surprising fact,
        no hashtags, no emoji, no links, must NOT start with "At 6Frame Studio"
  lines: array of 1-2 short plain-language lines (each under 100 characters) explaining the fact
  RULE: hook, on_screen_hook and lines may only describe things that appear in shot_list.
        If the caption says something breaks, explodes, melts etc., a beat must show exactly that.
  hashtags: array of 3-5 topic hashtags (e.g. #Physics #Engineering), no brand hashtags
  linkedin_angle: one plain sentence on how the clip was made with AI

Do NOT reuse any of these recent topics: {avoid}
{inspiration}"""


_TITLE_JUNK = re.compile(r"(?:\b(?:episode|ep\.?|part|series)\s*\d*\b|#\s*\d+|\bno\.\s*\d+|engineering at the extreme)", re.I)
TITLE_OVERLAP = 0.8


def clean_title(value: Any) -> str:
    """Per-post top title: 2-6 words, no numbering or series markers."""
    t = _clean_line(value or "").strip("\"'\u201c\u201d")
    t = _TITLE_JUNK.sub("", t)
    t = " ".join(t.split()).strip(" -:\u2013\u2014|.")
    return " ".join(t.split()[:6])[:48].strip()


def _title_key(text: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def past_titles(state: dict) -> List[str]:
    """Every earlier title: growth history + scheduled/published posts (incl. manual), all time."""
    out = [h.get("title_line") for h in state.get("history", []) if h.get("title_line")]
    try:
        posts = _main().load_scheduled_posts()
    except Exception:
        posts = []
    for p in posts or []:
        t = (p.get("concept") or {}).get("title_line") or p.get("title_line")
        if t:
            out.append(t)
    return [str(t) for t in dict.fromkeys(out)]


def title_repeats(title: Any, earlier: List[str]) -> Optional[str]:
    """Exact (normalized) or near repeat (>= 80% word overlap) of any earlier title."""
    key = _title_key(title)
    if not key:
        return "(empty)"
    a = set(key.split())
    for prev in earlier:
        pk = _title_key(prev)
        if not pk:
            continue
        b = set(pk.split())
        if key == pk or (min(len(a), len(b)) >= 2 and len(a & b) / min(len(a), len(b)) >= TITLE_OVERLAP):
            return prev
    return None


def pick_title(concept: dict, earlier: List[str]) -> str:
    """The concept's title_line if new; otherwise the cleaned topic if new; otherwise ''."""
    for i, cand in enumerate((concept.get("title_line"), concept.get("topic"))):
        t = clean_title(cand)
        if t and (i or len(t.split()) >= 2) and not title_repeats(t, earlier):
            return t
    return ""


def _clean_question(value: Any) -> str:
    q = clamp_words(_clean_line(value or ""), 80).strip()
    if not q:
        return ""
    if not q.endswith("?"):
        q = q.rstrip(".!") + "?"
    return q


def has_number(text: Any) -> bool:
    return bool(re.search(r"\d", str(text or "")))


def sanitize_concept(concept: Any) -> dict:
    if not isinstance(concept, dict):
        return {}
    prompt = " ".join(str(concept.get("veo_prompt") or "").split())
    hook = clean_hook(concept.get("hook") or "")
    if not prompt or not hook:
        return {}
    lines = concept.get("lines") or []
    if isinstance(lines, str):
        lines = [lines]
    shots = []
    for beat in (concept.get("shot_list") or [])[:4]:
        if isinstance(beat, dict) and _clean_line(beat.get("shows") or ""):
            shots.append({"t": str(beat.get("t") or "")[:10], "shows": clamp_words(_clean_line(beat.get("shows")), 160)})
        elif isinstance(beat, str) and _clean_line(beat):
            shots.append({"t": "", "shows": clamp_words(_clean_line(beat), 160)})
    if shots and "shot list" not in prompt.lower():
        prompt = prompt + " Shot list, in order: " + " ".join(f"{b['t'] or 'next'}: {b['shows']}." for b in shots)
    return {
        "topic": clamp_words(_clean_line(concept.get("topic") or hook), 80),
        "topic_key": str(concept.get("topic_key") or concept.get("topic") or hook),
        "veo_prompt": prompt[:1100],
        "shot_list": shots,
        "on_screen_hook": clamp_words(_clean_line(concept.get("on_screen_hook") or hook), 40),
        "hook": hook,
        "lines": [clamp_words(_clean_line(l), 110) for l in lines if _clean_line(l)][:2],
        "hashtags": normalize_hashtags(concept.get("hashtags")),
        "linkedin_angle": clamp_words(_clean_line(concept.get("linkedin_angle") or ""), 200),
        "comment_question": _clean_question(concept.get("comment_question")),
        "title_line": clean_title(concept.get("title_line")),
    }


TOPIC_DEDUPE_DAYS = 14
_TOPIC_STOP = {"the", "a", "an", "of", "and", "or", "to", "in", "on", "at", "for", "with", "by", "from", "how", "why",
               "what", "this", "that", "its", "it", "is", "are", "into", "vs", "versus", "explosion", "explosive",
               "physics", "science", "engineering", "slow", "motion", "macro", "shot", "video", "ai", "test", "shatter",
               "shattering", "destroy", "destroys", "breaking", "break", "experiment", "real", "extreme"}


def topic_tokens(text: Any) -> set:
    """Loose topic fingerprint: lowercase words, possessives/plurals folded, generic words dropped.
    "Prince Rupert's Drop Shatter" -> {"prince", "rupert", "drop"}."""
    words = re.findall(r"[a-z0-9]+", str(text or "").lower().replace("'s ", " ").replace("\u2019s ", " "))
    out = set()
    for w in words:
        if w in _TOPIC_STOP or len(w) < 3:
            continue
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(w)
    return out


def topics_similar(a: Any, b: Any) -> bool:
    ta, tb = topic_tokens(a), topic_tokens(b)
    if not ta or not tb:
        return False
    common = ta & tb
    return len(common) >= 2 or (len(common) >= 1 and len(common) / min(len(ta), len(tb)) >= 0.67)


def recent_post_topics(state: dict, days: int = TOPIC_DEDUPE_DAYS) -> List[str]:
    """Topics from the last `days` of growth history AND scheduled/published posts (incl. manual ones)."""
    out: List[str] = []
    for h in _recent_history(state, days):
        out += [h.get("topic") or "", h.get("topic_key") or ""]
    try:
        posts = _main().load_scheduled_posts()
    except Exception:
        posts = []
    cutoff = _utcnow() - timedelta(days=days)
    for p in posts or []:
        when = _parse_dt(p.get("posted_at") or p.get("scheduled_time") or p.get("created_at"))
        if not when or when < cutoff:
            continue
        c = p.get("concept") or {}
        out += [c.get("topic") or "", c.get("topic_key") or "", p.get("hook") or "", c.get("on_screen_hook") or ""]
    return [t for t in out if t and topic_tokens(t)]


def is_recent_topic(concept: dict, recent: List[str]) -> Optional[str]:
    for field in ("topic", "topic_key"):
        val = concept.get(field)
        for r in recent:
            if topics_similar(val, r):
                return r
    return None


def plan_concept(settings: dict, state: dict, extra_avoid: Optional[List[dict]] = None) -> dict:
    """extra_avoid: concepts already tried (and failed) in this run; never reuse their topic."""
    m = _main()
    avoid_keys = used_keys(state) | {concept_key(c) for c in (extra_avoid or []) if c}
    recent_topics = [h.get("topic") for h in _recent_history(state, REPEAT_WINDOW_DAYS) if h.get("topic")][-30:]
    recent_topics += [c.get("topic") for c in (extra_avoid or []) if c and c.get("topic")]
    recent_fuzzy = recent_post_topics(state) + [c.get("topic") or "" for c in (extra_avoid or []) if c]
    for t in recent_fuzzy:
        if t and t not in recent_topics and len(recent_topics) < 60:
            recent_topics.append(t)
    inspiration = ""
    try:
        titles = [t.get("title") for t in (m.load_growth_os().get("last_trend_scan") or []) if t.get("title")][:6]
        if titles:
            inspiration = "Context only (do not copy or name creators) - titles trending this week: " + "; ".join(titles)
    except Exception:
        pass
    titles = past_titles(state) + [c.get("title_line") for c in (extra_avoid or []) if c and c.get("title_line")]
    concept: dict = {}
    for _ in range(3):
        try:
            result = m.call_gemini_json(settings, CONCEPT_PROMPT.format(avoid=", ".join(recent_topics) or "none yet", inspiration=inspiration,
                                                                         avoid_titles="; ".join(titles[-40:]) or "none yet"), {}, use_search=False)
            if isinstance(result, list) and result:
                result = result[0]
            concept = sanitize_concept(result)
        except Exception as exc:
            logger.warning("%s concept generation failed: %s", MARKER, exc)
            concept = {}
        dup = is_recent_topic(concept, recent_fuzzy) if concept else None
        if dup:
            logger.info("%s concept %r rejected: too close to recent topic %r", MARKER, concept.get("topic"), dup)
        if concept and not has_number(concept.get("on_screen_hook")):
            if has_number(concept.get("hook")):
                concept["on_screen_hook"] = clamp_words(concept["hook"], 40)
            else:
                logger.info("%s concept %r rejected: on-screen hook has no number", MARKER, concept.get("topic"))
                recent_topics.append(concept.get("topic") or "")
                continue
        if concept and concept_key(concept) not in avoid_keys and not dup:
            title = pick_title(concept, titles)
            if not title:
                logger.info("%s concept %r rejected: title %r repeats an earlier title", MARKER, concept.get("topic"), concept.get("title_line"))
                titles.append(concept.get("title_line") or "")
                recent_topics.append(concept.get("topic") or "")
                continue
            concept["title_line"] = title
            concept["concept_source"] = "gemini"
            return concept
        if concept:
            recent_topics.append(concept.get("topic") or "")
    for candidate in FALLBACK_CONCEPTS:
        c = sanitize_concept(dict(candidate))
        title = pick_title(c, titles)
        if concept_key(c) not in avoid_keys and not is_recent_topic(c, recent_fuzzy) and title:
            c["title_line"] = title
            c["concept_source"] = "curated_fallback"
            return c
    raise RuntimeError("No unused concept available (all curated topics used in the last 90 days and Gemini unavailable).")


# --------------------------------------------------------------------------
# video
# --------------------------------------------------------------------------

def _bin(name: str) -> str:
    try:
        return _main().get_binary_path(name)
    except Exception:
        return name


def probe_video(path: str) -> dict:
    try:
        res = subprocess.run([_bin("ffprobe"), "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=width,height:format=duration", "-of", "json", path],
                             capture_output=True, text=True, timeout=20)
        data = json.loads(res.stdout or "{}")
        stream = (data.get("streams") or [{}])[0]
        audio = subprocess.run([_bin("ffprobe"), "-v", "error", "-select_streams", "a", "-show_entries",
                                "stream=index", "-of", "csv=p=0", path], capture_output=True, text=True, timeout=20)
        return {"width": stream.get("width"), "height": stream.get("height"),
                "duration": round(float((data.get("format") or {}).get("duration") or 0), 2),
                "has_audio": bool(audio.stdout.strip())}
    except Exception as exc:
        return {"error": str(exc)}


def _font() -> str:
    try:
        import orchestrator

        return orchestrator.get_font_path()
    except Exception:
        return ""


# drawtext reads textfile through ffmpeg's text expansion by default: a '%' (e.g. a "99% ..." hook)
# makes ffmpeg 7.1 log "Stray %" and draw NOTHING while still exiting 0, and a backslash is
# swallowed. Every textfile drawtext therefore sets expansion=none so the text is drawn literally.
def _textfile(text: str, width: int) -> str:
    wrapped = "\n".join(textwrap.wrap(text, width=width)) or text
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="g2txt_")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(wrapped)
    return path


AUDIO_FADE_SECONDS = 0.2


OPENING_SOUND_MIN_DB = -38.0


def mean_volume_db(path: str, start: float = 0.0, length: float = 1.0) -> Optional[float]:
    try:
        res = subprocess.run([_bin("ffmpeg"), "-hide_banner", "-nostats", "-ss", f"{start:.2f}", "-t", f"{length:.2f}", "-i", path,
                              "-vn", "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True, timeout=30)
        found = re.search(r"mean_volume:\s*(-?[\d.]+|-inf)", res.stderr or "")
        if not found:
            return None
        return -120.0 if found.group(1) == "-inf" else float(found.group(1))
    except Exception:
        return None


def ensure_opening_sound(src: str) -> Tuple[str, dict]:
    """Veo often opens near-silent even when asked for a sound effect. If the first second is quieter
    than OPENING_SOUND_MIN_DB, mix in a short synthesized whoosh/impact (filtered noise, ~0.45s) at t=0.
    Returns (path, info). No external assets."""
    before = mean_volume_db(src, 0.0, 1.0)
    info = {"opening_db_before": before, "sfx_added": False}
    if before is not None and before >= OPENING_SOUND_MIN_DB:
        info["opening_db_after"] = before
        return src, info
    dst = os.path.splitext(src)[0] + "_sfx.mp4"
    sfx = ("anoisesrc=color=pink:amplitude=0.9:duration=0.45:sample_rate=48000,"
           "highpass=f=180,lowpass=f=6000,afade=t=in:d=0.03,afade=t=out:st=0.12:d=0.33,volume=1.6,"
           "aformat=channel_layouts=stereo[sfx]")
    has_audio = bool(probe_video(src).get("has_audio"))
    if has_audio:
        filt = f"{sfx};[0:a]aformat=sample_rates=48000:channel_layouts=stereo[orig];[orig][sfx]amix=inputs=2:duration=first:normalize=0[a]"
        cmd = [_bin("ffmpeg"), "-y", "-i", src, "-filter_complex", filt, "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", dst]
    else:
        filt = f"{sfx};[sfx]apad[a]"
        cmd = [_bin("ffmpeg"), "-y", "-i", src, "-filter_complex", filt, "-map", "0:v", "-map", "[a]", "-shortest", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", dst]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if res.returncode != 0 or not os.path.exists(dst):
        logger.warning("%s opening sfx mix failed: %s", MARKER, (res.stderr or "")[-300:])
        return src, info
    info.update({"sfx_added": True, "opening_db_after": mean_volume_db(dst, 0.0, 1.0)})
    return dst, info


def render_vertical(src: str, dst: str, hook: str, commentary: Optional[List[str]] = None,
                    footer: str = "", max_seconds: float = 15.0, require_text: bool = False) -> dict:
    """1080x1920 blur-pad reframe + hook text for the first 2.8s, optional timed
    commentary lines and a persistent footer credit."""
    font = _font()
    if require_text and not font:
        raise RuntimeError("No font available for on-screen commentary; refusing an untransformed clip.")
    temps: List[str] = []
    try:
        src_info = probe_video(src)
        sw, sh = src_info.get("width") or 0, src_info.get("height") or 0
        native_vertical = bool(sw and sh and abs((sh / sw) - (16 / 9)) < 0.05)
        if native_vertical:
            # already 9:16 (e.g. Veo aspect_ratio=9:16): straight scale, no blur padding
            filt = "[0:v]scale=1080:1920:flags=lanczos,setsar=1,format=yuv420p"
        else:
            filt = ("[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,gblur=sigma=22[bg];"
                    "[0:v]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
                    "[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p")
        out_dur = min(float(src_info.get("duration") or max_seconds), max_seconds)
        fade_st = max(0.0, out_dur - AUDIO_FADE_SECONDS)
        if font and hook:
            tf = _textfile(hook.upper(), 18)
            temps.append(tf)
            filt += (f",drawtext=fontfile='{font}':textfile='{tf}':expansion=none:fontcolor=white:fontsize=78:line_spacing=10:"
                     f"box=1:boxcolor=black@0.6:boxborderw=24:x=(w-text_w)/2:y=h*0.14:enable='lt(t,2.8)'")
        if font and commentary:
            for (start, end), line in zip([(2.8, 7.0), (7.0, max_seconds)], commentary[:2]):
                tf = _textfile(line, 26)
                temps.append(tf)
                filt += (f",drawtext=fontfile='{font}':textfile='{tf}':expansion=none:fontcolor=white:fontsize=56:line_spacing=8:"
                         f"box=1:boxcolor=black@0.55:boxborderw=18:x=(w-text_w)/2:y=h*0.70:enable='between(t,{start},{end})'")
        if font and footer:
            tf = _textfile(footer, 48)
            temps.append(tf)
            filt += (f",drawtext=fontfile='{font}':textfile='{tf}':expansion=none:fontcolor=white@0.85:fontsize=34:"
                     f"box=1:boxcolor=black@0.45:boxborderw=10:x=(w-text_w)/2:y=h-170")
        filt += "[outv]"
        cmd = [_bin("ffmpeg"), "-y", "-i", src, "-t", f"{max_seconds:.2f}", "-filter_complex", filt,
               "-map", "[outv]", "-map", "0:a?", "-af", f"afade=t=out:st={fade_st:.2f}:d={AUDIO_FADE_SECONDS}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", dst]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if res.returncode != 0:
            if "drawtext" in (res.stderr or "").lower() and not require_text and (hook or footer):
                logger.warning("%s: ffmpeg lacks drawtext; rendering without hook text", MARKER)
                info = render_vertical(src, dst, "", None, "", max_seconds, False)
                info["text_burned"] = False
                return info
            raise RuntimeError(f"ffmpeg vertical render failed: {(res.stderr or '')[-600:]}")
        info = probe_video(dst)
        info["text_burned"] = bool(font and (hook or commentary or footer))
        info["native_vertical_source"] = native_vertical
        info["audio_fade_out_s"] = AUDIO_FADE_SECONDS
        return info
    finally:
        for path in temps:
            try:
                os.remove(path)
            except OSError:
                pass


# 2026-10-23 (Bret): Veo 3.1 Preview was shut down 2026-10-22 (404 "no longer available"), so Gemini Omni 1.1 Flash
# (720p, 9:16, ~8 s) is the primary engine. Retries stay on Omni; under the $0.85 cap a second Omni clip only fits
# after an unbilled (filtered/rejected) failure, so a slot can never pay for two clips.
DEFAULT_ENGINE_LADDER = ("gemini_omni", "gemini_omni", "gemini_omni")
SINGLE_CLIP_SECONDS = 8
# Gemini API list price per generated second, 720p with audio (2026-10). Same ledger rules as stitched Shorts:
# the cap is checked BEFORE every Veo call; a returned clip is charged in full (also if its caption check
# fails later); failed/filtered generations are normally not billed and count $0, except timeouts and
# lost downloads, which are charged in full because Google may still bill them.
VEO_USD_PER_SECOND = {"google_veo_lite": 0.05, "google_veo_fast": 0.10, "google_veo": 0.40}
# Omni bills tokens: $17.50/1M video tokens x 5,792 tokens/s at 720p = $0.10136/s -> $0.8109 for 8 s.
OMNI_ENGINE = "gemini_omni"
OMNI_USD_PER_SECOND = round(5792 * 17.50 / 1_000_000, 6)
OMNI_MAX_CLIP_USD = 0.82  # orchestrator.OMNI_MAX_OUTPUT_TOKENS (46,800) x $17.50/1M + 400 prompt tokens: hard ceiling per clip
SINGLE_COST_CAP = 0.85  # per-slot cap for the single 8 s clip on Omni (Bret, 2026-10-23); stitched stays at $0.75
PLAN_USD = 0.03   # concept planning: up to 3 Gemini text calls (generous estimate)
CHECK_USD = 0.02  # frame check: up to 2 Gemini vision calls (generous estimate)
PLAN_TEXT_USD = 0.0125  # Omni path: pre-check estimate for concept planning (charged at real token cost)
CHECK_TEXT_USD = 0.0125  # Omni path: pre-call reservation for the frame check (charged later at real token cost)


def single_clip_cost(engine: str) -> float:
    """Expected list price of one 8 s clip on `engine`."""
    if engine == OMNI_ENGINE:
        return round(OMNI_USD_PER_SECOND * SINGLE_CLIP_SECONDS, 4)
    return round(VEO_USD_PER_SECOND.get(engine, VEO_USD_PER_SECOND["google_veo"]) * SINGLE_CLIP_SECONDS, 4)


def single_clip_reserve(engine: str) -> float:
    """What the ledger reserves before the paid call: the worst case the engine can bill."""
    return OMNI_MAX_CLIP_USD if engine == OMNI_ENGINE else single_clip_cost(engine)


def single_cost_cap(settings: Optional[dict] = None) -> float:
    """GROWTH_COST_CAP_USD env / settings.growth_cost_cap_usd override; default $0.85. Never above $0.85."""
    for raw in (os.environ.get("GROWTH_COST_CAP_USD"), (settings or {}).get("growth_cost_cap_usd")):
        try:
            if raw not in (None, ""):
                return min(SINGLE_COST_CAP, max(0.1, float(raw)))
        except (TypeError, ValueError):
            pass
    return SINGLE_COST_CAP


def _text_usage(fn, *args, fallback: float = 0.0):
    """Run fn and return (result, actual Gemini text USD spent inside it)."""
    m = _main()
    reset = getattr(m, "text_usage_reset", None)
    if reset is None:
        return fn(*args), fallback
    reset()
    try:
        result = fn(*args)
    finally:
        spent = m.text_usage_usd()
    return result, spent


def veo_failure_billed(exc: Any) -> bool:
    low = str(exc).lower()
    return any(t in low for t in ("timed out", "timeout", "download", "rendered file missing"))


DEFAULT_BACKOFF_SECONDS = (0, 15, 30, 60, 90)


def engine_ladder() -> List[str]:
    """8 s Gemini Omni (Veo 3.1 Preview is retired). GROWTH_VIDEO_ENGINES=a,b,c overrides; the cap still applies."""
    raw = os.environ.get("GROWTH_VIDEO_ENGINES")
    ladder = [e.strip() for e in raw.split(",") if e.strip()] if raw else list(DEFAULT_ENGINE_LADDER)
    return ladder[:8] or list(DEFAULT_ENGINE_LADDER)


def backoff_seconds(attempt_index: int) -> float:
    raw = os.environ.get("GROWTH_RETRY_BACKOFF")
    seq = [float(x) for x in raw.split(",") if x.strip()] if raw else list(DEFAULT_BACKOFF_SECONDS)
    return seq[min(attempt_index, len(seq) - 1)] if seq else 0.0


def generate_original_clip(concept: dict, settings: dict, engine: str = "google_veo_lite") -> Tuple[str, dict]:
    """One Veo attempt on exactly `engine` (no hidden FAL / full-Veo fallback). Returns (abs_path, info)."""
    import orchestrator

    job_id = f"g2_{uuid.uuid4().hex}"
    orchestrator.run_video_generation(job_id, concept["veo_prompt"], settings, engine, 8, allow_fallback=False,
                                     aspect_ratio=os.environ.get("GROWTH_VEO_ASPECT", "9:16"))
    status = orchestrator.get_job_status(job_id)
    if status.get("status") == "SUCCESS":
        result = status.get("result") or {}
        abs_path = _main().resolve_local_video_path(result.get("video_path") or "")
        if abs_path and os.path.exists(abs_path):
            info = {"engine": result.get("engine") or engine, "job_id": job_id}
            if result.get("cost_usd") is not None:
                info["actual_cost_usd"] = result["cost_usd"]
            if result.get("usage"):
                info["usage"] = result["usage"]
            return abs_path, info
        raise RuntimeError(f"{engine}: rendered file missing")
    raise RuntimeError(f"{engine}: {status.get('message')}")


_SOURCE_RE = re.compile(r"(tiktok\.com/@[\w.]+/video/\d+|reddit\.com/r/[^/]+/comments/|youtube\.com/(watch|shorts)|youtu\.be/)", re.I)


def pick_transform_sources(state: dict, limit: int = 6) -> List[dict]:
    """Ordered fallback candidates. Reddit now auth-walls many video posts for
    yt-dlp (even via the Mini), so YouTube/TikTok sources are tried first and
    URLs that recently failed to download are skipped."""
    cooldown = creators_on_cooldown(state)
    used = used_source_urls(state)
    failed = set((state.get("failed_source_urls") or {}).keys())
    try:
        trends = _main().load_growth_os().get("last_trend_scan") or []
    except Exception:
        trends = []
    out: List[dict] = []
    seen = set()
    for trend in trends:
        url = (trend.get("url") or "").strip()
        if not url or url in used or url in failed or url in seen or not _SOURCE_RE.search(url):
            continue
        creator = str(trend.get("author") or creator_from_url(url) or "").lower().lstrip("@")
        if creator and creator in cooldown:
            continue
        seen.add(url)
        out.append({"url": url, "title": trend.get("title") or "", "creator": creator})
    out.sort(key=lambda c: source_download_rank(c["url"]))
    return out[:max(1, limit)]


def _is_youtube_url(url: str) -> bool:
    low = (url or "").lower()
    return "youtube.com" in low or "youtu.be" in low


def source_download_rank(url: str) -> int:
    """Port of #17: TikTok/Instagram first (yt-dlp fetches them without cookies),
    then YouTube, Reddit last (auth-walled for yt-dlp)."""
    low = (url or "").lower()
    if "tiktok.com" in low or "instagram.com" in low:
        return 0
    if "reddit.com" in low:
        return 2
    return 1


def is_youtube_bot_block(error: str) -> bool:
    low = (error or "").lower()
    return any(tok in low for tok in ("sign in to confirm", "not a bot", "cookies-from-browser", "use --cookies"))


def pick_transform_source(state: dict) -> Optional[dict]:
    sources = pick_transform_sources(state, 1)
    return sources[0] if sources else None


def _remember_failed_source(state: dict, url: str, reason: str) -> None:
    failed = state.setdefault("failed_source_urls", {})
    failed[url] = {"at": _utcnow().isoformat(), "reason": str(reason)[:200]}
    if len(failed) > 50:
        for key in sorted(failed, key=lambda k: failed[k].get("at") or "")[:-50]:
            failed.pop(key, None)


TRANSFORM_PROMPT = """You write on-screen commentary for a 15-second vertical science/engineering short
by 6Frame Studio that reacts to a clip titled: "{title}".
Return ONLY JSON: hook (max 9 words, max 60 chars, no emoji, no hashtags, not starting with "At 6Frame Studio"),
on_screen_hook (max 6 words), commentary (array of exactly 2 short factual lines, each under 70 chars, adding
explanation the original lacks), lines (array of 1-2 caption lines under 100 chars), hashtags (3-5 topic hashtags,
no brand tags), topic (short), topic_key (3-6 lowercase words), linkedin_angle (one sentence)."""


VISION_TRANSFORM_PROMPT = """You are looking at {n} frames sampled in order across a short source clip titled: "{title}".
Write on-screen commentary for a 15-second vertical short by 6Frame Studio that reacts to WHAT IS ACTUALLY VISIBLE
in these frames (subjects, actions, style, setting). Do not invent subjects that are not on screen; if the title and
the frames disagree, trust the frames.
Return ONLY JSON: hook (max 9 words, max 60 chars, no emoji, no hashtags, not starting with "At 6Frame Studio"),
on_screen_hook (max 6 words), commentary (array of exactly 2 short factual lines, each under 70 chars, about what
is shown and how it was likely made), lines (array of 1-2 caption lines under 100 chars), hashtags (3-5 topic hashtags,
no brand tags), topic (short), topic_key (3-6 lowercase words), linkedin_angle (one sentence),
visible_summary (one sentence describing what the frames show)."""


def extract_frames(path: str, count: int = 5, width: int = 512) -> List[bytes]:
    """Grab `count` JPEG frames spread evenly across the clip (skipping the very ends)."""
    import subprocess
    import tempfile
    try:
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                                   capture_output=True, text=True, timeout=30).stdout.strip() or 0)
    except Exception:
        dur = 0.0
    if dur <= 0:
        return []
    frames: List[bytes] = []
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(count):
            ts = dur * (i + 0.5) / count
            dst = os.path.join(tmp, f"f{i}.jpg")
            try:
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{ts:.2f}", "-i", path, "-frames:v", "1",
                                "-vf", f"scale={width}:-2", "-q:v", "4", dst], capture_output=True, timeout=30)
                if os.path.exists(dst) and os.path.getsize(dst) > 0:
                    with open(dst, "rb") as fh:
                        frames.append(fh.read())
            except Exception as exc:
                logger.warning("%s frame %s extract failed: %s", MARKER, i, exc)
    return frames


def call_gemini_vision_json(settings: dict, prompt: str, frames: List[bytes]) -> Any:
    """Same cheap model/key as main.call_gemini_json, but with sampled frames attached."""
    m = _main()
    api_key = settings.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("Missing Gemini API key.")
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=120000))
    parts = [types.Part.from_bytes(data=f, mime_type="image/jpeg") for f in frames] + [prompt]
    response = client.models.generate_content(
        model=getattr(m, "GEMINI_TEXT_MODEL", None) or "gemini-3.6-flash",
        contents=parts,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    if hasattr(m, "note_text_usage"):
        m.note_text_usage(response)
    return m.extract_json_object(response.text)


def build_transformed_clip(settings: dict, state: dict) -> Optional[Tuple[str, dict, dict]]:
    """Fallback only. A downloaded clip is cut to <=15s and gets a hook, two
    commentary lines and a credit burned in; without commentary it is refused."""
    if not transform_fallback_enabled():
        return None
    sources = pick_transform_sources(state)
    if not sources:
        return None
    m = _main()
    downloaded = None
    source: dict = {}
    failures: List[str] = []
    skip_youtube = False
    for cand in sources:
        if skip_youtube and _is_youtube_url(cand["url"]):
            failures.append(f"{cand['url']}: skipped YouTube after bot-check")
            continue
        try:
            downloaded = m.download_and_trim_original_video(cand["url"], cand["title"], 15, False, 180)
            source = cand
            break
        except Exception as exc:
            failures.append(f"{cand['url']}: {str(exc)[:120]}")
            _remember_failed_source(state, cand["url"], str(exc))
            if _is_youtube_url(cand["url"]) and is_youtube_bot_block(str(exc)):
                skip_youtube = True
            logger.warning("%s fallback source failed, trying next: %s (%s)", MARKER, cand["url"], str(exc)[:160])
    if not downloaded:
        raise RuntimeError(f"All {len(sources)} fallback sources failed to download: " + " ; ".join(failures)[:900])
    raw: dict = {}
    frames = extract_frames(downloaded, 5)
    if frames:
        try:
            result = call_gemini_vision_json(settings, VISION_TRANSFORM_PROMPT.format(title=source["title"][:200], n=len(frames)), frames)
            raw = result if isinstance(result, dict) else {}
        except Exception as exc:
            logger.warning("%s vision transform copy failed, falling back to title-only: %s", MARKER, exc)
    if not raw:
        try:
            result = m.call_gemini_json(settings, TRANSFORM_PROMPT.format(title=source["title"][:200]), {}, use_search=False)
            raw = result if isinstance(result, dict) else {}
            raw["_copy_mode"] = "title_only"
        except Exception as exc:
            logger.warning("%s transform copy failed: %s", MARKER, exc)
    copy_mode = raw.get("_copy_mode") or ("vision" if raw else "none")
    commentary = [clamp_words(_clean_line(c), 70) for c in (raw.get("commentary") or []) if _clean_line(c)][:2]
    hook = clean_hook(raw.get("hook") or "")
    if not hook or len(commentary) < 2:
        raise RuntimeError("No usable commentary for the fallback clip; refusing to post a downloaded clip as-is.")
    concept = {
        "topic": clamp_words(_clean_line(raw.get("topic") or hook), 80),
        "topic_key": str(raw.get("topic_key") or hook),
        "veo_prompt": "",
        "on_screen_hook": clamp_words(_clean_line(raw.get("on_screen_hook") or hook), 40),
        "hook": hook,
        "lines": [clamp_words(_clean_line(l), 110) for l in (raw.get("lines") or []) if _clean_line(l)][:2],
        "hashtags": normalize_hashtags(raw.get("hashtags")),
        "linkedin_angle": clamp_words(_clean_line(raw.get("linkedin_angle") or ""), 200),
        "commentary": commentary,
        "concept_source": "transformed_repost",
    }
    footer = (f"Original: @{source['creator']} | Commentary: 6Frame Studio" if source.get("creator") else "Commentary: 6Frame Studio")
    out = os.path.join(m.GENERATED_DIR, f"g2_transformed_{uuid.uuid4().hex}.mp4")
    info = render_vertical(downloaded, out, concept["on_screen_hook"], commentary, footer, 15.0, require_text=True)
    info["source"] = source
    info["copy_mode"] = copy_mode
    info["frames_sent"] = len(frames)
    return out, concept, info


# --------------------------------------------------------------------------
# platforms
# --------------------------------------------------------------------------

def resolve_platforms(settings: dict, state: Optional[dict] = None) -> Tuple[List[str], List[str]]:
    """Configured platforms + every live PostProxy channel; drops channels that
    are not connected (e.g. Facebook) and X while its quota cooldown runs.
    GROWTH_PLATFORMS=a,b,c pins an explicit list. Returns (platforms, notes)."""
    notes: List[str] = []
    explicit = os.environ.get("GROWTH_PLATFORMS")
    if explicit:
        wanted = [p.strip().lower() for p in explicit.split(",") if p.strip()]
    else:
        wanted = [str(p).lower() for p in (settings.get("autonomous_platforms") or [])]
    channels: Dict[str, Any] = {}
    try:
        channels = _main().build_postproxy_status_payload(settings, live=False).get("channels") or {}
    except Exception as exc:
        notes.append(f"channel status unavailable: {exc}")
    live = {name for name, ch in channels.items() if ch.get("live") or ch.get("connected")}
    if not explicit:
        for name in VIDEO_PLATFORMS_ORDER:
            if name in live and name not in wanted:
                wanted.append(name)
                notes.append(f"added connected channel {name} (was missing from autonomous_platforms)")
    out: List[str] = []
    for name in wanted:
        if channels and name not in live:
            notes.append(f"skipped {name}: not connected in PostProxy")
            continue
        if name not in out:
            out.append(name)
    state = state if state is not None else load_state()
    until = _parse_dt(state.get("x_quota_until"))
    if until and until > _utcnow() and "twitter" in out:
        out.remove("twitter")
        notes.append(f"skipped twitter: PostProxy X quota exhausted until {until.isoformat()}")
    return out, notes


# --------------------------------------------------------------------------
# the Autopilot run
# --------------------------------------------------------------------------

MATCH_PROMPT = """These {n} frames are sampled in order from an 8-second AI-generated video.
The post will be published with this on-screen hook and caption:
  title_line: "{title_line}"
  on_screen_hook: "{on_screen_hook}"
  hook: "{hook}"
  lines: {lines}
Judge strictly from what is VISIBLE. A caption claim fails if the frames do not show it (e.g. caption says
the object shatters but it stays intact; caption mentions a hammer that never appears).
Numbers and measurements (temperatures, speeds, pressures) are factual context, not visual claims: do NOT list them
as mismatches just because a camera cannot measure them. Only flag a number if the frames contradict the event it
describes (e.g. "melts at 3,422°C" but nothing melts).
Return ONLY JSON:
  visible_summary: one sentence describing what the frames actually show,
  matches: true only if every visual claim in on_screen_hook, hook and lines is shown,
  mismatches: array of short strings (claims not shown),
  rewrite: if matches is false, a corrected caption that describes ONLY what is shown, still factual and
           curiosity-driven: {{"on_screen_hook": max 6 words AND must contain one factually correct number in digits
           that fits what is shown (keep the original number if the frames don't contradict it),
           "title_line": 2-6 word punchy title for what is shown, no numbering,
           "hook": 8-10 words max 60 chars no emoji/hashtags,
           "lines": 1-2 lines under 100 chars}}; otherwise null."""


def verify_clip_matches(raw_path: str, concept: dict, settings: dict) -> dict:
    """Gemini looks at ~5 frames of the generated clip and checks the hook/caption.
    Returns {"matches", "visible_summary", "mismatches", "rewrite", "frames"}. Raises if it cannot check."""
    frames = extract_frames(raw_path, 5)
    if not frames:
        raise RuntimeError("caption check: could not extract frames")
    prompt = MATCH_PROMPT.format(n=len(frames), on_screen_hook=concept.get("on_screen_hook", ""), title_line=concept.get("title_line", ""),
                                 hook=concept.get("hook", ""), lines=json.dumps(concept.get("lines") or []))
    result = call_gemini_vision_json(settings, prompt, frames)
    if not isinstance(result, dict) or "matches" not in result:
        raise RuntimeError("caption check: unusable verifier response")
    result["matches"] = result.get("matches") is True
    result["frames"] = len(frames)
    return result


def apply_caption_rewrite(concept: dict, rewrite: Any) -> Optional[dict]:
    if not isinstance(rewrite, dict):
        return None
    hook = clean_hook(rewrite.get("hook") or "")
    lines = rewrite.get("lines") or []
    if isinstance(lines, str):
        lines = [lines]
    lines = [clamp_words(_clean_line(l), 110) for l in lines if _clean_line(l)][:2]
    if not hook or not lines:
        return None
    fixed = dict(concept)
    new_title = clean_title(rewrite.get("title_line"))
    if new_title and len(new_title.split()) >= 2:
        try:
            earlier = past_titles(load_state())
        except Exception:
            earlier = []
        if not title_repeats(new_title, earlier):
            fixed["title_line"] = new_title
    fixed.update({"hook": hook, "lines": lines,
                  "on_screen_hook": clamp_words(_clean_line(rewrite.get("on_screen_hook") or hook), 40),
                  "caption_rewritten_from_frames": True,
                  "original_caption": {"hook": concept.get("hook"), "lines": concept.get("lines"), "title_line": concept.get("title_line")}})
    return fixed


def ensure_caption_matches(raw_path: str, concept: dict, settings: dict) -> Tuple[dict, dict]:
    """Never publish a mismatch: pass -> keep; fail -> rewrite from frames and re-check;
    still failing (or unverifiable) -> raise so the retry loop makes a new original."""
    first = verify_clip_matches(raw_path, concept, settings)
    if first["matches"]:
        return concept, {"caption_check": "match", "visible_summary": first.get("visible_summary")}
    if isinstance(first.get("rewrite"), dict) and not has_number(first["rewrite"].get("on_screen_hook")) and has_number(concept.get("on_screen_hook")):
        num = re.search(r"[\d][\d,.]*\s*(?:°\s*[CF]|%|[a-zA-Z/]+)?", concept["on_screen_hook"])
        if num:
            first["rewrite"]["on_screen_hook"] = clamp_words(f"{num.group(0).strip()}: {first['rewrite'].get('on_screen_hook') or ''}".strip(": "), 40)
    fixed = apply_caption_rewrite(concept, first.get("rewrite"))
    if not fixed:
        raise RuntimeError(f"caption mismatch, no usable rewrite: {first.get('mismatches')} | shown: {first.get('visible_summary')}")
    if not has_number(fixed.get("on_screen_hook")):
        raise RuntimeError("caption rewrite dropped the number from the on-screen hook")
    second = verify_clip_matches(raw_path, fixed, settings)
    if not second["matches"]:
        raise RuntimeError(f"caption mismatch after rewrite: {second.get('mismatches')} | shown: {second.get('visible_summary')}")
    return fixed, {"caption_check": "rewritten_from_frames", "visible_summary": second.get("visible_summary"),
                   "mismatches_fixed": first.get("mismatches")}


async def produce(settings: dict, state: dict) -> Tuple[str, dict, dict]:
    """Originals only. Each failed Veo attempt gets a brand-new concept (new topic + prompt),
    with backoff, walking the Lite->Fast ladder. The transform/repost fallback only runs if
    AUTOPILOT_TRANSFORM_FALLBACK=1 is set explicitly (off by default)."""
    loop = asyncio.get_running_loop()
    if stitched_shorts.enabled(settings):
        return await loop.run_in_executor(None, produce_stitched, settings, state)
    errors: List[str] = []
    tried: List[dict] = []
    # Hard per-slot cap ($0.85 on Omni), checked BEFORE every paid video call. Omni reserves its worst case
    # (token-capped), text calls are charged at their real token cost, so plan + clip + check stay <= cap.
    ledger = stitched_shorts.CostLedger(single_cost_cap(settings))
    started = 0
    for i, engine in enumerate(engine_ladder()):
        veo_usd = single_clip_reserve(engine)
        plan_est = PLAN_TEXT_USD if engine == OMNI_ENGINE else PLAN_USD
        check_est = CHECK_TEXT_USD if engine == OMNI_ENGINE else CHECK_USD
        if not ledger.fits(plan_est + veo_usd + check_est):
            msg = (f"attempt {i + 1} {engine}: skipped, cost cap ${ledger.cap:.2f} would be exceeded "
                   f"(spent ${ledger.total:.2f} + ${plan_est + veo_usd + check_est:.2f})")
            errors.append(msg)
            logger.warning("%s %s", MARKER, msg)
            continue
        wait = backoff_seconds(started)
        if started and wait > 0:
            await asyncio.sleep(wait)
        started += 1
        concept: dict = {}
        try:
            if engine == OMNI_ENGINE:
                concept, plan_usd = await loop.run_in_executor(None, lambda: _text_usage(plan_concept, settings, state, list(tried), fallback=PLAN_USD))
                ledger.charge("plan", plan_usd)
            else:
                ledger.charge("plan", PLAN_USD)
                concept = await loop.run_in_executor(None, plan_concept, settings, state, list(tried))
            label = f"{engine} {SINGLE_CLIP_SECONDS}s"
            ledger.reserve(label, veo_usd + (check_est if engine == OMNI_ENGINE else 0.0))  # raises CostCapExceeded before the paid call
            try:
                raw_path, gen_info = await loop.run_in_executor(None, generate_original_clip, concept, settings, engine)
            except Exception as gen_exc:
                ledger.charge(label, veo_usd if veo_failure_billed(gen_exc) else 0.0, ok=False, error=str(gen_exc)[:200])
                raise
            clip_usd = veo_usd
            if engine == OMNI_ENGINE:
                actual = gen_info.get("actual_cost_usd")
                clip_usd = min(veo_usd, float(actual)) if actual is not None else veo_usd
            ledger.charge(label, clip_usd, topic=concept.get("topic"))
            if engine == OMNI_ENGINE:
                (concept, check), check_usd = await loop.run_in_executor(
                    None, lambda: _text_usage(ensure_caption_matches, raw_path, concept, settings, fallback=CHECK_USD))
                ledger.charge("caption check", check_usd)
            else:
                ledger.charge("caption check", CHECK_USD)
                concept, check = await loop.run_in_executor(None, ensure_caption_matches, raw_path, concept, settings)
            gen_info.update(check)
            raw_path, sound = await loop.run_in_executor(None, ensure_opening_sound, raw_path)
            gen_info.update(sound)
            out = os.path.join(_main().GENERATED_DIR, f"g2_original_{uuid.uuid4().hex}.mp4")
            info = await loop.run_in_executor(None, render_vertical, raw_path, out, concept["on_screen_hook"], None, "", 15.0, False)
            info.update(gen_info)
            info["media_source"] = "ai_generated"
            info["attempt"] = i + 1
            info["failed_attempts"] = [{"topic": c.get("topic"), "error": e[:200]} for c, e in zip(tried, errors)]
            info["format"] = "single_8s"
            info["cost"] = ledger.as_dict()
            info["cost_usd"] = ledger.total
            logger.info("%s 8s Short ready %s title=%r engine=%s cost=$%.3f (cap $%.2f)", MARKER, os.path.basename(out),
                        concept.get("title_line"), gen_info.get("engine") or engine, ledger.total, ledger.cap)
            return out, concept, info
        except Exception as exc:
            if concept:
                tried.append(concept)
            else:
                tried.append({"topic": f"(no concept, attempt {i + 1})", "topic_key": f"none-{i}"})
            errors.append(f"attempt {i + 1} {engine} [{concept.get('topic') if concept else '-'}]: {exc}")
            logger.error("%s original attempt %s/%s on %s failed: %s", MARKER, i + 1, len(engine_ladder()), engine, exc)
    if transform_fallback_enabled():
        try:
            transformed = await loop.run_in_executor(None, build_transformed_clip, settings, state)
        except Exception as exc:
            transformed = None
            errors.append(f"transform fallback: {exc}")
        if transformed:
            out, concept, info = transformed
            info["media_source"] = "transformed_repost"
            return out, concept, info
    raise RuntimeError(f"All {len(errors)} original video attempts failed: " + " | ".join(errors)[-1500:])


def produce_stitched(settings: dict, state: dict) -> Tuple[str, dict, dict]:
    """Stitched 20-30s Short (stitched_shorts.py): AI-filmmaking niche, originals only.
    Never falls back to a downloaded clip; per-shot retries and the cost cap live in the module."""
    recent = [t for t in recent_post_topics(state) if t][-30:]
    titles = past_titles(state)
    # run records live in STATE_DIR (not the public static dir); a stopped slot can be resumed by run_id
    viral = None
    if not _falsy(os.environ.get("VIRAL_RESEARCH", "1")):
        viral = viral_research.research()  # free (yt-dlp YouTube search), never raises
        logger.info("%s viral references for this slot: %s", MARKER,
                    [f'{r["views"]:,} views | {r["title"][:70]} | {r["url"]}' for r in viral["refs"][:5]] or viral["errors"])
    out, plan, info = stitched_shorts.produce_short(settings, _main().GENERATED_DIR, recent, titles,
                                                    runs_dir=os.path.join(_state_dir(), "stitched_runs"), viral=viral)
    logger.info("%s viral angle used: %s | based on: %s", MARKER, plan.get("viral_angle"), plan.get("viral_refs_used"))
    caps = info.get("platform_captions") or {}
    concept = {
        "topic": plan["topic"], "topic_key": plan.get("topic_key") or plan["topic"].lower(),
        "title_line": pick_title({"title_line": plan.get("title_line"), "on_screen_hook": plan["on_screen_hook"]}, titles) or plan.get("title_line"),
        "on_screen_hook": plan["on_screen_hook"],
        "hook": clean_hook((caps.get("youtube") or {}).get("title") or plan["on_screen_hook"]),
        "lines": [], "hashtags": (caps.get("tiktok") or {}).get("hashtags") or [],
        "comment_question": plan.get("comment_question"), "concept_source": plan.get("plan_source"),
        "shot_list": info.get("shot_list"), "veo_prompt": (plan["shots"][0] or {}).get("prompt"),
        "format": "stitched_short",
        "pinned_comment": info.get("pinned_comment"), "viral_research": info.get("viral_research"),
    }
    info["attempt"] = 1
    info["media_source"] = "ai_generated"
    return out, concept, info


def stitched_platform_captions(info: dict, platforms: List[str]) -> Dict[str, str]:
    caps = info.get("platform_captions") or {}
    out = {p: (caps.get(p) or {}).get("caption") for p in platforms if (caps.get(p) or {}).get("caption")}
    if "twitter" in platforms and "twitter" not in out:
        tt = (caps.get("tiktok") or {}).get("caption") or ""
        out["twitter"] = clamp_words(tt, 270)
    return out


def build_staggered_posts(post: dict, info: dict, platforms: List[str], start: Optional[datetime] = None) -> List[dict]:
    """One post per platform with its own caption/title, scheduled at staggered times
    (first platform now, others later) instead of identical simultaneous posts."""
    caps = stitched_platform_captions(info, platforms)
    sched = stitched_shorts.staggered_schedule(platforms, start)
    if "twitter" in sched and x_policy.enabled():
        # x-safe-v1: X goes out >= 4h after the last X post, at a varied minute.
        sched["twitter"] = x_policy.schedule_time(datetime.fromisoformat(sched["twitter"])).isoformat(timespec="seconds")
    group = post["id"]
    out = []
    for i, plat in enumerate(sorted(platforms, key=lambda p: sched[p])):
        p = json.loads(json.dumps(post))
        p["id"] = group if i == 0 else str(uuid.uuid4())
        p["stagger_group"] = group
        p["pinned_comment"] = info.get("pinned_comment") or ""
        p["platform"] = plat
        p["text"] = caps.get(plat) or post.get("text")
        p["platform_captions"] = {plat: p["text"]}
        if plat == "youtube":
            yt = ((info.get("platform_captions") or {}).get("youtube") or {}).get("title")
            if yt:
                p["youtube_title"] = yt
        p["scheduled_time"] = sched[plat]
        p["status"] = "PUBLISHING" if i == 0 else "PENDING"
        out.append(p)
    return out


def origin_of(media_source: Any) -> str:
    src = str(media_source or "").lower()
    if src == "ai_generated":
        return "original"
    if src in {"transformed_repost", "original_download", "hosted_fallback", "repost"} or "download" in src or "repost" in src:
        return "repost"
    return "unknown"



def build_post(abs_video: str, concept: dict, info: dict, platforms: List[str]) -> dict:
    web = f"/static/assets/generated/{os.path.basename(abs_video)}"
    source = info.get("source") or {}
    credit = {"creator": source.get("creator"), "url": source.get("url")} if info.get("media_source") == "transformed_repost" else None
    captions = build_captions(concept, credit)
    if info.get("format") == "stitched_short":
        captions.update(stitched_platform_captions(info, list(captions)))
    hook = clean_hook(concept["hook"])
    return {
        "id": str(uuid.uuid4()),
        "platform": ",".join(platforms) if platforms else "none",
        "text": captions["linkedin"],
        "thread": None,
        "platform_captions": captions,
        "hook": hook,
        "youtube_title": hook,
        "scheduled_time": datetime.now().isoformat(),
        "campaign_title": f"Autopilot: {hook}",
        "video_path": web,
        "vertical_video_path": web,
        "source_video_path": web,
        "source_url": source.get("url") or "",
        "source_creator": source.get("creator") or "",
        "media_source": info.get("media_source"),
        "origin": origin_of(info.get("media_source")),
        "media_info": {k: v for k, v in info.items() if k != "source"},
        "concept": {k: concept.get(k) for k in ("topic", "topic_key", "on_screen_hook", "hook", "lines", "hashtags", "concept_source", "commentary", "shot_list", "veo_prompt", "caption_rewritten_from_frames", "original_caption", "comment_question", "title_line")},
        "growth_v2": VERSION,
        "status": "PUBLISHING",
        "error_message": None,
        "posted_at": None,
    }


def record_history(state: dict, post: dict) -> None:
    concept = post.get("concept") or {}
    state.setdefault("history", []).append({
        "at": _utcnow().isoformat(),
        "post_id": post.get("id"),
        "topic": concept.get("topic"),
        "topic_key": concept_key(concept),
        "source_url": post.get("source_url") or None,
        "creator": post.get("source_creator") or None,
        "media_source": post.get("media_source"),
        "title_line": concept.get("title_line") or None,
    })


X_QUOTA_TOKENS = ("quota", "top up", "wallet", "24 hour", "24-hour", "tweet limit", "daily tweet", "byok")


def is_x_quota_error(text: Any, platforms: Optional[List[str]] = None) -> bool:
    """PostProxy's real X error (seen 2026-09-21): "twitter: 24 hour Twitter quota exceeded.
    Top up your wallet to keep posting or consider using BYOK." Shared with scripts/railway_boot_hotfix.py."""
    low = str(text or "").lower()
    xish = "twitter" in low or low.startswith("x:") or any(str(p).lower() in {"twitter", "x"} for p in (platforms or []))
    return xish and any(tok in low for tok in X_QUOTA_TOKENS)


def inspect_publish_errors(state: dict, post: dict, errors: List[str]) -> None:
    for err in errors or []:
        low = str(err).lower()
        if is_x_quota_error(err):
            state["x_quota_until"] = (_utcnow() + timedelta(hours=24)).isoformat()
            add_alert("x_quota_exhausted",
                      "PostProxy reports the 24-hour X/Twitter quota is used up (PostProxy wallet top-up needed). "
                      "X is skipped for 24h; other networks keep posting.", post.get("id"), state)
        if "youtube" in low and any(w in low for w in ("deleted", "removed", "rejected")):
            add_alert("youtube_upload_deleted", f"YouTube reported the upload as deleted/removed: {str(err)[:300]}", post.get("id"), state)


async def run_autopilot(settings: dict, dry_run: bool = False) -> dict:
    m = _main()
    state = load_state()
    platforms, notes = resolve_platforms(settings, state)
    try:
        abs_video, concept, info = await produce(settings, state)
    except Exception:
        if not dry_run and state.get("failed_source_urls"):
            save_state(state)  # remember dead fallback sources so the next slot skips them
        raise
    post = build_post(abs_video, concept, info, platforms)
    post["platform_notes"] = notes
    if dry_run:
        post["status"] = "DRY_RUN"
        post["youtube_title_preview"] = youtube_title_for(post)
        return {"ok": True, "dry_run": True, "post": post}
    if not platforms:
        raise RuntimeError("No connected platforms to publish to.")
    posts = m.load_scheduled_posts()
    if info.get("format") == "stitched_short" and len(platforms) > 1:
        staggered = build_staggered_posts(post, info, platforms)
        if settings.get("require_autopilot_approval", False):
            for p in staggered:
                p["status"] = "AWAITING_APPROVAL"
            posts.extend(staggered)
            m.save_scheduled_posts(posts)
            record_history(state, post)
            save_state(state)
            return {"ok": True, "awaiting_approval": True, "post_id": post["id"], "post_ids": [p["id"] for p in staggered]}
        post = staggered[0]
        posts.extend(staggered)
        m.save_scheduled_posts(posts)
        logger.info("%s stitched Short staggered: %s", MARKER, {p["platform"]: p["scheduled_time"] for p in staggered})
    elif settings.get("require_autopilot_approval", False):
        post["status"] = "AWAITING_APPROVAL"
        posts.append(post)
        m.save_scheduled_posts(posts)
        record_history(state, post)
        save_state(state)
        return {"ok": True, "awaiting_approval": True, "post_id": post["id"]}
    else:
        x_queued = None
        if x_policy.enabled() and "twitter" in platforms:
            # x-safe-v1: X never goes out in the same second as the slot; it gets its own
            # post at a varied minute, >= 4h after the previous X post.
            x_caption = x_policy.sanitize_caption((post.get("platform_captions") or {}).get("twitter") or post.get("text") or "",
                                                  post.get("hook") or "")
            others = [p for p in platforms if p != "twitter"]
            if others:
                post["platform"] = ",".join(others)
                post["platform_captions"] = {k: v for k, v in (post.get("platform_captions") or {}).items() if k != "twitter"}
            posts.append(post)
            m.save_scheduled_posts(posts)
            x_queued = m.split_x_post(post, x_caption, x_policy.schedule_time(datetime.now()))
            if not others:
                posts = [p for p in m.load_scheduled_posts() if p.get("id") != post["id"]]
                m.save_scheduled_posts(posts)
                record_history(state, post)
                save_state(state)
                logger.info("%s X-only slot queued for %s", MARKER, x_queued["scheduled_time"])
                return {"ok": True, "post_id": x_queued["id"], "successes": [], "errors": [], "x_queued_for": x_queued["scheduled_time"]}
            logger.info("%s X queued separately for %s (x-safe-v1)", MARKER, x_queued["scheduled_time"])
        else:
            posts.append(post)
            m.save_scheduled_posts(posts)
    result = await m.publish_post_to_platforms(post, settings)
    posts = m.load_scheduled_posts()
    for p in posts:
        if p["id"] == post["id"]:
            for key in ("vertical_video_path", "postproxy_post_ids", "postproxy_placements"):
                if post.get(key):
                    p[key] = post[key]
            m.apply_publish_result_to_post(p, result)
            post = p
            break
    m.save_scheduled_posts(posts)
    inspect_publish_errors(state, post, result.get("errors") or [])
    ok = bool(result.get("successes"))
    if ok:
        record_history(state, post)
    save_state(state)
    logger.info("%s Autopilot publish done. successes=%s errors=%s", MARKER, result.get("successes"), result.get("errors"))
    return {"ok": ok, "post_id": post.get("id"), "successes": result.get("successes"), "errors": result.get("errors")}


# --------------------------------------------------------------------------
# scheduler (shares STATE_DIR/autonomous_slot.json with autopilot_watch)
# --------------------------------------------------------------------------

def _parse_hours(raw: Any) -> List[int]:
    if raw in (None, ""):
        return []
    if isinstance(raw, (list, tuple)):
        values = list(raw)
    else:
        try:
            loaded = json.loads(str(raw))
            values = loaded if isinstance(loaded, list) else [loaded]
        except Exception:
            values = [p for p in str(raw).split(",") if p.strip()]
    out: List[int] = []
    for v in values:
        try:
            h = int(str(v).strip()) % 24
        except ValueError:
            continue
        if h not in out:
            out.append(h)
    return out


def slot_hours(settings: dict) -> List[int]:
    hours = _parse_hours(os.environ.get("AUTONOMOUS_HOURS") or settings.get("autonomous_hours"))
    if not hours:
        try:
            hours = [int(os.environ.get("AUTONOMOUS_HOUR") or settings.get("autonomous_hour", 9)) % 24]
        except ValueError:
            hours = [9]
    return hours


def _ledger_path() -> str:
    return os.path.join(_state_dir(), "autonomous_slot.json")


def _load_ledger() -> dict:
    try:
        with open(_ledger_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def mark_slot_success(slot: str) -> None:
    data = _load_ledger()
    successes = [s for s in (data.get("success_slots") or []) if s]
    if slot not in successes:
        successes.append(slot)
    data["success_slots"] = successes[-60:]
    data["last_success_slot"] = slot
    data["last_success_at"] = _utcnow().isoformat()
    path = _ledger_path()
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(path + ".tmp", path)


def slot_succeeded(slot: str) -> bool:
    data = _load_ledger()
    return data.get("last_success_slot") == slot or slot in (data.get("success_slots") or [])


def due_slot(now: datetime, settings: dict, state: dict) -> Optional[str]:
    if now.hour not in slot_hours(settings):
        return None
    slot = f"{now.date().isoformat()}-{now.hour}"
    if slot_succeeded(slot):
        return None
    info = state.get("slots", {}).get(slot) or {}
    if info.get("status") in {"gave_up", "published", "awaiting_approval"}:
        return None
    if int(info.get("attempts") or 0) >= max_slot_attempts():
        return None
    last = _parse_dt(info.get("last_attempt"))
    if last and (_utcnow() - last).total_seconds() < SLOT_RETRY_SECONDS:
        return None
    return slot


async def run_slot(slot: str, settings: dict) -> bool:
    state = load_state()
    info = state.setdefault("slots", {}).setdefault(slot, {"attempts": 0})
    info["attempts"] = attempt = int(info.get("attempts") or 0) + 1
    info["last_attempt"] = _utcnow().isoformat()
    save_state(state)
    logger.info("Autonomous autoposting slot %s reached. Initiating pipeline... (%s attempt %s/%s)", slot, MARKER, attempt, max_slot_attempts())
    error, ok, result = "", False, {}
    try:
        result = await run_autopilot(settings)
        ok = bool(result.get("ok"))
        if not ok:
            error = "; ".join(result.get("errors") or []) or "publish returned no successes"
    except Exception as exc:
        error = str(exc)
        logger.exception("%s slot %s crashed", MARKER, slot)
    state = load_state()
    info = state.setdefault("slots", {}).setdefault(slot, {"attempts": attempt})
    if ok:
        info["status"] = "awaiting_approval" if result.get("awaiting_approval") else "published"
        info["post_id"] = result.get("post_id")
        save_state(state)
        mark_slot_success(slot)
        logger.info("Autonomous slot %s published and persisted.", slot)
        return True
    info["last_error"] = error[-600:]
    if attempt >= max_slot_attempts():
        info["status"] = "gave_up"
        add_alert("autopilot_slot_failed", f"Slot {slot} failed {attempt}x and was stopped: {error[-400:]}", None, state)
        logger.error("Autonomous slot %s gave up after %s attempts: %s", slot, attempt, error[-300:])
    else:
        info["status"] = "retry_pending"
        logger.error("Autonomous slot %s did not publish (attempt %s/%s): %s", slot, attempt, max_slot_attempts(), error[-300:])
    save_state(state)
    return False


async def scheduler_loop():
    m = _main()
    await asyncio.sleep(5)
    logger.info("Background scheduler loop started (%s %s).", MARKER, VERSION)
    running = {"slot": False}
    last_metrics: Optional[float] = None
    last_engagement: Optional[float] = None
    loop = asyncio.get_running_loop()

    async def _slot(slot: str, settings: dict):
        running["slot"] = True
        try:
            await run_slot(slot, settings)
        finally:
            running["slot"] = False

    while True:
        try:
            posts = m.load_scheduled_posts()
            now = datetime.now()
            for post in posts:
                if post.get("status") == "PENDING":
                    try:
                        if now >= datetime.fromisoformat(post["scheduled_time"]):
                            post["status"] = "PUBLISHING"
                            m.save_scheduled_posts(posts)
                            asyncio.create_task(m.execute_scheduled_post(post))
                    except Exception as parse_err:
                        post["status"] = "FAILED"
                        post["error_message"] = f"Invalid scheduled time: {parse_err}"
                        m.save_scheduled_posts(posts)
            settings = m.load_settings()
            if settings.get("autonomous_posting", False) and not paused() and not running["slot"]:
                slot = due_slot(now, settings, load_state())
                if slot:
                    running["slot"] = True
                    asyncio.create_task(_slot(slot, settings))
            mono = time.monotonic()
            if last_metrics is None or mono - last_metrics >= METRICS_INTERVAL_SECONDS:
                last_metrics = mono
                loop.run_in_executor(None, _safe_refresh_metrics, settings)
            if last_engagement is None or mono - last_engagement >= getattr(m, "ENGAGEMENT_REFRESH_INTERVAL_SECONDS", 900):
                last_engagement = mono
                try:
                    await loop.run_in_executor(None, m.fetch_all_social_inbox, settings)
                except Exception as exc:
                    logger.error("Error fetching/drafting mention replies: %s", exc)
        except Exception as loop_err:
            logger.error("Error in scheduler loop: %s", loop_err)
        await asyncio.sleep(10)


# --------------------------------------------------------------------------
# metrics (real numbers only)
# --------------------------------------------------------------------------

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"


def parse_tiktok_html(html: str) -> Optional[dict]:
    match = re.search(r'id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', html or "", re.S)
    if not match:
        return None
    data = json.loads(match.group(1)).get("__DEFAULT_SCOPE__", {}).get("webapp.video-detail", {})
    item = (data.get("itemInfo") or {}).get("itemStruct")
    if not item:
        return {"available": False, "status": f"{data.get('statusCode')} {data.get('statusMsg') or ''}".strip()}
    stats = item.get("statsV2") or item.get("stats") or {}

    def num(key):
        try:
            return int(stats.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    return {"available": True, "views": num("playCount"), "likes": num("diggCount"), "comments": num("commentCount"),
            "shares": num("shareCount"), "saves": num("collectCount"), "duration_s": (item.get("video") or {}).get("duration")}


def parse_youtube_html(html: str) -> Optional[dict]:
    status = re.search(r'"playabilityStatus":\{"status":"([A-Z_]+)"(?:,"reason":"([^"]*)")?', html or "")
    views = re.search(r'"videoDetails":\{.*?"viewCount":"(\d+)"', html or "", re.S)
    likes = re.search(r'"likeCount":"?(\d+)', html or "")
    if not status and not views:
        return None
    st = status.group(1) if status else "OK"
    reason = status.group(2) if status else ""
    if st == "LOGIN_REQUIRED" or "not a bot" in (reason or "").lower():
        # YouTube bot wall on the server IP: says nothing about the video.
        return None
    return {"available": st == "OK", "status": st, "reason": status.group(2) if status else None,
            "views": int(views.group(1)) if views else None, "likes": int(likes.group(1)) if likes else None}


def _get(url: str) -> str:
    import requests

    return requests.get(url, headers={"User-Agent": _UA, "Accept-Language": "en-US"}, timeout=25).text


def _permalinks(post: dict) -> Dict[str, str]:
    links = dict(post.get("postproxy_permalinks") or {})
    result = post.get("postproxy_result") or {}
    for item in result.get("platforms") or []:
        if item.get("permalink") and item.get("platform"):
            links.setdefault(item["platform"], item["permalink"])
    return links


FOLLOWER_KEYS = ("followers_count", "follower_count", "followerCount", "subscriberCount", "subscriber_count", "fan_count")
FOLLOWER_SNAPSHOT_HOURS = 6


def _follower_value(stats: dict) -> Optional[int]:
    for key in FOLLOWER_KEYS:
        if isinstance(stats, dict) and stats.get(key) is not None:
            try:
                return int(stats[key])
            except (TypeError, ValueError):
                return None
    return None


def snapshot_followers(settings: dict, state: dict, force: bool = False) -> Optional[dict]:
    """Follower counts per platform from PostProxy profile stats (GET /profiles/:id latest_stats).
    LinkedIn/Facebook are per placement (company page). Appends to state["follower_history"]."""
    last = _parse_dt((state.get("followers") or {}).get("at"))
    if not force and last and (_utcnow() - last).total_seconds() < FOLLOWER_SNAPSHOT_HOURS * 3600:
        return None
    m = _main()
    data = m.postproxy_get(settings, "/profiles", timeout=30)
    snap: Dict[str, Any] = {}
    for prof in (data.get("data") or []):
        try:
            detail = m.postproxy_get(settings, f"/profiles/{prof['id']}", timeout=30)
        except Exception as exc:
            logger.warning("%s follower fetch failed for %s: %s", MARKER, prof.get("platform"), exc)
            continue
        detail = detail.get("data", detail) if isinstance(detail, dict) else {}
        platform = prof.get("platform") or detail.get("platform")
        for entry in detail.get("latest_stats") or []:
            value = _follower_value((entry or {}).get("stats") or {})
            if value is None:
                continue
            key = platform if not entry.get("placement_id") else f"{platform}:{entry['placement_id']}"
            snap[key] = {"followers": value, "recorded_at": entry.get("recorded_at"), "profile": prof.get("name")}
    if not snap:
        return None
    record = {"at": _utcnow().isoformat(), "by_platform": snap}
    state["followers"] = record
    hist = state.setdefault("follower_history", [])
    hist.append({"at": record["at"], **{k: v["followers"] for k, v in snap.items()}})
    del hist[:-120]
    return record


def fetch_post_stats(settings: dict, pp_ids: List[str]) -> Dict[str, Dict[str, dict]]:
    """Latest normalized PostProxy stats per post id and platform (GET /posts/stats, 50 ids per call):
    impressions, likes, comments, shares, saved, follows (IG), clicks (LinkedIn)..."""
    m = _main()
    out: Dict[str, Dict[str, dict]] = {}
    ids = [i for i in dict.fromkeys(pp_ids) if i]
    for start in range(0, len(ids), 50):
        chunk = ids[start:start + 50]
        try:
            data = m.postproxy_get(settings, "/posts/stats?post_ids=" + ",".join(chunk), timeout=45)
        except Exception as exc:
            logger.warning("%s post stats fetch failed: %s", MARKER, exc)
            continue
        for pid, body in (data.get("data") or {}).items():
            for plat in (body or {}).get("platforms") or []:
                records = plat.get("records") or []
                if not records or not records[-1].get("stats"):
                    continue
                last = records[-1]
                out.setdefault(pid, {})[plat.get("platform")] = dict(last["stats"], recorded_at=last.get("recorded_at"))
    return out


def refresh_metrics(settings: dict, max_age_days: int = METRICS_MAX_AGE_DAYS, max_requests: int = 200) -> dict:
    m = _main()
    posts = m.load_scheduled_posts()
    cutoff = _utcnow() - timedelta(days=max_age_days)
    state = load_state()
    requests_made = updated = 0
    pp_enabled = bool(settings.get("postproxy_enabled") and (settings.get("postproxy_api_key") or os.environ.get("POSTPROXY_API_KEY")))
    collected: Dict[str, dict] = {}
    for post in posts:
        if post.get("status") not in {"SUCCESS", "PARTIAL_SUCCESS"}:
            continue
        posted = _parse_dt(post.get("posted_at"))
        if not posted or posted < cutoff or requests_made >= max_requests:
            continue
        metrics = dict(post.get("metrics") or {})
        changed = False
        if pp_enabled:
            ids = post.get("postproxy_post_ids") or ([post.get("postproxy_post_id")] if post.get("postproxy_post_id") else [])
            pp: Dict[str, Any] = {}
            for pid in [i for i in ids if i]:
                try:
                    latest = m.postproxy_get(settings, f"/posts/{pid}", timeout=30)
                    requests_made += 1
                    for item in latest.get("platforms") or []:
                        if item.get("platform"):
                            insights = item.get("insights") or {}
                            pp[item["platform"]] = {"status": item.get("status"), "impressions": insights.get("impressions"),
                                                    "insights": insights, "permalink": item.get("permalink")}
                except Exception as exc:
                    logger.warning("PostProxy insights fetch failed for %s: %s", pid, exc)
            if pp:
                metrics["postproxy"] = pp
                changed = True
        public = dict(metrics.get("public") or {})
        for platform, url in _permalinks(post).items():
            parser = {"tiktok": parse_tiktok_html, "youtube": parse_youtube_html}.get(platform)
            if not parser:
                continue
            try:
                data = parser(_get(url))
                requests_made += 1
                time.sleep(0.8)
            except Exception as exc:
                logger.warning("Public metrics fetch failed for %s: %s", url, exc)
                continue
            if not data:
                continue
            data.update({"fetched_at": _utcnow().isoformat(), "url": url})
            public[platform] = data
            changed = True
            if platform == "youtube" and data.get("available") is False:
                add_alert("youtube_video_unavailable",
                          f"YouTube video is no longer available ({data.get('reason') or data.get('status')}): {url}",
                          post.get("id"), state)
            if platform == "tiktok" and data.get("available") is False and "deleted" in str(data.get("status")).lower():
                add_alert("tiktok_video_deleted", f"TikTok video deleted: {url}", post.get("id"), state, email=False)
        if public:
            metrics["public"] = public
        try:
            tw = m.fetch_twitter_metrics_for_post(post, settings)
            if tw:
                metrics["twitter"] = tw
                changed = True
        except Exception:
            pass
        if changed:
            metrics["refreshed_at"] = _utcnow().isoformat()
            collected[post["id"]] = metrics
            updated += 1
    if pp_enabled:
        recent_posts = [p for p in posts if p.get("status") in {"SUCCESS", "PARTIAL_SUCCESS"}
                        and (_parse_dt(p.get("posted_at")) or cutoff) >= cutoff]
        id_map: Dict[str, str] = {}
        for p in recent_posts:
            for pid in (p.get("postproxy_post_ids") or ([p.get("postproxy_post_id")] if p.get("postproxy_post_id") else [])):
                if pid:
                    id_map[pid] = p["id"]
        stats = fetch_post_stats(settings, list(id_map))
        requests_made += (len(id_map) + 49) // 50
        for pid, by_platform in stats.items():
            local = id_map.get(pid)
            if not local:
                continue
            post = next((p for p in recent_posts if p["id"] == local), None)
            metrics = collected.get(local) or dict((post or {}).get("metrics") or {})
            merged = dict(metrics.get("stats") or {})
            merged.update(by_platform)
            metrics["stats"] = merged
            metrics["origin"] = (post or {}).get("origin") or origin_of((post or {}).get("media_source"))
            metrics["refreshed_at"] = _utcnow().isoformat()
            if local not in collected:
                updated += 1
            collected[local] = metrics
        try:
            snapshot_followers(settings, state)
        except Exception as exc:
            logger.warning("%s follower snapshot failed: %s", MARKER, exc)
    for pid_local, metrics in collected.items():
        if "origin" not in metrics:
            post = next((p for p in posts if p.get("id") == pid_local), {})
            metrics["origin"] = post.get("origin") or origin_of(post.get("media_source"))
    if collected:
        latest_posts = m.load_scheduled_posts()  # merge so concurrent writes are kept
        for p in latest_posts:
            if p.get("id") in collected:
                p["metrics"] = collected[p["id"]]
        m.save_scheduled_posts(latest_posts)
    state["metrics_last_run"] = _utcnow().isoformat()
    state["metrics_last_result"] = {"posts_updated": updated, "requests": requests_made}
    save_state(state)
    logger.info("%s metrics refresh: %s posts updated, %s requests", MARKER, updated, requests_made)
    return {"posts_updated": updated, "requests": requests_made}


def _safe_refresh_metrics(settings: dict) -> None:
    try:
        refresh_metrics(settings)
    except Exception as exc:
        logger.error("%s metrics refresh failed: %s", MARKER, exc)


def post_performance(post: dict) -> Optional[dict]:
    """Views per platform = public-page views when available, else PostProxy
    impressions. Returns None when nothing real has been collected."""
    metrics = post.get("metrics") or {}
    public = metrics.get("public") or {}
    pp = metrics.get("postproxy") or {}
    per: Dict[str, dict] = {}
    for platform in sorted(set(public) | set(pp)):
        pub = public.get(platform) or {}
        views, source = pub.get("views"), "public_page"
        if views is None:
            views, source = (pp.get(platform) or {}).get("impressions"), "postproxy_insights"
        if views is None:
            continue
        per[platform] = {"views": int(views), "likes": pub.get("likes"), "comments": pub.get("comments"),
                         "shares": pub.get("shares"), "source": source}
    tw = metrics.get("twitter")
    if tw:
        entry = per.setdefault("twitter", {"views": None, "source": "x_api"})
        entry.update({"likes": tw.get("like_count"), "comments": tw.get("reply_count"),
                      "shares": int(tw.get("retweet_count") or 0) + int(tw.get("quote_count") or 0)})
    if not per:
        return None
    views = sum(v["views"] for v in per.values() if isinstance(v.get("views"), int))
    engagements = sum(int(v.get(k) or 0) for v in per.values() for k in ("likes", "comments", "shares"))
    return {"views": views, "engagements": engagements, "platforms": per}


def _median(values: List[float]) -> float:
    s = sorted(values)
    mid = len(s) // 2
    return float(s[mid]) if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0


def analytics_summary() -> dict:
    m = _main()
    try:
        from zoneinfo import ZoneInfo

        pt = ZoneInfo("America/Los_Angeles")
    except Exception:
        pt = None
    rows: List[dict] = []
    without = 0
    by_hour: Dict[int, List[float]] = {}
    hour_pt: Dict[int, int] = {}
    for post in m.load_scheduled_posts():
        if post.get("status") not in {"SUCCESS", "PARTIAL_SUCCESS"}:
            continue
        perf = post_performance(post)
        if not perf:
            without += 1
            continue
        posted = post.get("posted_at") or post.get("scheduled_time")
        server_hour = None
        try:
            server_hour = datetime.fromisoformat(str(posted)).hour
        except Exception:
            pass
        dt = _parse_dt(posted)
        ph = dt.astimezone(pt).hour if (dt and pt) else None
        score = perf["views"] + 10 * perf["engagements"]
        rows.append({"id": post.get("id"), "campaign_title": post.get("campaign_title"), "platform": post.get("platform"),
                     "posted_at": posted, "media_source": post.get("media_source"), "metrics": perf,
                     "metrics_source": "real", "engagement_score": score, "views": perf["views"],
                     "engagements": perf["engagements"], "hour": server_hour, "hour_pt": ph})
        if server_hour is not None:
            by_hour.setdefault(server_hour, []).append(score)
            if ph is not None:
                hour_pt[server_hour] = ph
    rows.sort(key=lambda r: r["engagement_score"], reverse=True)
    n = len(rows)
    comparable = {h: v for h, v in by_hour.items() if len(v) >= 3}
    best_hour = max(comparable, key=lambda h: _median(comparable[h])) if (n >= 8 and len(comparable) >= 2) else None
    if best_hour is None:
        confidence = "none" if n < 3 else "low"
    else:
        smallest = min(len(v) for v in comparable.values())
        confidence = "high" if (n >= 30 and smallest >= 10) else ("medium" if (n >= 15 and smallest >= 5) else "low")
    return {
        "posts": rows,
        "best_hour": best_hour,
        "best_hour_pt": hour_pt.get(best_hour) if best_hour is not None else None,
        "sample_size": n,
        "posts_without_metrics": without,
        "minimum_recommended_sample_size": 8,
        "confidence": confidence,
        "hour_breakdown": {h: round(_median(v), 1) for h, v in by_hour.items()},
        "hour_breakdown_note": "Median of (views + 10 x likes/comments/shares) per server hour, real metrics only; "
                               "an hour needs 3+ posts to be compared, and 2+ comparable hours are required to pick a best hour.",
        "metrics_last_run": load_state().get("metrics_last_run"),
    }


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------

def status_payload(settings: Optional[dict] = None) -> dict:
    state = load_state()
    settings = settings or _main().load_settings()
    try:
        platforms, notes = resolve_platforms(settings, state)
    except Exception as exc:
        platforms, notes = [], [str(exc)]
    slots = state.get("slots", {})
    return {
        "marker": MARKER, "version": VERSION, "enabled": enabled(), "paused": paused(),
        "stitched_shorts": stitched_shorts.enabled(settings), "stitched_version": stitched_shorts.VERSION,
        "shorts_cost_cap_usd": stitched_shorts.cost_cap(settings), "single_cost_cap_usd": single_cost_cap(settings),
        "video_engines": engine_ladder(), "autopilot_format": stitched_shorts.autopilot_format(settings),
        "transform_fallback": transform_fallback_enabled(), "max_slot_attempts": max_slot_attempts(),
        "legacy_boot_patches_skipped": skipped_legacy_modules(), "legacy_patches_allowed": legacy_patches_allowed(),
        "slot_hours_server": slot_hours(settings), "platforms": platforms, "platform_notes": notes,
        "x_quota_until": state.get("x_quota_until"),
        "recent_slots": {k: slots[k] for k in sorted(slots)[-10:]},
        "alerts": state.get("alerts", [])[-15:],
        "recent_topics": [h.get("topic") for h in state.get("history", [])[-10:]],
        "metrics_last_run": state.get("metrics_last_run"), "metrics_last_result": state.get("metrics_last_result"),
        "followers": state.get("followers"),
    }


def media_tools() -> dict:
    import shutil as _sh
    ff = _bin("ffmpeg")
    info = {"ffmpeg": bool(_sh.which(ff) or os.path.exists(ff)), "font": bool(_font()), "drawtext": None}
    try:
        res = subprocess.run([ff, "-hide_banner", "-filters"], capture_output=True, text=True, timeout=10)
        info["drawtext"] = " drawtext " in res.stdout
    except Exception:
        pass
    return info


_MEDIA_TOOLS: Optional[dict] = None


def health() -> dict:
    global _MEDIA_TOOLS
    if _MEDIA_TOOLS is None:
        _MEDIA_TOOLS = media_tools()
    out = {"marker": MARKER, "version": VERSION, "enabled": enabled(), "paused": paused(),
           "legacy_skipped": skipped_legacy_modules(), "media_tools": _MEDIA_TOOLS}
    try:  # read-only: which format and platforms the next slot will use (no secrets, no live API calls)
        settings = _main().load_settings()
        out["autopilot_format"] = stitched_shorts.autopilot_format(settings)
        out["shorts_cost_cap_usd"] = stitched_shorts.cost_cap(settings)
        out["single_cost_cap_usd"] = single_cost_cap(settings)
        out["video_engines"] = engine_ladder()
        out["platforms"] = resolve_platforms(settings)[0]
    except Exception as exc:
        out["platforms_error"] = str(exc)[:200]
    return out
