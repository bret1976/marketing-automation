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

import viral_research

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
BUDGET_DURATIONS = [4, 5, 5, 5]

VEO_FAST_MODEL = "veo-3.1-fast-generate-preview"
VEO_LITE_MODEL = "veo-3.1-lite-generate-preview"
SEEDANCE_MODEL = "fal-ai/bytedance/seedance/v1/pro/fast/text-to-video"
# Same price formula as text-to-video (fal model page, checked 2026-10-16).
SEEDANCE_I2V_MODEL = "fal-ai/bytedance/seedance/v1/pro/fast/image-to-video"
LOOP_TAIL_MAX = 1.0  # seconds of held loop frame allowed to reach the 20s minimum (no extra generation cost)
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

    def fits(self, usd: float) -> bool:
        return self.total + usd <= self.cap + 1e-9

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
     "youtube":   {{"title": 40-70 chars, curiosity + keyword + the hook number, no clickbait the video can't pay off,
                    "description": 3-5 lines: hook, how it was made, comment question, follow CTA, "hashtags": 3}},
     "tiktok":    {{"caption": 120-300 chars: hook line, 1-line BTS story, comment question, follow-for-part-2, "hashtags": 3-5}},
     "instagram": {{"caption": 5-8 short lines with line breaks: hook, story, how it was made, comment question,
                    save/follow CTA, "hashtags": 3-5 (Instagram max is 5)}},
     "linkedin":  {{"caption": 6-10 short lines: professional hook, the craft problem, the workflow, what it teaches,
                    a genuine question for practitioners; no engagement bait, "hashtags": 3}},
     "twitter":   {{"caption": max 240 chars WITHOUT hashtags or links; LINE 1 is a spectacle hook that names the
                    model (e.g. "Veo 3.1 + Seedance") or the wow moment, never a question or process opener;
                    then one fact, "hashtags": 1}}
  }}
}}
Consistency: text-to-video models cannot remember a face, so prefer a non-human hero (prop, robot, creature,
vehicle, landscape) or describe any person with the SAME exact age, hair, clothing in every prompt.
Shot captions: describe the craft of THAT shot (framing, light, motion); do NOT name any AI model in them
(the edit adds correct model credits). Allowed technique claims ONLY: one locked style line repeated in every
prompt; b-roll animated from the previous shot's last frame (image-to-video); loop ending matching shot 1.
Never mention seeds, LoRAs, ControlNet, fine-tuning, upscaling, render/attempt counts, percentages,
"identical" or "perfect" consistency.
Truth: never invent stats or process numbers (render counts, % savings); the hook number must describe what
is on screen or the real setup (e.g. shot count, seconds). Do not name tools other than Veo/Seedance.
Hashtags: specific (e.g. #AIFilmmaking #Veo3 #Seedance #CinematicAI #BehindTheScenes), never generic (#fyp #viral).
Do NOT reuse these recent topics: {avoid}

{viral_block}

VIRAL COPY RULES (every platform; researched 2026 best practice):
- First line = curiosity hook with the main keyword ("AI short film", "AI video") in the first words; open a loop
  the viewer needs the video to close. No "check this out", no brand opener.
- Then a 1-2 line behind-the-scenes story (what the subject does + how it was made, true facts only).
- Then ONE specific comment prompt the viewer can answer in a few words (spot the cut / which shot / what should
  happen next / would you watch part 2?). Never "comment YES", never ask for likes.
- Then a CTA: follow for part 2 (TikTok/IG/YouTube), save it (IG), repost/bookmark (X).
- Hashtags: specific, mixing broad reach (#AIvideo), mid (#AIFilmmaking #AIshortfilm) and niche/topic tags about
  THIS film's subject (e.g. #SciFi #Drone); tool tags (#Veo3 #Seedance) at most 1-2; never #fyp/#viral.
- on_screen_hook is readable in the first second and promises the payoff of the video.
Also return "viral_angle" (the hook format you chose) and "viral_refs_used" (list of reference titles), plus
"pinned_comment": a first comment that restarts the conversation (question or 'part 2?' prompt, max 150 chars)."""

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
        prompt += " Full-frame image edge to edge, no letterbox or black bars."
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
               llm: Optional[Callable[[dict, str], Any]] = None, n_shots: int = 4, ledger: Optional[CostLedger] = None,
               viral: Optional[dict] = None) -> dict:
    llm = llm or call_gemini_text_json
    prompt = PLAN_PROMPT.format(total_min=int(MIN_TOTAL), total_max=int(MAX_TOTAL), n_shots=n_shots,
                                hook_min=HOOK_MIN, hook_max=HOOK_MAX, broll_min=BROLL_MIN, broll_max=BROLL_MAX,
                                avoid=", ".join(avoid_topics or []) or "none yet",
                                avoid_titles="; ".join((avoid_titles or [])[-40:]) or "none yet",
                                viral_block=viral_research.prompt_block(viral))
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
            if isinstance(raw, dict):
                plan["viral_angle"] = _clean(raw.get("viral_angle") or "", 200)
                plan["viral_refs_used"] = [str(t)[:120] for t in (raw.get("viral_refs_used") or [])][:5]
                plan["pinned_comment"] = _clean(raw.get("pinned_comment") or "", 150)
            plan["viral_refs"] = (viral or {}).get("refs") or []
            plan["viral_hashtags"] = (viral or {}).get("hashtags") or []
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


ENGINE_LABELS = {"veo_fast": "Veo 3.1 Fast", "veo_lite": "Veo 3.1 Lite", "seedance_fast": "Seedance 1 Pro Fast"}
TOOL_MENTIONS = [  # (regex, engines that make the mention true; empty = never used by this pipeline)
    (r"\bveo\b|\bveo ?3(\.1)?\b|google veo", {"veo_fast", "veo_lite"}),
    (r"\bseedance\b|bytedance", {"seedance_fast"}),
    (r"\bsora\b|\brunway\b|\bkling\b|\bpika\b|midjourney|\bluma\b|\bflux\b|stable diffusion|\bhailuo\b|\bminimax\b|\bgen-?[34]\b", set()),
]
# Technique / result claims this pipeline never does or cannot prove.
FALSE_TECHNIQUE_RE = re.compile(
    r"\bseeds?\b|\blora\b|controlnet|fine-?tun|motion brush|inpaint|upscal|green ?screen|motion capture|mocap|"
    r"\b\d+\s*(?:renders?|tries|attempts|takes|iterations|hours|days)\b|\d+\s*%|percent|"
    r"\bidentical\b|\bperfect(?:ly)?\b|\bflawless\b|\bzero (?:drift|errors?)\b", re.I)


def copy_problems(text: str, allowed_engines: set) -> List[str]:
    probs = []
    for rx, engines in TOOL_MENTIONS:
        if re.search(rx, text or "", re.I) and not (engines & allowed_engines):
            probs.append(f"mentions tool not used: /{rx}/")
    m = FALSE_TECHNIQUE_RE.search(text or "")
    if m:
        probs.append(f"unverifiable technique/claim: {m.group(0)!r}")
    return probs


def scrub_copy(text: str, allowed_engines: set) -> Tuple[str, List[str]]:
    """Drop every sentence/line that names an unused tool or an unused technique/claim."""
    removed: List[str] = []
    out_lines = []
    for line in str(text or "").split("\n"):
        if line.strip().startswith("#"):
            out_lines.append(line)
            continue
        kept = []
        for sent in re.split(r"(?<=[.!?])\s+", line):
            if sent.strip() and copy_problems(sent, allowed_engines):
                removed.append(sent.strip())
            else:
                kept.append(sent)
        out_lines.append(" ".join(k for k in kept if k is not None).rstrip())
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(out_lines)).strip()
    return cleaned, removed


def factual_summary(shots: List[dict]) -> str:
    hook = next((s for s in shots if s.get("role") == "hook"), None)
    broll = [s for s in shots if s.get("role") == "broll"]
    parts = []
    if hook:
        parts.append(f"Hook shot: {ENGINE_LABELS.get(hook.get('engine'), 'AI video')}.")
    if broll:
        labels = sorted({ENGINE_LABELS.get(s.get("engine"), "AI video") for s in broll})
        chained = any(s.get("chained_from_previous_frame") for s in broll)
        parts.append(f"B-roll: {' + '.join(labels)}" + (", each shot started from the previous shot's last frame." if chained else "."))
    return " ".join(parts)


def audit_and_fix_copy(plan: dict, shots: List[dict], captions: Dict[str, dict]) -> dict:
    """Post-generation check: on-screen text and captions may only credit the model that made a shot,
    and may not claim techniques this pipeline did not use. Offending sentences are removed and replaced
    with copy derived from the actual per-shot records. Mutates plan/shots/captions; returns an audit log."""
    used = {s.get("engine") for s in shots if s.get("engine")}
    log: Dict[str, Any] = {"used_engines": sorted(used), "removed": {}, "replaced": {}}
    # hook text
    if copy_problems(plan.get("on_screen_hook", ""), used):
        log["replaced"]["on_screen_hook"] = plan["on_screen_hook"]
        plan["on_screen_hook"] = f"{len(shots)} AI shots, 1 story"
    # per-shot overlay captions: judged against THAT shot's engine only
    for s in shots:
        cap = s.get("caption") or ""
        if cap and copy_problems(cap, {s.get("engine")}):
            log["replaced"][f"shot{s['index']}_caption"] = cap
            s["caption"] = f"Shot {s['index']}: {ENGINE_LABELS.get(s.get('engine'), 'AI video')}"
    # platform captions + YouTube title
    facts = factual_summary(shots)
    for name, c in captions.items():
        body, removed = scrub_copy(c["caption"], used)
        if removed:
            log["removed"][name] = removed
            tags = " ".join(c.get("hashtags") or [])
            core = body.replace(tags, "").strip()
            body = f"{core}\n{facts}".strip() + (f"\n\n{tags}" if tags else "")
        c["caption"] = body
        if c.get("title") and copy_problems(c["title"], used):
            log["replaced"][f"{name}_title"] = c["title"]
            c["title"] = _clean(f"{plan.get('on_screen_hook')} | {plan.get('title_line') or 'AI short film'}", 100)
            c["caption"] = c["caption"].replace(log["replaced"][f"{name}_title"], c["title"])
    log["clean"] = all(not copy_problems(c["caption"], used) for c in captions.values()) and \
        all(not copy_problems(s.get("caption") or "", {s.get("engine")}) for s in shots)
    return log


def clamp_text(text: str, n: int) -> str:
    text = (text or "").strip()
    if len(text) <= n:
        return text
    cut = text[:n - 1].rsplit(" ", 1)[0].rstrip(",;:-")
    return cut + "…"


# --------------------------------------------------------------------------
# hashtag strategy: broad reach + mid + niche, live viral tags, per-platform counts
# --------------------------------------------------------------------------
TAG_BROAD = ["#AIvideo", "#AI", "#AIart", "#GenerativeAI"]
TAG_MID = ["#AIFilmmaking", "#AIshortfilm", "#AIanimation", "#CinematicAI", "#AIcinema"]
TAG_NICHE = ["#BehindTheScenes", "#Filmmaking", "#VFX", "#ShortFilm"]
TAG_TOOLS = ["#Veo3", "#Seedance"]
TAG_LINKEDIN = ["#GenerativeAI", "#AIFilmmaking", "#VideoProduction", "#CreativeTechnology", "#Filmmaking"]
# (count, broad, mid, tools_max) per platform. Count rules: Instagram caps posts at 5 tags (Dec 2025);
# YouTube shows the first 3 description tags above the title and ignores all tags past 60;
# TikTok guidance is 3-5 specific tags (we run 6 for wider test reach); LinkedIn "a handful".
TAG_RECIPE = {"tiktok": (7, 2, 2, 2), "instagram": (5, 1, 2, 1), "youtube": (4, 1, 2, 1),
              "twitter": (1, 0, 1, 0), "linkedin": (4, 0, 0, 0)}  # X: 1 tag max (x-safe-v1)
_VIRAL_TAG_OK = re.compile(r"ai|film|cinema|anim|vfx|veo|seedance|story|scifi|fantasy", re.I)


def viral_tags(tags: Optional[List[str]]) -> List[str]:
    out = []
    for t in tags or []:
        t = "#" + re.sub(r"[^\w]", "", str(t).lstrip("#"))
        if len(t) > 3 and _VIRAL_TAG_OK.search(t) and t.lower() not in BANNED_TAGS and t.lower() not in ("#ai",):
            canon = {x.lower(): x for x in TAG_BROAD + TAG_MID + TAG_NICHE}
            out.append(canon.get(t.lower(), t))
    return out


def platform_hashtags(platform: str, planner_tags: Any = None, live_tags: Optional[List[str]] = None,
                      used_engines: Optional[set] = None) -> List[str]:
    n, n_broad, n_mid, tools_max = TAG_RECIPE.get(platform, (4, 1, 2, 1))
    tools = filter_tool_tags(list(TAG_TOOLS), used_engines)
    planner = [t for t in clean_hashtags(planner_tags, [], 0, 10)] if planner_tags else []
    planner = filter_tool_tags(planner, used_engines)
    if platform == "linkedin":
        order = TAG_LINKEDIN[:3] + [t for t in planner if t.lower() not in {x.lower() for x in tools}][:1] + TAG_LINKEDIN
    else:
        order = (TAG_BROAD[:n_broad] + TAG_MID[:n_mid] + viral_tags(live_tags)[:2] + tools[:tools_max] +
                 [t for t in planner if t.lower() not in {x.lower() for x in tools}] + TAG_NICHE + TAG_MID)
    out, n_tools = [], 0
    for t in order:
        low = t.lower()
        if low in {x.lower() for x in out}:
            continue
        if low in {x.lower() for x in tools} or low in TOOL_TAGS:
            if n_tools >= tools_max:
                continue
            n_tools += 1
        out.append(t)
        if len(out) >= n:
            break
    return out


def build_platform_captions(plan: dict, used_engines: Optional[set] = None) -> Dict[str, dict]:
    hook = plan.get("on_screen_hook") or ""
    title = plan.get("title_line") or hook
    q = plan.get("comment_question") or ""
    p = plan.get("platforms") or {}
    base_tags = ["#AIFilmmaking", "#CinematicAI", "#BehindTheScenes", "#Veo3", "#Seedance"]

    base_tags = filter_tool_tags(base_tags, used_engines)

    live = plan.get("viral_hashtags") or []

    def _tags(tags, fallback=None, lo=3, hi=5, platform="tiktok"):  # researched per-platform mix, tool-aware
        return platform_hashtags(platform, tags, live, used_engines)

    def get(name: str) -> dict:
        v = p.get(name)
        return v if isinstance(v, dict) else {}

    yt = get("youtube")
    yt_title = _clean(yt.get("title") or f"{title}: {hook}", 100)
    if not has_number(yt_title):
        yt_title = _clean(f"{hook} | {title}", 100)
    yt_tags = _tags(yt.get("hashtags"), base_tags, platform="youtube")
    yt_desc = "\n".join(_clean(l, 300) for l in str(yt.get("description") or f"How this AI short was made, shot by shot.\n{q}\nFollow for part 2.").splitlines() if _clean(l))
    tt = get("tiktok")
    tt_tags = _tags(tt.get("hashtags"), base_tags, platform="tiktok")
    tt_cap = "\n".join(_clean(l, 300) for l in str(tt.get("caption") or f"{hook}. Watch it loop.\n{q}\nFollow for part 2.").splitlines() if _clean(l))[:400]
    ig = get("instagram")
    ig_tags = _tags(ig.get("hashtags"), base_tags, platform="instagram")
    ig_cap = str(ig.get("caption") or f"{hook}\nEvery frame AI-generated, cut like a real film.\nSave this for your next shot list.")
    ig_cap = "\n\n".join(_clean(l, 300) for l in ig_cap.splitlines() if _clean(l))
    li = get("linkedin")
    li_tags = _tags(li.get("hashtags"), base_tags, platform="linkedin")
    li_cap = str(li.get("caption") or f"{title}\n\n{hook}. One coherent shot list, two video models, one edit.\n\nThe workflow: plan the story first, lock the style, then generate shot by shot.")
    li_cap = "\n".join(_clean(l, 300) for l in li_cap.splitlines())
    tw = get("twitter")
    tw_tags = _tags(tw.get("hashtags"), base_tags, platform="twitter")
    tw_body = _clean(tw.get("caption") or f"{hook}. {q}", 280)
    for t in tw_tags:  # don't double-tag
        tw_body = tw_body.replace(t, "").strip()
    tw_room = 280 - len(" ".join(tw_tags)) - 2
    tw_cap = f"{clamp_text(tw_body, tw_room)}\n\n{' '.join(tw_tags)}"
    out = {
        "twitter": {"caption": tw_cap, "hashtags": tw_tags},
        "youtube": {"title": yt_title, "caption": f"{yt_title}\n{yt_desc}\n\n{' '.join(yt_tags + ['#Shorts'])}", "hashtags": yt_tags + ["#Shorts"]},
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
                 poll_s: float = 8.0, timeout_s: float = 900.0, on_submit: Optional[Callable[[str], None]] = None,
                 resume_id: Optional[str] = None, **_: Any) -> dict:
    """resume_id: an operation name saved by an earlier run -> poll/download it instead of submitting again."""
    key = settings.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("Missing Gemini API key")
    hdr = {"x-goog-api-key": key, "Content-Type": "application/json"}
    params = {"aspectRatio": "9:16", "resolution": "720p", "durationSeconds": int(seconds)}
    body = {"instances": [{"prompt": prompt}], "parameters": params}
    if resume_id:
        name = resume_id
    else:
        r = requests.post(f"{GEMINI_BASE}/models/{model}:predictLongRunning", headers=hdr, json=body, timeout=60)
        if r.status_code != 200 and "duration" in r.text.lower():
            params.pop("durationSeconds")  # model refuses explicit duration -> 8s default, trimmed in the edit
            r = requests.post(f"{GEMINI_BASE}/models/{model}:predictLongRunning", headers=hdr, json=body, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"Veo submit HTTP {r.status_code}: {r.text[:300]}")
        name = r.json()["name"]
        if on_submit:
            on_submit(name)
    t0 = time.time()
    while True:
        op = requests.get(f"{GEMINI_BASE}/{name}", headers=hdr, timeout=60).json()
        if op.get("done"):
            break
        if time.time() - t0 > timeout_s:
            raise RuntimeError(f"Veo timed out (operation {name} may still complete and bill)")
        time.sleep(poll_s)
    if op.get("error"):
        raise RuntimeError(f"Veo error: {str(op['error'])[:300]}")
    resp = op.get("response") or {}
    samples = (resp.get("generateVideoResponse") or {}).get("generatedSamples") or []
    if not samples:
        raise RuntimeError(f"Veo returned no video (filtered?): {str(resp)[:300]}")
    uri = samples[0]["video"]["uri"]
    _download(uri, dest, headers={"x-goog-api-key": key})
    return {"model": model, "billed_seconds": int(params.get("durationSeconds", 8)), "request_id": name}


def image_data_uri(path: str) -> str:
    import base64
    with open(path, "rb") as fh:
        return "data:image/jpeg;base64," + base64.b64encode(fh.read()).decode()


def seedance_generate(settings: dict, prompt: str, seconds: int, dest: str, image: Optional[str] = None,
                      poll_s: float = 5.0, timeout_s: float = 1500.0, on_submit: Optional[Callable[[str], None]] = None,
                      resume_id: Optional[str] = None, **_: Any) -> dict:
    """image: path to a start frame -> image-to-video (keeps the same subject); else text-to-video."""
    key = settings.get("fal_api_key") or os.environ.get("FAL_KEY") or os.environ.get("FAL_API_KEY")
    if not key:
        raise RuntimeError("Missing FAL key")
    hdr = {"Authorization": f"Key {key}", "Content-Type": "application/json"}
    payload = {"prompt": prompt, "aspect_ratio": "9:16", "resolution": "720p", "duration": str(int(seconds)),
               "camera_fixed": False, "enable_safety_checker": True}
    model = SEEDANCE_MODEL
    if image:
        model = SEEDANCE_I2V_MODEL
        payload["image_url"] = image_data_uri(image)
    if resume_id:
        # fetch an earlier (possibly timed-out) request by id - never resubmit
        base = f"https://queue.fal.run/fal-ai/bytedance/requests/{resume_id}"
        sub = {"request_id": resume_id, "status_url": f"{base}/status", "response_url": base}
    else:
        r = requests.post(f"https://queue.fal.run/{model}", headers=hdr, json=payload, timeout=60)
        if r.status_code not in (200, 201, 202):
            raise RuntimeError(f"Seedance submit HTTP {r.status_code}: {r.text[:300]}")
        sub = r.json()
        if on_submit:
            on_submit(sub.get("request_id"))
    status_url, result_url = sub["status_url"], sub["response_url"]
    logger.info("%s fal request %s submitted (%s)", MARKER, sub.get("request_id"), model)
    t0 = time.time()
    while True:
        st = requests.get(status_url, headers=hdr, timeout=30)
        status = (st.json() or {}).get("status") if st.status_code in (200, 202) else ""
        if status == "COMPLETED":
            break
        if status in {"FAILED", "ERROR", "CANCELLED"}:
            raise RuntimeError(f"Seedance failed: {st.text[:300]}")
        if time.time() - t0 > timeout_s:
            # The job keeps running (and is billed) on fal; keep the id so the result can be fetched later.
            raise RuntimeError(f"Seedance timed out after {int(timeout_s)}s (fal request {sub.get('request_id')} may still complete and bill)")
        time.sleep(poll_s)
    res = requests.get(result_url, headers=hdr, timeout=60)
    if res.status_code != 200:
        raise RuntimeError(f"Seedance result HTTP {res.status_code}: {res.text[:300]}")
    _download(res.json()["video"]["url"], dest)
    return {"model": model, "billed_seconds": int(seconds), "request_id": sub.get("request_id")}


GENERATORS: Dict[str, Callable[..., dict]] = {
    "veo_fast": lambda s, p, sec, d, **kw: veo_generate(s, p, sec, d, VEO_FAST_MODEL, **kw),
    "veo_lite": lambda s, p, sec, d, **kw: veo_generate(s, p, sec, d, VEO_LITE_MODEL, **kw),
    "seedance_fast": seedance_generate,
}


def engines_for(role: str) -> List[str]:
    return ["veo_fast"] if role == "hook" else ["seedance_fast", "veo_lite"]


def veo_billed_seconds(seconds: int) -> int:
    return seconds if seconds in (4, 6, 8) else 8


def veo_seconds_for(seconds: float) -> int:
    """Veo renders 4/6/8s; round UP so the edit can trim to the planned length."""
    return 4 if seconds <= 4 else (6 if seconds <= 6 else 8)


class RunState:
    """Durable per-run record: <runs_dir>/<run_id>/run.json (+ shot files next to it).
    Written right after planning and after every shot submit/finish, so a stopped run loses nothing
    and a rerun with the same run_id resumes (no new planning call, no regenerated shots)."""

    def __init__(self, runs_dir: str, run_id: Optional[str] = None):
        self.run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.dir = os.path.join(runs_dir, self.run_id)
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, "run.json")
        self.data: Dict[str, Any] = {"run_id": self.run_id, "created": datetime.now().isoformat(timespec="seconds"),
                                     "plan": None, "shots": {}, "pending": {}, "cost_items": [], "status": "started"}
        if os.path.exists(self.path):
            with open(self.path) as fh:
                self.data.update(json.load(fh))
            self.data["resumed"] = int(self.data.get("resumed", 0)) + 1

    def save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.data, fh, indent=2, default=str)
        os.replace(tmp, self.path)

    @property
    def plan(self) -> Optional[dict]:
        return self.data.get("plan")

    def set_plan(self, plan: dict) -> None:
        self.data["plan"] = plan
        self.data["plan_saved_at"] = datetime.now().isoformat(timespec="seconds")
        self.save()

    def done_shot(self, index: int) -> Optional[dict]:
        rec = self.data["shots"].get(str(index))
        return rec if rec and rec.get("path") and os.path.exists(rec["path"]) else None

    def mark_pending(self, index: int, engine: str, request_id: str, est: float) -> None:
        self.data["pending"][str(index)] = {"engine": engine, "request_id": request_id, "est_usd": est,
                                            "submitted": datetime.now().isoformat(timespec="seconds")}
        self.save()

    def pending(self, index: int) -> Optional[dict]:
        return self.data["pending"].get(str(index))

    def mark_done(self, rec: dict) -> None:
        self.data["shots"][str(rec["index"])] = {k: rec.get(k) for k in (
            "index", "role", "seconds", "engine", "model", "request_id", "cost_usd", "path", "caption", "prompt",
            "attempts", "chained_from_previous_frame", "errors", "resumed_from_request")}
        self.data["pending"].pop(str(rec["index"]), None)
        self.save()

    def add_cost(self, item: dict) -> None:
        self.data["cost_items"].append(item)
        self.save()

    def finish(self, **fields: Any) -> None:
        self.data.update(fields)
        self.save()


def last_frame(video: str, dest: str) -> str:
    """Final frame as JPEG (start image for the next shot's image-to-video)."""
    _run([_ff(), "-y", "-sseof", "-0.15", "-i", video, "-frames:v", "1", "-q:v", "3", "-update", "1", dest])
    if not os.path.exists(dest):
        _run([_ff(), "-y", "-i", video, "-vf", "reverse", "-frames:v", "1", "-q:v", "3", dest])
    return dest


def generate_shot(shot: dict, settings: dict, workdir: str, ledger: CostLedger,
                  generators: Optional[Dict[str, Callable[..., dict]]] = None, sleep: Callable[[float], None] = time.sleep,
                  start_image: Optional[str] = None, state: Optional[RunState] = None) -> dict:
    """Up to 1 + MAX_SHOT_RETRIES attempts per shot. Hook: Veo Fast only.
    B-roll: Seedance, falling back to Veo Lite once Seedance has failed (or retrying Seedance when
    Veo Lite would not fit under the remaining cost cap)."""
    gens = generators or GENERATORS
    errors: List[str] = []
    chain = engines_for(shot["role"])
    if shot["role"] == "broll" and generators is None and not (settings.get("fal_api_key") or os.environ.get("FAL_KEY") or os.environ.get("FAL_API_KEY")):
        chain = ["veo_lite"]  # no fal key configured: go straight to the Veo Lite fallback
    retries = max_retries(settings)
    if test_mode(settings):
        chain = chain[:1]  # no fallback to another paid model in test mode
    for attempt in range(1 + retries):
        pend = state.pending(shot["index"]) if state else None
        if pend and attempt == 0:
            engine = pend["engine"]  # resume: same engine, fetch by saved request id
        else:
            engine = chain[min(attempt, len(chain) - 1)] if shot["role"] == "broll" else chain[0]
            pend = None
        secs = shot["seconds"] if engine == "seedance_fast" else veo_seconds_for(shot["seconds"])
        est = shot_cost(engine, secs)
        if not pend and shot["role"] == "broll" and engine != chain[0] and not ledger.fits(est):
            # The pricier fallback (Veo Lite) no longer fits under the cap, but a retry on the cheaper
            # primary engine (Seedance) still does: retry Seedance instead of failing the whole Short
            # (and wasting the shots already paid for). Still capped by the same per-Short ledger.
            alt = chain[0]
            alt_secs = shot["seconds"] if alt == "seedance_fast" else veo_seconds_for(shot["seconds"])
            alt_est = shot_cost(alt, alt_secs)
            if ledger.fits(alt_est):
                logger.info("%s shot %s: %s ($%.3f) would exceed the $%.2f cap; retrying %s ($%.3f) instead",
                            MARKER, shot["index"], engine, est, ledger.cap, alt, alt_est)
                engine, secs, est = alt, alt_secs, alt_est
        if not pend:  # a pending request was already reserved/charged by the run that submitted it
            ledger.reserve(f"shot{shot['index']}:{engine}", est)  # raises CostCapExceeded
        dest = os.path.join(workdir, f"shot{shot['index']}_a{attempt + 1}_{engine}.mp4")
        try:
            kw: Dict[str, Any] = {"image": start_image} if (start_image and engine == "seedance_fast") else {}
            if state:
                kw["on_submit"] = lambda rid, _e=engine, _est=est: state.mark_pending(shot["index"], _e, rid, _est)
            if pend:
                kw["resume_id"] = pend["request_id"]
            info = gens[engine](settings, shot["prompt"], secs, dest, **kw)
            billed = shot_cost(engine, info.get("billed_seconds", secs))
            if pend:
                billed = 0.0 if any(c.get("label") == f"shot{shot['index']}:{engine}" and c.get("pending") for c in ledger.items) else billed
            ledger.charge(f"shot{shot['index']}:{engine}", billed, ok=True, attempt=attempt + 1, seconds=info.get("billed_seconds", secs),
                          request_id=info.get("request_id"))
            if state:
                state.add_cost(ledger.items[-1])
            rec = {**shot, "path": dest, "engine": engine, "model": info.get("model"), "attempts": attempt + 1,
                   "request_id": info.get("request_id") or (pend or {}).get("request_id"),
                   "resumed_from_request": bool(pend),
                   "chained_from_previous_frame": bool(start_image and engine == "seedance_fast"),
                   "cost_usd": billed, "errors": errors}
            if state:
                state.mark_done(rec)
            return rec
        except CostCapExceeded:
            raise
        except Exception as exc:
            # Failed/filtered generations are normally not billed; record $0 but keep the error.
            billed_anyway = est if "timed out" in str(exc) else 0.0  # timed-out jobs still finish and bill
            ledger.charge(f"shot{shot['index']}:{engine}", billed_anyway, ok=False, attempt=attempt + 1, error=str(exc)[:200],
                          pending=bool(billed_anyway))
            if state:
                state.add_cost(ledger.items[-1])
            if billed_anyway and state and state.pending(shot["index"]):
                raise RuntimeError(f"shot {shot['index']} timed out; rerun with run_id={state.run_id} to fetch it by request id") from exc
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


def detect_letterbox(src: str) -> Optional[str]:
    """Models sometimes bake cinematic black bars INTO a 9:16 frame (e.g. 'anamorphic' prompts).
    Returns an ffmpeg crop=w:h:x:y that removes them, or None when the frame is already full."""
    res = subprocess.run([_ff(), "-hide_banner", "-i", src, "-vf", "fps=4,cropdetect=limit=24:round=2:reset=0", "-f", "null", "-"],
                         capture_output=True, text=True, timeout=120)
    crops = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", res.stderr or "")
    pr = subprocess.run([shutil.which("ffprobe") or "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                         "stream=width,height", "-of", "csv=p=0", src], capture_output=True, text=True, timeout=30)
    try:
        iw, ih = [int(x) for x in pr.stdout.strip().split(",")[:2]]
    except ValueError:
        return None
    if not crops:
        return None
    w, h, x, y = [int(v) for v in crops[-1]]
    if (h < ih * 0.96 or w < iw * 0.96) and h > ih * 0.4 and w > iw * 0.4:
        return f"crop={w}:{h}:{x}:{y}"
    return None


def normalize_shot(src: str, dst: str, seconds: float, keep_audio: bool) -> None:
    """Scale/crop-or-pad to 1080x1920 @30fps, exact duration, stereo 48k audio (silence if none)."""
    # fill + center-crop (no black bars when a model returns a slightly non-9:16 frame)
    bars = detect_letterbox(src)
    vf = ((bars + ",") if bars else "") + (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
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


def loudnorm_two_pass(src: str, dst: str, target_i: float = -14.0, tp: float = -1.5, lra: float = 11.0) -> dict:
    """EBU R128 two-pass: measure, then apply measured values (linear) - video stream copied."""
    res = subprocess.run([_ff(), "-hide_banner", "-i", src, "-af", f"loudnorm=I={target_i}:TP={tp}:LRA={lra}:print_format=json",
                          "-f", "null", "-"], capture_output=True, text=True, timeout=300)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", res.stderr or "", re.S)
    if not m:
        raise RuntimeError("loudnorm measure failed")
    meas = json.loads(m.group(0))
    af = (f"loudnorm=I={target_i}:TP={tp}:LRA={lra}:measured_I={meas['input_i']}:measured_TP={meas['input_tp']}:"
          f"measured_LRA={meas['input_lra']}:measured_thresh={meas['input_thresh']}:offset={meas['target_offset']}:linear=true,"
          f"aresample=48000")
    _run([_ff(), "-y", "-i", src, "-c:v", "copy", "-af", af, "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", dst])
    return meas


def measure_lufs(path: str) -> Optional[float]:
    res = subprocess.run([_ff(), "-hide_banner", "-i", path, "-af", "ebur128", "-f", "null", "-"], capture_output=True, text=True, timeout=300)
    vals = re.findall(r"I:\s+(-?[\d.]+) LUFS", res.stderr or "")
    return float(vals[-1]) if vals else None


def assemble(shots: List[dict], hook_text: str, out_path: str, workdir: str) -> dict:
    """shots: dicts with path, seconds, role, caption. Returns probe info of the final mp4."""
    font = _font()
    norm: List[str] = []
    for s in shots:
        dst = os.path.join(workdir, f"norm{s['index']}.mp4")
        normalize_shot(s["path"], dst, float(s["seconds"]), keep_audio=(s["role"] == "hook"))
        norm.append(dst)
    durs = [float(s["seconds"]) for s in shots]
    gen_total = sum(durs)
    # Held loop frame (free) tops the Short up to MIN_TOTAL when generated footage is just short of it.
    tail = round(min(LOOP_TAIL_MAX, max(0.0, MIN_TOTAL - gen_total)), 2)
    total = gen_total + tail
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
    filters.append(f"{concat_in}concat=n={n}:v=1:a=1[vc0][ac0]")
    filters.append(f"[vc0]tpad=stop_mode=clone:stop_duration={tail:.2f}[vc];[ac0]apad=pad_dur={tail:.2f}[ac]")
    # loop ending: last LOOP_DISSOLVE seconds dissolve into the opening frame
    filters.append(f"[{n}:v]scale={W}:{H},setsar=1,fps={FPS},format=yuva420p,"
                   f"fade=t=in:st={gen_total - LOOP_DISSOLVE:.2f}:d={LOOP_DISSOLVE}:alpha=1[lf]")
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
    filters.append("[ac][music]amix=inputs=2:normalize=0:duration=first,aresample=48000[aout]")
    premix = os.path.join(workdir, "premix.mp4")
    cmd = [_ff(), "-y", *inputs, "-filter_complex", ";".join(filters), "-map", "[vout]", "-map", "[aout]",
           "-t", f"{total:.2f}", "-r", str(FPS), "-c:v", "libx264", "-preset", "medium", "-crf", "20",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", premix]
    try:
        _run(cmd, timeout=900)
        text_burned = True
    except RuntimeError as exc:
        if "drawtext" not in str(exc).lower():
            raise
        logger.warning("%s ffmpeg lacks drawtext; rendering without burned text", MARKER)
        filters[4] = "[vl]format=yuv420p[vout]"
        cmd[cmd.index("-filter_complex") + 1] = ";".join(filters)
        _run(cmd, timeout=900)
        text_burned = False
    loudnorm_two_pass(premix, out_path)
    info = probe(out_path)
    info.update({"text_burned": text_burned, "cuts_at": cuts, "loop_dissolve_s": LOOP_DISSOLVE, "loop_tail_s": tail,
                 "generated_seconds": gen_total, "integrated_lufs": measure_lufs(out_path),
                 "loudnorm": "two-pass I=-14 LUFS, TP=-1.5", "music": "synthesized pad + whoosh SFX (royalty-free)"})
    return info


# --------------------------------------------------------------------------
# end to end
# --------------------------------------------------------------------------

def produce_short(settings: dict, out_dir: str, avoid_topics: Optional[List[str]] = None,
                  avoid_titles: Optional[List[str]] = None, llm: Optional[Callable] = None,
                  generators: Optional[Dict[str, Callable[..., dict]]] = None,
                  sleep: Callable[[float], None] = time.sleep, n_shots: int = 4,
                  run_id: Optional[str] = None, runs_dir: Optional[str] = None,
                  viral: Optional[dict] = None) -> Tuple[str, dict, dict]:
    """Returns (mp4_path, plan, info). info carries cost ledger, shot list, captions, probe.
    Durable: plan + every finished/submitted shot are saved under runs_dir/run_id; pass the same run_id to resume."""
    ledger = CostLedger(cost_cap(settings))
    state = RunState(runs_dir or os.path.join(out_dir, "runs"), run_id)
    for item in state.data.get("cost_items", []):  # spend from earlier attempts of this run counts toward the cap
        ledger.items.append(dict(item))
    if state.plan:
        plan = state.plan
        logger.info("%s resuming run %s with saved plan (no planning call)", MARKER, state.run_id)
    else:
        plan = plan_short(settings, avoid_topics, avoid_titles, llm=llm, n_shots=n_shots, ledger=ledger, viral=viral)
        for item in ledger.items:
            state.data["cost_items"].append(dict(item))
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
    # Budget fit: if the planner's lengths can't fit the cap, fall back to the verified 4,5,5,5 plan
    # (Veo Fast 4s $0.40 + 3x Seedance 5s ~$0.108 = ~$0.73) instead of failing the slot.
    def _est(p: dict) -> float:
        lite = str(p.get("broll_engine", "")).startswith("veo_lite")
        return sum(shot_cost("veo_fast", veo_billed_seconds(s["seconds"])) if s["role"] == "hook"
                   else (shot_cost("veo_lite", veo_seconds_for(s["seconds"])) if lite else shot_cost("seedance_fast", s["seconds"]))
                   for s in p["shots"])
    if not state.plan and not plan.get("forced_durations") and ledger.total + _est(plan) > ledger.cap:
        for shot, sec in zip(plan["shots"], BUDGET_DURATIONS):
            shot["seconds"] = sec
        plan["shots"] = plan["shots"][:len(BUDGET_DURATIONS)]
        plan["budget_durations"] = BUDGET_DURATIONS
    if not state.plan:
        # durable right after planning: story, prompts, hook text, overlays, LLM per-platform copy
        plan["draft_platform_captions"] = build_platform_captions(plan)
        state.set_plan(plan)
    # pre-flight: remaining (not finished, not already submitted) shots must fit the cap
    lite_only = str(plan.get("broll_engine", "")).startswith("veo_lite")
    est = sum(shot_cost("veo_fast", veo_billed_seconds(s["seconds"])) if s["role"] == "hook"
              else (shot_cost("veo_lite", veo_seconds_for(s["seconds"])) if lite_only else shot_cost("seedance_fast", s["seconds"]))
              for s in plan["shots"] if not state.done_shot(s["index"]) and not state.pending(s["index"]))
    ledger.reserve("preflight_estimate", est)
    os.makedirs(out_dir, exist_ok=True)
    workdir = state.dir  # shot files live with run.json so they survive a stop
    done: List[dict] = []
    try:
        for shot in plan["shots"]:
            saved = state.done_shot(shot["index"])
            if saved:
                logger.info("%s run %s: reusing finished shot %s (%s)", MARKER, state.run_id, shot["index"], saved.get("request_id"))
                done.append({**shot, **saved})
                continue
            start = None
            if done and shot["role"] == "broll":
                start = last_frame(done[-1]["path"], os.path.join(workdir, f"start{shot['index']}.jpg"))
            done.append(generate_shot(shot, settings, workdir, ledger, generators, sleep, start_image=start, state=state))
        captions = build_platform_captions(plan, {s.get("engine") for s in done})
        copy_audit = audit_and_fix_copy(plan, done, captions)
        tw = captions.get("twitter")
        if tw and len(tw["caption"]) > 280:
            tags = " ".join(tw.get("hashtags") or [])
            tw["caption"] = f"{clamp_text(tw['caption'].replace(tags, '').strip(), 278 - len(tags))}\n\n{tags}".strip()
        out = os.path.join(out_dir, f"short_{uuid.uuid4().hex[:12]}.mp4")
        edit_dir = tempfile.mkdtemp(prefix="stitched_edit_")
        try:
            info = assemble(done, plan["on_screen_hook"], out, edit_dir)
        finally:
            shutil.rmtree(edit_dir, ignore_errors=True)
    except Exception as exc:
        state.finish(status="stopped", error=str(exc)[:500])
        logger.error("%s Short failed; cost so far %s; resume with run_id=%s (saved in %s)", MARKER,
                     json.dumps(ledger.as_dict()), state.run_id, state.dir)
        raise
    if not plan.get("forced_durations") and not (MIN_TOTAL - 0.5 <= info["duration"] <= MAX_TOTAL + 0.5):
        raise RuntimeError(f"final length {info['duration']}s outside {MIN_TOTAL}-{MAX_TOTAL}s")
    info.update({
        "format": "stitched_short", "version": VERSION, "media_source": "ai_generated",
        "cost": ledger.as_dict(), "cost_usd": ledger.total,
        "shot_list": [{k: s.get(k) for k in ("index", "role", "seconds", "engine", "model", "attempts", "cost_usd", "caption", "prompt", "errors", "chained_from_previous_frame", "request_id", "resumed_from_request")} for s in done],
        "platform_captions": captions, "copy_audit": copy_audit,
        "pinned_comment": plan.get("pinned_comment") or plan.get("comment_question") or "",
        "viral_research": {"angle": plan.get("viral_angle"), "refs_used": plan.get("viral_refs_used"),
                           "refs": [{k: r.get(k) for k in ("title", "views", "url")} for r in (plan.get("viral_refs") or [])]},
        "run_id": state.run_id, "run_dir": state.dir,
    })
    state.finish(status="done", output=out, final_captions=captions, copy_audit=copy_audit,
                 final_overlays={"hook": plan["on_screen_hook"], "shots": {s["index"]: s.get("caption") for s in done}},
                 total_usd=ledger.total, probe={k: info.get(k) for k in ("width", "height", "duration", "integrated_lufs")})
    logger.info("%s Short ready %s length=%.1fs cost=$%.3f (cap $%.2f)", MARKER, out, info["duration"], ledger.total, ledger.cap)
    return out, plan, info
