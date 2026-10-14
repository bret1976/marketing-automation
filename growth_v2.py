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

logger = logging.getLogger("growth_v2")

MARKER = "GROWTH_V2"
VERSION = "2026-10-14.2"
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
    return not _falsy(os.environ.get("AUTOPILOT_TRANSFORM_FALLBACK", "1"))


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


def _clean_line(text: Any) -> str:
    text = strip_links(str(text or ""))
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
        tags = re.split(r"[\s,]+", tags)
    out: List[str] = []
    for tag in tags or []:
        word = re.sub(r"[^\w]", "", str(tag).lstrip("#"))
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
    short_body = "\n".join([hook] + lines + ([credit_short] if credit_short else []))
    short = strip_links(f"{short_body}\n\n{tag_line}")
    angle = _clean_line(concept.get("linkedin_angle") or "")
    li_parts = [hook] + lines + ([angle] if angle else []) + ([credit_long] if credit_long else [])
    linkedin = "\n\n".join(p for p in li_parts if p) + f"\n\n{tag_line}"
    tweet_tags = " ".join(tags[:2])
    tweet = clamp_words(f"{hook} {lines[0]}" if lines else hook, 280 - len(tweet_tags) - 2)
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
subject centered (it will be cropped to 9:16), clear visible motion in the FIRST second.
It must be factually correct.

Return ONLY JSON with these keys:
  topic: short name
  topic_key: 3-6 lowercase words identifying the subject
  veo_prompt: 40-90 words, photoreal, describe camera + motion, centered subject
  on_screen_hook: max 6 words, shown on screen in the first 2 seconds
  hook: the caption's first line, about 8-10 words, max 60 characters, curiosity or surprising fact,
        no hashtags, no emoji, no links, must NOT start with "At 6Frame Studio"
  lines: array of 1-2 short plain-language lines (each under 100 characters) explaining the fact
  hashtags: array of 3-5 topic hashtags (e.g. #Physics #Engineering), no brand hashtags
  linkedin_angle: one plain sentence on how the clip was made with AI

Do NOT reuse any of these recent topics: {avoid}
{inspiration}"""


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
    return {
        "topic": clamp_words(_clean_line(concept.get("topic") or hook), 80),
        "topic_key": str(concept.get("topic_key") or concept.get("topic") or hook),
        "veo_prompt": prompt[:900],
        "on_screen_hook": clamp_words(_clean_line(concept.get("on_screen_hook") or hook), 40),
        "hook": hook,
        "lines": [clamp_words(_clean_line(l), 110) for l in lines if _clean_line(l)][:2],
        "hashtags": normalize_hashtags(concept.get("hashtags")),
        "linkedin_angle": clamp_words(_clean_line(concept.get("linkedin_angle") or ""), 200),
    }


def plan_concept(settings: dict, state: dict) -> dict:
    m = _main()
    avoid_keys = used_keys(state)
    recent_topics = [h.get("topic") for h in _recent_history(state, REPEAT_WINDOW_DAYS) if h.get("topic")][-30:]
    inspiration = ""
    try:
        titles = [t.get("title") for t in (m.load_growth_os().get("last_trend_scan") or []) if t.get("title")][:6]
        if titles:
            inspiration = "Context only (do not copy or name creators) - titles trending this week: " + "; ".join(titles)
    except Exception:
        pass
    concept: dict = {}
    for _ in range(2):
        try:
            result = m.call_gemini_json(settings, CONCEPT_PROMPT.format(avoid=", ".join(recent_topics) or "none yet", inspiration=inspiration), {}, use_search=False)
            if isinstance(result, list) and result:
                result = result[0]
            concept = sanitize_concept(result)
        except Exception as exc:
            logger.warning("%s concept generation failed: %s", MARKER, exc)
            concept = {}
        if concept and concept_key(concept) not in avoid_keys:
            concept["concept_source"] = "gemini"
            return concept
        if concept:
            recent_topics.append(concept.get("topic") or "")
    for candidate in FALLBACK_CONCEPTS:
        c = sanitize_concept(dict(candidate))
        if concept_key(c) not in avoid_keys:
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


def _textfile(text: str, width: int) -> str:
    wrapped = "\n".join(textwrap.wrap(text, width=width)) or text
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="g2txt_")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(wrapped)
    return path


def render_vertical(src: str, dst: str, hook: str, commentary: Optional[List[str]] = None,
                    footer: str = "", max_seconds: float = 15.0, require_text: bool = False) -> dict:
    """1080x1920 blur-pad reframe + hook text for the first 2.8s, optional timed
    commentary lines and a persistent footer credit."""
    font = _font()
    if require_text and not font:
        raise RuntimeError("No font available for on-screen commentary; refusing an untransformed clip.")
    temps: List[str] = []
    try:
        filt = ("[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,gblur=sigma=22[bg];"
                "[0:v]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
                "[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p")
        if font and hook:
            tf = _textfile(hook.upper(), 18)
            temps.append(tf)
            filt += (f",drawtext=fontfile='{font}':textfile='{tf}':fontcolor=white:fontsize=78:line_spacing=10:"
                     f"box=1:boxcolor=black@0.6:boxborderw=24:x=(w-text_w)/2:y=h*0.14:enable='lt(t,2.8)'")
        if font and commentary:
            for (start, end), line in zip([(2.8, 7.0), (7.0, max_seconds)], commentary[:2]):
                tf = _textfile(line, 26)
                temps.append(tf)
                filt += (f",drawtext=fontfile='{font}':textfile='{tf}':fontcolor=white:fontsize=56:line_spacing=8:"
                         f"box=1:boxcolor=black@0.55:boxborderw=18:x=(w-text_w)/2:y=h*0.70:enable='between(t,{start},{end})'")
        if font and footer:
            tf = _textfile(footer, 48)
            temps.append(tf)
            filt += (f",drawtext=fontfile='{font}':textfile='{tf}':fontcolor=white@0.85:fontsize=34:"
                     f"box=1:boxcolor=black@0.45:boxborderw=10:x=(w-text_w)/2:y=h-170")
        filt += "[outv]"
        cmd = [_bin("ffmpeg"), "-y", "-i", src, "-t", f"{max_seconds:.2f}", "-filter_complex", filt,
               "-map", "[outv]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
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
        return info
    finally:
        for path in temps:
            try:
                os.remove(path)
            except OSError:
                pass


def generate_original_clip(concept: dict, settings: dict) -> Tuple[str, dict]:
    """Veo first (run_video_generation already does labeled Veo->FAL fallback),
    then FAL directly. Returns (abs_path, info)."""
    import orchestrator

    preferred = os.environ.get("GROWTH_VIDEO_ENGINE") or settings.get("autonomous_video_engine") or "google_veo"
    engines = [preferred]
    if orchestrator.fal_key_present(settings) and "fal_hailuo_23" not in engines:
        engines.append("fal_hailuo_23")
    errors = []
    for engine in engines:
        job_id = f"g2_{uuid.uuid4().hex}"
        try:
            orchestrator.run_video_generation(job_id, concept["veo_prompt"], settings, engine, 8)
        except Exception as exc:
            errors.append(f"{engine}: {exc}")
            continue
        status = orchestrator.get_job_status(job_id)
        if status.get("status") == "SUCCESS":
            result = status.get("result") or {}
            abs_path = _main().resolve_local_video_path(result.get("video_path") or "")
            if abs_path and os.path.exists(abs_path):
                return abs_path, {"engine": result.get("engine") or engine, "job_id": job_id}
            errors.append(f"{engine}: rendered file missing")
        else:
            errors.append(f"{engine}: {status.get('message')}")
    raise RuntimeError("Original video generation failed: " + " | ".join(errors)[-800:])


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
    out.sort(key=lambda c: 1 if "reddit.com" in c["url"].lower() else 0)
    return out[:max(1, limit)]


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
    for cand in sources:
        try:
            downloaded = m.download_and_trim_original_video(cand["url"], cand["title"], 15, False, 180)
            source = cand
            break
        except Exception as exc:
            failures.append(f"{cand['url']}: {str(exc)[:120]}")
            _remember_failed_source(state, cand["url"], str(exc))
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

async def produce(settings: dict, state: dict) -> Tuple[str, dict, dict]:
    loop = asyncio.get_running_loop()
    errors: List[str] = []
    try:
        concept = await loop.run_in_executor(None, plan_concept, settings, state)
        raw_path, gen_info = await loop.run_in_executor(None, generate_original_clip, concept, settings)
        out = os.path.join(_main().GENERATED_DIR, f"g2_original_{uuid.uuid4().hex}.mp4")
        info = await loop.run_in_executor(None, render_vertical, raw_path, out, concept["on_screen_hook"], None, "", 15.0, False)
        info.update(gen_info)
        info["media_source"] = "ai_generated"
        return out, concept, info
    except Exception as exc:
        errors.append(str(exc))
        logger.error("%s original render failed: %s", MARKER, exc)
    try:
        transformed = await loop.run_in_executor(None, build_transformed_clip, settings, state)
    except Exception as exc:
        transformed = None
        errors.append(f"transform fallback: {exc}")
    if transformed:
        out, concept, info = transformed
        info["media_source"] = "transformed_repost"
        return out, concept, info
    if not transform_fallback_enabled():
        errors.append("transform fallback disabled (AUTOPILOT_TRANSFORM_FALLBACK=0)")
    raise RuntimeError(" | ".join(errors) or "no video produced")


def build_post(abs_video: str, concept: dict, info: dict, platforms: List[str]) -> dict:
    web = f"/static/assets/generated/{os.path.basename(abs_video)}"
    source = info.get("source") or {}
    credit = {"creator": source.get("creator"), "url": source.get("url")} if info.get("media_source") == "transformed_repost" else None
    captions = build_captions(concept, credit)
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
        "media_info": {k: v for k, v in info.items() if k != "source"},
        "concept": {k: concept.get(k) for k in ("topic", "topic_key", "on_screen_hook", "hook", "lines", "hashtags", "concept_source", "commentary")},
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
    if settings.get("require_autopilot_approval", False):
        post["status"] = "AWAITING_APPROVAL"
        posts.append(post)
        m.save_scheduled_posts(posts)
        record_history(state, post)
        save_state(state)
        return {"ok": True, "awaiting_approval": True, "post_id": post["id"]}
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
        "transform_fallback": transform_fallback_enabled(), "max_slot_attempts": max_slot_attempts(),
        "legacy_boot_patches_skipped": skipped_legacy_modules(), "legacy_patches_allowed": legacy_patches_allowed(),
        "slot_hours_server": slot_hours(settings), "platforms": platforms, "platform_notes": notes,
        "x_quota_until": state.get("x_quota_until"),
        "recent_slots": {k: slots[k] for k in sorted(slots)[-10:]},
        "alerts": state.get("alerts", [])[-15:],
        "recent_topics": [h.get("topic") for h in state.get("history", [])[-10:]],
        "metrics_last_run": state.get("metrics_last_run"), "metrics_last_result": state.get("metrics_last_result"),
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
    return {"marker": MARKER, "version": VERSION, "enabled": enabled(), "paused": paused(),
            "legacy_skipped": skipped_legacy_modules(), "media_tools": _MEDIA_TOOLS}
