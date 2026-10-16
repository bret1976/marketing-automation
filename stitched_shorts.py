"""Stitched 20-30s Shorts (Growth V2 format, 2026-10-16).

One coherent 3-4 shot story per Short, AI filmmaking niche (6Frame Studio voice:
behind-the-scenes, "how this shot was made"). Originals only - every frame is
generated here, nothing is downloaded from other creators.

  Shot 1  hook, 4-6s, Veo 3.1 Fast (Gemini API, 720p, 9:16, native audio)
  Shot 2-4 b-roll, Seedance 1 Pro Fast via fal (720p, 9:16, silent)
           fallback per shot: Veo 3.1 Lite (Gemini API)
  ffmpeg  normalize to 1080x1920 @30fps, hard cuts, burned hook (first second) +
          per-shot captions, synthesized royalty-free music bed + whoosh SFX on
          cuts under the silent shots (Veo audio kept on the hook), loop ending
          (last 0.5s dissolves back to the opening frame), EBU R128 loudnorm.

Guards: max 2 retries per shot (3 attempts), hard cost cap per Short
(default $1.50, SHORTS_COST_CAP_USD / settings.shorts_cost_cap_usd) checked
BEFORE every paid call, per-Short cost ledger in the result + log.

Kill switch: STITCHED_SHORTS=0 (or settings.autopilot_format="single") restores
the single 8s clip pipeline in growth_v2.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger("stitched_shorts")
MARKER = "STITCHED_SHORTS"
VERSION = "2026-10-16.1"

W, H, FPS = 1080, 1920, 30
MIN_TOTAL, MAX_TOTAL = 20.0, 30.0
HOOK_MIN, HOOK_MAX = 4, 6
BROLL_MIN, BROLL_MAX = 4, 8
MAX_SHOT_RETRIES = 2
DEFAULT_COST_CAP = 0.75  # hard cap per Short (Bret, 2026-10-16)
LOOP_DISSOLVE = 0.5

VEO_FAST_MODEL = "veo-3.1-fast-generate-preview"
VEO_LITE_MODEL = "veo-3.1-lite-generate-preview"
SEEDANCE_MODEL = "fal-ai/bytedance/seedance/v1/pro/fast/text-to-video"
GEMINI_TEXT_MODEL = "gemini-3.6-flash"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

# USD per generated second at 720p (Gemini API pricing page, 2026-10-15).
PRICE_PER_SECOND = {"veo_fast": 0.10, "veo_lite": 0.05}
# Seedance on fal bills by video tokens: w*h*fps*seconds/1024 at $1.0 / 1M tokens.
SEEDANCE_USD_PER_MTOKEN = 1.0
SEEDANCE_720P = (720, 1280, 24)
LLM_CALL_USD = 0.01  # generous estimate for one planning call

PLATFORMS = ("youtube", "tiktok", "instagram", "linkedin")
# Staggered posting offsets (minutes after the first platform) - never simultaneous.
STAGGER_MINUTES = {"youtube": 0, "tiktok": 45, "instagram": 100, "linkedin": 180, "twitter": 240}
_HASHTAG_RE = re.compile(r"#\w+")
_URL_RE = re.compile(r"(https?://\S+|www\.\S+)", re.I)


def enabled(settings: Optional[dict] = None) -> bool:
    if str(os.environ.get("STITCHED_SHORTS", "1")).strip().lower() in {"0", "false", "no", "off"}:
        return False
    return str((settings or {}).get("autopilot_format") or "stitched").lower() != "single"


def test_mode(settings: Optional[dict] = None) -> bool:
    """SHORTS_TEST_MODE=1 (or settings.shorts_test_mode): zero retries, no paid fallback model."""
    raw = os.environ.get("SHORTS_TEST_MODE", (settings or {}).get("shorts_test_mode", ""))
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def max_retries(settings: Optional[dict] = None) -> int:
    return 0 if test_mode(settings) else MAX_SHOT_RETRIES


def cost_cap(settings: Optional[dict] = None) -> float:
    for raw in (os.environ.get("SHORTS_COST_CAP_USD"), (settings or {}).get("shorts_cost_cap_usd")):
        try:
            if raw not in (None, ""):
                return max(0.1, float(raw))
        except (TypeError, ValueError):
            pass
    return DEFAULT_COST_CAP


# --------------------------------------------------------------------------
# cost ledger
# --------------------------------------------------------------------------

def seedance_cost(seconds: float, res: Tuple[int, int, int] = SEEDANCE_720P) -> float:
    w, h, fps = res
    return round(w * h * fps * float(seconds) / 1024 / 1_000_000 * SEEDANCE_USD_PER_MTOKEN, 4)


def shot_cost(engine: str, seconds: float) -> float:
    if engine == "seedance_fast":
        return seedance_cost(seconds)
    return round(PRICE_PER_SECOND[engine] * float(seconds), 4)


class CostCapExceeded(RuntimeError):
    pass


class CostLedger:
    def __init__(self, cap: float):
        self.cap = float(cap)
        self.items: List[dict] = []

    @property
    def total(self) -> float:
        return round(sum(i["usd"] for i in self.items), 4)

    def reserve(self, label: str, usd: float) -> None:
        """Raise BEFORE a paid call that would push the Short over the cap."""
        if self.total + usd > self.cap + 1e-9:
            raise CostCapExceeded(f"cost cap ${self.cap:.2f} would be exceeded by {label} "
                                  f"(spent ${self.total:.3f} + ${usd:.3f})")

    def charge(self, label: str, usd: float, ok: bool = True, **extra: Any) -> None:
        self.items.append({"label": label, "usd": round(float(usd), 4), "ok": ok, **extra})

    def as_dict(self) -> dict:
        return {"total_usd": self.total, "cap_usd": self.cap, "items": list(self.items)}


# --------------------------------------------------------------------------
# planning (one coherent story per Short)
# --------------------------------------------------------------------------

PLAN_PROMPT = """You are the short-form director for 6Frame Studio, an AI filmmaking studio that makes cinematic
AI short films. Voice: confident filmmaker showing behind-the-scenes craft ("how this shot was made"),
plain words, no hype, no "At 6Frame Studio" opener.
Plan ONE original vertical 9:16 Short of {total_min}-{total_max} seconds told in {n_shots} shots that form one
coherent mini-story: SAME subject/character, SAME world, SAME color palette and lens look in every shot.
Everything is AI-generated: no real people's faces, no celebrities, no logos/brands, no copyrighted
characters, no readable text inside the video.

Shot 1 is the HOOK ({hook_min}-{hook_max}s): motion AND a distinct sound effect in the very first second
(name the sound), no slow fade-in, no static opener.
Shots 2..{n_shots} are cinematic b-roll ({broll_min}-{broll_max}s each) continuing the story; they are silent
(music/SFX is added in the edit).
LOOP: the final shot ends on a composition that matches the opening frame of shot 1 (say so in its prompt).

Return ONLY JSON:
{{
  "topic": short name of the film idea,
  "topic_key": 3-6 lowercase words,
  "title_line": 2-6 word punchy title for THIS film, no numbering/hashtags/emoji/brand. Must differ from: {avoid_titles},
  "style": one sentence locking subject, palette, lens, lighting (repeated in every prompt),
  "on_screen_hook": max 6 words shown in the first second, MUST contain a number in digits
        (e.g. "3 prompts, 1 impossible shot", "This shot took 47 tries"),
  "shots": [ {{"role": "hook"|"broll", "seconds": integer, "prompt": 40-90 words for the video model: style + camera +
        subject + exact visible action (+ sound for the hook) + vertical framing, "caption": max 7 words on-screen
        caption for this shot (behind-the-scenes note, no hashtags)}} ... exactly {n_shots} items, hook first ],
  "comment_question": one short question ending with "?" (max 80 chars),
  "platforms": {{
     "youtube":   {{"title": max 70 chars, contains the hook number, "description": 1-2 short lines, "hashtags": 3-5}},
     "tiktok":    {{"caption": max 150 chars, casual, curiosity, "hashtags": 3-5}},
     "instagram": {{"caption": 2-3 short lines incl. a save/share reason, "hashtags": 3-5}},
     "linkedin":  {{"caption": 3-5 short lines, professional craft/process angle, what the workflow teaches, "hashtags": 3}}
  }}
}}
Consistency: text-to-video models cannot remember a face, so prefer a non-human hero (prop, robot, creature,
vehicle, landscape) or describe any person with the SAME exact age, hair, clothing in every prompt.
Truth: never invent stats or process numbers (render counts, % savings); the hook number must describe what
is on screen or the real setup (e.g. shot count, seconds). Do not name tools other than Veo/Seedance.
Hashtags: specific (e.g. #AIFilmmaking #Veo3 #Seedance #CinematicAI #BehindTheScenes), never generic (#fyp #viral).
Do NOT reuse these recent topics: {avoid}"""

FALLBACK_PLANS: List[dict] = [{
    "topic": "Neon rain detective chase",
    "topic_key": "neon rain detective chase",
    "title_line": "One Alley, Four Shots",
    "style": "Lone figure in a long charcoal coat, rain-soaked neon alley, teal and magenta palette, anamorphic 35mm look, shallow depth of field.",
    "on_screen_hook": "4 shots, 0 cameras",
    "shots": [
        {"role": "hook", "seconds": 5, "caption": "Shot 1: the hook",
         "prompt": "Lone figure in a long charcoal coat, rain-soaked neon alley, teal and magenta palette, anamorphic 35mm look. Instant start: a puddle explodes under a boot with a sharp splash and thunder crack, camera whips up to the figure turning toward lens, rain streaks, vertical 9:16 framing, subject centered."},
        {"role": "broll", "seconds": 6, "caption": "Same coat, same rain, new angle",
         "prompt": "Lone figure in a long charcoal coat, rain-soaked neon alley, teal and magenta palette, anamorphic 35mm look. Low tracking shot following the figure running past flickering neon signs without text, water spraying from each step, vertical 9:16, centered."},
        {"role": "broll", "seconds": 6, "caption": "Lighting carries the story",
         "prompt": "Lone figure in a long charcoal coat, rain-soaked neon alley, teal and magenta palette, anamorphic 35mm look. Close-up of a gloved hand pushing open a rusted door, magenta light spilling across wet bricks, slow push-in, vertical 9:16."},
        {"role": "broll", "seconds": 6, "caption": "Ends where it began",
         "prompt": "Lone figure in a long charcoal coat, rain-soaked neon alley, teal and magenta palette, anamorphic 35mm look. Wide shot pulling back down the alley as the figure stops by the same puddle and turns, ending on the opening composition, vertical 9:16."},
    ],
    "comment_question": "Which shot would you redo first?",
    "platforms": {},
}]


def call_gemini_text_json(settings: dict, prompt: str) -> Any:
    key = settings.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("Missing Gemini API key")
    r = requests.post(f"{GEMINI_BASE}/models/{GEMINI_TEXT_MODEL}:generateContent",
                      headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                      json={"contents": [{"parts": [{"text": prompt}]}],
                            "generationConfig": {"responseMimeType": "application/json", "temperature": 1.0}},
                      timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini plan HTTP {r.status_code}: {r.text[:300]}")
    text = "".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"])
    text = text.strip().removeprefix("```json").removesuffix("```").strip()
    return json.loads(text)


def _clean(text: Any, limit: int = 400) -> str:
    return " ".join(_URL_RE.sub("", str(text or "")).split())[:limit].strip()


def has_number(text: Any) -> bool:
    return bool(re.search(r"\d", str(text or "")))


BANNED_TAGS = {"#fyp", "#foryou", "#viral", "#trending", "#foryoupage", "#sora", "#soraai", "#runway", "#runwayml",
               "#kling", "#klingai", "#pika", "#midjourney", "#luma"}  # generic, or tools we did not use


def clean_hashtags(tags: Any, fallback: Optional[List[str]] = None, lo: int = 3, hi: int = 5) -> List[str]:
    raw = tags if isinstance(tags, list) else _HASHTAG_RE.findall(str(tags or ""))
    out: List[str] = []
    for t in list(raw) + list(fallback or ["#AIFilmmaking", "#CinematicAI", "#BehindTheScenes", "#AIVideo", "#Veo3"]):
        t = "#" + re.sub(r"[^\w]", "", str(t).lstrip("#"))
        if len(t) < 3 or t.lower() in BANNED_TAGS:
            continue
        if t.lower() not in {x.lower() for x in out}:
            out.append(t)
        if len(out) >= hi:
            break
    return out[:max(lo, min(hi, len(out)))]


def sanitize_plan(plan: Any) -> dict:
    """Clamp durations so the Short lands in 20-30s with 3-4 shots and a numbered hook."""
    if not isinstance(plan, dict):
        raise ValueError("plan is not an object")
    shots = [s for s in (plan.get("shots") or []) if isinstance(s, dict) and _clean(s.get("prompt"))][:4]
    if len(shots) < 3:
        raise ValueError(f"plan needs 3-4 shots, got {len(shots)}")
    style = _clean(plan.get("style"), 300)
    out_shots = []
    for i, s in enumerate(shots):
        role = "hook" if i == 0 else "broll"
        lo, hi = (HOOK_MIN, HOOK_MAX) if role == "hook" else (BROLL_MIN, BROLL_MAX)
        try:
            secs = int(round(float(s.get("seconds") or hi)))
        except (TypeError, ValueError):
            secs = hi
        prompt = _clean(s.get("prompt"), 1100)
        if style and style.lower()[:40] not in prompt.lower():
            prompt = f"{style} {prompt}"
        if "9:16" not in prompt:
            prompt += " Vertical 9:16 framing, subject centered."
        out_shots.append({"index": i + 1, "role": role, "seconds": max(lo, min(hi, secs)),
                          "prompt": prompt, "caption": _clean(s.get("caption"), 60)})
    if out_shots[0]["seconds"] % 2:  # Veo accepts 4/6/8
        out_shots[0]["seconds"] = 4 if out_shots[0]["seconds"] < 5 else 6
    fit_durations(out_shots)
    hook = _clean(plan.get("on_screen_hook"), 48)
    if not has_number(hook):
        raise ValueError("on-screen hook has no number")
    return {
        "topic": _clean(plan.get("topic"), 80), "topic_key": _clean(plan.get("topic_key"), 60).lower(),
        "title_line": _clean(plan.get("title_line"), 60), "style": style, "on_screen_hook": hook,
        "shots": out_shots, "comment_question": _clean(plan.get("comment_question"), 80),
        "platforms": plan.get("platforms") if isinstance(plan.get("platforms"), dict) else {},
    }


def fit_durations(shots: List[dict]) -> None:
    """Grow/shrink b-roll so the total sits in [MIN_TOTAL, MAX_TOTAL] (loop dissolve overlaps 0)."""
    total = lambda: sum(s["seconds"] for s in shots)
    broll = [s for s in shots if s["role"] == "broll"]
    guard = 0
    while total() < MIN_TOTAL and guard < 40:
        guard += 1
        grow = [s for s in broll if s["seconds"] < BROLL_MAX]
        if not grow:
            break
        min(grow, key=lambda s: s["seconds"])["seconds"] += 1
    while total() > MAX_TOTAL and guard < 80:
        guard += 1
        shrink = [s for s in broll if s["seconds"] > BROLL_MIN]
        if not shrink:
            break
        max(shrink, key=lambda s: s["seconds"])["seconds"] -= 1


def plan_short(settings: dict, avoid_topics: Optional[List[str]] = None, avoid_titles: Optional[List[str]] = None,
               llm: Optional[Callable[[dict, str], Any]] = None, n_shots: int = 4, ledger: Optional[CostLedger] = None) -> dict:
    llm = llm or call_gemini_text_json
    prompt = PLAN_PROMPT.format(total_min=int(MIN_TOTAL), total_max=int(MAX_TOTAL), n_shots=n_shots,
                                hook_min=HOOK_MIN, hook_max=HOOK_MAX, broll_min=BROLL_MIN, broll_max=BROLL_MAX,
                                avoid=", ".join(avoid_topics or []) or "none yet",
                                avoid_titles="; ".join((avoid_titles or [])[-40:]) or "none yet")
    last_err = None
    for _ in range(1 if test_mode(settings) else 3):
        try:
            if ledger:
                ledger.reserve("plan_llm", LLM_CALL_USD)
                ledger.charge("plan_llm", LLM_CALL_USD)
            raw = llm(settings, prompt)
            if isinstance(raw, list) and raw:
                raw = raw[0]
            plan = sanitize_plan(raw)
            low_topics = {t.lower() for t in (avoid_topics or []) if t}
            if plan["topic"].lower() in low_topics:
                raise ValueError("topic repeats a recent topic")
            plan["plan_source"] = "gemini"
            return plan
        except Exception as exc:  # retry with a new plan
            last_err = exc
            logger.warning("%s plan rejected: %s", MARKER, exc)
    logger.warning("%s using curated fallback plan (%s)", MARKER, last_err)
    plan = sanitize_plan(json.loads(json.dumps(FALLBACK_PLANS[0])))
    plan["plan_source"] = "curated_fallback"
    return plan


# --------------------------------------------------------------------------
# per-platform captions + staggered schedule
# --------------------------------------------------------------------------

TOOL_TAGS = {"#veo3": {"veo_fast", "veo_lite"}, "#veo": {"veo_fast", "veo_lite"}, "#seedance": {"seedance_fast"}}


def filter_tool_tags(tags: List[str], used_engines: Optional[set]) -> List[str]:
    if used_engines is None:
        return tags
    return [t for t in tags if t.lower() not in TOOL_TAGS or TOOL_TAGS[t.lower()] & used_engines]


def build_platform_captions(plan: dict, used_engines: Optional[set] = None) -> Dict[str, dict]:
    hook = plan.get("on_screen_hook") or ""
    title = plan.get("title_line") or hook
    q = plan.get("comment_question") or ""
    p = plan.get("platforms") or {}
    base_tags = ["#AIFilmmaking", "#CinematicAI", "#BehindTheScenes", "#Veo3", "#Seedance"]

    base_tags = filter_tool_tags(base_tags, used_engines)

    def _tags(tags, fallback=None, lo=3, hi=5):  # tool-aware: never tag a model we did not use
        raw = tags if isinstance(tags, list) else _HASHTAG_RE.findall(str(tags or ""))
        return clean_hashtags(filter_tool_tags(list(raw), used_engines), fallback, lo, hi)

    def get(name: str) -> dict:
        v = p.get(name)
        return v if isinstance(v, dict) else {}

    yt = get("youtube")
    yt_title = _clean(yt.get("title") or f"{title}: {hook}", 100)
    if not has_number(yt_title):
        yt_title = _clean(f"{hook} | {title}", 100)
    yt_tags = _tags(yt.get("hashtags"), base_tags)
    yt_desc = _clean(yt.get("description") or f"How this AI short was made, shot by shot. {q}", 300)
    tt = get("tiktok")
    tt_tags = _tags(tt.get("hashtags"), base_tags)
    tt_cap = _clean(tt.get("caption") or f"{hook}. Watch it loop. {q}", 150)
    ig = get("instagram")
    ig_tags = _tags(ig.get("hashtags"), base_tags)
    ig_cap = str(ig.get("caption") or f"{hook}\nEvery frame AI-generated, cut like a real film.\nSave this for your next shot list.")
    ig_cap = "\n".join(_clean(l, 200) for l in ig_cap.splitlines() if _clean(l))
    li = get("linkedin")
    li_tags = _tags(li.get("hashtags"), base_tags, lo=3, hi=3)
    li_cap = str(li.get("caption") or f"{title}\n\n{hook}. One coherent shot list, two video models, one edit.\n\nThe workflow: plan the story first, lock the style, then generate shot by shot.")
    li_cap = "\n".join(_clean(l, 300) for l in li_cap.splitlines())
    out = {
        "youtube": {"title": yt_title, "caption": f"{yt_title}\n{yt_desc}\n\n{' '.join(yt_tags)}", "hashtags": yt_tags},
        "tiktok": {"caption": f"{tt_cap}\n\n{' '.join(tt_tags)}", "hashtags": tt_tags},
        "instagram": {"caption": f"{ig_cap}\n\n{' '.join(ig_tags)}", "hashtags": ig_tags},
        "linkedin": {"caption": f"{li_cap.strip()}\n\n{' '.join(li_tags)}", "hashtags": li_tags},
    }
    for v in out.values():
        v["caption"] = _URL_RE.sub("", v["caption"]).strip()
    return out


def staggered_schedule(platforms: List[str], start: Optional[datetime] = None) -> Dict[str, str]:
    start = start or datetime.now()
    order = sorted(platforms, key=lambda p: STAGGER_MINUTES.get(p, 300))
    sched: Dict[str, str] = {}
    used: set = set()
    for i, p in enumerate(order):
        minutes = STAGGER_MINUTES.get(p, 300 + 30 * i)
        while minutes in used:
            minutes += 15
        used.add(minutes)
        sched[p] = (start + timedelta(minutes=minutes)).isoformat(timespec="seconds")
    return sched


# --------------------------------------------------------------------------
# generation clients
# --------------------------------------------------------------------------

def _download(url: str, dest: str, headers: Optional[dict] = None) -> str:
    with requests.get(url, headers=headers or {}, stream=True, timeout=300, allow_redirects=True) as r:
        if r.status_code != 200:
            raise RuntimeError(f"download HTTP {r.status_code}")
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(1 << 20):
                fh.write(chunk)
    if os.path.getsize(dest) < 10_000:
        raise RuntimeError("downloaded video is empty/too small")
    return dest


def veo_generate(settings: dict, prompt: str, seconds: int, dest: str, model: str = VEO_FAST_MODEL,
                 poll_s: float = 8.0, timeout_s: float = 600.0) -> dict:
    key = settings.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("Missing Gemini API key")
    hdr = {"x-goog-api-key": key, "Content-Type": "application/json"}
    params = {"aspectRatio": "9:16", "resolution": "720p", "durationSeconds": int(seconds)}
    body = {"instances": [{"prompt": prompt}], "parameters": params}
    r = requests.post(f"{GEMINI_BASE}/models/{model}:predictLongRunning", headers=hdr, json=body, timeout=60)
    if r.status_code != 200 and "duration" in r.text.lower():
        params.pop("durationSeconds")  # model refuses explicit duration -> 8s default, trimmed in the edit
        r = requests.post(f"{GEMINI_BASE}/models/{model}:predictLongRunning", headers=hdr, json=body, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"Veo submit HTTP {r.status_code}: {r.text[:300]}")
    name = r.json()["name"]
    t0 = time.time()
    while True:
        op = requests.get(f"{GEMINI_BASE}/{name}", headers=hdr, timeout=60).json()
        if op.get("done"):
            break
        if time.time() - t0 > timeout_s:
            raise RuntimeError("Veo timed out")
        time.sleep(poll_s)
    if op.get("error"):
        raise RuntimeError(f"Veo error: {str(op['error'])[:300]}")
    resp = op.get("response") or {}
    samples = (resp.get("generateVideoResponse") or {}).get("generatedSamples") or []
    if not samples:
        raise RuntimeError(f"Veo returned no video (filtered?): {str(resp)[:300]}")
    uri = samples[0]["video"]["uri"]
    _download(uri, dest, headers={"x-goog-api-key": key})
    return {"model": model, "billed_seconds": int(params.get("durationSeconds", 8))}


def seedance_generate(settings: dict, prompt: str, seconds: int, dest: str, poll_s: float = 5.0, timeout_s: float = 600.0) -> dict:
    key = settings.get("fal_api_key") or os.environ.get("FAL_KEY") or os.environ.get("FAL_API_KEY")
    if not key:
        raise RuntimeError("Missing FAL key")
    hdr = {"Authorization": f"Key {key}", "Content-Type": "application/json"}
    payload = {"prompt": prompt, "aspect_ratio": "9:16", "resolution": "720p", "duration": str(int(seconds)),
               "camera_fixed": False, "enable_safety_checker": True}
    r = requests.post(f"https://queue.fal.run/{SEEDANCE_MODEL}", headers=hdr, json=payload, timeout=60)
    if r.status_code not in (200, 201, 202):
        raise RuntimeError(f"Seedance submit HTTP {r.status_code}: {r.text[:300]}")
    sub = r.json()
    status_url, result_url = sub["status_url"], sub["response_url"]
    t0 = time.time()
    while True:
        st = requests.get(status_url, headers=hdr, timeout=30)
        status = (st.json() or {}).get("status") if st.status_code in (200, 202) else ""
        if status == "COMPLETED":
            break
        if status in {"FAILED", "ERROR", "CANCELLED"}:
            raise RuntimeError(f"Seedance failed: {st.text[:300]}")
        if time.time() - t0 > timeout_s:
            raise RuntimeError("Seedance timed out")
        time.sleep(poll_s)
    res = requests.get(result_url, headers=hdr, timeout=60)
    if res.status_code != 200:
        raise RuntimeError(f"Seedance result HTTP {res.status_code}: {res.text[:300]}")
    _download(res.json()["video"]["url"], dest)
    return {"model": SEEDANCE_MODEL, "billed_seconds": int(seconds)}


GENERATORS: Dict[str, Callable[..., dict]] = {
    "veo_fast": lambda s, p, sec, d: veo_generate(s, p, sec, d, VEO_FAST_MODEL),
    "veo_lite": lambda s, p, sec, d: veo_generate(s, p, sec, d, VEO_LITE_MODEL),
    "seedance_fast": seedance_generate,
}


def engines_for(role: str) -> List[str]:
    return ["veo_fast"] if role == "hook" else ["seedance_fast", "veo_lite"]


def veo_billed_seconds(seconds: int) -> int:
    return seconds if seconds in (4, 6, 8) else 8


def veo_seconds_for(seconds: float) -> int:
    """Veo renders 4/6/8s; round UP so the edit can trim to the planned length."""
    return 4 if seconds <= 4 else (6 if seconds <= 6 else 8)


def generate_shot(shot: dict, settings: dict, workdir: str, ledger: CostLedger,
                  generators: Optional[Dict[str, Callable[..., dict]]] = None, sleep: Callable[[float], None] = time.sleep) -> dict:
    """Up to 1 + MAX_SHOT_RETRIES attempts per shot. Hook: Veo Fast only.
    B-roll: Seedance, falling back to Veo Lite once Seedance has failed."""
    gens = generators or GENERATORS
    errors: List[str] = []
    chain = engines_for(shot["role"])
    if shot["role"] == "broll" and generators is None and not (settings.get("fal_api_key") or os.environ.get("FAL_KEY") or os.environ.get("FAL_API_KEY")):
        chain = ["veo_lite"]  # no fal key configured: go straight to the Veo Lite fallback
    retries = max_retries(settings)
    if test_mode(settings):
        chain = chain[:1]  # no fallback to another paid model in test mode
    for attempt in range(1 + retries):
        engine = chain[min(attempt, len(chain) - 1)] if shot["role"] == "broll" else chain[0]
        secs = shot["seconds"] if engine == "seedance_fast" else veo_seconds_for(shot["seconds"])
        est = shot_cost(engine, secs)
        ledger.reserve(f"shot{shot['index']}:{engine}", est)  # raises CostCapExceeded
        dest = os.path.join(workdir, f"shot{shot['index']}_a{attempt + 1}_{engine}.mp4")
        try:
            info = gens[engine](settings, shot["prompt"], secs, dest)
            billed = shot_cost(engine, info.get("billed_seconds", secs))
            ledger.charge(f"shot{shot['index']}:{engine}", billed, ok=True, attempt=attempt + 1, seconds=info.get("billed_seconds", secs))
            return {**shot, "path": dest, "engine": engine, "model": info.get("model"), "attempts": attempt + 1,
                    "cost_usd": billed, "errors": errors}
        except CostCapExceeded:
            raise
        except Exception as exc:
            # Failed/filtered generations are normally not billed; record $0 but keep the error.
            ledger.charge(f"shot{shot['index']}:{engine}", 0.0, ok=False, attempt=attempt + 1, error=str(exc)[:200])
            errors.append(f"{engine} attempt {attempt + 1}: {exc}")
            logger.warning("%s shot %s %s attempt %s failed: %s", MARKER, shot["index"], engine, attempt + 1, exc)
            if attempt < retries:
                sleep(min(20.0, 5.0 * (attempt + 1)))
    raise RuntimeError(f"shot {shot['index']} failed after {1 + retries} attempt(s): " + " | ".join(errors)[-800:])


# --------------------------------------------------------------------------
# ffmpeg assembly
# --------------------------------------------------------------------------

def _ff() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def _run(cmd: List[str], timeout: int = 600) -> None:
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {(res.stderr or '')[-800:]}")


def probe(path: str) -> dict:
    res = subprocess.run([shutil.which("ffprobe") or "ffprobe", "-v", "error", "-show_entries",
                          "stream=codec_type,width,height,r_frame_rate:format=duration", "-of", "json", path],
                         capture_output=True, text=True, timeout=30)
    data = json.loads(res.stdout or "{}")
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    return {"width": v.get("width"), "height": v.get("height"), "fps": v.get("r_frame_rate"),
            "duration": round(float((data.get("format") or {}).get("duration") or 0), 2),
            "has_audio": any(s.get("codec_type") == "audio" for s in data.get("streams", []))}


def _font() -> str:
    for f in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"):
        if os.path.exists(f):
            return f
    return ""


def _wrap(text: str, width: int) -> str:
    import textwrap
    return "\n".join(textwrap.wrap(text, width=width)) or text


def _drawtext(textfile: str, font: str, size: int, y: str, start: float, end: float) -> str:
    """One drawtext per line so every line is centered on its own (not left-aligned in a block)."""
    fontpart = f"fontfile='{font}':" if font else ""
    with open(textfile) as fh:
        lines = [l for l in fh.read().splitlines() if l.strip()] or [""]
    out = []
    for i, line in enumerate(lines):
        lf = f"{textfile}.{i}"
        with open(lf, "w") as fh:
            fh.write(line)
        out.append(f"drawtext={fontpart}textfile='{lf}':fontsize={size}:fontcolor=white:borderw=6:bordercolor=black@0.85:"
                   f"x=(w-text_w)/2:y={y}+{i}*{int(size * 1.2)}:enable='between(t,{start:.2f},{end:.2f})'")
    return ",".join(out)


def normalize_shot(src: str, dst: str, seconds: float, keep_audio: bool) -> None:
    """Scale/crop-or-pad to 1080x1920 @30fps, exact duration, stereo 48k audio (silence if none)."""
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,"
          f"setsar=1,fps={FPS},format=yuv420p")
    has_a = probe(src)["has_audio"] and keep_audio
    cmd = [_ff(), "-y", "-i", src]
    if not has_a:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    cmd += ["-t", f"{seconds:.3f}", "-vf", vf, "-map", "0:v:0", "-map", "0:a:0" if has_a else "1:a:0",
            "-af", "aresample=48000,aformat=channel_layouts=stereo", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "18", "-c:a", "aac", "-b:a", "192k", "-shortest", dst]
    _run(cmd)
    got = probe(dst)["duration"]
    if got < seconds - 0.3:  # source shorter than planned: freeze last frame
        pad = dst + ".pad.mp4"
        _run([_ff(), "-y", "-i", dst, "-vf", f"tpad=stop_mode=clone:stop_duration={seconds - got + 0.1:.2f}",
              "-af", f"apad=pad_dur={seconds - got + 0.1:.2f}", "-t", f"{seconds:.3f}", "-c:v", "libx264",
              "-preset", "veryfast", "-crf", "18", "-c:a", "aac", pad])
        os.replace(pad, dst)


def music_bed_filter(total: float, cut_times: List[float], hook_len: float) -> str:
    """Royalty-free bed synthesized in ffmpeg (no third-party music): low pulse + minor pad,
    plus a filtered-noise whoosh on every cut. Ducked under the Veo hook audio."""
    pad = ("aevalsrc='0.10*sin(2*PI*110*t)*(0.6+0.4*sin(2*PI*0.5*t))+0.07*sin(2*PI*130.81*t)+0.06*sin(2*PI*164.81*t)"
           "+0.05*sin(2*PI*55*t)*(0.5+0.5*sin(2*PI*2*t))':s=48000:d=%.2f,aformat=channel_layouts=stereo,"
           "lowpass=f=1800,afade=t=in:d=0.3,afade=t=out:st=%.2f:d=0.8" % (total, max(0.0, total - 0.8)))
    duck = f"volume='if(lt(t,{hook_len:.2f}),0.35,1.0)':eval=frame"
    parts = [f"{pad},{duck}[bed]"]
    labels = ["[bed]"]
    for i, ct in enumerate(cut_times):
        st = max(0.0, ct - 0.35)
        parts.append(f"anoisesrc=d=0.7:c=pink:a=0.5:r=48000,aformat=channel_layouts=stereo,bandpass=f=900:w=1200,"
                     f"afade=t=in:d=0.35,afade=t=out:st=0.35:d=0.35,adelay={int(st * 1000)}|{int(st * 1000)},apad=whole_dur={total:.2f}[w{i}]")
        labels.append(f"[w{i}]")
    parts.append(f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0:duration=first[music]")
    return ";".join(parts)


def assemble(shots: List[dict], hook_text: str, out_path: str, workdir: str) -> dict:
    """shots: dicts with path, seconds, role, caption. Returns probe info of the final mp4."""
    font = _font()
    norm: List[str] = []
    for s in shots:
        dst = os.path.join(workdir, f"norm{s['index']}.mp4")
        normalize_shot(s["path"], dst, float(s["seconds"]), keep_audio=(s["role"] == "hook"))
        norm.append(dst)
    durs = [float(s["seconds"]) for s in shots]
    total = sum(durs)
    cuts, acc = [], 0.0
    for d in durs[:-1]:
        acc += d
        cuts.append(acc)
    # loop frame = first frame of the hook
    loop_png = os.path.join(workdir, "loop_first.png")
    _run([_ff(), "-y", "-i", norm[0], "-frames:v", "1", loop_png])
    hook_file = os.path.join(workdir, "hook.txt")
    with open(hook_file, "w") as fh:
        fh.write(_wrap(hook_text.upper(), 16))
    filters: List[str] = []
    inputs: List[str] = []
    for p in norm:
        inputs += ["-i", p]
    inputs += ["-loop", "1", "-t", f"{total:.2f}", "-i", loop_png]
    n = len(norm)
    concat_in = "".join(f"[{i}:v][{i}:a]" for i in range(n))
    filters.append(f"{concat_in}concat=n={n}:v=1:a=1[vc][ac]")
    # loop ending: last LOOP_DISSOLVE seconds dissolve into the opening frame
    filters.append(f"[{n}:v]scale={W}:{H},setsar=1,fps={FPS},format=yuva420p,"
                   f"fade=t=in:st={total - LOOP_DISSOLVE:.2f}:d={LOOP_DISSOLVE}:alpha=1[lf]")
    filters.append(f"[vc][lf]overlay=0:0:shortest=1[vl]")
    text_chain = []
    # hook text from frame 0 (first second guaranteed) through the hook shot
    text_chain.append(_drawtext(hook_file, font, 96, "h*0.16", 0.0, durs[0] - 0.05))
    t = durs[0]
    for s, d in zip(shots[1:], durs[1:]):
        if s.get("caption"):
            cf = os.path.join(workdir, f"cap{s['index']}.txt")
            with open(cf, "w") as fh:
                fh.write(_wrap(s["caption"], 22))
            text_chain.append(_drawtext(cf, font, 64, "h*0.72", t + 0.15, t + d - 0.2))
        t += d
    filters.append(f"[vl]{','.join(text_chain)},format=yuv420p[vout]")
    filters.append(music_bed_filter(total, cuts, durs[0]))
    filters.append("[ac][music]amix=inputs=2:normalize=0:duration=first,loudnorm=I=-14:TP=-1.5:LRA=11,"
                   "aresample=48000[aout]")
    cmd = [_ff(), "-y", *inputs, "-filter_complex", ";".join(filters), "-map", "[vout]", "-map", "[aout]",
           "-t", f"{total:.2f}", "-r", str(FPS), "-c:v", "libx264", "-preset", "medium", "-crf", "20",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out_path]
    try:
        _run(cmd, timeout=900)
        text_burned = True
    except RuntimeError as exc:
        if "drawtext" not in str(exc).lower():
            raise
        logger.warning("%s ffmpeg lacks drawtext; rendering without burned text", MARKER)
        filters[3] = "[vl]format=yuv420p[vout]"
        cmd[cmd.index("-filter_complex") + 1] = ";".join(filters)
        _run(cmd, timeout=900)
        text_burned = False
    info = probe(out_path)
    info.update({"text_burned": text_burned, "cuts_at": cuts, "loop_dissolve_s": LOOP_DISSOLVE,
                 "loudnorm": "I=-14 LUFS, TP=-1.5", "music": "synthesized pad + whoosh SFX (royalty-free)"})
    return info


# --------------------------------------------------------------------------
# end to end
# --------------------------------------------------------------------------

def produce_short(settings: dict, out_dir: str, avoid_topics: Optional[List[str]] = None,
                  avoid_titles: Optional[List[str]] = None, llm: Optional[Callable] = None,
                  generators: Optional[Dict[str, Callable[..., dict]]] = None,
                  sleep: Callable[[float], None] = time.sleep, n_shots: int = 4) -> Tuple[str, dict, dict]:
    """Returns (mp4_path, plan, info). info carries cost ledger, shot list, captions, probe."""
    ledger = CostLedger(cost_cap(settings))
    plan = plan_short(settings, avoid_topics, avoid_titles, llm=llm, n_shots=n_shots, ledger=ledger)
    forced = settings.get("shorts_forced_durations") or os.environ.get("SHORTS_FORCED_DURATIONS")
    if forced:
        # Exact per-shot seconds from the run config (e.g. "4,5,5,5"); the planner cannot lengthen them.
        secs = [int(x) for x in (forced if isinstance(forced, list) else str(forced).split(","))]
        plan["shots"] = plan["shots"][:len(secs)]
        if len(plan["shots"]) != len(secs):
            raise RuntimeError(f"plan has {len(plan['shots'])} shots, forced durations need {len(secs)}")
        for shot, sec in zip(plan["shots"], secs):
            shot["seconds"] = sec
        plan["forced_durations"] = secs
    elif generators is None and not (settings.get("fal_api_key") or os.environ.get("FAL_KEY") or os.environ.get("FAL_API_KEY")):
        # Veo-Lite-only b-roll renders 4/6/8s; plan on 6s shots and a 4s hook so the Short fits the cap.
        plan["shots"][0]["seconds"] = HOOK_MIN
        for s in plan["shots"][1:]:
            s["seconds"] = 6
        plan["broll_engine"] = "veo_lite (no fal key)"
    # pre-flight: planned path must fit the cap
    lite_only = plan.get("broll_engine", "").startswith("veo_lite")
    est = sum(shot_cost("veo_fast", veo_billed_seconds(s["seconds"])) if s["role"] == "hook"
              else (shot_cost("veo_lite", veo_seconds_for(s["seconds"])) if lite_only else shot_cost("seedance_fast", s["seconds"]))
              for s in plan["shots"])
    ledger.reserve("preflight_estimate", est)
    os.makedirs(out_dir, exist_ok=True)
    workdir = tempfile.mkdtemp(prefix="stitched_")
    done: List[dict] = []
    try:
        for shot in plan["shots"]:
            done.append(generate_shot(shot, settings, workdir, ledger, generators, sleep))
        out = os.path.join(out_dir, f"short_{uuid.uuid4().hex[:12]}.mp4")
        info = assemble(done, plan["on_screen_hook"], out, workdir)
    except Exception:
        logger.error("%s Short failed; cost so far %s; shots kept in %s", MARKER, json.dumps(ledger.as_dict()), workdir)
        raise
    shutil.rmtree(workdir, ignore_errors=True)
    if not plan.get("forced_durations") and not (MIN_TOTAL - 0.5 <= info["duration"] <= MAX_TOTAL + 0.5):
        raise RuntimeError(f"final length {info['duration']}s outside {MIN_TOTAL}-{MAX_TOTAL}s")
    info.update({
        "format": "stitched_short", "version": VERSION, "media_source": "ai_generated",
        "cost": ledger.as_dict(), "cost_usd": ledger.total,
        "shot_list": [{k: s.get(k) for k in ("index", "role", "seconds", "engine", "model", "attempts", "cost_usd", "caption", "prompt", "errors")} for s in done],
        "platform_captions": build_platform_captions(plan, {s.get("engine") for s in done}),
    })
    logger.info("%s Short ready %s length=%.1fs cost=$%.3f (cap $%.2f)", MARKER, out, info["duration"], ledger.total, ledger.cap)
    return out, plan, info
