#!/usr/bin/env python3
from pathlib import Path
import subprocess, sys, urllib.request

V8 = 'https://clip-host-5pm-production.up.railway.app/hotfix.py'
try:
    urllib.request.urlretrieve(V8, '/tmp/v8_hotfix.py')
    subprocess.check_call([sys.executable, '/tmp/v8_hotfix.py'])
except Exception as exc:
    print('V8 hotfix skipped or failed:', exc, flush=True)

SOURCE = r'''"""Always-post Autopilot wrapper. Must run AFTER cockpit_overlay.apply().

Live Railway still boots GitHub main plus the V8 overlay hotfix. That overlay
replaces execute/download/scheduler with nested closures and builds a worklist
of TikTok + YouTube only — hosted morning/evening.mp4 is dropped, LAST_RESORT
is excluded, and a failed hour retries the same broken list every 10 minutes.

Repo edits to main.py do not change that path. This module overwrites the
closures the overlay actually calls, then replaces execute so every 8 AM / 5 PM
slot still publishes when TikTok extractors, YouTube bot-checks, or audio-only
downloads fail.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional

MARKER = "COCKPIT_V10_ALWAYS_POST"
VIDEO_FORMAT = "bv*+ba/b[vcodec!=none][ext=mp4]/b[vcodec!=none]"
_DUPLICATE_ID_RE = re.compile(r'"duplicate_post_id"\s*:\s*"([^"]+)"')
MORNING_CLIP = "https://clip-host-5pm-production.up.railway.app/morning.mp4"
EVENING_CLIP = "https://clip-host-5pm-production.up.railway.app/evening.mp4"
OPENAI_CLIP = "https://clip-host-5pm-production.up.railway.app/tiktok_openai.mp4"
HOSTED_CLIPS = (
    EVENING_CLIP,
    MORNING_CLIP,
    OPENAI_CLIP,
)
CLIP_TITLES = {
    EVENING_CLIP: "Alibaba Cloud x ATLAS V — Powered by Wan",
    MORNING_CLIP: "Alibaba Cloud x ATLAS V — Powered by Wan",
    OPENAI_CLIP: "Cinematic AI short — OpenAI video model",
}
# Give the live overlay a full scan + download walk before any last-resort clip.
OVERLAY_TIMEOUT_SEC = 720
GENERIC_COPY_MARKERS = (
    "packaged for today's autopilot",
    "for today's autopilot slot",
    "6frame hosted autopilot original",
)


def file_has_video_stream(path: str) -> bool:
    if not path or not os.path.exists(path):
        return False
    try:
        res = subprocess.run(
            [
                shutil.which("ffprobe") or "ffprobe",
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_name,width,height",
                "-of", "csv=p=0",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return bool((res.stdout or "").strip())
    except Exception:
        return False


def duplicate_id(message: str) -> Optional[str]:
    match = _DUPLICATE_ID_RE.search(message or "")
    return match.group(1) if match else None


def is_direct_video_url(url: str) -> bool:
    lowered = (url or "").split("?", 1)[0].lower()
    return lowered.startswith("http") and lowered.endswith((".mp4", ".mov", ".m4v", ".webm"))


def hosted_clip_for_hour(now: Optional[datetime] = None) -> str:
    forced = (os.environ.get("AUTOPILOT_FORCE_HOSTED_URL") or os.environ.get("LAST_RESORT_MEDIA_URL") or "").strip()
    if forced:
        return forced
    hour = (now or datetime.now(timezone.utc)).hour
    # 15:00 UTC = 8 AM Pacific, 00:00 UTC = 5 PM Pacific.
    if hour == 15 or 12 <= hour <= 19:
        return MORNING_CLIP
    return EVENING_CLIP


def is_generic_autopilot_copy(text: Optional[str]) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return True
    return any(marker in lowered for marker in GENERIC_COPY_MARKERS)


def hosted_trend(now: Optional[datetime] = None) -> dict:
    url = hosted_clip_for_hour(now)
    return {
        "url": url,
        "title": CLIP_TITLES.get(url, ""),
        "platform": "hosted",
        "recreated_linkedin_post": "",
        "recreated_twitter_thread": None,
        "recreated_instagram_caption": "",
        "suggested_hashtags": ["#6FrameStudio", "#AIFilmmaking", "#AICinema"],
    }


def _looks_like_fresh_scan_execute(fn: Any) -> bool:
    code = getattr(fn, "__code__", None)
    names = getattr(code, "co_names", ()) or ()
    return any(
        name in names
        for name in ("_scan_autopilot_trends", "collect_autopilot_trend_candidates")
    )


def rewrite_ytdlp_extra(extra: List[str]) -> List[str]:
    out = list(extra)
    if "-f" in out:
        idx = out.index("-f")
        if idx + 1 < len(out) and "vcodec!=none" not in out[idx + 1]:
            out[idx + 1] = VIDEO_FORMAT
    if "--merge-output-format" not in out:
        out = ["--merge-output-format", "mp4"] + out
    return out


def wrap_ytdlp_attempts(orig_attempts: Callable[..., List[List[str]]]) -> Callable[..., List[List[str]]]:
    def ytdlp_download_attempts(url: str, max_duration_sec: int) -> List[List[str]]:
        rewritten = [rewrite_ytdlp_extra(extra) for extra in orig_attempts(url, max_duration_sec)]
        return rewritten or orig_attempts(url, max_duration_sec)

    return ytdlp_download_attempts


def http_download_video(url: str, dest_dir: str, error_cls: type) -> str:
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, f"hosted_{os.urandom(8).hex()}.mp4")
    cmd = [shutil.which("curl") or "curl", "-fsSL", "--max-time", "90", "-o", dest, url]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=100)
    if res.returncode != 0 or not os.path.exists(dest) or os.path.getsize(dest) < 1000:
        if os.path.exists(dest):
            try:
                os.remove(dest)
            except OSError:
                pass
        raise error_cls(f"Hosted clip download failed for {url}: {(res.stderr or res.stdout or '')[-300:]}")
    if not file_has_video_stream(dest):
        try:
            os.remove(dest)
        except OSError:
            pass
        raise error_cls(f"Hosted clip has no video stream: {url}")
    return dest


def wrap_download(orig_download: Callable[..., str], error_cls: type, dest_dir: Optional[str] = None) -> Callable[..., str]:
    def download_and_trim_original_video(*args, **kwargs):
        url = str(args[0] if args else kwargs.get("url") or "").strip()
        if dest_dir and is_direct_video_url(url):
            path = http_download_video(url, dest_dir, error_cls)
            ensure = None
            try:
                import main as m
                ensure = getattr(m, "ensure_video_under_limit", None)
            except Exception:
                ensure = None
            if callable(ensure):
                try:
                    return ensure(path, max_duration_sec=kwargs.get("max_duration_sec") or 60)
                except Exception:
                    return path
            return path
        path = orig_download(*args, **kwargs)
        if not file_has_video_stream(path):
            raise error_cls(
                "Downloaded file has no video stream (audio-only). Skip this clip and try the next scanned trend."
            )
        return path

    return download_and_trim_original_video


def wrap_try_download(orig_try: Callable):
    """Do not inject the hosted clip here. A hosted winner skips the viral scan."""

    async def try_download_candidates(candidates, skipped):
        try:
            return await orig_try(candidates, skipped)
        except TypeError:
            return await orig_try(candidates)

    return try_download_candidates


def _iter_closure_cells(fn: Any, _seen: Optional[set] = None):
    """Walk nested closures without following self-recursive cells (V8 _safe_publish)."""
    seen = _seen if _seen is not None else set()
    fn_id = id(fn)
    if fn_id in seen:
        return
    seen.add(fn_id)
    names = getattr(getattr(fn, "__code__", None), "co_freevars", ()) or ()
    cells = getattr(fn, "__closure__", None) or ()
    for name, cell in zip(names, cells):
        yield name, cell
        try:
            value = cell.cell_contents
        except ValueError:
            continue
        if callable(value) and id(value) not in seen:
            yield from _iter_closure_cells(value, seen)


def patch_overlay_closures(root_fn: Any, replacements: dict[str, Any]) -> List[str]:
    patched: List[str] = []
    seen: set[int] = set()
    for name, cell in _iter_closure_cells(root_fn):
        cell_id = id(cell)
        if cell_id in seen:
            continue
        seen.add(cell_id)
        if name not in replacements:
            continue
        try:
            current = cell.cell_contents
        except ValueError:
            current = None
        replacement = replacements[name]
        if current is replacement:
            continue
        cell.cell_contents = replacement
        patched.append(name)
    return patched


def _slot_already_published(before_ids: set) -> bool:
    import main as m
    try:
        posts = m.load_scheduled_posts()
    except Exception:
        return False
    for post in posts:
        if post.get("id") in before_ids:
            continue
        if not (post.get("video_path") or "").strip():
            continue
        status = post.get("status")
        if status in {"SUCCESS", "PARTIAL_SUCCESS"} and post.get("posted_at"):
            return True
        if status == "AWAITING_APPROVAL" and not post.get("blocked_by_daily_limit"):
            return True
    return False


async def _call_maybe(fn: Callable, *args):
    maybe = fn(*args)
    if asyncio.iscoroutine(maybe):
        return await maybe
    return maybe


async def publish_hosted_clip(settings: dict, bypass_daily_limit: bool = True) -> bool:
    """Curl a known-good clip-host mp4 and publish it. Does not use the V8 worklist."""
    import main as m

    error_cls = getattr(m, "OriginalVideoDownloadError", ValueError)
    dest_dir = getattr(m, "GENERATED_DIR", "/app/data/generated")
    hosted = hosted_trend()
    used = set()
    posted_urls = getattr(m, "posted_autopilot_source_urls", None)
    if callable(posted_urls):
        try:
            used = set(posted_urls())
        except Exception:
            used = set()
    path = None
    last_err = ""
    for url in (hosted["url"], *HOSTED_CLIPS):
        if not url:
            continue
        try:
            path = http_download_video(url, dest_dir, error_cls)
            hosted = {
                **hosted,
                "url": url,
                "title": CLIP_TITLES.get(url, hosted.get("title") or ""),
                "recreated_linkedin_post": "",
                "recreated_instagram_caption": "",
            }
            break
        except Exception as exc:
            last_err = str(exc)
            m.logger.warning("V10 hosted curl failed for %s: %s", url, exc)
    if not path:
        m.logger.error("V10 guaranteed publish had no hosted video: %s", last_err)
        return False

    ensure = getattr(m, "ensure_video_under_limit", None)
    if callable(ensure):
        try:
            path = ensure(path, max_duration_sec=60)
        except Exception as exc:
            m.logger.warning("V10 could not trim hosted clip: %s", exc)

    rel = f"/static/assets/generated/{os.path.basename(path)}"
    copy = {
        "linkedin_text": "",
        "twitter_thread": None,
        "instagram_caption": "",
        "suggested_hashtags": ["#6FrameStudio", "#AIFilmmaking", "#AICinema"],
    }
    resolver = getattr(m, "resolve_autopilot_copy", None)
    if callable(resolver):
        try:
            resolved = resolver(hosted, settings)
            if isinstance(resolved, dict):
                copy.update({k: v for k, v in resolved.items() if v and not is_generic_autopilot_copy(str(v))})
        except Exception as exc:
            m.logger.warning("V10 copy fallback after resolve_autopilot_copy failed: %s", exc)
    if is_generic_autopilot_copy(copy.get("linkedin_text")):
        copy["linkedin_text"] = ""
    if is_generic_autopilot_copy(copy.get("instagram_caption")):
        copy["instagram_caption"] = ""
    if not copy.get("linkedin_text"):
        title = hosted.get("title") or "Cinematic AI short"
        url = hosted.get("url") or ""
        copy["linkedin_text"] = (
            f"{title}\n\n"
            "A 6Frame Studio Autopilot pick — cinematic generative video craft, "
            "composition, and motion design.\n\n"
            f"Source: {url}\n\n"
            "#6FrameStudio #AIFilmmaking #AICinema"
        )

    platforms = list(settings.get("autonomous_platforms") or ["twitter", "linkedin", "instagram", "tiktok", "youtube"])
    now = datetime.now()
    log_post = {
        "id": str(__import__("uuid").uuid4()),
        "platform": ",".join(platforms),
        "text": copy.get("linkedin_text") or hosted["title"],
        "thread": copy.get("twitter_thread"),
        "instagram_caption": copy.get("instagram_caption") or "",
        "suggested_hashtags": copy.get("suggested_hashtags") or [],
        "title": hosted["title"],
        "scheduled_time": now.isoformat(),
        "campaign_title": f"Autonomous: {hosted['title']}",
        "video_path": rel,
        "vertical_video_path": None,
        "source_video_path": rel,
        "source_url": hosted["url"],
        "media_source": "hosted_fallback",
        "viral_template_id": None,
        "status": "PUBLISHING",
        "error_message": None,
        "skipped_sources": [],
        "posted_at": None,
    }
    apply_tags = getattr(m, "apply_platform_hashtags", None)
    apply_thread = getattr(m, "apply_platform_twitter_thread", None)
    if callable(apply_tags):
        try:
            log_post["text"] = apply_tags(log_post, "linkedin")
            if log_post.get("instagram_caption"):
                log_post["instagram_caption"] = apply_tags(log_post, "instagram")
        except Exception:
            pass
    if callable(apply_thread) and log_post.get("thread"):
        try:
            log_post["thread"] = apply_thread(log_post)
        except Exception:
            pass

    if any(str(p).lower() in {"instagram", "tiktok", "youtube", "facebook", "threads"} for p in platforms):
        try:
            abs_source = path
            resolve_local = getattr(m, "resolve_local_video_path", None)
            if callable(resolve_local):
                abs_source = resolve_local(rel) or path
            vertical_name = f"{os.path.splitext(os.path.basename(path))[0]}_vertical_9x16.mp4"
            abs_vertical = os.path.join(dest_dir, vertical_name)
            render = getattr(m, "render_variant_with_fallback", None)
            if callable(render):
                font = getattr(m, "get_font_path", lambda: "")()
                render(abs_source, abs_vertical, 1080, 1920, "", font, 320)
                log_post["vertical_video_path"] = f"/static/assets/generated/{vertical_name}"
        except Exception as exc:
            m.logger.warning("V10 vertical render skipped: %s", exc)

    posts = m.load_scheduled_posts()
    posts.append(log_post)
    m.save_scheduled_posts(posts)

    if settings.get("require_autopilot_approval"):
        log_post["status"] = "AWAITING_APPROVAL"
        posts = m.load_scheduled_posts()
        for item in posts:
            if item.get("id") == log_post["id"]:
                item["status"] = "AWAITING_APPROVAL"
                break
        m.save_scheduled_posts(posts)
        m.logger.info("V10 hosted clip staged for approval from %s", hosted["url"])
        return True

    try:
        result = await m.publish_post_to_platforms(log_post, settings, bypass_daily_limit)
    except TypeError:
        result = await m.publish_post_to_platforms(log_post, settings)
    except Exception as exc:
        m.logger.error("V10 hosted publish raised: %s", exc)
        posts = m.load_scheduled_posts()
        for item in posts:
            if item.get("id") == log_post["id"]:
                item["status"] = "FAILED"
                item["error_message"] = f"V10 hosted publish failed: {exc}"
                break
        m.save_scheduled_posts(posts)
        return False

    posts = m.load_scheduled_posts()
    for item in posts:
        if item.get("id") == log_post["id"]:
            apply = getattr(m, "apply_publish_result_to_post", None)
            if callable(apply):
                apply(item, result)
            else:
                errors = result.get("errors") or []
                successes = result.get("successes") or []
                if successes and not errors:
                    item["status"] = "SUCCESS"
                    item["posted_at"] = datetime.now().isoformat()
                elif successes:
                    item["status"] = "PARTIAL_SUCCESS"
                    item["posted_at"] = datetime.now().isoformat()
                    item["error_message"] = "; ".join(errors)
                else:
                    item["status"] = "FAILED"
                    item["error_message"] = "; ".join(errors) or "hosted publish failed"
            break
    m.save_scheduled_posts(posts)
    status = next((item.get("status") for item in posts if item.get("id") == log_post["id"]), "FAILED")
    m.logger.info("V10 guaranteed publish from %s status=%s", hosted["url"], status)
    return status in {"SUCCESS", "PARTIAL_SUCCESS"}


def apply(force: bool = False) -> None:
    import main as m

    if getattr(m, "_cockpit_v10_marker", "") == MARKER and not force:
        return

    error_cls = getattr(m, "OriginalVideoDownloadError", ValueError)
    dest_dir = getattr(m, "GENERATED_DIR", "/app/data/generated")

    orig_attempts = m.ytdlp_download_attempts
    ytdlp_download_attempts = wrap_ytdlp_attempts(orig_attempts)
    orig_download = m.download_and_trim_original_video
    download_and_trim_original_video = wrap_download(orig_download, error_cls, dest_dir=dest_dir)
    orig_post = m.postproxy_post

    def postproxy_post(settings, path, payload, timeout=120):
        try:
            return orig_post(settings, path, payload, timeout=timeout)
        except TypeError:
            return orig_post(settings, path, payload)
        except ValueError as exc:
            err = str(exc)
            if path.rstrip("/").endswith("/posts") and "409" in err.lower():
                dup = duplicate_id(err)
                if dup:
                    m.logger.warning("V10 adopting PostProxy duplicate %s", dup)
                    requested = list((payload or {}).get("profiles") or [])
                    try:
                        existing = m.postproxy_get(settings, f"/posts/{dup}", timeout=60)
                        if isinstance(existing, dict):
                            existing.setdefault("id", dup)
                            return existing
                    except Exception:
                        pass
                    return {
                        "id": dup,
                        "status": "processed",
                        "duplicate_adopted": True,
                        "platforms": [{"platform": p, "status": "published"} for p in requested],
                    }
            raise

    orig_meta = getattr(m, "ensure_meta_compatible_video", None)

    def ensure_meta_compatible_video(local_path, min_duration_sec=5):
        if not file_has_video_stream(local_path):
            raise ValueError("Cannot create Meta-compatible video: source has no video stream (audio-only download).")
        if orig_meta:
            return orig_meta(local_path, min_duration_sec=min_duration_sec)
        return local_path

    orig_media = getattr(m, "postproxy_media_url", None)

    def postproxy_media_url(post, settings, platforms):
        try:
            if orig_media:
                return orig_media(post, settings, platforms)
        except Exception as exc:
            m.logger.warning("V10 landscape fallback after vertical/meta failure: %s", exc)
        return m.get_public_video_url(post, settings, "linkedin")

    orig_publish = getattr(m, "publish_via_postproxy", None)

    def publish_via_postproxy(post, settings, platforms):
        if not orig_publish:
            raise RuntimeError("publish_via_postproxy missing")
        try:
            return orig_publish(post, settings, platforms)
        except ValueError as exc:
            err = str(exc).lower()
            quota = any(token in err for token in ("24-hour", "24 hour", "tweet limit", "daily tweet", "rate limit"))
            twitterish = any(str(p).lower() in {"twitter", "x"} for p in (platforms or []))
            if quota and twitterish:
                remaining = [p for p in platforms if str(p).lower() not in {"twitter", "x"}]
                if remaining:
                    m.logger.error("V10 isolating Twitter quota; publishing remaining networks: %s", remaining)
                    result = orig_publish(post, settings, remaining)
                    result["errors"] = list(result.get("errors") or []) + [str(exc)]
                    return result
            raise

    m.ytdlp_download_attempts = ytdlp_download_attempts
    m.download_and_trim_original_video = download_and_trim_original_video
    m.postproxy_post = postproxy_post
    if orig_meta:
        m.ensure_meta_compatible_video = ensure_meta_compatible_video
    if orig_media:
        m.postproxy_media_url = postproxy_media_url
    if orig_publish:
        m.publish_via_postproxy = publish_via_postproxy
    m.file_has_video_stream = file_has_video_stream
    m.hosted_clip_for_hour = hosted_clip_for_hour

    overlay_download = None
    overlay_attempts = None
    overlay_try = None
    orig_execute = None
    overlay_execute = getattr(m, "execute_autonomous_autopost", None)
    if callable(overlay_execute) and getattr(overlay_execute, "_cockpit_v10", False) is True:
        overlay_execute = None
    patched = []
    try:
        if callable(overlay_execute):
            for name, cell in _iter_closure_cells(overlay_execute):
                try:
                    value = cell.cell_contents
                except ValueError:
                    continue
                if name == "download_and_trim_original_video" and callable(value):
                    overlay_download = value
                elif name == "ytdlp_download_attempts" and callable(value):
                    overlay_attempts = value
                elif name == "try_download_candidates" and callable(value):
                    overlay_try = value
                elif name == "orig_execute" and callable(value):
                    orig_execute = value
                    if _looks_like_fresh_scan_execute(value):
                        # Keep the repo scan, not the V8 worklist wrapper.
                        orig_execute = value

        replacements: dict[str, Any] = {}
        if overlay_attempts is not None:
            replacements["ytdlp_download_attempts"] = wrap_ytdlp_attempts(overlay_attempts)
        if overlay_download is not None:
            wrapped_overlay_download = wrap_download(overlay_download, error_cls, dest_dir=dest_dir)
            if "ytdlp_download_attempts" in replacements:
                patch_overlay_closures(overlay_download, {
                    "ytdlp_download_attempts": replacements["ytdlp_download_attempts"],
                })
            replacements["download_and_trim_original_video"] = wrapped_overlay_download
        if overlay_try is not None:
            replacements["try_download_candidates"] = wrap_try_download(overlay_try)
        if replacements and callable(overlay_execute):
            patched = patch_overlay_closures(overlay_execute, replacements)
    except Exception as walk_err:
        m.logger.warning("V10 overlay closure walk failed; guaranteed publish still applies: %s", walk_err)

    async def execute_autonomous_autopost(settings, forced_trend=None, bypass_daily_limit=True):
        m.logger.info("Starting Autopilot (%s)...", MARKER)
        before_ids = {p.get("id") for p in m.load_scheduled_posts()}
        # Prefer the repo execute that runs a new viral scan every slot.
        # The live V8 overlay reuses last_trend_scan / injects the same hosted clip.
        runner = orig_execute if (orig_execute and _looks_like_fresh_scan_execute(orig_execute)) else (overlay_execute or orig_execute)
        if callable(runner) and runner is orig_execute and runner is not overlay_execute:
            m.logger.info("V10 using fresh-scan execute — new viral scan this slot, no reused worklist")
        if callable(runner):
            try:
                await asyncio.wait_for(_call_maybe(runner, settings, forced_trend), timeout=OVERLAY_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                m.logger.error("Overlay Autopilot timed out after %ss; publishing hosted clip", OVERLAY_TIMEOUT_SEC)
            except TypeError:
                try:
                    await asyncio.wait_for(_call_maybe(runner, settings), timeout=OVERLAY_TIMEOUT_SEC)
                except Exception as exc:
                    m.logger.error("Overlay Autopilot raised: %s", exc)
            except Exception as exc:
                m.logger.error("Overlay Autopilot raised: %s", exc)
        if _slot_already_published(before_ids):
            return True
        m.logger.warning("V10 last-resort unused hosted clip after a fresh scan produced no download")
        return await publish_hosted_clip(settings, bypass_daily_limit=True)

    execute_autonomous_autopost._cockpit_v10 = True
    m.execute_autonomous_autopost = execute_autonomous_autopost
    m.publish_hosted_clip = publish_hosted_clip
    scheduler = getattr(m, "scheduler_loop", None)
    scheduler_patched = []
    if callable(scheduler):
        try:
            scheduler_patched = patch_overlay_closures(scheduler, {
                "execute_autonomous_autopost": execute_autonomous_autopost,
            })
        except Exception as sched_err:
            m.logger.warning("V10 scheduler closure patch failed: %s", sched_err)
            try:
                m.scheduler_loop = scheduler
            except Exception:
                pass

    m._cockpit_v10_marker = MARKER
    m.logger.info(
        "Applied %s — curl hosted morning/evening last resort, refuse audio-only, adopt 409s%s%s",
        MARKER,
        f"; execute closures {patched}" if patched else "",
        f"; scheduler closures {scheduler_patched}" if scheduler_patched else "",
    )
'''
dest = Path('/app/cockpit_v10.py')
if not dest.parent.exists():
    dest = Path('cockpit_v10.py')
dest.write_text(SOURCE, encoding='utf-8')
print('HOTFIX_V10_FRESH_SCAN_OK', dest, flush=True)

# Hook ALWAYS_POST into main.py so uvicorn import applies it after V8 overlay.
root = Path('/app') if (Path('/app') / 'main.py').exists() else Path('.')
main = root / 'main.py'
mt = main.read_text(encoding='utf-8')
hook = (
    "\n\ntry:\n"
    "    import cockpit_v10\n"
    "    cockpit_v10.apply(force=True)\n"
    "except Exception as _v10_err:\n"
    "    print('ALWAYS_POST apply failed', _v10_err)\n"
)
if 'cockpit_v10.apply' not in mt:
    main.write_text(mt + hook, encoding='utf-8')
    print('ALWAYS_POST hooked into main.py', flush=True)
else:
    print('ALWAYS_POST already hooked', flush=True)
