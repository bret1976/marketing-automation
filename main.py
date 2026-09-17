import os
import time
import uuid
import json
import logging
import asyncio
import html
import subprocess
import hmac
import hashlib
import secrets
import threading
import base64
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, HTTPException, Request, Response
from fastapi.responses import JSONResponse, FileResponse, PlainTextResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from google import genai
from google.genai import types
import tweepy
import requests

from orchestrator import (
    run_multi_agent_pipeline, get_job_status, update_job_status, run_live_trend_scanner,
    run_video_generation, normalize_video_duration, jobs_status, repurpose_video_link_copy,
    generate_video_variants, generate_virality_score, draft_engagement_reply,
    render_variant_with_fallback, get_font_path
)
from template_renderer import list_viral_templates, render_template_video

# Setup logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("main")

app = FastAPI(title="6Frame Studio Marketing Automation Hub")

PUBLIC_API_PATHS = {"/api/auth/login", "/api/auth/status", "/api/postproxy/callback"}
# These routes must remain reachable without credentials. Social networks fetch
# generated media directly, while /r and /b are intentionally public campaign URLs.
PUBLIC_PREFIXES = ("/static/", "/r/", "/b/", "/uploads/")
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(200 * 1024 * 1024)))
ALLOWED_UPLOAD_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm"}
STATE_LOCKS: Dict[str, threading.RLock] = {}
STATE_LOCKS_GUARD = threading.RLock()

def get_state_lock(path: str) -> threading.RLock:
    with STATE_LOCKS_GUARD:
        if path not in STATE_LOCKS:
            STATE_LOCKS[path] = threading.RLock()
        return STATE_LOCKS[path]

def read_json_file(path: str, default: Any):
    lock = get_state_lock(path)
    with lock:
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Error reading JSON file {path}: {e}")
        return default

def write_json_file(path: str, data: Any):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lock = get_state_lock(path)
    with lock:
        tmp_path = f"{path}.{uuid.uuid4().hex}.tmp"
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=4)
        os.replace(tmp_path, path)

def auth_enabled() -> bool:
    return bool(os.environ.get("ADMIN_PASSWORD"))

def session_signature() -> str:
    password = os.environ.get("ADMIN_PASSWORD", "")
    secret = os.environ.get("AUTH_SECRET") or os.environ.get("ADMIN_AUTH_SECRET") or password
    return hmac.new(secret.encode(), password.encode(), hashlib.sha256).hexdigest()

def basic_auth_is_valid(request: Request) -> bool:
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(authorization[6:], validate=True).decode("utf-8")
        username, password = decoded.split(":", 1)
    except (ValueError, UnicodeDecodeError):
        return False
    expected_username = os.environ.get("ADMIN_USERNAME", "admin")
    expected_password = os.environ.get("ADMIN_PASSWORD", "")
    return bool(expected_password) and hmac.compare_digest(username, expected_username) and hmac.compare_digest(
        password, expected_password
    )

def request_is_authenticated(request: Request) -> bool:
    if not auth_enabled():
        return True
    cookie_value = request.cookies.get("admin_session", "")
    header_value = request.headers.get("x-admin-session", "")
    expected = session_signature()
    return (
        hmac.compare_digest(cookie_value, expected)
        or hmac.compare_digest(header_value, expected)
        or basic_auth_is_valid(request)
    )

def is_public_path(path: str) -> bool:
    return (
        path in PUBLIC_API_PATHS
        or path in ("/privacy-policy", "/terms-of-service")
        or path.startswith(PUBLIC_PREFIXES)
        or path.startswith("/tiktok")
        or path.startswith("/oauth/postproxy/")
    )

@app.middleware("http")
async def require_admin_session(request: Request, call_next):
    if auth_enabled() and not is_public_path(request.url.path) and not request_is_authenticated(request):
        headers = {"WWW-Authenticate": 'Basic realm="6Frame Studio Admin", charset="UTF-8"'}
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Authentication required."}, status_code=401, headers=headers)
        return PlainTextResponse("Authentication required.", status_code=401, headers=headers)
    return await call_next(request)

@app.middleware("http")
async def add_no_cache_headers(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("STATE_DIR") or os.environ.get("DATA_DIR") or BASE_DIR
os.makedirs(STATE_DIR, exist_ok=True)

def get_binary_path(name: str) -> str:
    import shutil
    brew_path = f"/opt/homebrew/bin/{name}"
    if os.path.exists(brew_path):
        return brew_path
    which_path = shutil.which(name)
    if which_path:
        return which_path
    return name

# Directories for durable runtime data. On Railway, STATE_DIR is mounted to
# /app/data; generated media must live there or deploys will orphan URLs.
UPLOAD_DIR = os.path.join(STATE_DIR, "uploads")
GENERATED_DIR = os.path.join(STATE_DIR, "generated")
REPORTS_DIR = os.path.join(STATE_DIR, "reports")
SETTINGS_FILE = os.path.join(STATE_DIR, "settings.json")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(GENERATED_DIR, exist_ok=True)
os.makedirs(REPORTS_DIR, exist_ok=True)

# Default Settings
DEFAULT_SETTINGS = {
    "gemini_api_key": "",
    "brand_voice": (
        "6Frame Studio is a premium cinematic AI video production lab. "
        "Our tone is artistic, visionary, and technical but refined. "
        "Focus on cinematography, visual storytelling, and advanced generative AI workflows "
        "(Sora, Midjourney, Kling, Runway Gen-3). "
        "Avoid using generic marketing buzzwords like 'game-changer', 'revolutionize', or 'mind-blown'."
    ),
    "twitter_consumer_key": "",
    "twitter_consumer_secret": "",
    "twitter_access_token": "",
    "twitter_access_token_secret": "",
    "linkedin_access_token": "",
    "linkedin_person_urn": "",
    "mock_mode": True,
    "runway_api_key": "",
    "fal_api_key": "",
    "autonomous_posting": False,
    "autonomous_hour": 9,
    "autonomous_platforms": ["twitter", "linkedin"],
    "autonomous_video_engine": "fal_hailuo_23",
    "autonomous_video_duration": 10,
    "require_autopilot_approval": True,
    "viral_template_enabled": False,
    "viral_template_style": "hook_burst",
    "viral_template_quality": "standard",
    "meta_app_id": "",
    "meta_app_secret": "",
    "instagram_access_token": "",
    "instagram_business_account_id": "",
    "facebook_page_access_token": "",
    "facebook_page_id": "",
    "tiktok_client_key": "",
    "tiktok_client_secret": "",
    "tiktok_access_token": "",
    "tiktok_refresh_token": "",
    "youtube_client_id": "",
    "youtube_client_secret": "",
    "youtube_refresh_token": "",
    "threads_access_token": "",
    "threads_user_id": "",
    "report_email_to": "",
    "report_email_provider": "smtp",
    "resend_api_key": "",
    "resend_from": "",
    "postproxy_enabled": False,
    "postproxy_api_key": "",
    "postproxy_profile_group_id": "",
    "postproxy_daily_publish_limit": 2,
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_user": "",
    "smtp_password": "",
    "smtp_from": "",
    "smtp_tls": True,
    "public_base_url": "",
    "instagram_search_hashtags": [
        "aivideo", "runwaygen3", "soraai", "aifilmmaking",
        "generativevideo", "klingai", "aicinematography", "midjourneyvideo",
    ]
}

def load_settings():
    settings = DEFAULT_SETTINGS.copy()
    
    # 1. Load from file if exists
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                file_settings = json.load(f)
                for k, v in file_settings.items():
                    if v is not None and v != "":
                        settings[k] = v
        except Exception as e:
            logger.error(f"Error reading settings file: {e}")
            
    # 2. Override with system environment variables
    for k in settings.keys():
        env_val = os.environ.get(k.upper())
        if env_val is not None and env_val != "":
            if env_val.lower() == "true":
                settings[k] = True
            elif env_val.lower() == "false":
                settings[k] = False
            elif k in ("autonomous_hour", "autonomous_video_duration", "smtp_port", "postproxy_daily_publish_limit"):
                try:
                    settings[k] = int(env_val)
                except:
                    pass
            elif k in ("autonomous_platforms", "instagram_search_hashtags"):
                try:
                    settings[k] = json.loads(env_val)
                except:
                    settings[k] = [p.strip() for p in env_val.split(",") if p.strip()]
            else:
                settings[k] = env_val
                
    return settings

def save_settings(settings):
    try:
        write_json_file(SETTINGS_FILE, settings)
    except Exception as e:
        logger.error(f"Error saving settings file: {e}")

class SettingsSchema(BaseModel):
    gemini_api_key: str
    brand_voice: str
    twitter_consumer_key: str
    twitter_consumer_secret: str
    twitter_access_token: str
    twitter_access_token_secret: str
    linkedin_access_token: str
    linkedin_person_urn: str
    mock_mode: bool
    runway_api_key: str
    fal_api_key: str
    autonomous_posting: bool
    autonomous_hour: int
    autonomous_platforms: List[str]
    autonomous_video_engine: str
    autonomous_video_duration: int
    require_autopilot_approval: bool
    viral_template_enabled: bool = False
    viral_template_style: str = "hook_burst"
    viral_template_quality: str = "standard"
    meta_app_id: str
    meta_app_secret: str
    instagram_access_token: str
    instagram_business_account_id: str
    facebook_page_access_token: str
    facebook_page_id: str
    tiktok_client_key: str
    tiktok_client_secret: str
    tiktok_access_token: str
    tiktok_refresh_token: str
    youtube_client_id: str
    youtube_client_secret: str
    youtube_refresh_token: str
    threads_access_token: str
    threads_user_id: str
    report_email_to: str = ""
    report_email_provider: str = "smtp"
    resend_api_key: str = ""
    resend_from: str = ""
    postproxy_enabled: bool = False
    postproxy_api_key: str = ""
    postproxy_profile_group_id: str = ""
    postproxy_daily_publish_limit: int = 2
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_tls: bool = True
    public_base_url: str
    instagram_search_hashtags: List[str]

class LoginRequest(BaseModel):
    password: str

class AnalyzeRequest(BaseModel):
    video_path: str
    website_url: str

class ApplyTemplateRequest(BaseModel):
    video_path: str
    template_id: Optional[str] = None
    title: Optional[str] = None
    subtitle: Optional[str] = None

class PublishTwitterRequest(BaseModel):
    text: Optional[str] = None
    thread: Optional[List[str]] = None
    is_thread: bool = False
    video_path: Optional[str] = None

class PublishLinkedinRequest(BaseModel):
    text: str
    video_path: Optional[str] = None

class PostProxyConnectRequest(BaseModel):
    platform: str
    redirect_url: Optional[str] = None
    profile_group_id: Optional[str] = None
    reconnect: bool = False

class ControlledPostProxyPublishRequest(BaseModel):
    platforms: Optional[List[str]] = None
    text: Optional[str] = None
    video_path: Optional[str] = None
    campaign_title: Optional[str] = None

def resolve_local_video_path(video_path: str) -> str:
    if not video_path:
        raise ValueError("Missing video_path.")
    if video_path.startswith("/static/assets/generated/"):
        candidate = os.path.join(GENERATED_DIR, os.path.basename(video_path))
    elif video_path.startswith("/static/"):
        candidate = os.path.join(BASE_DIR, video_path.lstrip("/"))
    elif video_path.startswith("static/assets/generated/"):
        candidate = os.path.join(GENERATED_DIR, os.path.basename(video_path))
    elif video_path.startswith("static/"):
        candidate = os.path.join(BASE_DIR, video_path)
    elif os.path.isabs(video_path):
        candidate = video_path
    else:
        candidate = os.path.join(BASE_DIR, video_path)

    resolved = os.path.realpath(candidate)
    allowed_roots = (
        os.path.realpath(UPLOAD_DIR),
        os.path.realpath(GENERATED_DIR),
        os.path.realpath(os.path.join(BASE_DIR, "static")),
    )
    if not any(os.path.commonpath((resolved, root)) == root for root in allowed_roots):
        raise ValueError("video_path must reference an uploaded or generated media file.")
    extension = os.path.splitext(resolved)[1].lower()
    if extension not in ALLOWED_UPLOAD_EXTENSIONS:
        raise ValueError("video_path must reference a supported video file.")
    return resolved

def upload_video_to_twitter(video_path: str, settings: dict) -> Optional[int]:
    required_keys = ["twitter_consumer_key", "twitter_consumer_secret", "twitter_access_token", "twitter_access_token_secret"]
    if not all(settings.get(k) for k in required_keys):
        logger.error("Missing Twitter API credentials for video upload.")
        return None
        
    abs_video_path = resolve_local_video_path(video_path)
        
    if not os.path.exists(abs_video_path):
        logger.error(f"Twitter upload: Video file does not exist at {abs_video_path}")
        return None
        
    try:
        logger.info(f"Uploading video {abs_video_path} to Twitter/X...")
        auth = tweepy.OAuth1UserHandler(
            settings["twitter_consumer_key"],
            settings["twitter_consumer_secret"],
            settings["twitter_access_token"],
            settings["twitter_access_token_secret"]
        )
        api = tweepy.API(auth)
        media = api.media_upload(filename=abs_video_path, chunked=True, media_category="tweet_video")
        logger.info(f"Video uploaded successfully to X. Media ID: {media.media_id}")
        return media.media_id
    except Exception as e:
        logger.error(f"Twitter video upload failed: {e}")
        return None

def upload_video_to_linkedin(video_path: str, author_urn: str, settings: dict) -> Optional[str]:
    if not settings.get("linkedin_access_token"):
        logger.error("Missing LinkedIn access token for video upload.")
        return None
        
    abs_video_path = resolve_local_video_path(video_path)
        
    if not os.path.exists(abs_video_path):
        logger.error(f"LinkedIn upload: Video file does not exist at {abs_video_path}")
        return None
        
    try:
        logger.info(f"Uploading video {abs_video_path} to LinkedIn...")
        register_url = "https://api.linkedin.com/v2/assets?action=registerUpload"
        headers = {
            'Authorization': f'Bearer {settings["linkedin_access_token"]}',
            'X-Restli-Protocol-Version': '2.0.0',
            'Content-Type': 'application/json'
        }
        register_payload = {
            "registerUploadRequest": {
                "recipes": ["urn:li:digitalmediaRecipe:feedshare-video"],
                "owner": author_urn,
                "serviceRelationships": [
                    {
                        "identifier": "urn:li:userGeneratedContent",
                        "relationshipType": "OWNER"
                    }
                ]
            }
        }
        reg_res = requests.post(register_url, headers=headers, json=register_payload)
        if reg_res.status_code != 200:
            logger.error(f"Failed to register video upload on LinkedIn: {reg_res.text}")
            return None
            
        reg_data = reg_res.json()
        upload_url = reg_data["value"]["uploadMechanism"]["com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest"]["uploadUrl"]
        asset_urn = reg_data["value"]["asset"]
        
        with open(abs_video_path, "rb") as f:
            video_data = f.read()
            
        upload_res = requests.put(upload_url, data=video_data, headers={"Authorization": f"Bearer {settings['linkedin_access_token']}"})
        if upload_res.status_code not in [200, 201, 204]:
            logger.error(f"Failed to upload video binary to LinkedIn. HTTP status: {upload_res.status_code}, Response: {upload_res.text}")
            return None
            
        logger.info(f"Video uploaded successfully to LinkedIn. Asset URN: {asset_urn}")
        return asset_urn
    except Exception as e:
        logger.error(f"LinkedIn video upload failed: {e}")
        return None

def get_public_video_url(post: dict, settings: dict, platform_key: str = "") -> str:
    """Instagram/TikTok/Facebook/Threads require a publicly reachable HTTPS URL for the video,
    not a raw file upload. Requires PUBLIC_BASE_URL to be set to this app's own public domain
    (e.g. the Railway production URL) since these platforms fetch the file themselves.
    Instagram Reels and TikTok require vertical (9:16) video — if the post carries a
    vertical_video_path (rendered as a variant of the main video), prefer it for those two
    platforms; other platforms use the original video_path as-is."""
    base_url = settings.get("public_base_url", "").rstrip("/")
    if not base_url:
        raise ValueError(
            "Missing Public Base URL setting. Instagram/TikTok/Facebook/Threads fetch the video "
            "from a public HTTPS URL — set 'Public Base URL' in Settings to this app's public domain "
            "(e.g. your Railway production URL) before publishing to these platforms."
        )
    video_path = post.get("video_path")
    if platform_key in ("instagram", "tiktok", "youtube") and post.get("vertical_video_path"):
        video_path = post["vertical_video_path"]
    if not video_path:
        raise ValueError("This post has no video attached — Instagram/TikTok/Facebook/Threads require a video.")
    return f"{base_url}{video_path}"

def get_video_duration_seconds(video_path: str) -> Optional[float]:
    try:
        res = subprocess.run(
            [
                get_binary_path("ffprobe"),
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if res.returncode == 0 and res.stdout.strip():
            return float(res.stdout.strip())
    except Exception as e:
        logger.warning(f"Could not read video duration for {video_path}: {e}")
    return None

def ensure_min_video_duration(local_path: str, min_duration_sec: int = 5) -> str:
    duration = get_video_duration_seconds(local_path)
    if duration is None or duration >= min_duration_sec:
        return local_path
    base, ext = os.path.splitext(local_path)
    extended_path = f"{base}_min{min_duration_sec}{ext or '.mp4'}"
    if os.path.exists(extended_path):
        return extended_path
    logger.info(f"Extending short video {local_path} from {duration:.2f}s to {min_duration_sec}s for provider compatibility.")
    res = subprocess.run(
        [
            get_binary_path("ffmpeg"), "-y",
            "-stream_loop", "-1",
            "-i", local_path,
            "-t", str(min_duration_sec),
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "22",
            "-pix_fmt", "yuv420p",
            "-an",
            "-movflags", "+faststart",
            extended_path,
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )
    if res.returncode != 0 or not os.path.exists(extended_path):
        raise ValueError(f"Failed to extend short video for provider compatibility: {res.stderr[:500]}")
    return extended_path

def ensure_meta_compatible_video(local_path: str, min_duration_sec: int = 5) -> str:
    if f"_meta{min_duration_sec}" in os.path.basename(local_path):
        return local_path
    duration = get_video_duration_seconds(local_path) or min_duration_sec
    target_duration = max(float(duration), float(min_duration_sec))
    base, ext = os.path.splitext(local_path)
    meta_path = f"{base}_meta{min_duration_sec}{ext or '.mp4'}"
    if os.path.exists(meta_path):
        return meta_path
    logger.info(f"Transcoding {local_path} to Meta-compatible H.264/AAC MP4 at {target_duration:.2f}s.")
    res = subprocess.run(
        [
            get_binary_path("ffmpeg"), "-y",
            "-stream_loop", "-1",
            "-i", local_path,
            "-f", "lavfi",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-t", f"{target_duration:.3f}",
            "-shortest",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "22",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            meta_path,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if res.returncode != 0 or not os.path.exists(meta_path):
        raise ValueError(f"Failed to create Meta-compatible video: {res.stderr[:500]}")
    return meta_path

def ensure_vertical_video_variant(post: dict, min_duration_sec: int = 5) -> Optional[str]:
    if post.get("vertical_video_path"):
        vertical_path = resolve_local_video_path(post["vertical_video_path"])
        compatible = ensure_meta_compatible_video(vertical_path, min_duration_sec)
        if compatible != vertical_path:
            post["vertical_video_path"] = f"/static/assets/generated/{os.path.basename(compatible)}"
        return post.get("vertical_video_path")
    source_public = post.get("video_path")
    if not source_public:
        return None
    source_path = resolve_local_video_path(source_public)
    if not os.path.exists(source_path):
        raise ValueError(f"Source video file does not exist at {source_path}")
    base_name = os.path.splitext(os.path.basename(source_path))[0]
    vertical_name = f"{base_name}_vertical_9x16.mp4"
    vertical_path = os.path.join(GENERATED_DIR, vertical_name)
    if not os.path.exists(vertical_path):
        font_path = get_font_path()
        render_variant_with_fallback(source_path, vertical_path, 1080, 1920, "", font_path, 320)
    compatible = ensure_meta_compatible_video(vertical_path, min_duration_sec)
    if compatible != vertical_path:
        vertical_name = os.path.basename(compatible)
    post["vertical_video_path"] = f"/static/assets/generated/{vertical_name}"
    return post["vertical_video_path"]

def publish_instagram_post(post: dict, settings: dict):
    access_token = settings.get("instagram_access_token")
    ig_user_id = settings.get("instagram_business_account_id")
    if not access_token or not ig_user_id:
        raise ValueError("Missing Instagram credentials (access token / business account ID).")

    ensure_vertical_video_variant(post, min_duration_sec=5)
    video_url = get_public_video_url(post, settings, "instagram")
    create_url = f"https://graph.facebook.com/v21.0/{ig_user_id}/media"
    create_res = requests.post(create_url, data={
        "media_type": "REELS",
        "video_url": video_url,
        "caption": post.get("text", ""),
        "access_token": access_token
    })
    if create_res.status_code != 200:
        raise ValueError(f"Instagram media container creation failed: {create_res.text}")
    creation_id = create_res.json().get("id")

    # Poll container status until FINISHED (video must be downloaded and processed by Meta first)
    status_url = f"https://graph.facebook.com/v21.0/{creation_id}"
    for _ in range(30):
        time.sleep(5)
        status_res = requests.get(status_url, params={"fields": "status_code,status", "access_token": access_token})
        status_code = status_res.json().get("status_code")
        if status_code == "FINISHED":
            break
        if status_code == "ERROR":
            raise ValueError(f"Instagram media processing failed: {status_res.text}")
    else:
        raise ValueError("Instagram media processing timed out after 150 seconds.")

    publish_url = f"https://graph.facebook.com/v21.0/{ig_user_id}/media_publish"
    publish_res = requests.post(publish_url, data={"creation_id": creation_id, "access_token": access_token})
    if publish_res.status_code != 200:
        raise ValueError(f"Instagram publish failed: {publish_res.text}")
    return publish_res.json()

def get_tiktok_access_token(settings: dict) -> str:
    """TikTok access tokens expire in 24h. Rather than persist a refreshed token
    (which wouldn't stick on Railway — env vars always override settings.json on
    every load_settings() call), mint a fresh one from the long-lived refresh_token
    on every publish, mirroring get_youtube_access_token()'s approach."""
    if not all(settings.get(k) for k in ["tiktok_client_key", "tiktok_client_secret", "tiktok_refresh_token"]):
        raise ValueError("Missing TikTok credentials (client key / secret / refresh token).")
    res = requests.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": settings["tiktok_client_key"],
            "client_secret": settings["tiktok_client_secret"],
            "grant_type": "refresh_token",
            "refresh_token": settings["tiktok_refresh_token"],
        }
    )
    if res.status_code != 200:
        raise ValueError(f"Failed to refresh TikTok access token: {res.text}")
    access_token = res.json().get("access_token")
    if not access_token:
        raise ValueError(f"TikTok refresh response had no access_token: {res.text}")
    return access_token

def publish_tiktok_post(post: dict, settings: dict):
    access_token = get_tiktok_access_token(settings)

    video_url = get_public_video_url(post, settings, "tiktok")
    url = "https://open.tiktokapis.com/v2/post/publish/video/init/"
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    payload = {
        "post_info": {
            "title": (post.get("text") or "")[:2200],
            "privacy_level": "SELF_ONLY",
            "disable_duet": False,
            "disable_comment": False,
            "disable_stitch": False,
            "is_aigc": True  # discloses AI-generated content per TikTok's content policy
        },
        "source_info": {
            "source": "PULL_FROM_URL",
            "video_url": video_url
        }
    }
    res = requests.post(url, headers=headers, json=payload)
    if res.status_code != 200:
        raise ValueError(f"TikTok publish init failed: {res.text}")
    return res.json()

def publish_facebook_post(post: dict, settings: dict):
    access_token = settings.get("facebook_page_access_token")
    page_id = settings.get("facebook_page_id")
    if not access_token or not page_id:
        raise ValueError("Missing Facebook credentials (page access token / page ID).")

    video_path = post.get("vertical_video_path") or post.get("video_path")
    if not video_path:
        raise ValueError("This post has no video attached — Facebook requires a video.")
    abs_video_path = ensure_min_video_duration(resolve_local_video_path(video_path), 5)
    url = f"https://graph.facebook.com/v21.0/{page_id}/videos"
    with open(abs_video_path, "rb") as video_file:
        res = requests.post(
            url,
            data={
                "description": post.get("text", ""),
                "access_token": access_token
            },
            files={"source": (os.path.basename(abs_video_path), video_file, "video/mp4")},
            timeout=300,
        )
    if res.status_code != 200:
        raise ValueError(f"Facebook video publish failed: {res.text}")
    return res.json()

def publish_threads_post(post: dict, settings: dict):
    access_token = settings.get("threads_access_token")
    threads_user_id = settings.get("threads_user_id")
    if not access_token or not threads_user_id:
        raise ValueError("Missing Threads credentials (access token / user ID).")

    video_url = get_public_video_url(post, settings)
    create_url = f"https://graph.threads.net/v1.0/{threads_user_id}/threads"
    create_res = requests.post(create_url, data={
        "media_type": "VIDEO",
        "video_url": video_url,
        "text": post.get("text", ""),
        "access_token": access_token
    })
    if create_res.status_code != 200:
        raise ValueError(f"Threads media container creation failed: {create_res.text}")
    creation_id = create_res.json().get("id")

    # Poll container status until FINISHED
    status_url = f"https://graph.threads.net/v1.0/{creation_id}"
    for _ in range(30):
        time.sleep(5)
        status_res = requests.get(status_url, params={"fields": "status", "access_token": access_token})
        status = status_res.json().get("status")
        if status == "FINISHED":
            break
        if status == "ERROR":
            raise ValueError(f"Threads media processing failed: {status_res.text}")
    else:
        raise ValueError("Threads media processing timed out after 150 seconds.")

    publish_url = f"https://graph.threads.net/v1.0/{threads_user_id}/threads_publish"
    publish_res = requests.post(publish_url, data={"creation_id": creation_id, "access_token": access_token})
    if publish_res.status_code != 200:
        raise ValueError(f"Threads publish failed: {publish_res.text}")
    return publish_res.json()

def get_youtube_access_token(settings: dict) -> str:
    res = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": settings.get("youtube_client_id"),
        "client_secret": settings.get("youtube_client_secret"),
        "refresh_token": settings.get("youtube_refresh_token"),
        "grant_type": "refresh_token"
    })
    if res.status_code != 200:
        raise ValueError(f"Failed to refresh YouTube access token: {res.text}")
    return res.json()["access_token"]

def publish_youtube_short(post: dict, settings: dict):
    if not all(settings.get(k) for k in ["youtube_client_id", "youtube_client_secret", "youtube_refresh_token"]):
        raise ValueError("Missing YouTube credentials (client ID / secret / refresh token).")

    video_path = post.get("vertical_video_path") or post.get("video_path")
    if not video_path:
        raise ValueError("This post has no video attached — YouTube requires a video.")
    abs_video_path = resolve_local_video_path(video_path)
    if not os.path.exists(abs_video_path):
        raise ValueError(f"Video file does not exist at {abs_video_path}")

    access_token = get_youtube_access_token(settings)
    title = (post.get("campaign_title") or (post.get("text") or "")[:80] or "6Frame Studio")[:100]
    metadata = {
        "snippet": {
            "title": title,
            "description": post.get("text", ""),
            "categoryId": "1"
        },
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}
    }

    init_res = requests.post(
        "https://www.googleapis.com/upload/youtube/v3/videos",
        params={"uploadType": "resumable", "part": "snippet,status"},
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json=metadata
    )
    if init_res.status_code != 200:
        raise ValueError(f"YouTube upload session init failed: {init_res.text}")
    upload_url = init_res.headers.get("Location")
    if not upload_url:
        raise ValueError("YouTube did not return a resumable upload URL.")

    with open(abs_video_path, "rb") as f:
        video_data = f.read()
    upload_res = requests.put(upload_url, headers={"Content-Type": "video/mp4"}, data=video_data)
    if upload_res.status_code not in [200, 201]:
        raise ValueError(f"YouTube video upload failed: {upload_res.text}")
    return upload_res.json()

POSTPROXY_API_BASE = "https://api.postproxy.dev/api"
POSTPROXY_STATE_FILE = os.path.join(STATE_DIR, "postproxy_state.json")
POSTPROXY_DASHBOARD_URL = "https://app.postproxy.dev"
TWITTER_CHAR_LIMIT = 280
POSTPROXY_SUPPORTED_PLATFORMS = {
    "twitter", "linkedin", "instagram", "facebook", "tiktok", "youtube",
    "threads", "pinterest", "bluesky", "google_business", "telegram",
}
POSTPROXY_CONNECTABLE_PLATFORMS = [
    "instagram", "tiktok", "linkedin", "twitter", "youtube", "facebook",
    "threads", "pinterest", "bluesky",
]
# Platforms that 422 unless a placement id is sent with the post.
PLACEMENT_PARAM_BY_PLATFORM = {
    "facebook": "page_id",
    "google_business": "location_id",
    "pinterest": "board_id",
    "telegram": "chat_id",
    "linkedin": "organization_id",
}

def postproxy_headers(settings: dict) -> dict:
    api_key = settings.get("postproxy_api_key") or os.environ.get("POSTPROXY_API_KEY")
    if not api_key:
        raise ValueError("Missing PostProxy API key.")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

def postproxy_error_snippet(res) -> str:
    text = res.text or ""
    lowered = text.lower()
    if "<html" in lowered or text.lstrip().startswith("<!"):
        return f"HTML error page (HTTP {res.status_code})"
    return text[:500]

def postproxy_parse(res, method: str, path: str) -> dict:
    if res.status_code >= 400:
        raise ValueError(f"PostProxy {method} {path} failed HTTP {res.status_code}: {postproxy_error_snippet(res)}")
    if not res.content:
        return {}
    try:
        return res.json()
    except ValueError:
        raise ValueError(f"PostProxy {method} {path} returned non-JSON ({postproxy_error_snippet(res)})")

def postproxy_get(settings: dict, path: str, timeout: int = 60) -> dict:
    res = requests.get(f"{POSTPROXY_API_BASE}{path}", headers=postproxy_headers(settings), timeout=timeout)
    return postproxy_parse(res, "GET", path)

def postproxy_post(settings: dict, path: str, payload: dict, timeout: int = 120) -> dict:
    res = requests.post(f"{POSTPROXY_API_BASE}{path}", headers=postproxy_headers(settings), json=payload, timeout=timeout)
    return postproxy_parse(res, "POST", path)

def postproxy_profiles(settings: dict, profile_group_id: str = "") -> List[dict]:
    path = "/profiles"
    group_id = profile_group_id or settings.get("postproxy_profile_group_id") or os.environ.get("POSTPROXY_PROFILE_GROUP_ID") or ""
    if group_id:
        path = f"/profiles?profile_group_id={group_id}"
    return postproxy_get(settings, path).get("data", [])

def postproxy_placements(settings: dict, profile_id: str) -> List[dict]:
    if not profile_id:
        return []
    payload = postproxy_get(settings, f"/profiles/{profile_id}/placements")
    if isinstance(payload, list):
        return payload
    return payload.get("data") or payload.get("placements") or []

def postproxy_profile_groups(settings: dict) -> List[dict]:
    return postproxy_get(settings, "/profile_groups").get("data", [])

def resolve_postproxy_profile_group_id(settings: dict) -> str:
    configured = settings.get("postproxy_profile_group_id") or os.environ.get("POSTPROXY_PROFILE_GROUP_ID")
    if configured:
        return configured
    groups = postproxy_profile_groups(settings)
    if not groups:
        raise ValueError("No PostProxy profile groups found.")
    return groups[0]["id"]

def platform_to_postproxy(platform: str) -> str:
    mapping = {
        "x": "twitter",
        "twitter": "twitter",
        "linkedin": "linkedin",
        "instagram": "instagram",
        "facebook": "facebook",
        "tiktok": "tiktok",
        "youtube": "youtube",
        "threads": "threads",
        "pinterest": "pinterest",
        "bluesky": "bluesky",
        "google_business": "google_business",
        "telegram": "telegram",
    }
    return mapping.get((platform or "").lower(), (platform or "").lower())

def load_postproxy_state() -> dict:
    return read_json_file(POSTPROXY_STATE_FILE, {
        "synced_at": None,
        "profile_group_id": "",
        "groups": [],
        "profiles": [],
        "placements": {},
        "auth_error": None,
        "key_valid": None,
    })

def save_postproxy_state(state: dict):
    try:
        write_json_file(POSTPROXY_STATE_FILE, state)
    except Exception as e:
        logger.error(f"Error saving PostProxy state: {e}")

def pick_placement_id(placements: List[dict]) -> Optional[str]:
    for item in placements or []:
        if not isinstance(item, dict):
            continue
        pid = item.get("id")
        if pid not in (None, ""):
            return str(pid)
    return None

def pick_placement_name(placements: List[dict], placement_id: Optional[str]) -> Optional[str]:
    if not placement_id:
        return None
    for item in placements or []:
        if isinstance(item, dict) and str(item.get("id") or "") == str(placement_id):
            return item.get("name")
    return None

def postproxy_key_configured(settings: Optional[dict] = None) -> bool:
    settings = settings or load_settings()
    return bool(settings.get("postproxy_api_key") or os.environ.get("POSTPROXY_API_KEY"))

def postproxy_is_enabled(settings: Optional[dict] = None) -> bool:
    settings = settings or load_settings()
    return bool(settings.get("postproxy_enabled") and postproxy_key_configured(settings))

def sanitize_postproxy_error(message: str) -> str:
    text = str(message or "")
    lowered = text.lower()
    if any(token in lowered for token in ("401", "403", "unauthorized", "invalid api", "invalid key", "forbidden")):
        return "PostProxy API key was rejected. Check POSTPROXY_API_KEY on Railway."
    return text[:400]

def sync_postproxy_account(settings: Optional[dict] = None) -> dict:
    settings = settings or load_settings()
    if not postproxy_key_configured(settings):
        state = load_postproxy_state()
        state.update({
            "synced_at": datetime.now().isoformat(),
            "auth_error": "PostProxy API key is not configured.",
            "key_valid": False,
        })
        save_postproxy_state(state)
        return state
    try:
        groups = postproxy_profile_groups(settings)
        group_id = settings.get("postproxy_profile_group_id") or os.environ.get("POSTPROXY_PROFILE_GROUP_ID") or (groups[0]["id"] if groups else "")
        profiles = postproxy_profiles(settings, group_id)
        placements_by_platform: Dict[str, dict] = {}
        for profile in profiles:
            platform = platform_to_postproxy(profile.get("platform") or "")
            profile_id = profile.get("id")
            if not platform or not profile_id:
                continue
            placements: List[dict] = []
            placement_error = None
            if platform in PLACEMENT_PARAM_BY_PLATFORM or platform in ("instagram", "threads"):
                try:
                    placements = postproxy_placements(settings, profile_id)
                except Exception as e:
                    placement_error = sanitize_postproxy_error(str(e))
                    logger.warning(f"PostProxy placements for {platform} ({profile_id}) failed: {placement_error}")
            param = PLACEMENT_PARAM_BY_PLATFORM.get(platform)
            placement_id = pick_placement_id(placements)
            if not placement_id and platform == "facebook":
                placement_id = settings.get("facebook_page_id") or None
            placements_by_platform[platform] = {
                "profile_id": profile_id,
                "profile_name": profile.get("name"),
                "profile_status": profile.get("status"),
                "param": param,
                "placement_id": placement_id,
                "placement_name": pick_placement_name(placements, placement_id),
                "placements": placements,
                "error": placement_error,
            }
        state = {
            "synced_at": datetime.now().isoformat(),
            "profile_group_id": group_id,
            "groups": [{"id": g.get("id"), "name": g.get("name"), "profiles_count": g.get("profiles_count")} for g in groups],
            "profiles": profiles,
            "placements": placements_by_platform,
            "auth_error": None,
            "key_valid": True,
        }
        save_postproxy_state(state)
        return state
    except Exception as e:
        message = sanitize_postproxy_error(str(e))
        state = load_postproxy_state()
        state.update({
            "synced_at": datetime.now().isoformat(),
            "auth_error": message,
            "key_valid": False if "API key" in message or "rejected" in message.lower() else state.get("key_valid"),
        })
        save_postproxy_state(state)
        raise ValueError(message) from e

def cached_or_live_profiles(settings: dict) -> List[dict]:
    state = load_postproxy_state()
    if state.get("profiles"):
        return state["profiles"]
    try:
        return sync_postproxy_account(settings).get("profiles") or []
    except Exception:
        return []

def postproxy_profile_for_platform(settings: dict, platform: str, require_active: bool = True) -> Optional[dict]:
    mapped = platform_to_postproxy(platform)
    candidates = [p for p in cached_or_live_profiles(settings) if platform_to_postproxy(p.get("platform") or "") == mapped]
    if require_active:
        active = [p for p in candidates if p.get("status") == "active"]
        if active:
            return active[0]
        return None
    return candidates[0] if candidates else None

def first_postproxy_placement_id(settings: dict, platform: str) -> Optional[str]:
    mapped = platform_to_postproxy(platform)
    state = load_postproxy_state()
    cached = (state.get("placements") or {}).get(mapped) or {}
    if cached.get("placement_id"):
        return str(cached["placement_id"])
    if mapped == "facebook" and settings.get("facebook_page_id"):
        return str(settings["facebook_page_id"])
    profile = postproxy_profile_for_platform(settings, mapped, require_active=False)
    if not profile:
        return None
    try:
        placements = postproxy_placements(settings, profile.get("id", ""))
    except Exception as e:
        logger.warning(f"Could not fetch PostProxy placements for {mapped}: {e}")
        return settings.get("facebook_page_id") if mapped == "facebook" else None
    placement_id = pick_placement_id(placements)
    if placement_id:
        placements_state = state.get("placements") or {}
        placements_state[mapped] = {
            "profile_id": profile.get("id"),
            "profile_name": profile.get("name"),
            "profile_status": profile.get("status"),
            "param": PLACEMENT_PARAM_BY_PLATFORM.get(mapped),
            "placement_id": placement_id,
            "placement_name": pick_placement_name(placements, placement_id),
            "placements": placements,
        }
        state["placements"] = placements_state
        save_postproxy_state(state)
    return placement_id

def resolve_postproxy_platform_params(settings: dict, platforms: List[str]) -> Dict[str, dict]:
    params: Dict[str, dict] = {}
    for platform in platforms:
        mapped = platform_to_postproxy(platform)
        param_name = PLACEMENT_PARAM_BY_PLATFORM.get(mapped)
        if not param_name:
            continue
        # LinkedIn may omit organization_id to post as the personal profile.
        placement_id = first_postproxy_placement_id(settings, mapped)
        if placement_id:
            params[mapped] = {param_name: placement_id}
    return params

def truncate_twitter_text(text: str, limit: int = TWITTER_CHAR_LIMIT) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    ellipsis = "…"
    budget = max(1, limit - len(ellipsis))
    cut = text[:budget]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    cut = cut.rstrip(" ,;:-")
    return (cut or text[:budget]) + ellipsis

def split_twitter_caption(text: str, existing_thread: Optional[List[str]] = None, limit: int = TWITTER_CHAR_LIMIT) -> List[str]:
    parts: List[str] = []
    if existing_thread:
        parts = [str(item).strip() for item in existing_thread if str(item).strip()]
    if not parts:
        remaining = (text or "").strip()
        if not remaining:
            return [""]
        chunks: List[str] = []
        while remaining:
            if len(remaining) <= limit:
                chunks.append(remaining)
                break
            window = remaining[:limit]
            split_at = max(
                window.rfind("\n\n"),
                window.rfind("\n"),
                window.rfind(". "),
                window.rfind("! "),
                window.rfind("? "),
                window.rfind(" "),
            )
            if split_at < int(limit * 0.45):
                piece = truncate_twitter_text(remaining, limit)
                consumed = max(1, min(len(remaining), max(len(piece) - 1, 1)))
                chunks.append(piece)
                remaining = remaining[consumed:].lstrip()
                continue
            if split_at < len(window) and window[split_at] in ".!?":
                split_at += 1
            chunks.append(remaining[:split_at].strip())
            remaining = remaining[split_at:].lstrip()
        parts = [chunk for chunk in chunks if chunk]
    return [truncate_twitter_text(part, limit) for part in parts] or [""]

def postproxy_media_url(post: dict, settings: dict, platforms: List[str]) -> Optional[str]:
    if not post.get("video_path"):
        return None
    if any(p in platforms for p in ("instagram", "tiktok", "youtube")):
        ensure_vertical_video_variant(post)
    platform_key = "instagram" if "instagram" in platforms else ("tiktok" if "tiktok" in platforms else "")
    return get_public_video_url(post, settings, platform_key)

def estimated_postproxy_publish_units(post: dict, platforms: List[str]) -> int:
    mapped_platforms = [platform_to_postproxy(p) for p in platforms if platform_to_postproxy(p) in POSTPROXY_SUPPORTED_PLATFORMS]
    if not mapped_platforms:
        return 0
    units = 1
    twitter_parts = split_twitter_caption(post.get("text", ""), post.get("thread"))
    if "twitter" in mapped_platforms and (len(twitter_parts) > 1 or len((post.get("text") or "")) > TWITTER_CHAR_LIMIT):
        units += 1
    if post.get("video_path") and "google_business" in mapped_platforms and len(mapped_platforms) > 1:
        units += 1
    return units

def postproxy_publish_units_used_today(posts: Optional[List[dict]] = None, now: Optional[datetime] = None) -> int:
    posts = posts if posts is not None else load_scheduled_posts()
    today = (now or datetime.now()).date()
    used = 0
    for post in posts:
        posted_at = post.get("posted_at")
        if not posted_at:
            continue
        try:
            post_date = datetime.fromisoformat(posted_at).date()
        except ValueError:
            continue
        if post_date != today:
            continue
        post_ids = post.get("postproxy_post_ids") or ([post.get("postproxy_post_id")] if post.get("postproxy_post_id") else [])
        used += len([pid for pid in post_ids if pid])
    return used

def enforce_postproxy_daily_limit(post: dict, settings: dict, platforms: List[str]) -> Optional[dict]:
    if not postproxy_is_enabled(settings):
        return None
    try:
        limit = int(settings.get("postproxy_daily_publish_limit", 2))
    except (TypeError, ValueError):
        limit = 2
    limit = max(1, limit)
    requested_units = estimated_postproxy_publish_units(post, platforms)
    if requested_units <= 0:
        return None
    used_units = postproxy_publish_units_used_today()
    if used_units + requested_units <= limit:
        return None
    message = (
        f"PostProxy daily safety limit reached: {used_units}/{limit} post records already used today; "
        f"this publish needs {requested_units}. Approve this draft manually or raise the daily limit in Settings."
    )
    logger.warning(message)
    return {
        "successes": [],
        "errors": [message],
        "tweet_id": None,
        "tweet_ids": None,
        "blocked_by_daily_limit": True,
        "postproxy_daily_used": used_units,
        "postproxy_daily_limit": limit,
        "postproxy_requested_units": requested_units,
    }

def build_social_channel_status(settings: Optional[dict] = None, state: Optional[dict] = None) -> List[dict]:
    settings = settings or load_settings()
    state = state or load_postproxy_state()
    profiles_by_platform: Dict[str, dict] = {}
    for profile in state.get("profiles") or []:
        platform = platform_to_postproxy(profile.get("platform") or "")
        if not platform:
            continue
        current = profiles_by_platform.get(platform)
        if not current or (profile.get("status") == "active" and current.get("status") != "active"):
            profiles_by_platform[platform] = profile
    labels = [
        ("instagram", "Instagram"),
        ("tiktok", "TikTok"),
        ("linkedin", "LinkedIn"),
        ("twitter", "Twitter / X"),
        ("youtube", "YouTube"),
        ("facebook", "Facebook"),
        ("threads", "Threads"),
        ("pinterest", "Pinterest"),
        ("bluesky", "Bluesky"),
    ]
    channels = []
    seen = set()
    for key, label in labels:
        seen.add(key)
        profile = profiles_by_platform.get(key)
        placement = (state.get("placements") or {}).get(key) or {}
        if profile:
            raw_status = (profile.get("status") or "active").lower()
            live = raw_status == "active"
            badge = "LIVE" if live else ("EXPIRED" if raw_status == "expired" else "INACTIVE")
        else:
            live = False
            badge = "INACTIVE"
        channels.append({
            "platform": key,
            "label": label,
            "status": badge,
            "live": live,
            "connected": bool(profile),
            "source": "postproxy" if profile else None,
            "profile_id": (profile or {}).get("id"),
            "profile_name": (profile or {}).get("name"),
            "profile_status": (profile or {}).get("status"),
            "placement_id": placement.get("placement_id"),
            "placement_param": placement.get("param"),
            "placement_name": placement.get("placement_name"),
            "expires_at": (profile or {}).get("expires_at"),
        })
    for platform, profile in profiles_by_platform.items():
        if platform in seen:
            continue
        placement = (state.get("placements") or {}).get(platform) or {}
        raw_status = (profile.get("status") or "active").lower()
        live = raw_status == "active"
        badge = "LIVE" if live else ("EXPIRED" if raw_status == "expired" else "INACTIVE")
        channels.append({
            "platform": platform,
            "label": platform.replace("_", " ").title(),
            "status": badge,
            "live": live,
            "connected": True,
            "source": "postproxy",
            "profile_id": profile.get("id"),
            "profile_name": profile.get("name"),
            "profile_status": profile.get("status"),
            "placement_id": placement.get("placement_id"),
            "placement_param": placement.get("param"),
            "placement_name": placement.get("placement_name"),
            "expires_at": profile.get("expires_at"),
        })
    return channels


def sanitize_postproxy_profile(profile: dict, placements: Optional[List[dict]] = None) -> dict:
    return {
        "id": profile.get("id") or "",
        "name": profile.get("name") or "",
        "platform": platform_to_postproxy(profile.get("platform") or ""),
        "status": profile.get("status") or "",
        "placements": [
            {"id": item.get("id"), "name": item.get("name") or "", "metadata": item.get("metadata") or {}}
            for item in (placements or [])
            if isinstance(item, dict)
        ],
    }

def build_postproxy_status_payload(settings: Optional[dict] = None, live: bool = False) -> dict:
    settings = settings or load_settings()
    error = None
    state = load_postproxy_state()
    if live and postproxy_key_configured(settings):
        try:
            state = sync_postproxy_account(settings)
        except Exception as e:
            error = sanitize_postproxy_error(str(e))
            state = load_postproxy_state()
    elif not state.get("profiles") and postproxy_key_configured(settings):
        try:
            state = sync_postproxy_account(settings)
        except Exception as e:
            error = sanitize_postproxy_error(str(e))
            state = load_postproxy_state()
    socials = build_social_channel_status(settings, state)
    placements_by_platform = state.get("placements") or {}
    profiles = []
    for profile in state.get("profiles") or []:
        platform = platform_to_postproxy(profile.get("platform") or "")
        placement_info = placements_by_platform.get(platform) or {}
        profiles.append(sanitize_postproxy_profile(profile, placement_info.get("placements") or []))
    channels = {item["platform"]: item for item in socials if item.get("platform")}
    error = error or state.get("auth_error")
    return {
        "status": "SUCCESS" if not error else "ERROR",
        "configured": postproxy_key_configured(settings),
        "enabled": bool(settings.get("postproxy_enabled")),
        "group_configured": bool(state.get("profile_group_id") or settings.get("postproxy_profile_group_id") or os.environ.get("POSTPROXY_PROFILE_GROUP_ID")),
        "profile_group_id": state.get("profile_group_id") or settings.get("postproxy_profile_group_id") or os.environ.get("POSTPROXY_PROFILE_GROUP_ID") or "",
        "key_valid": state.get("key_valid"),
        "profiles": profiles,
        "channels": channels,
        "socials": socials,
        "facebook_page_id": (placements_by_platform.get("facebook") or {}).get("placement_id"),
        "linkedin_organization_id": (placements_by_platform.get("linkedin") or {}).get("placement_id"),
        "has_facebook_page_id": bool((placements_by_platform.get("facebook") or {}).get("placement_id")),
        "has_linkedin_organization_id": bool((placements_by_platform.get("linkedin") or {}).get("placement_id")),
        "error": error,
        "synced_at": state.get("synced_at"),
    }

def build_capabilities_payload(settings: Optional[dict] = None, sync: bool = False) -> dict:
    settings = settings or load_settings()
    state = load_postproxy_state()
    error = None
    if sync and postproxy_key_configured(settings):
        try:
            state = sync_postproxy_account(settings)
        except Exception as e:
            error = sanitize_postproxy_error(str(e))
            state = load_postproxy_state()
    elif not state.get("profiles") and postproxy_key_configured(settings):
        try:
            state = sync_postproxy_account(settings)
        except Exception as e:
            error = sanitize_postproxy_error(str(e))
            state = load_postproxy_state()
    channels = build_social_channel_status(settings, state)
    live_platforms = [c["platform"] for c in channels if c.get("connected")]
    return {
        "status": "SUCCESS",
        "engines": {
            "gemini": bool(settings.get("gemini_api_key")),
            "runway": bool(settings.get("runway_api_key")),
            "fal": bool(settings.get("fal_api_key")),
        },
        "socials": channels,
        "postproxy": {
            "enabled": bool(settings.get("postproxy_enabled")),
            "configured": postproxy_key_configured(settings),
            "key_valid": state.get("key_valid"),
            "profile_group_id": state.get("profile_group_id") or settings.get("postproxy_profile_group_id") or "",
            "profiles_count": len(state.get("profiles") or []),
            "platforms": live_platforms,
            "synced_at": state.get("synced_at"),
            "error": error or state.get("auth_error"),
            "connectable": POSTPROXY_CONNECTABLE_PLATFORMS,
        },
    }

def publish_via_postproxy(post: dict, settings: dict, platforms: List[str]) -> dict:
    mapped_platforms = [platform_to_postproxy(p) for p in platforms if platform_to_postproxy(p) in POSTPROXY_SUPPORTED_PLATFORMS]
    if not mapped_platforms:
        return {"successes": [], "errors": ["PostProxy: no supported platforms selected."]}
    media_url = postproxy_media_url(post, settings, mapped_platforms)
    twitter_parts = split_twitter_caption(post.get("text", ""), post.get("thread"))
    platform_params = resolve_postproxy_platform_params(settings, mapped_platforms)
    skipped_errors: List[str] = []
    publishable: List[str] = []
    for platform in mapped_platforms:
        if platform == "facebook" and not (platform_params.get("facebook") or {}).get("page_id"):
            skipped_errors.append("facebook: Missing required page_id. Refresh PostProxy placements or reconnect Facebook.")
            continue
        if platform == "google_business" and not (platform_params.get("google_business") or {}).get("location_id"):
            skipped_errors.append("google_business: Missing location_id from PostProxy placements.")
            continue
        publishable.append(platform)
    if not publishable:
        return {"successes": [], "errors": skipped_errors or ["PostProxy: no publishable platforms."]}

    group_id = settings.get("postproxy_profile_group_id") or os.environ.get("POSTPROXY_PROFILE_GROUP_ID") or (load_postproxy_state().get("profile_group_id") or "")

    def publish_batch(batch_platforms: List[str], include_media: bool, body: str, thread_parts: Optional[List[str]] = None) -> dict:
        params = {k: v for k, v in platform_params.items() if k in batch_platforms}
        if "instagram" in batch_platforms and include_media:
            params.setdefault("instagram", {})
            params["instagram"]["format"] = "reel"
        if "youtube" in batch_platforms:
            params.setdefault("youtube", {})
            params["youtube"].update({
                "title": (post.get("campaign_title") or (body or "6Frame Studio")[:80])[:100],
                "privacy_status": "public",
                "made_for_kids": False,
            })
        payload = {
            "post": {
                "body": body,
                "draft": False,
            },
            "profiles": batch_platforms,
        }
        if group_id:
            payload["profile_group_id"] = group_id
        if include_media and media_url:
            payload["media"] = [media_url]
        if thread_parts and len(thread_parts) > 1 and set(batch_platforms).issubset({"twitter", "threads"}):
            payload["post"]["body"] = thread_parts[0]
            payload["thread"] = [{"body": part} for part in thread_parts[1:]]
        if params:
            payload["platforms"] = params
        return postproxy_post(settings, "/posts", payload, timeout=180)

    twitter_needs_own_batch = "twitter" in publishable and (
        len(twitter_parts) > 1 or len((post.get("text") or "")) > TWITTER_CHAR_LIMIT
    )
    remaining = list(publishable)
    batches = []
    if twitter_needs_own_batch:
        remaining = [p for p in remaining if p != "twitter"]
        batches.append((["twitter"], bool(media_url), twitter_parts[0], twitter_parts))
    if media_url and "google_business" in remaining:
        media_platforms = [p for p in remaining if p != "google_business"]
        if media_platforms:
            batches.append((media_platforms, True, post.get("text", ""), None))
        batches.append((["google_business"], False, post.get("text", ""), None))
    elif remaining:
        batches.append((remaining, bool(media_url), post.get("text", ""), None))

    results = [publish_batch(batch_platforms, include_media, body, thread_parts) for batch_platforms, include_media, body, thread_parts in batches]
    platform_results = []
    for result in results:
        platform_results.extend(result.get("platforms") or [])
    successes = []
    errors = list(skipped_errors)
    for item in platform_results:
        platform = item.get("platform") or "unknown"
        status = item.get("status")
        if status in ("published", "processing", "processed", "scheduled", "pending"):
            successes.append(platform)
        else:
            errors.append(f"{platform}: {item.get('error') or status or 'unknown PostProxy error'}")
    if not platform_results and any(result.get("id") for result in results):
        successes = publishable
    post_ids = [result.get("id") for result in results if result.get("id")]
    post["postproxy_post_id"] = post_ids[0] if post_ids else None
    post["postproxy_post_ids"] = post_ids
    post["postproxy_result"] = results[0] if len(results) == 1 else {"posts": results, "platforms": platform_results}
    post["postproxy_placements"] = {k: v for k, v in platform_params.items() if k in publishable}
    return {
        "successes": successes,
        "errors": errors,
        "tweet_id": None,
        "tweet_ids": None,
        "postproxy_post_id": post.get("postproxy_post_id"),
        "postproxy_post_ids": post_ids,
        "postproxy_result": post.get("postproxy_result"),
    }

def postproxy_reply_to_comment(settings: dict, target: dict) -> dict:
    post_id = target.get("postproxy_post_id") or target.get("post_id")
    platform = target.get("platform", "")
    profile_id = target.get("postproxy_profile_id") or target.get("profile_id")
    parent_id = target.get("postproxy_comment_id") or target.get("source_comment_id")
    if not post_id or not parent_id:
        raise ValueError("PostProxy reply requires postproxy_post_id and source comment id.")
    if not profile_id:
        profile = postproxy_profile_for_platform(settings, platform)
        profile_id = profile.get("id") if profile else ""
    if not profile_id:
        raise ValueError(f"No active PostProxy profile found for {platform}.")
    return postproxy_post(
        settings,
        f"/posts/{post_id}/comments?profile_id={profile_id}",
        {"body": target["drafted_reply"], "parent_id": parent_id},
        timeout=60,
    )

class GenerateVideoRequest(BaseModel):
    prompt: str
    engine: str = "google_veo"
    duration: int = 8

@app.get("/api/auth/status")
def auth_status(request: Request):
    return {"auth_required": auth_enabled(), "authenticated": request_is_authenticated(request)}

@app.post("/api/auth/login")
def auth_login(req: LoginRequest, response: Response):
    expected = os.environ.get("ADMIN_PASSWORD", "")
    if not expected:
        return {"status": "SUCCESS", "auth_required": False}
    if not hmac.compare_digest(req.password, expected):
        raise HTTPException(status_code=401, detail="Invalid password.")
    secure_cookie = os.environ.get("COOKIE_SECURE", "true").lower() != "false"
    response.set_cookie(
        "admin_session",
        session_signature(),
        httponly=True,
        secure=secure_cookie,
        samesite="lax",
        max_age=60 * 60 * 24 * 14,
        path="/",
    )
    return {"status": "SUCCESS"}

SECRET_SETTING_KEYS = [
    "gemini_api_key", "twitter_consumer_key", "twitter_consumer_secret",
    "twitter_access_token", "twitter_access_token_secret", "linkedin_access_token", "runway_api_key", "fal_api_key",
    "meta_app_secret", "instagram_access_token", "facebook_page_access_token",
    "tiktok_client_secret", "tiktok_access_token", "tiktok_refresh_token",
    "youtube_client_secret", "youtube_refresh_token", "threads_access_token",
    "smtp_password", "resend_api_key", "postproxy_api_key"
]

@app.get("/api/settings")
def get_settings():
    settings = load_settings()
    # Mask API keys for safety
    masked_settings = settings.copy()
    for k in SECRET_SETTING_KEYS:
        val = masked_settings.get(k, "")
        if val:
            masked_settings[k] = val[:4] + "*" * (len(val) - 8) + val[-4:] if len(val) > 8 else "********"
    return masked_settings

@app.post("/api/settings")
def update_settings(data: SettingsSchema):
    current = load_settings()
    new_data = data.model_dump()

    # Re-apply original key if masked value was submitted
    for k in SECRET_SETTING_KEYS:
        if "*" in new_data[k]:
            new_data[k] = current.get(k, "")

    save_settings(new_data)
    return {"message": "Settings updated successfully."}

@app.get("/api/postproxy/profiles")
def get_postproxy_profiles():
    settings = load_settings()
    try:
        state = sync_postproxy_account(settings)
        return {
            "status": "SUCCESS",
            "enabled": bool(settings.get("postproxy_enabled")),
            "configured": postproxy_key_configured(settings),
            "key_valid": state.get("key_valid"),
            "profile_group_id": state.get("profile_group_id") or "",
            "groups": state.get("groups") or [],
            "profiles": state.get("profiles") or [],
            "placements": state.get("placements") or {},
            "socials": build_social_channel_status(settings, state),
            "synced_at": state.get("synced_at"),
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=sanitize_postproxy_error(str(e)))

@app.get("/api/postproxy/posts/{post_id}")
def get_postproxy_post_status(post_id: str):
    settings = load_settings()
    try:
        return {"status": "SUCCESS", "post": postproxy_get(settings, f"/posts/{post_id}", timeout=60)}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/postproxy/status")
def get_postproxy_status():
    return build_postproxy_status_payload(live=True)

@app.post("/api/postproxy/sync")
def sync_postproxy_profiles():
    settings = load_settings()
    try:
        state = sync_postproxy_account(settings)
        payload = build_postproxy_status_payload(settings, live=False)
        payload.update({
            "groups": state.get("groups") or [],
            "placements": state.get("placements") or {},
        })
        return payload
    except Exception as e:
        message = sanitize_postproxy_error(str(e))
        payload = build_postproxy_status_payload(settings, live=False)
        payload["status"] = "ERROR"
        payload["error"] = message
        return payload

@app.get("/api/capabilities")
def get_capabilities():
    return build_capabilities_payload(sync=True)

@app.get("/api/credentials")
def get_credentials():
    return build_capabilities_payload(sync=False)

@app.get("/oauth/postproxy/callback")
@app.get("/api/postproxy/callback")
def postproxy_oauth_callback(request: Request, failure: Optional[str] = None, error_code: Optional[str] = None):
    params = request.query_params
    failed = bool(failure or params.get("error") or params.get("error_description") or params.get("error_code"))
    status_val = (params.get("status") or params.get("postproxy") or "").lower()
    if status_val in ("failure", "failed", "error", "denied"):
        failed = True
    if failed:
        return RedirectResponse(url=f"/?postproxy=failure&error_code={error_code or params.get('error_code') or 'unknown'}")
    try:
        sync_postproxy_account(load_settings())
    except Exception as e:
        logger.warning(f"PostProxy OAuth callback sync failed: {sanitize_postproxy_error(str(e))}")
    return RedirectResponse(url="/?postproxy=ok")

@app.post("/api/postproxy/connect")
def create_postproxy_connection(req: PostProxyConnectRequest, request: Request):
    settings = load_settings()
    try:
        group_id = req.profile_group_id or settings.get("postproxy_profile_group_id") or resolve_postproxy_profile_group_id(settings)
        origin = str(request.base_url).rstrip("/")
        public_base = (settings.get("public_base_url") or os.environ.get("PUBLIC_BASE_URL") or origin).rstrip("/")
        redirect_url = req.redirect_url or f"{public_base}/api/postproxy/callback"
        payload = {
            "platform": platform_to_postproxy(req.platform),
            "redirect_url": redirect_url,
        }
        result = postproxy_post(settings, f"/profile_groups/{group_id}/initialize_connection", payload, timeout=60)
        return {"status": "SUCCESS", "profile_group_id": group_id, "reconnect": req.reconnect, **result}
    except Exception as e:
        message = sanitize_postproxy_error(str(e))
        if "already connected" in str(e).lower():
            return {
                "status": "SUCCESS",
                "already_connected": True,
                "reconnect": req.reconnect,
                "profile_group_id": req.profile_group_id or settings.get("postproxy_profile_group_id") or "",
                "dashboard_url": POSTPROXY_DASHBOARD_URL,
                "message": message,
            }
        raise HTTPException(status_code=400, detail=message)

@app.post("/api/postproxy/test-publish")
async def create_postproxy_test_publish(req: ControlledPostProxyPublishRequest):
    settings = load_settings()
    platforms = req.platforms or settings.get("autonomous_platforms") or ["twitter", "linkedin"]
    platforms = [platform_to_postproxy(p) for p in platforms if p]
    post = {
        "id": str(uuid.uuid4()),
        "platform": ",".join(platforms),
        "text": req.text or (
            "6Frame Studio live automation verification. "
            "This controlled post confirms the production PostProxy publishing pipeline is active."
        ),
        "thread": None,
        "scheduled_time": datetime.now().isoformat(),
        "campaign_title": req.campaign_title or "Controlled PostProxy Publish Verification",
        "video_path": req.video_path or "",
        "status": "PROCESSING",
        "created_at": datetime.now().isoformat(),
    }
    posts = load_scheduled_posts()
    posts.insert(0, post)
    save_scheduled_posts(posts)
    result = await publish_post_to_platforms(post, settings)
    if result.get("successes") and not result.get("errors"):
        post["status"] = "SUCCESS"
        post["error_message"] = None
    elif result.get("successes"):
        post["status"] = "PARTIAL_SUCCESS"
        post["error_message"] = f"Success: {', '.join(result.get('successes', []))}. Errors: {'; '.join(result.get('errors', []))}"
    else:
        post["status"] = "FAILED"
        post["error_message"] = "; ".join(result.get("errors", [])) or "PostProxy publish failed."
    post["posted_at"] = datetime.now().isoformat()
    apply_publish_result_to_post(post, result)
    posts = load_scheduled_posts()
    for idx, existing in enumerate(posts):
        if existing.get("id") == post["id"]:
            posts[idx] = post
            break
    save_scheduled_posts(posts)
    return {"status": post["status"], "post": post, "result": result}

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    file_id = str(uuid.uuid4())
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Unsupported upload type. Use MP4, MOV, M4V, or WEBM.")
    filename = f"{file_id}{ext}"
    dest_path = os.path.join(UPLOAD_DIR, filename)
    written = 0
    with open(dest_path, "wb") as buffer:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > MAX_UPLOAD_BYTES:
                try:
                    os.remove(dest_path)
                except OSError:
                    pass
                raise HTTPException(status_code=413, detail=f"Upload exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)}MB limit.")
            buffer.write(chunk)
        
    return {"video_path": dest_path, "filename": file.filename}

@app.post("/api/analyze")
def analyze_video(req: AnalyzeRequest, background_tasks: BackgroundTasks):
    try:
        source_path = resolve_local_video_path(req.video_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not os.path.isfile(source_path):
        raise HTTPException(status_code=404, detail="Uploaded video file was not found.")

    job_id = str(uuid.uuid4())
    settings = load_settings()
    
    # Initialize status
    update_job_status(job_id, "PENDING", 0, "Job enqueued in background...")
    
    # Trigger orchestrator pipeline in FastAPI background tasks
    background_tasks.add_task(
        run_multi_agent_pipeline,
        job_id=job_id,
        video_path=source_path,
        website_url=req.website_url,
        settings=settings
    )
    
    return {"job_id": job_id}

@app.get("/api/status/{job_id}")
def get_status(job_id: str):
    return get_job_status(job_id)

@app.get("/api/viral-templates")
def get_viral_templates():
    return {"templates": list_viral_templates()}

def persist_trend_scan_result(result: Any):
    if not isinstance(result, dict):
        return
    trends = result.get("trends")
    if not isinstance(trends, list):
        return
    state = load_growth_os()
    state["last_trend_scan"] = trends[:25]
    state["last_trend_scan_at"] = datetime.now().isoformat()
    save_growth_os(state)

def run_template_render_job(job_id: str, req: ApplyTemplateRequest, settings: dict):
    try:
        update_job_status(job_id, "PROCESSING", 10, "Preparing viral template render...")
        source_path = resolve_local_video_path(req.video_path)
        if not os.path.exists(source_path):
            raise FileNotFoundError(f"Source video not found: {req.video_path}")

        template_id = req.template_id or settings.get("viral_template_style", "hook_burst")
        title = req.title or "6Frame Studio"
        subtitle = req.subtitle or "AI-native social video system"
        filename = f"{job_id}_viral_template.mp4"
        output_path = os.path.join(GENERATED_DIR, filename)
        work_root = os.path.join(UPLOAD_DIR, "template_renders")
        os.makedirs(work_root, exist_ok=True)

        update_job_status(job_id, "PROCESSING", 35, "Rendering template with HyperFrames...")
        render_info = render_template_video(
            source_video_path=source_path,
            output_path=output_path,
            template_id=template_id,
            title=title,
            subtitle=subtitle,
            work_root=work_root,
            quality=settings.get("viral_template_quality", "standard"),
        )
        public_path = f"/static/assets/generated/{filename}"
        update_job_status(
            job_id,
            "SUCCESS",
            100,
            "Template render complete.",
            {
                "video_path": public_path,
                "template_id": render_info["template_id"],
                "template_label": render_info["template_label"],
            },
        )
    except Exception as e:
        logger.exception("Template render failed")
        update_job_status(job_id, "FAILED", 0, f"Template render failed: {str(e)}")

@app.post("/api/apply-template")
def apply_template(req: ApplyTemplateRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    settings = load_settings()
    update_job_status(job_id, "PENDING", 0, "Template render queued...")
    background_tasks.add_task(run_template_render_job, job_id, req, settings)
    return {"job_id": job_id}

@app.get("/api/template-status/{job_id}")
def get_template_status(job_id: str):
    return get_job_status(job_id)

@app.post("/api/viral-search")
def start_viral_search(background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    settings = load_settings()
    
    # Initialize status
    update_job_status(job_id, "PENDING", 0, "Trend search job enqueued...")
    
    def run_and_persist_trends():
        run_live_trend_scanner(job_id=job_id, settings=settings)
        status = get_job_status(job_id)
        if status.get("status") == "SUCCESS":
            persist_trend_scan_result(status.get("result"))
    background_tasks.add_task(run_and_persist_trends)
    
    return {"job_id": job_id}

@app.get("/api/viral-status/{job_id}")
def get_viral_status(job_id: str):
    return get_job_status(job_id)

def ensure_video_under_limit(video_path: str, max_duration_sec: int = 90) -> str:
    """ Checks video duration and returns a trimmed path if it exceeds limit. """
    import subprocess
    import os
    abs_video_path = resolve_local_video_path(video_path)
    
    probe_cmd = [
        get_binary_path("ffprobe"),
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        abs_video_path
    ]
    try:
        res = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=5)
        if res.returncode == 0 and res.stdout.strip():
            duration = float(res.stdout.strip())
            logger.info(f"Checking video duration: {duration}s (max allowed: {max_duration_sec}s)")
            if duration <= max_duration_sec:
                return abs_video_path
            
            logger.info(f"Video is too long ({duration}s). Trimming to {max_duration_sec}s...")
            base, ext = os.path.splitext(abs_video_path)
            trimmed_path = f"{base}_trimmed{ext}"
            
            trim_cmd = [
                get_binary_path("ffmpeg"), "-y",
                "-ss", "00:00:00",
                "-i", abs_video_path,
                "-t", str(max_duration_sec),
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "22",
                "-c:a", "aac",
                trimmed_path
            ]
            trim_res = subprocess.run(trim_cmd, capture_output=True, text=True, timeout=180)
            if trim_res.returncode == 0 and os.path.exists(trimmed_path):
                logger.info(f"Trimmed video created: {trimmed_path}")
                return trimmed_path
    except Exception as e:
        logger.error(f"Error checking duration or trimming video: {e}")
    return abs_video_path

@app.post("/api/publish/twitter")
def publish_twitter(req: PublishTwitterRequest):
    settings = load_settings()
    
    if settings.get("mock_mode", True):
        logger.info("[MOCK] Publishing to Twitter/X...")
        return {"status": "SUCCESS", "message": "Successfully published (Mock Mode)!", "tweet_id": "mock_tweet_12345"}
        
    # Check credentials
    required_keys = ["twitter_consumer_key", "twitter_consumer_secret", "twitter_access_token", "twitter_access_token_secret"]
    if not all(settings.get(k) for k in required_keys):
         raise HTTPException(status_code=400, detail="Missing Twitter API credentials. Turn on Mock Mode in settings to test without keys.")
         
    try:
        client = tweepy.Client(
            consumer_key=settings["twitter_consumer_key"],
            consumer_secret=settings["twitter_consumer_secret"],
            access_token=settings["twitter_access_token"],
            access_token_secret=settings["twitter_access_token_secret"]
        )
        
        media_ids = None
        if req.video_path:
            # Auto-trim if video exceeds standard Twitter API limit (120s)
            verified_video = ensure_video_under_limit(req.video_path)
            media_id = upload_video_to_twitter(verified_video, settings)
            if media_id:
                media_ids = [media_id]
        
        if req.is_thread and req.thread:
            previous_tweet_id = None
            tweet_ids = []
            for idx, tweet in enumerate(req.thread):
                if idx == 0 and media_ids:
                    response = client.create_tweet(text=tweet, media_ids=media_ids)
                elif previous_tweet_id:
                    response = client.create_tweet(text=tweet, in_reply_to_tweet_id=previous_tweet_id)
                else:
                    response = client.create_tweet(text=tweet)
                previous_tweet_id = response.data["id"]
                tweet_ids.append(previous_tweet_id)
            return {"status": "SUCCESS", "message": f"Successfully published thread of {len(tweet_ids)} tweets!", "tweet_ids": tweet_ids}
        else:
            if not req.text:
                raise HTTPException(status_code=400, detail="Tweet text cannot be empty.")
            if media_ids:
                response = client.create_tweet(text=req.text, media_ids=media_ids)
            else:
                response = client.create_tweet(text=req.text)
            return {"status": "SUCCESS", "message": "Successfully published tweet!", "tweet_id": response.data["id"]}
            
    except Exception as e:
        logger.exception("Twitter posting failed")
        raise HTTPException(status_code=500, detail=f"Twitter API Error: {str(e)}")

@app.post("/api/publish/linkedin")
def publish_linkedin(req: PublishLinkedinRequest):
    settings = load_settings()
    
    if settings.get("mock_mode", True):
        logger.info("[MOCK] Publishing to LinkedIn...")
        return {"status": "SUCCESS", "message": "Successfully published (Mock Mode)!"}
        
    # Check credentials
    if not settings.get("linkedin_access_token") or not settings.get("linkedin_person_urn"):
        raise HTTPException(status_code=400, detail="Missing LinkedIn credentials. Turn on Mock Mode in settings to test without keys.")
        
    try:
        url = 'https://api.linkedin.com/v2/ugcPosts'
        headers = {
            'Authorization': f'Bearer {settings["linkedin_access_token"]}',
            'X-Restli-Protocol-Version': '2.0.0',
            'Content-Type': 'application/json'
        }
        
        person_urn = settings['linkedin_person_urn']
        author_urn = person_urn if person_urn.startswith("urn:li:") else f"urn:li:person:{person_urn}"

        asset_urn = None
        if req.video_path:
            verified_video = ensure_video_under_limit(req.video_path, max_duration_sec=90)
            asset_urn = upload_video_to_linkedin(verified_video, author_urn, settings)

        if asset_urn:
            payload = {
                "author": author_urn,
                "lifecycleState": "PUBLISHED",
                "specificContent": {
                    "com.linkedin.ugc.ShareContent": {
                        "shareCommentary": {
                            "text": req.text
                        },
                        "shareMediaCategory": "VIDEO",
                        "media": [
                            {
                                "status": "READY",
                                "media": asset_urn
                            }
                        ]
                    }
                },
                "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"}
            }
        else:
            payload = {
                "author": author_urn,
                "lifecycleState": "PUBLISHED",
                "specificContent": {
                    "com.linkedin.ugc.ShareContent": {
                        "shareCommentary": {
                            "text": req.text
                        },
                        "shareMediaCategory": "NONE"
                    }
                },
                "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"}
            }
        
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code == 201:
            return {"status": "SUCCESS", "message": "Successfully posted to LinkedIn!"}
        else:
            logger.error(f"LinkedIn API error: {response.text}")
            raise HTTPException(status_code=response.status_code, detail=f"LinkedIn API Error: {response.text}")
            
    except Exception as e:
        logger.exception("LinkedIn posting failed")
        raise HTTPException(status_code=500, detail=f"LinkedIn Error: {str(e)}")

@app.post("/api/generate-video")
def generate_video(req: GenerateVideoRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    settings = load_settings()
    
    # Initialize status
    update_job_status(job_id, "PENDING", 0, "Video generation job enqueued...")
    
    # Trigger background task
    background_tasks.add_task(
        run_video_generation,
        job_id=job_id,
        prompt=req.prompt,
        settings=settings,
        engine=req.engine,
        duration=normalize_video_duration(req.engine, req.duration)
    )
    
    return {"job_id": job_id}

class VideoVariantsRequest(BaseModel):
    video_path: str
    hook_text: str

@app.post("/api/generate-video-variants")
def generate_video_variants_endpoint(req: VideoVariantsRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    update_job_status(job_id, "PENDING", 0, "Platform variants job enqueued...")
    background_tasks.add_task(
        generate_video_variants,
        job_id=job_id,
        video_path=req.video_path,
        hook_text=req.hook_text
    )
    return {"job_id": job_id}

@app.get("/api/video-variants-status/{job_id}")
def get_video_variants_status(job_id: str):
    return get_job_status(job_id)

class ViralityScoreRequest(BaseModel):
    post_text: str
    video_prompt: str
    platform: str = "Twitter/X"

@app.post("/api/virality-score")
def virality_score_endpoint(req: ViralityScoreRequest):
    settings = load_settings()
    try:
        result = generate_virality_score(req.post_text, req.video_prompt, req.platform, settings)
        return {"status": "SUCCESS", "data": result.model_dump()}
    except ValueError as val_err:
        raise HTTPException(status_code=400, detail=str(val_err))
    except Exception as e:
        logger.exception("Failed to generate virality score")
        raise HTTPException(status_code=500, detail=f"Failed to generate virality score: {str(e)}")


class OriginalVideoDownloadError(Exception):
    """Raised when the exact source video cannot be downloaded or trimmed."""


def _is_placeholder_video_url(target_url: str) -> bool:
    markers = (
        "status/178543210987",
        "status/12345",
        "DigitalDreams",
        "AIVoyager",
        "ChronoDrifter",
        "abcdef",
        "C9xabcd",
        "7385512345",
        "Luma",
        "examplecyber",
        "example",
        "dQw4w9WgXcQ",
        "results?search_query=",
    )
    return any(marker in target_url for marker in markers)


def download_and_trim_original_video(
    url: str,
    title: Optional[str] = None,
    max_duration_sec: int = 60,
    allow_fallback: bool = False,
    timeout_sec: int = 180,
) -> str:
    """Download a source URL into GENERATED_DIR and trim it under max_duration_sec.

    Autopilot awaits this helper (via an executor). It must not fire-and-forget
    the /api/load-original-video background job.
    """
    if not url or not str(url).strip():
        raise OriginalVideoDownloadError("No source video URL was provided.")

    target_url = str(url).strip()
    file_id = f"original_{uuid.uuid4().hex}"
    out_template = os.path.join(GENERATED_DIR, f"{file_id}.%(ext)s")
    is_mock_url = _is_placeholder_video_url(target_url)

    if is_mock_url and not allow_fallback:
        raise OriginalVideoDownloadError(
            "Refusing to substitute a placeholder/search URL. Provide the exact original video URL or explicitly enable fallback."
        )
    if is_mock_url and title and allow_fallback:
        logger.info(f"Mock URL detected. Swapping to YouTube search for: {title}")
        target_url = f"ytsearch1:{title}"

    def run_ytdlp(extra_args, download_url):
        cmd = [
            get_binary_path("yt-dlp"),
            *extra_args,
            "--no-playlist",
            "-o", out_template,
            download_url,
        ]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)

    section = f"*0-{int(max_duration_sec)}"
    try:
        result = run_ytdlp(
            ["-f", "b[ext=mp4]/b", "--download-sections", section, "--force-keyframes-at-cuts"],
            target_url,
        )
        if result.returncode != 0:
            logger.warning(f"yt-dlp sectioned mp4 download failed: {result.stderr}. Trying full-file fallback...")
            result = run_ytdlp(["-f", "b[ext=mp4]/b"], target_url)
        if result.returncode != 0:
            logger.warning(f"yt-dlp strict mp4 check failed: {result.stderr}. Trying merge fallback...")
            result = run_ytdlp(["--merge-output-format", "mp4"], target_url)

        if result.returncode != 0 and not allow_fallback:
            raise OriginalVideoDownloadError(
                "Could not download this specific video. The source platform may block automated "
                "downloads (common for Instagram/TikTok/X without login) or the link may be private "
                "or invalid. The generated commentary text is still usable without the video."
            )

        if result.returncode != 0 and title and not str(target_url).startswith("ytsearch1:"):
            logger.info(f"Direct download failed. Swapping to final YouTube search fallback for: {title}")
            result = run_ytdlp(["--merge-output-format", "mp4"], f"ytsearch1:{title}")

        if result.returncode != 0 and allow_fallback:
            logger.info("Search fallback failed. Using verified cinematic trailer fallback...")
            result = run_ytdlp(["--merge-output-format", "mp4"], "https://www.youtube.com/watch?v=aqz-KE-bpKQ")
            if result.returncode != 0:
                raise OriginalVideoDownloadError(f"Failed to download video: {result.stderr}")
        elif result.returncode != 0:
            raise OriginalVideoDownloadError("Could not download this specific video and fallback is disabled.")
    except subprocess.TimeoutExpired:
        raise OriginalVideoDownloadError(f"Video scraping request timed out (limit: {timeout_sec} seconds).")

    downloaded_file = None
    for filename in os.listdir(GENERATED_DIR):
        if filename.startswith(file_id):
            downloaded_file = os.path.join(GENERATED_DIR, filename)
            break

    if not downloaded_file or not os.path.exists(downloaded_file):
        raise OriginalVideoDownloadError("Video downloaded but target file was not found on disk.")

    ext = os.path.splitext(downloaded_file)[1].lower()
    target_path = os.path.join(GENERATED_DIR, f"{file_id}.mp4")
    if ext != ".mp4":
        conv_cmd = [get_binary_path("ffmpeg"), "-y", "-i", downloaded_file, "-c", "copy", target_path]
        conv_res = subprocess.run(conv_cmd, capture_output=True, text=True, timeout=timeout_sec)
        if conv_res.returncode != 0:
            conv_cmd_enc = [
                get_binary_path("ffmpeg"), "-y",
                "-i", downloaded_file,
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                target_path,
            ]
            conv_res = subprocess.run(conv_cmd_enc, capture_output=True, text=True, timeout=timeout_sec)
            if conv_res.returncode != 0:
                raise OriginalVideoDownloadError(f"FFmpeg container conversion failed: {conv_res.stderr}")
        if os.path.exists(downloaded_file):
            os.remove(downloaded_file)
        downloaded_file = target_path

    return ensure_video_under_limit(downloaded_file, max_duration_sec=max_duration_sec)


def resolve_autopilot_copy(top_trend: dict, settings: dict) -> tuple:
    """Use scan copy when present; otherwise Gemini-repurpose the source URL."""
    linkedin_text = (top_trend.get("recreated_linkedin_post") or "").strip()
    twitter_thread = top_trend.get("recreated_twitter_thread") or None
    if isinstance(twitter_thread, list):
        twitter_thread = [t for t in twitter_thread if str(t).strip()]
        if not twitter_thread:
            twitter_thread = None
    source_url = (top_trend.get("url") or "").strip()
    title = top_trend.get("title") or "Viral clip"
    if (not linkedin_text or not twitter_thread) and source_url:
        try:
            content = repurpose_video_link_copy(source_url, settings)
            if not linkedin_text:
                linkedin_text = (content.repurposed_linkedin_post or "").strip()
            if not twitter_thread:
                twitter_thread = content.repurposed_twitter_thread
        except Exception as copy_err:
            logger.error(f"Autopilot: repurpose_video_link_copy failed: {copy_err}")
    if not linkedin_text:
        credit = f"Inspired by the original: {source_url}" if source_url else "Inspired by a viral moment."
        linkedin_text = f"{title}\n\n{credit}"
    return linkedin_text, twitter_thread

class LoadOriginalVideoRequest(BaseModel):
    url: str
    title: Optional[str] = None
    # When False, refuses to silently substitute a different video (YouTube search
    # match or the safety-fallback clip) if the exact URL can't be downloaded —
    # used by the Repurposer Workshop, where the point is repurposing THIS video.
    allow_fallback: bool = False

@app.post("/api/load-original-video")
def load_original_video(req: LoadOriginalVideoRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    # We will run this inside a background task to prevent timing out on long yt-dlp downloads,
    # utilizing the existing update_job_status mechanism.
    update_job_status(job_id, "PENDING", 0, "Initializing video scraper...")

    async def download_task():
        try:
            update_job_status(job_id, "PROCESSING", 10, "Running yt-dlp to scrape video file...")
            downloaded_file = await asyncio.to_thread(
                download_and_trim_original_video,
                req.url,
                req.title,
                60,
                req.allow_fallback,
                180,
            )
            web_path = f"/static/assets/generated/{os.path.basename(downloaded_file)}"
            update_job_status(job_id, "SUCCESS", 100, "Original video loaded successfully!", result={"video_path": web_path})
        except OriginalVideoDownloadError as e:
            update_job_status(job_id, "FAILED", 0, str(e))
        except asyncio.TimeoutError:
            update_job_status(job_id, "FAILED", 0, "Video scraping request timed out (limit: 180 seconds).")
        except Exception as e:
            logger.exception("Error loading original video")
            update_job_status(job_id, "FAILED", 0, f"Scraper execution failed: {str(e)}")

    background_tasks.add_task(download_task)
    return {"job_id": job_id}

@app.get("/api/video-status/{job_id}")
def get_video_status(job_id: str):
    return get_job_status(job_id)

class RepurposeVideoLinkRequest(BaseModel):
    url: str

@app.post("/api/repurpose-video-link")
def repurpose_video_link(req: RepurposeVideoLinkRequest):
    settings = load_settings()
    try:
        content = repurpose_video_link_copy(req.url, settings)
        return {"status": "SUCCESS", "data": content.model_dump()}
    except ValueError as val_err:
        raise HTTPException(status_code=400, detail=str(val_err))
    except Exception as e:
        logger.exception("Failed to repurpose link copy")
        raise HTTPException(status_code=500, detail=f"Failed to generate repurposed copy: {str(e)}")

# Models for Post Scheduling
class SchedulePostRequest(BaseModel):
    platform: str # "twitter", "linkedin", or "both"
    text: str
    thread: Optional[List[str]] = None
    scheduled_time: str # ISO format string e.g. "2026-06-18T10:00:00"
    campaign_title: Optional[str] = "Staged Post"
    video_path: Optional[str] = None

SCHEDULED_POSTS_FILE = os.path.join(STATE_DIR, "scheduled_posts.json")

def load_scheduled_posts() -> List[dict]:
    return read_json_file(SCHEDULED_POSTS_FILE, [])

def save_scheduled_posts(posts: List[dict]):
    try:
        write_json_file(SCHEDULED_POSTS_FILE, posts)
    except Exception as e:
        logger.error(f"Error saving scheduled posts file: {e}")

def postproxy_platform_results(post: dict) -> List[dict]:
    result = post.get("postproxy_result") or {}
    if isinstance(result.get("platforms"), list):
        return result["platforms"]
    rows = []
    for child in result.get("posts") or []:
        if isinstance(child, dict):
            rows.extend(child.get("platforms") or [])
    return rows

def postproxy_published_platforms(post: dict) -> List[dict]:
    return [
        item for item in postproxy_platform_results(post)
        if item.get("status") == "published"
    ]

def reconcile_postproxy_posts(posts: List[dict], settings: dict) -> bool:
    changed = False
    if not settings.get("postproxy_enabled") or not (settings.get("postproxy_api_key") or os.environ.get("POSTPROXY_API_KEY")):
        return False
    for post in posts:
        post_ids = post.get("postproxy_post_ids") or ([post.get("postproxy_post_id")] if post.get("postproxy_post_id") else [])
        post_ids = [pid for pid in post_ids if pid]
        if not post_ids:
            continue
        if post.get("status") == "SUCCESS" and postproxy_published_platforms(post):
            continue
        try:
            latest_posts = [postproxy_get(settings, f"/posts/{post_id}", timeout=30) for post_id in post_ids]
            platform_results = []
            for latest in latest_posts:
                platform_results.extend(latest.get("platforms") or [])
            failures = [
                item for item in platform_results
                if item.get("status") not in ("published", "processing", "processed", "scheduled", "pending")
            ]
            published = [item for item in platform_results if item.get("status") == "published"]
            accepted = [
                item for item in platform_results
                if item.get("status") in ("published", "processing", "processed", "scheduled", "pending")
            ]
            post["postproxy_result"] = latest_posts[0] if len(latest_posts) == 1 else {"posts": latest_posts, "platforms": platform_results}
            if failures and accepted:
                post["status"] = "PARTIAL_SUCCESS"
                post["error_message"] = "; ".join(f"{i.get('platform')}: {i.get('error') or i.get('status')}" for i in failures)
            elif failures:
                post["status"] = "FAILED"
                post["error_message"] = "; ".join(f"{i.get('platform')}: {i.get('error') or i.get('status')}" for i in failures)
            elif accepted:
                post["status"] = "SUCCESS"
                post["error_message"] = None
            if published:
                post["postproxy_permalinks"] = {
                    item.get("platform"): item.get("permalink")
                    for item in published if item.get("permalink")
                }
            changed = True
        except Exception as e:
            logger.warning(f"Could not reconcile PostProxy post(s) {post_ids}: {e}")
    return changed

# ==========================================================================
# ENGAGEMENT AUTOMATION — Twitter mention fetching + AI-drafted replies,
# queued for approval. LinkedIn is not included: its API doesn't expose
# comment/mention reading for personal profiles without organization-level
# permissions this app doesn't have.
# ==========================================================================

ENGAGEMENT_QUEUE_FILE = os.path.join(STATE_DIR, "engagement_queue.json")
ENGAGEMENT_STATE_FILE = os.path.join(STATE_DIR, "engagement_state.json")

def load_engagement_queue() -> List[dict]:
    return read_json_file(ENGAGEMENT_QUEUE_FILE, [])

def save_engagement_queue(items: List[dict]):
    try:
        write_json_file(ENGAGEMENT_QUEUE_FILE, items)
    except Exception as e:
        logger.error(f"Error saving engagement queue file: {e}")

def load_engagement_state() -> dict:
    return read_json_file(ENGAGEMENT_STATE_FILE, {})

def save_engagement_state(state: dict):
    try:
        write_json_file(ENGAGEMENT_STATE_FILE, state)
    except Exception as e:
        logger.error(f"Error saving engagement state file: {e}")

def fetch_and_draft_mention_replies(settings: dict):
    required_keys = ["twitter_consumer_key", "twitter_consumer_secret", "twitter_access_token", "twitter_access_token_secret"]
    if not all(settings.get(k) for k in required_keys):
        return
    try:
        fetch_and_draft_mention_replies_with_bearer(settings)
        return
    except Exception as bearer_err:
        logger.warning(f"Twitter bearer mention fetch failed, trying OAuth1 user-context fallback: {bearer_err}")
    try:
        client = tweepy.Client(
            consumer_key=settings["twitter_consumer_key"],
            consumer_secret=settings["twitter_consumer_secret"],
            access_token=settings["twitter_access_token"],
            access_token_secret=settings["twitter_access_token_secret"]
        )
        me = client.get_me()
        if not me.data:
            return
        user_id = me.data.id

        state = load_engagement_state()
        since_id = state.get("twitter_since_id")

        kwargs = {"max_results": 20, "tweet_fields": ["created_at", "author_id"], "expansions": ["author_id"]}
        if since_id:
            kwargs["since_id"] = since_id
        mentions = client.get_users_mentions(id=user_id, **kwargs)

        if not mentions.data:
            return

        author_lookup = {}
        if mentions.includes and "users" in mentions.includes:
            author_lookup = {u.id: u.username for u in mentions.includes["users"]}

        queue = load_engagement_queue()
        existing_ids = {item["source_tweet_id"] for item in queue}
        newest_id = since_id

        for mention in mentions.data:
            if not newest_id or int(mention.id) > int(newest_id):
                newest_id = str(mention.id)
            if str(mention.id) in existing_ids:
                continue
            author_username = author_lookup.get(mention.author_id, "unknown")
            try:
                reply_text = draft_engagement_reply(mention.text, author_username, settings)
            except Exception as draft_err:
                logger.warning(f"Failed to draft reply for mention {mention.id}: {draft_err}")
                continue
            queue.append({
                "id": str(uuid.uuid4()),
                "platform": "twitter",
                "source_tweet_id": str(mention.id),
                "source_author": author_username,
                "source_text": mention.text,
                "drafted_reply": reply_text,
                "status": "PENDING_REVIEW",
                "created_at": datetime.now().isoformat(),
                "sent_at": None
            })

        save_engagement_queue(queue)
        if newest_id:
            save_engagement_state({"twitter_since_id": newest_id})
    except Exception as e:
        logger.warning(f"Twitter OAuth1 mention fetch failed: {e}")

def fetch_twitter_bearer_token(settings: dict) -> Optional[str]:
    consumer_key = settings.get("twitter_consumer_key")
    consumer_secret = settings.get("twitter_consumer_secret")
    if not consumer_key or not consumer_secret:
        return None
    try:
        res = requests.post(
            "https://api.twitter.com/oauth2/token",
            auth=(consumer_key, consumer_secret),
            data={"grant_type": "client_credentials"},
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
            timeout=30,
        )
        if res.status_code != 200:
            logger.warning(f"Twitter bearer token request failed: HTTP {res.status_code}")
            return None
        return res.json().get("access_token")
    except Exception as e:
        logger.warning(f"Twitter bearer token request failed: {e}")
        return None

def fetch_and_draft_mention_replies_with_bearer(settings: dict):
    bearer_token = fetch_twitter_bearer_token(settings)
    access_token = settings.get("twitter_access_token", "")
    user_id = access_token.split("-")[0] if "-" in access_token else ""
    if not bearer_token or not user_id:
        return

    state = load_engagement_state()
    since_id = state.get("twitter_since_id")
    params = {
        "max_results": 20,
        "tweet.fields": "created_at,author_id",
        "expansions": "author_id",
        "user.fields": "username",
    }
    if since_id:
        params["since_id"] = since_id
    res = requests.get(
        f"https://api.twitter.com/2/users/{user_id}/mentions",
        headers={"Authorization": f"Bearer {bearer_token}"},
        params=params,
        timeout=30,
    )
    if res.status_code != 200:
        raise RuntimeError(f"Twitter bearer mentions failed: HTTP {res.status_code} {res.text[:180]}")
    payload = res.json()
    mentions = payload.get("data") or []
    if not mentions:
        return
    author_lookup = {
        str(user.get("id")): user.get("username", "unknown")
        for user in payload.get("includes", {}).get("users", [])
    }
    queue = load_engagement_queue()
    existing_ids = {item.get("source_tweet_id") for item in queue}
    newest_id = since_id
    for mention in mentions:
        mention_id = str(mention.get("id"))
        if not newest_id or int(mention_id) > int(newest_id):
            newest_id = mention_id
        if mention_id in existing_ids:
            continue
        author_username = author_lookup.get(str(mention.get("author_id")), "unknown")
        try:
            reply_text = draft_engagement_reply(mention.get("text", ""), author_username, settings)
        except Exception as draft_err:
            logger.warning(f"Failed to draft reply for mention {mention_id}: {draft_err}")
            reply_text = ""
        queue.append({
            "id": str(uuid.uuid4()),
            "platform": "twitter",
            "source_tweet_id": mention_id,
            "source_author": author_username,
            "source_text": mention.get("text", ""),
            "drafted_reply": reply_text,
            "status": "PENDING_REVIEW",
            "created_at": datetime.now().isoformat(),
            "sent_at": None,
        })
    save_engagement_queue(queue)
    if newest_id:
        save_engagement_state({"twitter_since_id": newest_id})

def resolve_platform_list(platform_field: str) -> List[str]:
    if platform_field == "both":
        return ["twitter", "linkedin"]
    if platform_field == "all":
        return ["twitter", "linkedin", "instagram", "tiktok", "youtube", "facebook", "threads"]
    return [p.strip() for p in platform_field.split(",") if p.strip()]

async def publish_post_to_platforms(post: dict, settings: dict, bypass_daily_limit: bool = False) -> dict:
    """Shared publisher used by the scheduler, autopilot approval, and manual publish endpoints.
    Returns {"successes": [...], "errors": [...], "tweet_id": str|None, "tweet_ids": [...]|None}."""
    platforms = resolve_platform_list(post["platform"])
    successes = []
    errors = []
    tweet_id = None
    tweet_ids = None
    if settings.get("postproxy_enabled") and (settings.get("postproxy_api_key") or os.environ.get("POSTPROXY_API_KEY")):
        if not bypass_daily_limit:
            limit_result = enforce_postproxy_daily_limit(post, settings, platforms)
            if limit_result:
                return limit_result
        try:
            logger.info(f"Publishing post {post.get('id')} through PostProxy for platforms: {platforms}")
            return publish_via_postproxy(post, settings, platforms)
        except Exception as pp_err:
            logger.exception("PostProxy publish failed")
            return {"successes": [], "errors": [f"PostProxy: {str(pp_err)}"], "tweet_id": None, "tweet_ids": None}

    if post.get("video_path") and any(p in platforms for p in ("instagram", "tiktok", "youtube")):
        try:
            ensure_vertical_video_variant(post)
        except Exception as variant_err:
            logger.warning(f"Could not prepare vertical platform variant before publishing: {variant_err}")

    # 1. Post to Twitter
    if "twitter" in platforms:
        try:
            if settings.get("mock_mode", True):
                logger.info("[MOCK] Post to Twitter")
                successes.append("twitter (mock)")
                tweet_id = "mock_tweet_12345"
            else:
                client = tweepy.Client(
                    consumer_key=settings["twitter_consumer_key"],
                    consumer_secret=settings["twitter_consumer_secret"],
                    access_token=settings["twitter_access_token"],
                    access_token_secret=settings["twitter_access_token_secret"]
                )

                media_ids = None
                if post.get("video_path"):
                    verified_video = ensure_video_under_limit(post["video_path"], max_duration_sec=90)
                    media_id = upload_video_to_twitter(verified_video, settings)
                    if media_id:
                        media_ids = [media_id]

                if post.get("thread"):
                    prev_id = None
                    ids_collected = []
                    for idx, tweet in enumerate(post["thread"]):
                        if idx == 0 and media_ids:
                            res = client.create_tweet(text=tweet, media_ids=media_ids)
                        elif prev_id:
                            res = client.create_tweet(text=tweet, in_reply_to_tweet_id=prev_id)
                        else:
                            res = client.create_tweet(text=tweet)
                        prev_id = res.data["id"]
                        ids_collected.append(prev_id)
                    tweet_ids = ids_collected
                    tweet_id = ids_collected[0] if ids_collected else None
                else:
                    if media_ids:
                        res = client.create_tweet(text=post["text"], media_ids=media_ids)
                    else:
                        res = client.create_tweet(text=post["text"])
                    tweet_id = res.data["id"]
                successes.append("twitter")
        except Exception as tw_err:
            logger.exception("Twitter post failed")
            errors.append(f"Twitter: {str(tw_err)}")

    # 2. Post to LinkedIn
    if "linkedin" in platforms:
        try:
            if settings.get("mock_mode", True):
                logger.info("[MOCK] Post to LinkedIn")
                successes.append("linkedin (mock)")
            else:
                person_urn = settings['linkedin_person_urn']
                author_urn = person_urn if person_urn.startswith("urn:li:") else f"urn:li:person:{person_urn}"
                url = 'https://api.linkedin.com/v2/ugcPosts'
                headers = {
                    'Authorization': f'Bearer {settings["linkedin_access_token"]}',
                    'X-Restli-Protocol-Version': '2.0.0',
                    'Content-Type': 'application/json'
                }

                asset_urn = None
                if post.get("video_path"):
                    verified_video = ensure_video_under_limit(post["video_path"], max_duration_sec=90)
                    asset_urn = upload_video_to_linkedin(verified_video, author_urn, settings)

                if asset_urn:
                    payload = {
                        "author": author_urn,
                        "lifecycleState": "PUBLISHED",
                        "specificContent": {
                            "com.linkedin.ugc.ShareContent": {
                                "shareCommentary": {
                                    "text": post["text"]
                                },
                                "shareMediaCategory": "VIDEO",
                                "media": [
                                    {
                                        "status": "READY",
                                        "media": asset_urn
                                    }
                                ]
                            }
                        },
                        "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"}
                    }
                else:
                    payload = {
                        "author": author_urn,
                        "lifecycleState": "PUBLISHED",
                        "specificContent": {
                            "com.linkedin.ugc.ShareContent": {
                                "shareCommentary": {
                                    "text": post["text"]
                                },
                                "shareMediaCategory": "NONE"
                            }
                        },
                        "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"}
                    }
                res = requests.post(url, headers=headers, json=payload)
                if res.status_code == 201:
                    successes.append("linkedin")
                else:
                    errors.append(f"LinkedIn HTTP {res.status_code}: {res.text}")
        except Exception as li_err:
            logger.exception("LinkedIn post failed")
            errors.append(f"LinkedIn: {str(li_err)}")

    # 3. Post to Instagram, TikTok, YouTube, Facebook, Threads
    for platform_name, publish_fn in [
        ("instagram", publish_instagram_post),
        ("tiktok", publish_tiktok_post),
        ("youtube", publish_youtube_short),
        ("facebook", publish_facebook_post),
        ("threads", publish_threads_post),
    ]:
        if platform_name in platforms:
            try:
                if settings.get("mock_mode", True):
                    logger.info(f"[MOCK] Post to {platform_name}")
                    successes.append(f"{platform_name} (mock)")
                else:
                    publish_fn(post, settings)
                    successes.append(platform_name)
            except Exception as plat_err:
                logger.exception(f"{platform_name} post failed")
                errors.append(f"{platform_name.capitalize()}: {str(plat_err)}")

    return {"successes": successes, "errors": errors, "tweet_id": tweet_id, "tweet_ids": tweet_ids}

def apply_publish_result_to_post(p: dict, result: dict):
    successes, errors = result["successes"], result["errors"]
    if result.get("blocked_by_daily_limit"):
        p["status"] = "AWAITING_APPROVAL"
        p["error_message"] = "; ".join(errors)
    elif errors and not successes:
        p["status"] = "FAILED"
        p["error_message"] = "; ".join(errors)
    elif errors:
        p["status"] = "PARTIAL_SUCCESS"
        p["error_message"] = f"Success: {', '.join(successes)}. Errors: {'; '.join(errors)}"
        p["posted_at"] = datetime.now().isoformat()
    else:
        p["status"] = "SUCCESS"
        p["posted_at"] = datetime.now().isoformat()
    if result.get("tweet_id"):
        p["tweet_id"] = result["tweet_id"]
    if result.get("tweet_ids"):
        p["tweet_ids"] = result["tweet_ids"]
    if result.get("postproxy_post_id"):
        p["postproxy_post_id"] = result["postproxy_post_id"]
    if result.get("postproxy_result"):
        p["postproxy_result"] = result["postproxy_result"]
    if result.get("blocked_by_daily_limit"):
        p["blocked_by_daily_limit"] = True
        p["postproxy_daily_limit"] = result.get("postproxy_daily_limit")
        p["postproxy_daily_used"] = result.get("postproxy_daily_used")
        p["postproxy_requested_units"] = result.get("postproxy_requested_units")

async def execute_scheduled_post(post: dict):
    settings = load_settings()
    result = await publish_post_to_platforms(post, settings)

    posts = load_scheduled_posts()
    for p in posts:
        if p["id"] == post["id"]:
            if post.get("vertical_video_path"):
                p["vertical_video_path"] = post["vertical_video_path"]
            apply_publish_result_to_post(p, result)
            break
    save_scheduled_posts(posts)

async def execute_autonomous_autopost(settings: dict, forced_trend: Optional[dict] = None):
    logger.info("Starting autonomous autopilot pipeline...")
    loop = asyncio.get_running_loop()
    top_trend = forced_trend if isinstance(forced_trend, dict) and forced_trend.get("url") else None

    if top_trend:
        logger.info(f"Using provided/persisted pick: {top_trend.get('title') or top_trend.get('url')}")
    else:
        job_id = str(uuid.uuid4())
        await loop.run_in_executor(None, run_live_trend_scanner, job_id, settings)
        status = jobs_status.get(job_id, {})
        if status.get("status") != "SUCCESS":
            logger.error(f"Autonomous trend scanning failed: {status.get('message')}")
            return
        result = status.get("result", {})
        persist_trend_scan_result(result)
        trends = result.get("trends", [])
        if not trends:
            logger.error("Autonomous trend scanning found no trends.")
            return
        top_trend = trends[0]
        logger.info(f"Selected top trend: {top_trend['title']}")

    source_url = (top_trend.get("url") or "").strip()
    title = top_trend.get("title") or "Viral clip"
    linkedin_text, twitter_thread = await loop.run_in_executor(None, resolve_autopilot_copy, top_trend, settings)

    # Repurpose the ORIGINAL viral video: download + trim < 60s. Do not generate a new clip.
    generated_video_path = None
    video_error = None
    logger.info(f"Autopilot: downloading original video from {source_url} (no Veo/FAL/Runway generation).")
    try:
        downloaded_file = await loop.run_in_executor(
            None,
            download_and_trim_original_video,
            source_url,
            title,
            60,
            False,
            180,
        )
        generated_video_path = f"/static/assets/generated/{os.path.basename(downloaded_file)}"
        logger.info(f"Autopilot: Original video ready (trimmed to 60s): {generated_video_path}")
    except Exception as download_err:
        video_error = str(download_err)
        logger.error(f"Autopilot: Original video download failed; staging copy without media. {download_err}")

    platforms = settings.get("autonomous_platforms", ["twitter", "linkedin"])

    # Instagram Reels and TikTok require vertical (9:16) video — the main render above is
    # landscape (matches Twitter/LinkedIn/YouTube/Facebook fine), so render a vertical variant
    # specifically for those two platforms when they're selected. Failure here is non-fatal:
    # falls back to the landscape video rather than blocking the whole autopilot run.
    vertical_video_path = None
    if generated_video_path and any(p in platforms for p in ("instagram", "tiktok")):
        try:
            abs_source = resolve_local_video_path(generated_video_path)
            vertical_filename = f"{os.path.splitext(os.path.basename(generated_video_path))[0]}_vertical_9x16.mp4"
            abs_vertical = os.path.join(GENERATED_DIR, vertical_filename)
            font_path = get_font_path()
            render_variant_with_fallback(abs_source, abs_vertical, 1080, 1920, "", font_path, 320)
            vertical_video_path = f"/static/assets/generated/{vertical_filename}"
            logger.info(f"Autopilot: Rendered 9:16 vertical variant for Instagram/TikTok: {vertical_video_path}")
        except Exception as e:
            logger.error(f"Autopilot: Failed to render vertical variant, Instagram/TikTok will use the landscape video instead: {e}")

    original_generated_video_path = generated_video_path
    if generated_video_path and settings.get("viral_template_enabled", False):
        try:
            template_source_path = vertical_video_path or generated_video_path
            abs_template_source = resolve_local_video_path(template_source_path)
            template_filename = f"{os.path.splitext(os.path.basename(generated_video_path))[0]}_viral_template.mp4"
            abs_template_output = os.path.join(GENERATED_DIR, template_filename)
            template_title = top_trend.get("title", "AI Trend Recreation")
            template_subtitle = "Recreated and packaged by 6Frame Studio"
            logger.info(f"Autopilot: Applying viral template '{settings.get('viral_template_style', 'hook_burst')}'...")
            await loop.run_in_executor(
                None,
                lambda: render_template_video(
                    source_video_path=abs_template_source,
                    output_path=abs_template_output,
                    template_id=settings.get("viral_template_style", "hook_burst"),
                    title=template_title,
                    subtitle=template_subtitle,
                    work_root=os.path.join(UPLOAD_DIR, "template_renders"),
                    quality=settings.get("viral_template_quality", "standard"),
                )
            )
            templated_path = f"/static/assets/generated/{template_filename}"
            generated_video_path = templated_path
            vertical_video_path = templated_path
            logger.info(f"Autopilot: Viral template render complete: {templated_path}")
        except Exception as e:
            logger.error(f"Autopilot: Viral template render failed; falling back to generated video: {e}")

    now = datetime.now()
    log_post = {
        "id": str(uuid.uuid4()),
        "platform": ",".join(platforms) if platforms else "none",
        "text": linkedin_text,
        "thread": twitter_thread,
        "scheduled_time": now.isoformat(),
        "campaign_title": f"Autonomous: {title}",
        "video_path": generated_video_path,
        "vertical_video_path": vertical_video_path,
        "source_video_path": original_generated_video_path,
        "source_url": source_url,
        "media_source": "original_download" if generated_video_path else "missing",
        "viral_template_id": settings.get("viral_template_style") if settings.get("viral_template_enabled", False) else None,
        "status": "PUBLISHING",
        "error_message": None if generated_video_path else f"Original video missing: {video_error or 'download failed'}",
        "posted_at": None
    }

    require_approval = settings.get("require_autopilot_approval", True)
    if require_approval:
        log_post["status"] = "AWAITING_APPROVAL"
        posts = load_scheduled_posts()
        posts.append(log_post)
        save_scheduled_posts(posts)
        logger.info(f"Autopilot pick '{title}' staged for review — awaiting manual approval before publishing.")
        return

    posts = load_scheduled_posts()
    posts.append(log_post)
    save_scheduled_posts(posts)

    result = await publish_post_to_platforms(log_post, settings)
    posts = load_scheduled_posts()
    for p in posts:
        if p["id"] == log_post["id"]:
            apply_publish_result_to_post(p, result)
            break
    save_scheduled_posts(posts)
    logger.info(f"Autonomous autopilot pipeline complete. Status: {result['successes']}, Errors: {result['errors']}")

METRICS_REFRESH_INTERVAL_SECONDS = 1800  # 30 minutes

def compute_engagement_score(metrics: dict) -> float:
    return (
        metrics.get("like_count", 0)
        + metrics.get("retweet_count", 0) * 2
        + metrics.get("reply_count", 0) * 2
        + metrics.get("quote_count", 0) * 2
    )

def fetch_twitter_metrics_for_post(post: dict, settings: dict) -> Optional[dict]:
    ids = post.get("tweet_ids") or ([post["tweet_id"]] if post.get("tweet_id") else [])
    ids = [i for i in ids if i and not str(i).startswith("mock_")]
    if not ids:
        return None
    required_keys = ["twitter_consumer_key", "twitter_consumer_secret", "twitter_access_token", "twitter_access_token_secret"]
    if not all(settings.get(k) for k in required_keys):
        return None
    bearer_token = fetch_twitter_bearer_token(settings)
    if bearer_token:
        try:
            totals = {"like_count": 0, "retweet_count": 0, "reply_count": 0, "quote_count": 0}
            res = requests.get(
                "https://api.twitter.com/2/tweets",
                headers={"Authorization": f"Bearer {bearer_token}"},
                params={"ids": ",".join(ids), "tweet.fields": "public_metrics"},
                timeout=30,
            )
            if res.status_code == 200:
                data = res.json().get("data") or []
                if data:
                    for tweet in data:
                        pm = tweet.get("public_metrics") or {}
                        for k in totals:
                            totals[k] += int(pm.get(k, 0) or 0)
                    return {**totals, "fetched_at": datetime.now().isoformat()}
            else:
                logger.warning(f"Twitter bearer metrics fetch failed for {ids}: HTTP {res.status_code} {res.text[:180]}")
        except Exception as e:
            logger.warning(f"Twitter bearer metrics fetch failed for post {post.get('id')}: {e}")
    try:
        client = tweepy.Client(
            consumer_key=settings["twitter_consumer_key"],
            consumer_secret=settings["twitter_consumer_secret"],
            access_token=settings["twitter_access_token"],
            access_token_secret=settings["twitter_access_token_secret"]
        )
        totals = {"like_count": 0, "retweet_count": 0, "reply_count": 0, "quote_count": 0}
        found_any = False
        for tid in ids:
            res = client.get_tweet(tid, tweet_fields=["public_metrics"])
            if res.data and res.data.public_metrics:
                found_any = True
                pm = res.data.public_metrics
                for k in totals:
                    totals[k] += pm.get(k, 0)
        if not found_any:
            return None
        return {**totals, "fetched_at": datetime.now().isoformat()}
    except Exception as e:
        logger.warning(f"Failed to fetch Twitter metrics for post {post.get('id')}: {e}")
        return None

def refresh_post_metrics(settings: dict, max_age_days: int = 14) -> bool:
    """Pulls fresh engagement metrics for recently-published Twitter posts. LinkedIn is skipped —
    personal-profile posts aren't exposed by LinkedIn's standard API without organization-level
    permissions this app doesn't have."""
    posts = load_scheduled_posts()
    now = datetime.now()
    changed = False
    for post in posts:
        if post.get("status") != "SUCCESS":
            continue
        posted_at = post.get("posted_at")
        if not posted_at:
            continue
        try:
            posted_dt = datetime.fromisoformat(posted_at)
        except Exception:
            continue
        if (now - posted_dt).days > max_age_days:
            continue
        metrics = fetch_twitter_metrics_for_post(post, settings)
        if metrics:
            post.setdefault("metrics", {})["twitter"] = metrics
            changed = True
    if changed:
        save_scheduled_posts(posts)
    return changed

ENGAGEMENT_REFRESH_INTERVAL_SECONDS = 900  # 15 minutes

async def scheduler_loop():
    await asyncio.sleep(5)
    logger.info("Background scheduler loop started.")
    last_autonomous_date = None
    last_metrics_refresh = None
    last_engagement_refresh = None

    while True:
        try:
            # 1. Check Scheduled Queue
            posts = load_scheduled_posts()
            now = datetime.now()
            updated = False

            for post in posts:
                if post["status"] == "PENDING":
                    try:
                        sched_time = datetime.fromisoformat(post["scheduled_time"])
                        if now >= sched_time:
                            logger.info(f"Scheduled post {post['id']} is due. Publishing...")
                            post["status"] = "PUBLISHING"
                            save_scheduled_posts(posts)
                            updated = True
                            asyncio.create_task(execute_scheduled_post(post))
                    except Exception as parse_err:
                        logger.error(f"Error parsing time for post {post['id']}: {parse_err}")
                        post["status"] = "FAILED"
                        post["error_message"] = f"Invalid scheduled time: {str(parse_err)}"
                        updated = True

            # 2. Check Autonomous Autoposting
            settings = load_settings()
            if settings.get("autonomous_posting", False):
                current_hour = now.hour
                target_hour = int(settings.get("autonomous_hour", 9))
                current_date = now.date().isoformat()

                if current_hour == target_hour and current_date != last_autonomous_date:
                    logger.info("Autonomous autoposting time reached. Initiating pipeline...")
                    last_autonomous_date = current_date
                    asyncio.create_task(execute_autonomous_autopost(settings))

            # 3. Periodically refresh post performance metrics (analytics feedback loop)
            if last_metrics_refresh is None or (now - last_metrics_refresh).total_seconds() >= METRICS_REFRESH_INTERVAL_SECONDS:
                last_metrics_refresh = now
                try:
                    refresh_post_metrics(settings)
                except Exception as metrics_err:
                    logger.error(f"Error refreshing post metrics: {metrics_err}")

            # 4. Periodically fetch new mentions and draft AI replies (engagement automation)
            if last_engagement_refresh is None or (now - last_engagement_refresh).total_seconds() >= ENGAGEMENT_REFRESH_INTERVAL_SECONDS:
                last_engagement_refresh = now
                try:
                    fetch_all_social_inbox(settings)
                except Exception as engagement_err:
                    logger.error(f"Error fetching/drafting mention replies: {engagement_err}")

        except Exception as loop_err:
            logger.error(f"Error in scheduler loop: {loop_err}")

        await asyncio.sleep(10)

@app.on_event("startup")
def startup_event():
    if os.environ.get("DISABLE_BACKGROUND_SCHEDULER", "").lower() == "true":
        logger.info("Background scheduler disabled by DISABLE_BACKGROUND_SCHEDULER=true.")
        return
    asyncio.create_task(scheduler_loop())

@app.post("/api/schedule-post")
def schedule_post(req: SchedulePostRequest):
    try:
        datetime.fromisoformat(req.scheduled_time)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid scheduled_time format: {e}")
        
    post = {
        "id": str(uuid.uuid4()),
        "platform": req.platform,
        "text": req.text,
        "thread": req.thread,
        "scheduled_time": req.scheduled_time,
        "campaign_title": req.campaign_title,
        "video_path": req.video_path,
        "status": "PENDING",
        "error_message": None,
        "posted_at": None
    }
    
    posts = load_scheduled_posts()
    posts.append(post)
    save_scheduled_posts(posts)
    return {"status": "SUCCESS", "message": "Post scheduled successfully.", "post_id": post["id"]}

@app.get("/api/scheduled-queue")
def get_scheduled_queue():
    posts = load_scheduled_posts()
    if reconcile_postproxy_posts(posts, load_settings()):
        save_scheduled_posts(posts)
    pending = [p for p in posts if p["status"] == "PENDING"]
    completed = [p for p in posts if p["status"] not in ("PENDING", "AWAITING_APPROVAL")]

    pending.sort(key=lambda x: x["scheduled_time"])
    completed.sort(key=lambda x: x.get("posted_at") or x["scheduled_time"], reverse=True)

    return pending + completed

@app.delete("/api/scheduled-queue/{post_id}")
def delete_scheduled_post(post_id: str):
    posts = load_scheduled_posts()
    filtered_posts = [p for p in posts if p["id"] != post_id]

    if len(filtered_posts) == len(posts):
        raise HTTPException(status_code=404, detail="Post not found in queue.")

    save_scheduled_posts(filtered_posts)
    return {"status": "SUCCESS", "message": "Scheduled post cancelled."}

class TriggerAutopilotRequest(BaseModel):
    url: Optional[str] = None
    title: Optional[str] = None
    use_persisted_pick: bool = False


@app.post("/api/trigger-autopilot")
def trigger_autopilot(background_tasks: BackgroundTasks, req: Optional[TriggerAutopilotRequest] = None):
    settings = load_settings()
    forced_trend = None
    body = req or TriggerAutopilotRequest()
    if body.use_persisted_pick:
        trends = (load_growth_os().get("last_trend_scan") or [])
        if body.url:
            forced_trend = next((t for t in trends if (t.get("url") or "").strip() == body.url.strip()), None)
        if not forced_trend and trends:
            forced_trend = trends[0]
        if forced_trend and body.url and not (forced_trend.get("url") or "").strip():
            forced_trend = {**forced_trend, "url": body.url}
        if forced_trend and body.title and not forced_trend.get("title"):
            forced_trend = {**forced_trend, "title": body.title}
    if not forced_trend and body.url:
        forced_trend = {
            "url": body.url,
            "title": body.title or body.url,
            "recreated_linkedin_post": "",
            "recreated_twitter_thread": None,
        }
    background_tasks.add_task(execute_autonomous_autopost, settings, forced_trend)
    return {"status": "SUCCESS", "message": "Autonomous autopilot pipeline triggered."}

# ==========================================================================
# APPROVAL QUEUE — Autopilot picks stage here before publishing when
# require_autopilot_approval is enabled ("autonomous with guardrails").
# ==========================================================================

class ApprovalEditRequest(BaseModel):
    text: Optional[str] = None
    thread: Optional[List[str]] = None

@app.get("/api/approval-queue")
def get_approval_queue():
    posts = load_scheduled_posts()
    pending_approval = [p for p in posts if p["status"] == "AWAITING_APPROVAL"]
    pending_approval.sort(key=lambda x: x["scheduled_time"], reverse=True)
    return pending_approval

@app.patch("/api/approval-queue/{post_id}")
def edit_approval_post(post_id: str, req: ApprovalEditRequest):
    posts = load_scheduled_posts()
    for p in posts:
        if p["id"] == post_id:
            if p["status"] != "AWAITING_APPROVAL":
                raise HTTPException(status_code=400, detail="Post is no longer awaiting approval.")
            if req.text is not None:
                p["text"] = req.text
            if req.thread is not None:
                p["thread"] = req.thread
            save_scheduled_posts(posts)
            return {"status": "SUCCESS", "message": "Draft updated."}
    raise HTTPException(status_code=404, detail="Post not found in approval queue.")

@app.post("/api/approval-queue/{post_id}/approve")
async def approve_post(post_id: str):
    posts = load_scheduled_posts()
    target = next((p for p in posts if p["id"] == post_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Post not found in approval queue.")
    if target["status"] != "AWAITING_APPROVAL":
        raise HTTPException(status_code=400, detail="Post is no longer awaiting approval.")

    settings = load_settings()
    target["status"] = "PUBLISHING"
    save_scheduled_posts(posts)

    result = await publish_post_to_platforms(target, settings, bypass_daily_limit=True)
    posts = load_scheduled_posts()
    for p in posts:
        if p["id"] == post_id:
            if target.get("vertical_video_path"):
                p["vertical_video_path"] = target["vertical_video_path"]
            apply_publish_result_to_post(p, result)
            break
    save_scheduled_posts(posts)
    return {"status": "SUCCESS", "message": "Post approved and published.", "result": result}

@app.post("/api/approval-queue/{post_id}/reject")
def reject_post(post_id: str):
    posts = load_scheduled_posts()
    for p in posts:
        if p["id"] == post_id:
            if p["status"] != "AWAITING_APPROVAL":
                raise HTTPException(status_code=400, detail="Post is no longer awaiting approval.")
            p["status"] = "REJECTED"
            save_scheduled_posts(posts)
            return {"status": "SUCCESS", "message": "Post rejected."}
    raise HTTPException(status_code=404, detail="Post not found in approval queue.")

# ==========================================================================
# ANALYTICS FEEDBACK LOOP — tracks real Twitter engagement on published posts
# and surfaces a data-driven recommended posting hour. LinkedIn metrics are
# not available: personal-profile posts aren't exposed by LinkedIn's API
# without organization-level permissions this app doesn't have.
# ==========================================================================

@app.get("/api/analytics/summary")
def get_analytics_summary():
    posts = load_scheduled_posts()
    scored = []
    hour_buckets = {}

    for post in posts:
        if post.get("status") != "SUCCESS":
            continue
        tw_metrics = (post.get("metrics") or {}).get("twitter")
        published_platforms = postproxy_published_platforms(post)
        if not tw_metrics and not published_platforms:
            continue
        metrics_source = "twitter_public_metrics" if tw_metrics else "postproxy_publish_status"
        metrics = tw_metrics or {
            "published_platform_count": len(published_platforms),
            "published_platforms": [item.get("platform") for item in published_platforms if item.get("platform")],
            "engagement_unavailable": True,
        }
        score = compute_engagement_score(tw_metrics) if tw_metrics else 0
        posted_at = post.get("posted_at") or post.get("scheduled_time")
        hour = None
        try:
            hour = datetime.fromisoformat(posted_at).hour
        except Exception:
            pass

        scored.append({
            "id": post["id"],
            "campaign_title": post.get("campaign_title"),
            "platform": post.get("platform"),
            "posted_at": posted_at,
            "metrics": metrics,
            "metrics_source": metrics_source,
            "engagement_score": score,
            "hour": hour
        })
        if hour is not None:
            hour_buckets.setdefault(hour, []).append(score)

    best_hour = None
    hour_breakdown = {}
    if hour_buckets:
        hour_breakdown = {h: round(sum(s) / len(s), 1) for h, s in hour_buckets.items()}
        best_hour = max(hour_breakdown, key=hour_breakdown.get)

    scored.sort(key=lambda x: x["engagement_score"], reverse=True)
    sample_size = len(scored)
    confidence = "high" if sample_size >= 20 else ("medium" if sample_size >= 8 else ("low" if sample_size > 0 else "none"))
    return {
        "posts": scored,
        "best_hour": best_hour,
        "sample_size": sample_size,
        "minimum_recommended_sample_size": 8,
        "confidence": confidence,
        "hour_breakdown": hour_breakdown
    }

@app.post("/api/analytics/refresh")
def refresh_analytics(background_tasks: BackgroundTasks):
    settings = load_settings()
    background_tasks.add_task(refresh_post_metrics, settings)
    return {"status": "SUCCESS", "message": "Metrics refresh triggered in background."}

class ApplyBestHourRequest(BaseModel):
    hour: int

@app.post("/api/analytics/apply-best-hour")
def apply_best_hour(req: ApplyBestHourRequest):
    settings = load_settings()
    settings["autonomous_hour"] = req.hour
    save_settings(settings)
    return {"status": "SUCCESS", "message": f"Autopilot posting hour set to {req.hour}:00."}

# ==========================================================================
# GROWTH OS — expanded social automation command center modules.
# Stores are JSON-backed so the product can be used immediately without a
# database migration; external integrations are connector-ready via webhooks.
# ==========================================================================

GROWTH_OS_FILE = os.path.join(STATE_DIR, "growth_os.json")

DEFAULT_BRAND_KIT = {
    "workspace_id": "default",
    "colors": ["#a855f7", "#ec4899", "#0a0a0c", "#f8fafc"],
    "fonts": ["Inter", "Outfit"],
    "logo_url": "",
    "tone_presets": [
        "premium cinematic",
        "AI-native filmmaking",
        "clear technical authority",
        "creator-first social copy",
    ],
    "template_rules": {
        "default_template": "hook_burst",
        "high_urgency": "trend_radar",
        "case_study": "documentary_interview",
        "transformation": "then_vs_now",
        "brand_colors_enabled": True,
        "auto_apply_to_generated_videos": True,
    },
}

def default_growth_os_state() -> dict:
    now = datetime.now().isoformat()
    return {
        "workspaces": [
            {"id": "default", "name": "6Frame Studio", "role": "owner", "created_at": now}
        ],
        "brand_kit": DEFAULT_BRAND_KIT.copy(),
        "listening_topics": [
            {"id": "topic-ai-video", "keyword": "AI video marketing", "status": "active", "created_at": now},
            {"id": "topic-generative-video", "keyword": "generative video ads", "status": "active", "created_at": now},
            {"id": "topic-viral-ai-video", "keyword": "viral AI video", "status": "active", "created_at": now},
            {"id": "topic-sora-veo-runway", "keyword": "Sora Veo Runway Kling AI video", "status": "active", "created_at": now},
        ],
        "listening_signals": [],
        "competitors": [
            {"id": "competitor-opusclip", "name": "OpusClip", "handle": "OpusClip", "platform": "multi", "created_at": now},
            {"id": "competitor-captions", "name": "Captions", "handle": "CaptionsApp", "platform": "multi", "created_at": now},
            {"id": "competitor-runway", "name": "Runway", "handle": "runwayml", "platform": "multi", "created_at": now},
            {"id": "competitor-canva", "name": "Canva", "handle": "canva", "platform": "multi", "created_at": now},
        ],
        "evergreen_buckets": [],
        "campaign_plan": [],
        "ab_tests": [],
        "crm_contacts": [],
        "link_in_bio": {
            "slug": "6frame",
            "headline": "6Frame Studio",
            "links": [],
            "featured_video": ""
        },
        "utm_campaigns": [],
        "automation_rules": [],
        "automation_events": [],
        "integrations": [],
        "report_history": [],
        "last_trend_scan": [],
        "last_trend_scan_at": None,
        "last_automation_generation": {}
    }

def normalize_growth_os_state(state: dict) -> dict:
    brand = state.get("brand_kit") if isinstance(state.get("brand_kit"), dict) else {}
    merged_brand = DEFAULT_BRAND_KIT.copy()
    merged_brand.update({k: v for k, v in brand.items() if v not in (None, "", [], {})})
    template_rules = DEFAULT_BRAND_KIT["template_rules"].copy()
    template_rules.update(brand.get("template_rules") or {})
    merged_brand["template_rules"] = template_rules
    state["brand_kit"] = merged_brand

    if not state.get("evergreen_buckets"):
        state["evergreen_buckets"] = [{
            "id": "evergreen-cinematic-winners",
            "name": "Cinematic Winners",
            "cadence": "monthly",
            "recycle_days": 30,
            "items": [],
            "created_at": datetime.now().isoformat(),
        }]
    return state

def load_growth_os() -> dict:
    state = default_growth_os_state()
    stored = read_json_file(GROWTH_OS_FILE, {})
    for key, value in stored.items():
        state[key] = value
    defaults = default_growth_os_state()
    if not state.get("listening_topics"):
        state["listening_topics"] = defaults["listening_topics"]
    if not state.get("competitors"):
        state["competitors"] = defaults["competitors"]
    return normalize_growth_os_state(state)

def save_growth_os(state: dict):
    try:
        write_json_file(GROWTH_OS_FILE, state)
    except Exception as e:
        logger.error(f"Error saving Growth OS file: {e}")

def connected(settings: dict, keys: List[str]) -> bool:
    return all(bool(settings.get(k)) for k in keys)

def setting_or_env(settings: dict, key: str) -> Any:
    return settings.get(key) or os.environ.get(key.upper())

def build_live_integration_status(settings: dict, state: dict) -> List[dict]:
    specs = [
        ("twitter", "Twitter / X", ["twitter_consumer_key", "twitter_consumer_secret", "twitter_access_token", "twitter_access_token_secret"], "publishing, mentions, metrics"),
        ("linkedin", "LinkedIn", ["linkedin_access_token", "linkedin_person_urn"], "publishing"),
        ("instagram", "Instagram", ["instagram_access_token", "instagram_business_account_id"], "publishing, hashtag scan, comments when Graph permissions allow"),
        ("facebook", "Facebook", ["facebook_page_access_token", "facebook_page_id"], "publishing, page comments when Graph permissions allow"),
        ("tiktok", "TikTok", ["tiktok_client_key", "tiktok_client_secret", "tiktok_refresh_token"], "publishing"),
        ("youtube", "YouTube", ["youtube_client_id", "youtube_client_secret", "youtube_refresh_token"], "publishing, comments when OAuth scope allows"),
        ("threads", "Threads", ["threads_access_token", "threads_user_id"], "publishing"),
        ("postproxy", "PostProxy", ["postproxy_api_key"], "unified OAuth, publishing, comments, DMs, analytics"),
        ("fal", "FAL", ["fal_api_key"], "video generation"),
        ("gemini", "Gemini", ["gemini_api_key"], "planning, scanning, copy, grounded research"),
        ("runway", "Runway", ["runway_api_key"], "video generation"),
    ]
    custom = {item.get("id"): item for item in state.get("integrations", []) if item.get("id")}
    pp_platforms = {
        platform_to_postproxy((profile or {}).get("platform") or "")
        for profile in (load_postproxy_state().get("profiles") or [])
        if (profile or {}).get("platform") and str((profile or {}).get("status") or "").lower() == "active"
    }
    rows = []
    for key, name, required, capability in specs:
        via_postproxy = key in pp_platforms or (key == "postproxy" and postproxy_key_configured(settings))
        native = all(bool(setting_or_env(settings, item)) for item in required)
        if via_postproxy and key != "postproxy":
            status = "live"
            capability = f"{capability} via PostProxy"
        elif key == "postproxy" and native:
            status = "live" if load_postproxy_state().get("key_valid") is not False else "blocked"
        else:
            status = "credentials_present" if native else "needs_credentials"
        rows.append({
            "id": key,
            "name": name,
            "status": status,
            "capability": capability,
            "webhook_url": custom.get(key, {}).get("webhook_url", ""),
            "last_checked": datetime.now().isoformat()
        })
    for item in state.get("integrations", []):
        if item.get("id") not in {row["id"] for row in rows}:
            rows.append(item)
    return rows

@app.get("/api/provider-diagnostics")
def provider_diagnostics():
    settings = load_settings()
    diagnostics = {
        "report_email": {
            "status": "needs_configuration",
            "message": "Missing report recipient or SMTP host.",
            "configured": False,
        },
        "twitter_mentions": {
            "status": "not_checked",
            "message": "",
            "configured": connected(settings, ["twitter_consumer_key", "twitter_consumer_secret", "twitter_access_token"]),
        },
        "youtube_comments": {
            "status": "not_checked",
            "message": "",
            "configured": connected(settings, ["youtube_client_id", "youtube_client_secret", "youtube_refresh_token"]),
        },
        "postproxy": {
            "status": "not_checked",
            "message": "",
            "configured": bool(settings.get("postproxy_api_key") or os.environ.get("POSTPROXY_API_KEY")),
        },
    }

    email_cfg = report_email_config(settings)
    if email_cfg["provider"] == "resend":
        email_ready = bool(email_cfg["to_addr"] and email_cfg["resend_api_key"] and email_cfg["resend_from"])
        email_message = "Resend report email is configured." if email_ready else "Add Report Recipient, Resend API Key, and Resend From Address in Settings."
    else:
        email_ready = bool(email_cfg["to_addr"] and email_cfg["host"])
        email_message = "SMTP report email is configured." if email_ready else "Add Report Email To and SMTP Host in Settings, or set REPORT_EMAIL_TO and SMTP_HOST on Railway."
    diagnostics["report_email"].update({
        "configured": email_ready,
        "status": "ready" if email_ready else "needs_configuration",
        "message": email_message,
        "provider": email_cfg["provider"],
        "source": email_cfg["source"],
        "has_smtp_auth": bool(email_cfg["user"] and email_cfg["password"]),
        "has_resend_key": bool(email_cfg["resend_api_key"]),
    })

    if diagnostics["twitter_mentions"]["configured"]:
        try:
            bearer_token = fetch_twitter_bearer_token(settings)
            access_token = settings.get("twitter_access_token", "")
            user_id = access_token.split("-")[0] if "-" in access_token else ""
            if not bearer_token or not user_id:
                raise ValueError("Could not derive X bearer token or user id.")
            res = requests.get(
                f"https://api.twitter.com/2/users/{user_id}/mentions",
                headers={"Authorization": f"Bearer {bearer_token}"},
                params={"max_results": 5, "tweet.fields": "created_at,author_id"},
                timeout=30,
            )
            if res.status_code == 200:
                payload = res.json()
                diagnostics["twitter_mentions"].update({
                    "status": "ready",
                    "message": "X mentions endpoint is accessible.",
                    "http_status": res.status_code,
                    "sample_count": len(payload.get("data") or []),
                })
            else:
                diagnostics["twitter_mentions"].update({
                    "status": "blocked",
                    "message": f"X mentions endpoint returned HTTP {res.status_code}.",
                    "http_status": res.status_code,
                })
        except Exception as e:
            diagnostics["twitter_mentions"].update({"status": "blocked", "message": str(e)})
    else:
        diagnostics["twitter_mentions"].update({"status": "needs_credentials", "message": "Missing X/Twitter credentials."})

    if diagnostics["youtube_comments"]["configured"]:
        try:
            token = youtube_access_token(settings)
            if not token:
                raise ValueError("Could not refresh YouTube access token.")
            scope_res = requests.get("https://oauth2.googleapis.com/tokeninfo", params={"access_token": token}, timeout=30)
            scopes = set()
            if scope_res.status_code == 200:
                scopes = set((scope_res.json().get("scope") or "").split())
            channel_res = requests.get(
                "https://www.googleapis.com/youtube/v3/channels",
                params={"part": "id,snippet", "mine": "true"},
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            channel_res.raise_for_status()
            channels = channel_res.json().get("items") or []
            channel_id = channels[0].get("id") if channels else ""
            comments_res = requests.get(
                "https://www.googleapis.com/youtube/v3/commentThreads",
                params={"part": "snippet", "allThreadsRelatedToChannelId": channel_id, "maxResults": 5, "textFormat": "plainText"},
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            if comments_res.status_code == 200:
                diagnostics["youtube_comments"].update({
                    "status": "ready",
                    "message": "YouTube commentThreads endpoint is accessible.",
                    "http_status": comments_res.status_code,
                    "channel_title": ((channels[0].get("snippet") or {}).get("title") if channels else ""),
                    "sample_count": len(comments_res.json().get("items") or []),
                    "has_force_ssl_scope": "https://www.googleapis.com/auth/youtube.force-ssl" in scopes,
                    "has_readonly_scope": "https://www.googleapis.com/auth/youtube.readonly" in scopes,
                })
            else:
                diagnostics["youtube_comments"].update({
                    "status": "blocked",
                    "message": f"YouTube comments endpoint returned HTTP {comments_res.status_code}.",
                    "http_status": comments_res.status_code,
                })
        except Exception as e:
            diagnostics["youtube_comments"].update({"status": "blocked", "message": str(e)})
    else:
        diagnostics["youtube_comments"].update({"status": "needs_credentials", "message": "Missing YouTube OAuth credentials."})

    if diagnostics["postproxy"]["configured"]:
        try:
            state = sync_postproxy_account(settings)
            platforms = sorted({
                platform_to_postproxy(p.get("platform") or "")
                for p in (state.get("profiles") or [])
                if p.get("platform")
            })
            diagnostics["postproxy"].update({
                "status": "ready",
                "message": "PostProxy API is accessible.",
                "profile_group_id": state.get("profile_group_id") or "",
                "groups_count": len(state.get("groups") or []),
                "profiles_count": len(state.get("profiles") or []),
                "platforms": platforms,
                "socials": build_social_channel_status(settings, state),
                "enabled": bool(settings.get("postproxy_enabled")),
                "key_valid": state.get("key_valid"),
            })
        except Exception as e:
            diagnostics["postproxy"].update({"status": "blocked", "message": sanitize_postproxy_error(str(e))})
    else:
        diagnostics["postproxy"].update({"status": "needs_credentials", "message": "Add PostProxy API Key in Settings."})

    return {"status": "SUCCESS", "diagnostics": diagnostics, "checked_at": datetime.now().isoformat()}

def merge_queue_item(item: dict):
    queue = load_engagement_queue()
    source_key = item.get("source_id") or item.get("source_tweet_id") or item.get("source_comment_id")
    if source_key and any((q.get("source_id") or q.get("source_tweet_id") or q.get("source_comment_id")) == source_key for q in queue):
        return False
    item.setdefault("id", str(uuid.uuid4()))
    item.setdefault("status", "PENDING_REVIEW")
    item.setdefault("created_at", datetime.now().isoformat())
    queue.append(item)
    save_engagement_queue(queue)
    return True

def fetch_instagram_comments(settings: dict) -> int:
    if not connected(settings, ["instagram_access_token", "instagram_business_account_id"]):
        return 0
    token = settings["instagram_access_token"]
    ig_id = settings["instagram_business_account_id"]
    count = 0
    try:
        res = requests.get(
            f"https://graph.facebook.com/v21.0/{ig_id}/media",
            params={
                "fields": "id,caption,permalink,timestamp,comments.limit(20){id,text,username,timestamp}",
                "limit": 10,
                "access_token": token
            },
            timeout=30
        )
        if res.status_code != 200:
            logger.warning(f"Instagram comments fetch failed: {res.text}")
            return 0
        for media in res.json().get("data", []):
            for comment in (media.get("comments") or {}).get("data", []):
                text = comment.get("text") or ""
                username = comment.get("username") or "instagram_user"
                try:
                    reply = draft_engagement_reply(text, username, settings)
                except Exception:
                    reply = ""
                if merge_queue_item({
                    "platform": "instagram",
                    "source_id": f"instagram:{comment.get('id')}",
                    "source_comment_id": comment.get("id"),
                    "source_author": username,
                    "source_text": text,
                    "source_url": media.get("permalink", ""),
                    "drafted_reply": reply,
                    "reply_capability": "instagram_comment_reply"
                }):
                    count += 1
    except Exception as e:
        logger.warning(f"Failed to fetch Instagram comments: {e}")
    return count

def fetch_facebook_comments(settings: dict) -> int:
    if not connected(settings, ["facebook_page_access_token", "facebook_page_id"]):
        return 0
    token = settings["facebook_page_access_token"]
    page_id = settings["facebook_page_id"]
    count = 0
    try:
        res = requests.get(
            f"https://graph.facebook.com/v21.0/{page_id}/posts",
            params={
                "fields": "id,message,permalink_url,created_time,comments.limit(20){id,message,from,created_time}",
                "limit": 10,
                "access_token": token
            },
            timeout=30
        )
        if res.status_code != 200:
            logger.warning(f"Facebook comments fetch failed: {res.text}")
            return 0
        for post in res.json().get("data", []):
            for comment in (post.get("comments") or {}).get("data", []):
                text = comment.get("message") or ""
                author = (comment.get("from") or {}).get("name") or "facebook_user"
                try:
                    reply = draft_engagement_reply(text, author, settings)
                except Exception:
                    reply = ""
                if merge_queue_item({
                    "platform": "facebook",
                    "source_id": f"facebook:{comment.get('id')}",
                    "source_comment_id": comment.get("id"),
                    "source_author": author,
                    "source_text": text,
                    "source_url": post.get("permalink_url", ""),
                    "drafted_reply": reply,
                    "reply_capability": "facebook_comment_reply"
                }):
                    count += 1
    except Exception as e:
        logger.warning(f"Failed to fetch Facebook comments: {e}")
    return count

def youtube_access_token(settings: dict) -> Optional[str]:
    try:
        return get_youtube_access_token(settings)
    except Exception as e:
        logger.warning(f"Failed to refresh YouTube token: {e}")
        return None

def fetch_youtube_comments(settings: dict) -> int:
    if not connected(settings, ["youtube_client_id", "youtube_client_secret", "youtube_refresh_token"]):
        return 0
    token = youtube_access_token(settings)
    if not token:
        return 0
    headers = {"Authorization": f"Bearer {token}"}
    count = 0
    try:
        channel_res = requests.get(
            "https://www.googleapis.com/youtube/v3/channels",
            params={"part": "id", "mine": "true"},
            headers=headers,
            timeout=30
        )
        if channel_res.status_code != 200:
            logger.warning(f"YouTube channel fetch failed: {channel_res.text}")
            return 0
        items = channel_res.json().get("items", [])
        if not items:
            return 0
        channel_id = items[0]["id"]
        comments_res = requests.get(
            "https://www.googleapis.com/youtube/v3/commentThreads",
            params={"part": "snippet", "allThreadsRelatedToChannelId": channel_id, "maxResults": 20, "order": "time"},
            headers=headers,
            timeout=30
        )
        if comments_res.status_code != 200:
            logger.warning(f"YouTube comments fetch failed: {comments_res.text}")
            return 0
        for thread in comments_res.json().get("items", []):
            top = thread.get("snippet", {}).get("topLevelComment", {})
            snippet = top.get("snippet", {})
            text = snippet.get("textDisplay") or snippet.get("textOriginal") or ""
            author = snippet.get("authorDisplayName") or "youtube_user"
            try:
                reply = draft_engagement_reply(text, author, settings)
            except Exception:
                reply = ""
            if merge_queue_item({
                "platform": "youtube",
                "source_id": f"youtube:{top.get('id')}",
                "source_comment_id": top.get("id"),
                "source_author": author,
                "source_text": text,
                "source_url": f"https://www.youtube.com/watch?v={snippet.get('videoId', '')}",
                "drafted_reply": reply,
                "reply_capability": "youtube_comment_reply"
            }):
                count += 1
    except Exception as e:
        logger.warning(f"Failed to fetch YouTube comments: {e}")
    return count

def fetch_all_social_inbox(settings: dict) -> dict:
    before = len(load_engagement_queue())
    fetch_and_draft_mention_replies(settings)
    counts = {
        "instagram": fetch_instagram_comments(settings),
        "facebook": fetch_facebook_comments(settings),
        "youtube": fetch_youtube_comments(settings),
    }
    after = len(load_engagement_queue())
    counts["total_new"] = max(0, after - before)
    return counts

def extract_json_object(text: str) -> Any:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    start_candidates = [idx for idx in (cleaned.find("{"), cleaned.find("[")) if idx >= 0]
    if not start_candidates:
        raise ValueError("No JSON object found in model response.")
    start = min(start_candidates)
    end = max(cleaned.rfind("}"), cleaned.rfind("]"))
    if end <= start:
        raise ValueError("Incomplete JSON object in model response.")
    return json.loads(cleaned[start:end + 1])

def call_gemini_json(settings: dict, prompt: str, fallback: Any, use_search: bool = True) -> Any:
    api_key = settings.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("Missing Gemini API key.")
    client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=180000))
    config_kwargs = {}
    if use_search:
        config_kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]
        prompt = f"{prompt}\n\nReturn only valid JSON. Do not wrap it in Markdown."
    else:
        config_kwargs["response_mime_type"] = "application/json"
    response = client.models.generate_content(
        model="gemini-3.6-flash",
        contents=prompt,
        config=types.GenerateContentConfig(**config_kwargs)
    )
    try:
        return extract_json_object(response.text)
    except Exception as e:
        logger.warning(f"Failed to parse Gemini JSON: {e}; text={response.text[:500]}")
        return fallback

def refresh_live_listening_and_competitors(state: dict, settings: dict) -> dict:
    topics = [t for t in state.get("listening_topics", []) if t.get("keyword")]
    competitors = [c for c in state.get("competitors", []) if c.get("name") or c.get("handle")]
    changed = {"listening_signals": 0, "competitors": 0}
    if topics:
        topic_text = ", ".join(t.get("keyword", "") for t in topics[:12])
        prompt = f"""
        Search the live web and social web for current public social media signals about: {topic_text}.
        Return JSON only in this shape:
        {{"signals":[{{"topic":"...","platform":"...","url":"...","author":"...","summary":"...","metric":"...","detected_at":"..."}}]}}
        Use real public URLs only. If a URL cannot be verified, omit that item.
        """
        data = call_gemini_json(settings, prompt, {"signals": []})
        signals = data.get("signals", []) if isinstance(data, dict) else []
        for signal in signals:
            signal.setdefault("id", str(uuid.uuid4()))
            signal.setdefault("detected_at", datetime.now().isoformat())
        state["listening_signals"] = signals[:40]
        changed["listening_signals"] = len(state["listening_signals"])
    if competitors:
        comp_text = ", ".join((c.get("handle") or c.get("name") or "") for c in competitors[:12])
        prompt = f"""
        Search live public social platforms for competitor posting intelligence about: {comp_text}.
        Return JSON only in this shape:
        {{"competitors":[{{"name":"...","handle":"...","platform":"...","posting_frequency":"...","top_post_url":"...","top_post_summary":"...","engagement_velocity":"...","format_patterns":["..."],"last_checked":"..."}}]}}
        Use real URLs and real observations only.
        """
        data = call_gemini_json(settings, prompt, {"competitors": []})
        refreshed = data.get("competitors", []) if isinstance(data, dict) else []
        if refreshed:
            for row in refreshed:
                row.setdefault("id", str(uuid.uuid4()))
                row.setdefault("last_checked", datetime.now().isoformat())
            state["competitors"] = refreshed[:40]
            changed["competitors"] = len(state["competitors"])
    return changed

def list_generated_assets() -> List[dict]:
    assets = []
    if not os.path.isdir(GENERATED_DIR):
        return assets
    for name in sorted(os.listdir(GENERATED_DIR), reverse=True):
        if not name.lower().endswith((".mp4", ".png", ".jpg", ".jpeg", ".webp")):
            continue
        path = os.path.join(GENERATED_DIR, name)
        try:
            stat = os.stat(path)
        except OSError:
            continue
        kind = "video" if name.lower().endswith(".mp4") else "image"
        assets.append({
            "id": name,
            "name": name,
            "type": kind,
            "url": f"/static/assets/generated/{name}",
            "size": stat.st_size,
            "created_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "tags": ["generated", kind]
        })
    return assets[:80]

def build_calendar_items() -> List[dict]:
    items = []
    for post in load_scheduled_posts():
        items.append({
            "id": post.get("id"),
            "title": post.get("campaign_title") or "Untitled post",
            "platform": post.get("platform"),
            "scheduled_time": post.get("scheduled_time"),
            "status": post.get("status"),
            "video_path": post.get("video_path") or post.get("vertical_video_path"),
            "approval_status": "awaiting" if post.get("status") == "AWAITING_APPROVAL" else "none"
        })
    return sorted(items, key=lambda x: x.get("scheduled_time") or "", reverse=True)

def build_auto_report() -> dict:
    analytics = get_analytics_summary()
    posts = load_scheduled_posts()
    pending = len([p for p in posts if p.get("status") == "PENDING"])
    awaiting = len([p for p in posts if p.get("status") == "AWAITING_APPROVAL"])
    failed = len([p for p in posts if p.get("status") == "FAILED"])
    success = len([p for p in posts if p.get("status") == "SUCCESS"])
    top_posts = analytics.get("posts", [])[:5]
    return {
        "id": str(uuid.uuid4()),
        "created_at": datetime.now().isoformat(),
        "summary": {
            "scheduled": pending,
            "awaiting_approval": awaiting,
            "published": success,
            "failed": failed,
            "best_hour": analytics.get("best_hour"),
            "sample_size": analytics.get("sample_size", 0)
        },
        "top_posts": top_posts,
        "recommendations": [
            "Turn high-performing trend scans into multi-scene campaigns.",
            "Recycle top evergreen posts every 30-45 days.",
            "Use Hook Burst or Trend Radar templates on posts with strong metrics.",
            "Route high-intent comments into the CRM follow-up queue."
        ]
    }

def write_growth_report_files(report: dict) -> dict:
    report_id = report["id"]
    pdf_path = os.path.join(REPORTS_DIR, f"{report_id}.pdf")
    html_path = os.path.join(REPORTS_DIR, f"{report_id}.html")
    summary = report.get("summary", {})

    html_body = f"""<!doctype html><html><head><meta charset="utf-8"><title>6Frame Growth Report</title>
    <style>body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0a0a0c;color:#f8fafc;padding:40px;line-height:1.5}}.card{{border:1px solid #333;padding:20px;border-radius:10px;margin:14px 0;background:#111116}}a{{color:#7dd3fc}}</style></head>
    <body><h1>6Frame Growth Report</h1><p>{html.escape(report.get('created_at',''))}</p>
    <div class="card"><h2>Summary</h2><p>Scheduled: {summary.get('scheduled',0)} | Awaiting approval: {summary.get('awaiting_approval',0)} | Published: {summary.get('published',0)} | Failed: {summary.get('failed',0)} | Best hour: {summary.get('best_hour') if summary.get('best_hour') is not None else 'N/A'} | Analytics sample: {summary.get('sample_size',0)}</p></div>
    <div class="card"><h2>Recommendations</h2><ul>{''.join(f'<li>{html.escape(str(r))}</li>' for r in report.get('recommendations', []))}</ul></div>
    <div class="card"><h2>Top Posts</h2>{''.join(f'<p><strong>{html.escape(str(p.get("campaign_title","Post")))}</strong><br>Score: {html.escape(str(p.get("engagement_score","")))} | Platform: {html.escape(str(p.get("platform","")))}</p>' for p in report.get('top_posts', [])) or '<p>No scored posts yet.</p>'}</div>
    </body></html>"""
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_body)

    pdf_lines = [
        "6Frame Growth Report",
        report.get("created_at", ""),
        "",
        f"Scheduled: {summary.get('scheduled', 0)}",
        f"Awaiting approval: {summary.get('awaiting_approval', 0)}",
        f"Published: {summary.get('published', 0)}",
        f"Failed: {summary.get('failed', 0)}",
        f"Best hour: {summary.get('best_hour') if summary.get('best_hour') is not None else 'N/A'}",
        f"Analytics sample: {summary.get('sample_size', 0)}",
        "",
        "Recommendations:",
        *[f"- {rec}" for rec in report.get("recommendations", [])],
        "",
        "Top Posts:",
    ]
    if report.get("top_posts"):
        pdf_lines.extend([f"- {p.get('campaign_title', 'Post')} | score {p.get('engagement_score', '')}" for p in report["top_posts"]])
    else:
        pdf_lines.append("- No scored posts yet.")
    write_simple_pdf(pdf_path, pdf_lines)
    return {
        "pdf_path": f"/reports/{os.path.basename(pdf_path)}",
        "html_path": f"/reports/{os.path.basename(html_path)}",
    }

def write_simple_pdf(path: str, lines: List[str]):
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import inch
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

        doc = SimpleDocTemplate(
            path,
            pagesize=letter,
            rightMargin=0.65 * inch,
            leftMargin=0.65 * inch,
            topMargin=0.65 * inch,
            bottomMargin=0.65 * inch,
        )
        styles = getSampleStyleSheet()
        styles.add(ParagraphStyle(
            name="ReportTitle",
            parent=styles["Title"],
            textColor=colors.HexColor("#111827"),
            fontSize=22,
            leading=26,
            spaceAfter=14,
        ))
        styles.add(ParagraphStyle(
            name="ReportBody",
            parent=styles["BodyText"],
            fontSize=10,
            leading=14,
            textColor=colors.HexColor("#1f2937"),
        ))
        story = [Paragraph(html.escape(lines[0] if lines else "6Frame Growth Report"), styles["ReportTitle"])]
        if len(lines) > 1:
            story.append(Paragraph(html.escape(lines[1]), styles["ReportBody"]))
            story.append(Spacer(1, 0.2 * inch))
        summary_rows = []
        rec_lines = []
        top_lines = []
        section = "summary"
        for line in lines[2:]:
            if line == "Recommendations:":
                section = "recommendations"
                continue
            if line == "Top Posts:":
                section = "top"
                continue
            if not line:
                continue
            if section == "summary" and ":" in line:
                key, value = line.split(":", 1)
                summary_rows.append([Paragraph(f"<b>{html.escape(key)}</b>", styles["ReportBody"]), Paragraph(html.escape(value.strip()), styles["ReportBody"])])
            elif section == "recommendations":
                rec_lines.append(line.lstrip("- "))
            else:
                top_lines.append(line.lstrip("- "))
        if summary_rows:
            story.append(Table(summary_rows, colWidths=[2.0 * inch, 4.0 * inch], style=[
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f3f4f6")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d1d5db")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("PADDING", (0, 0), (-1, -1), 7),
            ]))
            story.append(Spacer(1, 0.25 * inch))
        if rec_lines:
            story.append(Paragraph("Recommendations", styles["Heading2"]))
            for rec in rec_lines:
                story.append(Paragraph(f"- {html.escape(rec)}", styles["ReportBody"]))
            story.append(Spacer(1, 0.18 * inch))
        if top_lines:
            story.append(Paragraph("Top Posts", styles["Heading2"]))
            for top in top_lines:
                story.append(Paragraph(f"- {html.escape(top)}", styles["ReportBody"]))
        doc.build(story)
        return
    except Exception as e:
        logger.warning(f"ReportLab PDF generation failed, falling back to simple PDF: {e}")

    def esc(text: str) -> str:
        return str(text).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    content_lines = ["BT", "/F1 13 Tf", "50 750 Td", "16 TL"]
    for idx, line in enumerate(lines[:42]):
        if idx:
            content_lines.append("T*")
        content_lines.append(f"({esc(line)}) Tj")
    content_lines.append("ET")
    stream = "\n".join(content_lines).encode("latin-1", "replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf.extend(f"{i} 0 obj\n".encode())
        pdf.extend(obj)
        pdf.extend(b"\nendobj\n")
    xref = len(pdf)
    pdf.extend(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        pdf.extend(f"{offset:010d} 00000 n \n".encode())
    pdf.extend(f"trailer << /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    with open(path, "wb") as f:
        f.write(pdf)

def report_email_config(settings: Optional[dict] = None) -> dict:
    settings = settings or load_settings()
    return {
        "provider": (os.environ.get("REPORT_EMAIL_PROVIDER") or settings.get("report_email_provider", "smtp") or "smtp").lower(),
        "to_addr": os.environ.get("REPORT_EMAIL_TO") or settings.get("report_email_to", ""),
        "resend_api_key": os.environ.get("RESEND_API_KEY") or settings.get("resend_api_key", ""),
        "resend_from": os.environ.get("RESEND_FROM") or settings.get("resend_from", ""),
        "host": os.environ.get("SMTP_HOST") or settings.get("smtp_host", ""),
        "port": int(os.environ.get("SMTP_PORT") or settings.get("smtp_port") or 587),
        "user": os.environ.get("SMTP_USER") or settings.get("smtp_user", ""),
        "password": os.environ.get("SMTP_PASSWORD") or settings.get("smtp_password", ""),
        "from_addr": os.environ.get("SMTP_FROM") or settings.get("smtp_from", ""),
        "tls": (str(os.environ.get("SMTP_TLS", settings.get("smtp_tls", True))).lower() != "false"),
        "source": "env" if os.environ.get("REPORT_EMAIL_TO") or os.environ.get("SMTP_HOST") else "settings",
    }

def send_growth_report_with_resend(report: dict, files: dict, cfg: dict) -> str:
    api_key = cfg.get("resend_api_key")
    to_addr = cfg.get("to_addr")
    from_addr = cfg.get("resend_from") or cfg.get("from_addr")
    if not api_key or not to_addr or not from_addr:
        return "not_configured"

    pdf_abs = os.path.join(REPORTS_DIR, os.path.basename(files["pdf_path"]))
    html_abs = os.path.join(REPORTS_DIR, os.path.basename(files["html_path"]))
    with open(pdf_abs, "rb") as f:
        pdf_content = base64.b64encode(f.read()).decode("ascii")
    with open(html_abs, "r", encoding="utf-8") as f:
        html_body = f.read()

    payload = {
        "from": from_addr,
        "to": [to_addr],
        "subject": f"6Frame Growth Report {report.get('created_at', '')[:10]}",
        "html": html_body,
        "attachments": [
            {
                "filename": os.path.basename(pdf_abs),
                "content": pdf_content,
            }
        ],
        "tags": [{"name": "source", "value": "growth_report"}],
    }
    res = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
    if res.status_code >= 400:
        return f"failed: Resend HTTP {res.status_code}: {res.text[:220]}"
    email_id = (res.json() or {}).get("id", "")
    return f"sent: resend:{email_id}" if email_id else "sent: resend"

def email_growth_report_if_configured(report: dict, files: dict, settings: Optional[dict] = None) -> str:
    cfg = report_email_config(settings)
    if cfg["provider"] == "resend":
        try:
            return send_growth_report_with_resend(report, files, cfg)
        except Exception as e:
            logger.warning(f"Resend growth report email failed: {e}")
            return f"failed: Resend {type(e).__name__}: {str(e)[:160]}"

    to_addr = cfg["to_addr"]
    host = cfg["host"]
    user = cfg["user"]
    password = cfg["password"]
    if not to_addr or not host:
        return "not_configured"
    try:
        import smtplib
        import socket
        from email.message import EmailMessage
        msg = EmailMessage()
        msg["Subject"] = f"6Frame Growth Report {report.get('created_at', '')[:10]}"
        msg["From"] = cfg["from_addr"] or user or "reports@6frame.local"
        msg["To"] = to_addr
        msg.set_content(
            f"Your 6Frame Growth Report is ready.\n\nPDF: {files.get('pdf_path')}\nHTML: {files.get('html_path')}\n"
        )
        pdf_abs = os.path.join(REPORTS_DIR, os.path.basename(files["pdf_path"]))
        with open(pdf_abs, "rb") as f:
            msg.add_attachment(f.read(), maintype="application", subtype="pdf", filename=os.path.basename(pdf_abs))
        original_getaddrinfo = socket.getaddrinfo
        addresses = [
            item for item in original_getaddrinfo(host, cfg["port"], socket.AF_INET, socket.SOCK_STREAM)
            if item[0] == socket.AF_INET
        ]
        if host.lower() in {"smtp.gmail.com", "smtp.googlemail.com"}:
            for ip in ("142.250.101.108", "142.251.116.108", "64.233.180.108"):
                fallback = (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, cfg["port"]))
                if all(existing[4][0] != ip for existing in addresses):
                    addresses.append(fallback)
        if not addresses:
            raise OSError(f"No IPv4 SMTP addresses resolved for {host}.")

        last_error = None
        for address in addresses[:4]:
            def selected_getaddrinfo(*args, **kwargs):
                return [address]
            socket.getaddrinfo = selected_getaddrinfo
            try:
                logger.info(f"Sending growth report email via {host}:{cfg['port']} using IPv4 {address[4][0]}")
                if cfg["port"] == 465:
                    smtp_context = smtplib.SMTP_SSL(host, cfg["port"], timeout=25)
                else:
                    smtp_context = smtplib.SMTP(host, cfg["port"], timeout=25)
                with smtp_context as smtp:
                    if cfg["tls"] and cfg["port"] != 465:
                        logger.info("Starting SMTP TLS session for growth report email.")
                        smtp.starttls()
                    if user and password:
                        logger.info("Authenticating SMTP session for growth report email.")
                        smtp.login(user, password)
                    logger.info("Sending growth report email message.")
                    smtp.send_message(msg)
                last_error = None
                break
            except Exception as attempt_error:
                last_error = attempt_error
                logger.warning(f"Growth report email SMTP attempt failed via {address[4][0]}: {attempt_error}")
            finally:
                socket.getaddrinfo = original_getaddrinfo
        if last_error:
            raise last_error
        return "sent"
    except Exception as e:
        logger.warning(f"Growth report email failed: {e}")
        return f"failed: {type(e).__name__}: {str(e)[:160]}"

def log_automation_event(state: dict, rule: dict, status: str, message: str, result: Optional[dict] = None):
    event = {
        "id": str(uuid.uuid4()),
        "rule_id": rule.get("id"),
        "trigger": rule.get("trigger"),
        "action": rule.get("action"),
        "status": status,
        "message": message,
        "result": result or {},
        "created_at": datetime.now().isoformat(),
    }
    state.setdefault("automation_events", []).insert(0, event)
    state["automation_events"] = state["automation_events"][:100]
    return event

def post_success_platforms(post: dict) -> List[str]:
    if post.get("postproxy_result", {}).get("platforms"):
        return [
            item.get("platform")
            for item in post["postproxy_result"]["platforms"]
            if item.get("status") in ("published", "processing", "processed", "scheduled")
        ]
    if post.get("status") == "SUCCESS":
        return resolve_platform_list(post.get("platform", ""))
    if post.get("status") == "PARTIAL_SUCCESS" and post.get("error_message"):
        msg = post.get("error_message", "")
        if "Success:" in msg:
            success_part = msg.split("Success:", 1)[1].split("Errors:", 1)[0]
            return [p.strip().strip(".;:").lower() for p in success_part.split(",") if p.strip()]
    return []

def seed_ab_test_from_latest_trend(state: dict) -> Optional[dict]:
    if state.get("ab_tests"):
        return None
    trends = state.get("last_trend_scan") or []
    if not trends:
        return None
    trend = trends[0]
    title = trend.get("title") or "Latest scanned trend"
    test = {
        "id": str(uuid.uuid4()),
        "name": f"Hook test: {title[:72]}",
        "source_trend_url": trend.get("url"),
        "metric": "engagement_score",
        "status": "draft",
        "variants": [
            {
                "id": "hook-a",
                "label": "Direct cinematic hook",
                "hook": (trend.get("recreated_twitter_thread") or [title])[0],
                "template_id": "hook_burst",
            },
            {
                "id": "hook-b",
                "label": "Trend context hook",
                "hook": trend.get("studio_adaptation_concept") or trend.get("original_concept") or title,
                "template_id": "trend_radar",
            },
        ],
        "created_at": datetime.now().isoformat(),
    }
    state.setdefault("ab_tests", []).append(test)
    return test

def run_growth_automation_rules(state: dict, settings: dict) -> List[dict]:
    events = []
    rules = [r for r in state.get("automation_rules", []) if r.get("enabled", True)]
    posts = load_scheduled_posts()
    inbox = load_engagement_queue()
    analytics = get_analytics_summary()

    for rule in rules:
        trigger = (rule.get("trigger") or "").lower()
        action = (rule.get("action") or "").lower()
        condition = (rule.get("condition") or "").lower()
        try:
            if "new inbox" in trigger or "new mention" in trigger or "comment" in trigger:
                if "crm" in action:
                    known = {c.get("handle") for c in state.get("crm_contacts", [])}
                    added = 0
                    for item in inbox:
                        handle = item.get("source_author")
                        if handle and handle not in known:
                            state.setdefault("crm_contacts", []).append({
                                "id": str(uuid.uuid4()),
                                "name": handle,
                                "handle": handle,
                                "platform": item.get("platform", "social"),
                                "labels": ["engaged"],
                                "notes": item.get("source_text", "")[:240],
                                "suggested_followup": item.get("drafted_reply", ""),
                                "created_at": datetime.now().isoformat(),
                            })
                            known.add(handle)
                            added += 1
                    events.append(log_automation_event(state, rule, "SUCCESS", f"Synced {added} inbox contacts to CRM.", {"added": added}))
                    continue

            if "evergreen" in trigger or "recycle" in action:
                successful = [p for p in posts if post_success_platforms(p) and p.get("text")]
                added = 0
                bucket = next((b for b in state.get("evergreen_buckets", []) if b.get("name", "").lower() == "auto recycled winners"), None)
                if not bucket:
                    bucket = {"id": str(uuid.uuid4()), "name": "Auto Recycled Winners", "cadence": "monthly", "recycle_days": 30, "items": [], "created_at": datetime.now().isoformat()}
                    state.setdefault("evergreen_buckets", []).append(bucket)
                existing = {i.get("post_id") for i in bucket.get("items", [])}
                for post in successful[:10]:
                    if post.get("id") not in existing:
                        bucket.setdefault("items", []).append({
                            "post_id": post.get("id"),
                            "title": post.get("campaign_title"),
                            "text": post.get("text"),
                            "successful_platforms": post_success_platforms(post),
                            "source_status": post.get("status"),
                            "added_at": datetime.now().isoformat(),
                        })
                        added += 1
                events.append(log_automation_event(state, rule, "SUCCESS", f"Added {added} winners to evergreen queue.", {"added": added}))
                continue

            if "ab" in trigger or "a/b" in trigger or "variant" in action:
                seeded = seed_ab_test_from_latest_trend(state)
                scored = analytics.get("posts", [])
                for test in state.get("ab_tests", []):
                    if test.get("status") == "draft" and scored:
                        test["status"] = "analyzed"
                        test["winner"] = scored[0].get("campaign_title")
                        test["updated_at"] = datetime.now().isoformat()
                    elif test.get("status") == "draft":
                        test["status"] = "collecting_data"
                        test["updated_at"] = datetime.now().isoformat()
                events.append(log_automation_event(
                    state,
                    rule,
                    "SUCCESS",
                    f"Prepared/evaluated {len(state.get('ab_tests', []))} A/B tests.",
                    {"sample_size": analytics.get("sample_size", 0), "seeded_test_id": seeded.get("id") if seeded else None},
                ))
                continue

            if "report" in action:
                report = build_auto_report()
                files = write_growth_report_files(report)
                report.update(files)
                report["email_status"] = email_growth_report_if_configured(report, files, settings)
                state.setdefault("report_history", []).insert(0, report)
                state["report_history"] = state["report_history"][:20]
                events.append(log_automation_event(state, rule, "SUCCESS", "Generated report from automation rule.", {"report_id": report["id"], **files}))
                continue

            if "virality" in trigger and "generate" in action:
                trends = state.get("last_trend_scan") or []
                if not trends:
                    scan_job_id = str(uuid.uuid4())
                    run_live_trend_scanner(scan_job_id, settings)
                    scan_status = get_job_status(scan_job_id)
                    if scan_status.get("status") == "SUCCESS":
                        trends = (scan_status.get("result") or {}).get("trends", [])
                        state["last_trend_scan"] = trends[:25]
                        state["last_trend_scan_at"] = datetime.now().isoformat()
                if not trends:
                    events.append(log_automation_event(state, rule, "FAILED", "No verified trend scan result was available for virality-triggered generation."))
                    continue
                trend = trends[0]
                trend_key = trend.get("url") or trend.get("title")
                last_generation = state.get("last_automation_generation") or {}
                if last_generation.get("trend_key") == trend_key:
                    events.append(log_automation_event(state, rule, "SUCCESS", "Virality generation already completed for the latest top trend.", last_generation))
                    continue
                video_job_id = str(uuid.uuid4())
                engine = settings.get("autonomous_video_engine", "fal_hailuo_23")
                duration = normalize_video_duration(engine, int(settings.get("autonomous_video_duration", 10)))
                run_video_generation(
                    job_id=video_job_id,
                    prompt=trend.get("recreated_video_prompt") or trend.get("studio_adaptation_concept") or trend.get("title") or "cinematic AI trend recreation",
                    settings=settings,
                    engine=engine,
                    duration=duration,
                )
                video_status = get_job_status(video_job_id)
                if video_status.get("status") != "SUCCESS":
                    events.append(log_automation_event(state, rule, "FAILED", f"Virality-triggered video generation failed: {video_status.get('message')}"))
                    continue
                generation = {
                    "trend_key": trend_key,
                    "trend_title": trend.get("title"),
                    "trend_url": trend.get("url"),
                    "video_job_id": video_job_id,
                    "video_path": (video_status.get("result") or {}).get("video_path"),
                    "engine": engine,
                    "duration": duration,
                    "created_at": datetime.now().isoformat(),
                }
                state["last_automation_generation"] = generation
                events.append(log_automation_event(state, rule, "SUCCESS", "Generated a video from the latest persisted viral trend.", generation))
                continue

            events.append(log_automation_event(state, rule, "FAILED", "This rule does not match an executable trigger/action. Edit it to use inbox->CRM, evergreen recycle, A/B evaluate, report, or virality->generate."))
        except Exception as e:
            logger.exception("Growth automation rule failed")
            events.append(log_automation_event(state, rule, "FAILED", str(e)))

    save_growth_os(state)
    return events

class GrowthItemRequest(BaseModel):
    collection: str
    item: dict

class GrowthUpdateRequest(BaseModel):
    data: dict

class GrowthCollectionUpdateRequest(BaseModel):
    item: dict

class CampaignPlanRequest(BaseModel):
    business_url: Optional[str] = None
    goals: Optional[str] = None
    audience: Optional[str] = None

class UtmBuildRequest(BaseModel):
    base_url: str
    source: str = "social"
    medium: str = "organic"
    campaign: str = "6frame"
    content: Optional[str] = None

class MultiSceneVideoRequest(BaseModel):
    prompt: str
    engine: str = "fal_hailuo_02"
    scene_count: int = 3
    scene_duration: int = 6
    apply_template: bool = True
    template_id: Optional[str] = None
    title: Optional[str] = None
    subtitle: Optional[str] = None

@app.get("/api/growth-os")
def get_growth_os():
    state = load_growth_os()
    settings = load_settings()
    analytics = get_analytics_summary()
    inbox = get_engagement_queue()
    state["calendar"] = build_calendar_items()
    state["assets"] = list_generated_assets()
    state["reports_preview"] = build_auto_report()
    state["integrations"] = build_live_integration_status(settings, state)
    state["best_time"] = {
        "hour": analytics.get("best_hour"),
        "sample_size": analytics.get("sample_size", 0),
        "minimum_recommended_sample_size": analytics.get("minimum_recommended_sample_size", 8),
        "confidence": analytics.get("confidence", "none"),
        "hour_breakdown": analytics.get("hour_breakdown", {})
    }
    state["unified_inbox"] = inbox
    state["approval_items"] = get_approval_queue()
    state["listening_digest"] = {
        "active_topics": len([t for t in state.get("listening_topics", []) if t.get("status") == "active"]),
        "competitors_tracked": len(state.get("competitors", [])),
        "signals": state.get("listening_signals", [])[:10],
        "last_refresh": state.get("last_live_refresh")
    }
    state["automation_events"] = state.get("automation_events", [])[:50]
    return state

@app.post("/api/growth-os/live-refresh")
def refresh_growth_os_live(background_tasks: BackgroundTasks):
    settings = load_settings()
    def do_refresh():
        state = load_growth_os()
        inbox_counts = fetch_all_social_inbox(settings)
        research_counts = {"listening_signals": 0, "competitors": 0}
        try:
            research_counts = refresh_live_listening_and_competitors(state, settings)
        except Exception as e:
            logger.warning(f"Live listening/competitor refresh failed: {e}")
        latest = load_growth_os()
        latest["listening_signals"] = state.get("listening_signals", latest.get("listening_signals", []))
        latest["competitors"] = state.get("competitors", latest.get("competitors", []))
        latest["last_live_refresh"] = datetime.now().isoformat()
        latest["last_live_refresh_counts"] = {"inbox": inbox_counts, "research": research_counts}
        run_growth_automation_rules(latest, settings)
        save_growth_os(latest)
    background_tasks.add_task(do_refresh)
    return {"status": "SUCCESS", "message": "Live Growth OS refresh started."}

@app.post("/api/growth-os/item")
def add_growth_item(req: GrowthItemRequest):
    allowed = {
        "workspaces", "listening_topics", "competitors", "evergreen_buckets",
        "ab_tests", "crm_contacts", "utm_campaigns", "automation_rules", "integrations"
    }
    if req.collection not in allowed:
        raise HTTPException(status_code=400, detail="Unsupported Growth OS collection.")
    state = load_growth_os()
    item = req.item.copy()
    item.setdefault("id", str(uuid.uuid4()))
    item.setdefault("created_at", datetime.now().isoformat())
    state.setdefault(req.collection, []).append(item)
    save_growth_os(state)
    return {"status": "SUCCESS", "item": item}

@app.patch("/api/growth-os/item/{collection}/{item_id}")
def update_growth_item(collection: str, item_id: str, req: GrowthCollectionUpdateRequest):
    allowed = {
        "workspaces", "listening_topics", "competitors", "evergreen_buckets",
        "ab_tests", "crm_contacts", "utm_campaigns", "automation_rules", "integrations"
    }
    if collection not in allowed:
        raise HTTPException(status_code=400, detail="Unsupported Growth OS collection.")
    state = load_growth_os()
    items = state.get(collection, [])
    for item in items:
        if item.get("id") == item_id:
            item.update(req.item)
            item["updated_at"] = datetime.now().isoformat()
            save_growth_os(state)
            return {"status": "SUCCESS", "item": item}
    raise HTTPException(status_code=404, detail="Growth OS item not found.")

@app.delete("/api/growth-os/item/{collection}/{item_id}")
def delete_growth_item(collection: str, item_id: str):
    allowed = {
        "workspaces", "listening_topics", "competitors", "evergreen_buckets",
        "ab_tests", "crm_contacts", "utm_campaigns", "automation_rules", "integrations"
    }
    if collection not in allowed:
        raise HTTPException(status_code=400, detail="Unsupported Growth OS collection.")
    state = load_growth_os()
    items = state.get(collection, [])
    filtered = [item for item in items if item.get("id") != item_id]
    if len(filtered) == len(items):
        raise HTTPException(status_code=404, detail="Growth OS item not found.")
    state[collection] = filtered
    save_growth_os(state)
    return {"status": "SUCCESS", "message": "Growth OS item deleted."}

@app.patch("/api/growth-os/{section}")
def update_growth_section(section: str, req: GrowthUpdateRequest):
    allowed = {"brand_kit", "link_in_bio"}
    if section not in allowed:
        raise HTTPException(status_code=400, detail="Unsupported Growth OS section.")
    state = load_growth_os()
    existing = state.get(section, {})
    if isinstance(existing, dict):
        existing.update(req.data)
        state[section] = existing
    else:
        state[section] = req.data
    save_growth_os(state)
    return {"status": "SUCCESS", "section": state[section]}

@app.post("/api/growth-os/campaign-plan")
def generate_campaign_plan(req: CampaignPlanRequest):
    state = load_growth_os()
    settings = load_settings()
    start = datetime.now().date()
    prompt = f"""
    Build a 30-day social media campaign calendar from real current strategy context.
    Business URL: {req.business_url or settings.get("public_base_url") or "unknown"}
    Goals: {req.goals or "not specified"}
    Audience: {req.audience or "not specified"}
    Brand kit: {json.dumps(state.get("brand_kit", {}))}
    Recent listening signals: {json.dumps(state.get("listening_signals", [])[:12])}
    Competitor intelligence: {json.dumps(state.get("competitors", [])[:12])}

    Return JSON only:
    {{"plan":[{{"day":1,"date":"YYYY-MM-DD","platform":"linkedin|twitter|instagram|youtube|tiktok","theme":"...","hook":"...","asset_type":"...","template":"...","goal":"...","source_signal_url":"..."}}]}}
    Make exactly 30 entries starting on {start.isoformat()}. Use real signal URLs where available, otherwise empty string.
    """
    try:
        data = call_gemini_json(settings, prompt, {"plan": []})
        plan = data.get("plan", []) if isinstance(data, dict) else []
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"AI campaign planning failed: {str(e)}")
    if len(plan) != 30:
        raise HTTPException(status_code=502, detail="AI campaign planner did not return a complete 30-day plan.")
    for idx, item in enumerate(plan):
        item.setdefault("id", f"day-{idx + 1}")
        item["day"] = idx + 1
        item.setdefault("date", (start + timedelta(days=idx)).isoformat())
    state["campaign_plan"] = plan
    save_growth_os(state)
    return {"status": "SUCCESS", "plan": plan}

@app.post("/api/growth-os/report")
def create_growth_report():
    state = load_growth_os()
    settings = load_settings()
    report = build_auto_report()
    files = write_growth_report_files(report)
    report.update(files)
    report["email_status"] = email_growth_report_if_configured(report, files, settings)
    state.setdefault("report_history", []).insert(0, report)
    state["report_history"] = state["report_history"][:20]
    save_growth_os(state)
    return {"status": "SUCCESS", "report": report}

@app.post("/api/growth-os/run-automation-rules")
def run_growth_rules_endpoint():
    state = load_growth_os()
    settings = load_settings()
    events = run_growth_automation_rules(state, settings)
    return {"status": "SUCCESS", "events": events}

@app.post("/api/growth-os/utm")
def build_utm_link(req: UtmBuildRequest):
    from urllib.parse import urlencode
    state = load_growth_os()
    params = {
        "utm_source": req.source,
        "utm_medium": req.medium,
        "utm_campaign": req.campaign,
    }
    if req.content:
        params["utm_content"] = req.content
    separator = "&" if "?" in req.base_url else "?"
    url = f"{req.base_url}{separator}{urlencode(params)}"
    item = {
        "id": str(uuid.uuid4()),
        "base_url": req.base_url,
        "url": url,
        "tracked_url": f"/r/{{id}}",
        "source": req.source,
        "medium": req.medium,
        "campaign": req.campaign,
        "content": req.content,
        "clicks": 0,
        "created_at": datetime.now().isoformat()
    }
    item["tracked_url"] = f"/r/{item['id']}"
    state.setdefault("utm_campaigns", []).insert(0, item)
    save_growth_os(state)
    return {"status": "SUCCESS", "utm": item}

@app.get("/r/{link_id}")
def tracked_redirect(link_id: str):
    state = load_growth_os()
    for item in state.get("utm_campaigns", []):
        if item.get("id") == link_id:
            item["clicks"] = int(item.get("clicks") or 0) + 1
            item["last_click_at"] = datetime.now().isoformat()
            save_growth_os(state)
            return RedirectResponse(item.get("url") or item.get("base_url") or "/")
    raise HTTPException(status_code=404, detail="Tracked link not found.")

@app.get("/b/{slug}", response_class=HTMLResponse)
def link_in_bio_page(slug: str):
    state = load_growth_os()
    bio = state.get("link_in_bio", {})
    if slug != bio.get("slug", "6frame"):
        raise HTTPException(status_code=404, detail="Link-in-bio page not found.")
    links = "".join(
        f"""<a href="{html.escape(link.get('url', '#'))}" style="display:block;margin:12px 0;padding:14px 18px;border:1px solid rgba(255,255,255,.18);border-radius:10px;color:#fff;text-decoration:none;background:rgba(255,255,255,.06);">{html.escape(link.get('label', 'Link'))}</a>"""
        for link in bio.get("links", [])
    )
    featured = bio.get("featured_video")
    video_html = f"""<video src="{html.escape(featured)}" controls style="width:100%;border-radius:12px;margin:18px 0;"></video>""" if featured else ""
    return f"""<html><head><title>{html.escape(bio.get('headline', '6Frame Studio'))}</title>
    <meta name="viewport" content="width=device-width,initial-scale=1" /></head>
    <body style="margin:0;background:#0a0a0c;color:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
      <main style="max-width:520px;margin:0 auto;padding:42px 22px;text-align:center;">
        <h1>{html.escape(bio.get('headline', '6Frame Studio'))}</h1>
        {video_html}
        {links}
      </main>
    </body></html>"""

def run_multiscene_video_job(job_id: str, req: MultiSceneVideoRequest, settings: dict):
    try:
        scene_count = max(3, min(int(req.scene_count or 3), 9))
        max_duration_by_engine = {
            "fal_hailuo_23": 10,
            "fal_hailuo_02": 10,
            "fal_seedance_fast": 12,
            "fal_ltx_fast": 20,
            "google_veo_lite": 8,
            "google_veo": 8,
            "google_veo_fast": 8,
        }
        max_duration = max_duration_by_engine.get(req.engine, 10)
        default_scene = 8 if req.engine.startswith("google_veo") else (10 if max_duration >= 10 else max_duration)
        scene_duration = max(2, min(int(req.scene_duration or default_scene), max_duration))
        if req.engine.startswith("google_veo"):
            scene_duration = 8
        update_job_status(job_id, "PROCESSING", 5, f"Preparing {scene_count}-scene long-form render...")

        clip_paths = []
        for idx in range(scene_count):
            sub_job_id = f"{job_id}_scene_{idx + 1}"
            scene_prompt = (
                f"{req.prompt}\n\n"
                f"Scene {idx + 1} of {scene_count}: create a distinct shot with cinematic continuity, "
                f"no burned-in captions, social ad pacing, premium lighting."
            )
            update_job_status(job_id, "PROCESSING", 8 + idx, f"Rendering scene {idx + 1}/{scene_count}...")
            run_video_generation(
                job_id=sub_job_id,
                prompt=scene_prompt,
                settings=settings,
                engine=req.engine,
                duration=scene_duration
            )
            sub_status = get_job_status(sub_job_id)
            if sub_status.get("status") != "SUCCESS":
                raise RuntimeError(sub_status.get("message") or f"Scene {idx + 1} failed.")
            public_path = sub_status.get("result", {}).get("video_path")
            if not public_path:
                raise RuntimeError(f"Scene {idx + 1} did not return a video path.")
            clip_paths.append(resolve_local_video_path(public_path))
            progress = min(70, 10 + int(((idx + 1) / scene_count) * 60))
            update_job_status(job_id, "PROCESSING", progress, f"Scene {idx + 1}/{scene_count} complete.")

        concat_file_path = os.path.join(GENERATED_DIR, f"{job_id}_concat.txt")
        stitched_path = os.path.join(GENERATED_DIR, f"{job_id}_multiscene.mp4")
        with open(concat_file_path, "w") as cf:
            for clip_path in clip_paths:
                cf.write(f"file '{clip_path}'\n")

        update_job_status(job_id, "PROCESSING", 78, "Stitching scenes into one long-form video...")
        concat_result = subprocess.run(
            [
                get_binary_path("ffmpeg"), "-y", "-f", "concat", "-safe", "0", "-i", concat_file_path,
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart",
                stitched_path
            ],
            capture_output=True,
            text=True,
            timeout=120
        )
        if concat_result.returncode != 0 or not os.path.exists(stitched_path):
            raise RuntimeError(f"ffmpeg stitching failed: {concat_result.stderr}")
        if os.path.exists(concat_file_path):
            os.remove(concat_file_path)

        final_path = stitched_path
        template_info = None
        if req.apply_template:
            update_job_status(job_id, "PROCESSING", 88, "Applying internal HyperFrames template...")
            templated_path = os.path.join(GENERATED_DIR, f"{job_id}_multiscene_template.mp4")
            template_info = render_template_video(
                source_video_path=stitched_path,
                output_path=templated_path,
                template_id=req.template_id or settings.get("viral_template_style", "hook_burst"),
                title=req.title or "Multi-Scene Campaign",
                subtitle=req.subtitle or "Generated by 6Frame Studio Growth OS",
                work_root=os.path.join(UPLOAD_DIR, "template_renders"),
                quality=settings.get("viral_template_quality", "standard"),
                timeout=360
            )
            final_path = templated_path

        public_final = f"/static/assets/generated/{os.path.basename(final_path)}"
        update_job_status(
            job_id,
            "SUCCESS",
            100,
            "Multi-scene video complete.",
            {
                "video_path": public_final,
                "scene_count": scene_count,
                "scene_duration": scene_duration,
                "engine": req.engine,
                "clips": [f"/static/assets/generated/{os.path.basename(path)}" for path in clip_paths],
                "template": template_info,
            }
        )
    except Exception as e:
        logger.exception("Multi-scene video generation failed")
        update_job_status(job_id, "FAILED", 0, f"Multi-scene video failed: {str(e)}")

@app.post("/api/growth-os/multiscene-video")
def create_multiscene_video(req: MultiSceneVideoRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    settings = load_settings()
    update_job_status(job_id, "PENDING", 0, "Multi-scene video job queued...")
    background_tasks.add_task(run_multiscene_video_job, job_id, req, settings)
    return {"job_id": job_id}

# ==========================================================================
# ENGAGEMENT INBOX — AI-drafted replies to Twitter/X mentions, queued here
# for approval before sending. Nothing is auto-sent.
# ==========================================================================

@app.get("/api/engagement-queue")
def get_engagement_queue():
    items = load_engagement_queue()
    pending = [i for i in items if i["status"] == "PENDING_REVIEW"]
    pending.sort(key=lambda x: x["created_at"], reverse=True)
    return pending

class EngagementEditRequest(BaseModel):
    reply_text: str

@app.patch("/api/engagement-queue/{item_id}")
def edit_engagement_reply(item_id: str, req: EngagementEditRequest):
    items = load_engagement_queue()
    for item in items:
        if item["id"] == item_id:
            if item["status"] != "PENDING_REVIEW":
                raise HTTPException(status_code=400, detail="Reply is no longer pending review.")
            item["drafted_reply"] = req.reply_text
            save_engagement_queue(items)
            return {"status": "SUCCESS", "message": "Draft reply updated."}
    raise HTTPException(status_code=404, detail="Item not found in engagement queue.")

@app.post("/api/engagement-queue/{item_id}/send")
def send_engagement_reply(item_id: str):
    items = load_engagement_queue()
    target = next((i for i in items if i["id"] == item_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Item not found in engagement queue.")
    if target["status"] != "PENDING_REVIEW":
        raise HTTPException(status_code=400, detail="Reply is no longer pending review.")

    settings = load_settings()
    try:
        if settings.get("mock_mode", True):
            logger.info("[MOCK] Sending engagement reply")
        elif target.get("platform") == "twitter":
            client = tweepy.Client(
                consumer_key=settings["twitter_consumer_key"],
                consumer_secret=settings["twitter_consumer_secret"],
                access_token=settings["twitter_access_token"],
                access_token_secret=settings["twitter_access_token_secret"]
            )
            client.create_tweet(text=target["drafted_reply"], in_reply_to_tweet_id=target["source_tweet_id"])
        elif target.get("platform") == "instagram":
            if not settings.get("instagram_access_token") or not target.get("source_comment_id"):
                raise ValueError("Missing Instagram token or comment ID.")
            res = requests.post(
                f"https://graph.facebook.com/v21.0/{target['source_comment_id']}/replies",
                data={"message": target["drafted_reply"], "access_token": settings["instagram_access_token"]},
                timeout=30
            )
            if res.status_code != 200:
                raise ValueError(f"Instagram reply failed: {res.text}")
        elif target.get("platform") == "facebook":
            if not settings.get("facebook_page_access_token") or not target.get("source_comment_id"):
                raise ValueError("Missing Facebook page token or comment ID.")
            res = requests.post(
                f"https://graph.facebook.com/v21.0/{target['source_comment_id']}/comments",
                data={"message": target["drafted_reply"], "access_token": settings["facebook_page_access_token"]},
                timeout=30
            )
            if res.status_code != 200:
                raise ValueError(f"Facebook reply failed: {res.text}")
        elif target.get("platform") == "youtube":
            token = youtube_access_token(settings)
            if not token or not target.get("source_comment_id"):
                raise ValueError("Missing YouTube token or comment ID.")
            res = requests.post(
                "https://www.googleapis.com/youtube/v3/comments",
                params={"part": "snippet"},
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"snippet": {"parentId": target["source_comment_id"], "textOriginal": target["drafted_reply"]}},
                timeout=30
            )
            if res.status_code not in (200, 201):
                raise ValueError(f"YouTube reply failed: {res.text}")
        elif settings.get("postproxy_enabled") and (settings.get("postproxy_api_key") or os.environ.get("POSTPROXY_API_KEY")):
            target["postproxy_reply_result"] = postproxy_reply_to_comment(settings, target)
        else:
            raise ValueError(f"Sending replies is not implemented for platform: {target.get('platform')}")
        target["status"] = "SENT"
        target["sent_at"] = datetime.now().isoformat()
        save_engagement_queue(items)
        return {"status": "SUCCESS", "message": "Reply sent."}
    except Exception as e:
        logger.exception("Failed to send engagement reply")
        raise HTTPException(status_code=500, detail=f"Failed to send reply: {str(e)}")

@app.post("/api/engagement-queue/{item_id}/dismiss")
def dismiss_engagement_reply(item_id: str):
    items = load_engagement_queue()
    for item in items:
        if item["id"] == item_id:
            if item["status"] != "PENDING_REVIEW":
                raise HTTPException(status_code=400, detail="Reply is no longer pending review.")
            item["status"] = "DISMISSED"
            save_engagement_queue(items)
            return {"status": "SUCCESS", "message": "Reply dismissed."}
    raise HTTPException(status_code=404, detail="Item not found in engagement queue.")

@app.post("/api/engagement-queue/refresh")
def refresh_engagement_queue(background_tasks: BackgroundTasks):
    settings = load_settings()
    background_tasks.add_task(fetch_all_social_inbox, settings)
    return {"status": "SUCCESS", "message": "Checking connected social inboxes in the background."}

# TikTok domain (URL prefix) verification file — for the Content Posting API's
# pull_by_url requirement
@app.get("/tiktokfa6qOoVQvk1SxaIGW7xhpCLHQf0Ek3JZ.txt", response_class=PlainTextResponse)
def tiktok_domain_verification():
    return "tiktok-developers-site-verification=fa6qOoVQvk1SxaIGW7xhpCLHQf0Ek3JZ"

# TikTok URL-prefix verification file — for the app's URL properties (Terms of
# Service / Privacy Policy / Web-Desktop URL), required for the Content Posting
# API production audit submission
@app.get("/tiktok9QQRKWG0STxFZwZJveeoFpXDjPZV5rLy.txt", response_class=PlainTextResponse)
def tiktok_url_properties_verification():
    return "tiktok-developers-site-verification=9QQRKWG0STxFZwZJveeoFpXDjPZV5rLy"

# TikTok OAuth callback — TikTok's sandbox rejects localhost redirect URIs, so this
# public endpoint receives the code instead and displays it for manual copy-paste
# into tiktok_auth.py running locally.
@app.get("/tiktok-callback", response_class=HTMLResponse)
def tiktok_oauth_callback(code: str = None, error: str = None, error_description: str = None):
    if code:
        body = f"""
        <div style="font-family:-apple-system,sans-serif;background:#0f172a;color:#f8fafc;text-align:center;padding:50px;">
            <div style="background:#1e293b;padding:30px;border-radius:8px;display:inline-block;max-width:600px;">
                <h1 style="color:#10b981;">Authorization Successful!</h1>
                <p>Copy this code and paste it back into your terminal:</p>
                <textarea readonly style="width:100%;height:60px;font-family:monospace;font-size:14px;padding:10px;">{code}</textarea>
            </div>
        </div>
        """
    else:
        body = f"""
        <div style="font-family:-apple-system,sans-serif;background:#0f172a;color:#f8fafc;text-align:center;padding:50px;">
            <h1 style="color:#ef4444;">Authorization Failed</h1>
            <p>Error: {error_description or error or 'Unknown error'}</p>
        </div>
        """
    return f"<html><head></head><body>{body}</body></html>"

LEGAL_PAGE_STYLE = """
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #0a0a0c;
         color: #e5e5ea; max-width: 780px; margin: 0 auto; padding: 48px 24px 96px; line-height: 1.6; }
  h1 { color: #fff; font-size: 1.75rem; margin-bottom: 0.25rem; }
  h2 { color: #fff; font-size: 1.15rem; margin-top: 2rem; }
  p, li { color: #c7c7cf; font-size: 0.95rem; }
  .updated { color: #8b8b96; font-size: 0.85rem; margin-bottom: 2rem; }
  a { color: #a855f7; }
</style>
"""

@app.get("/privacy-policy", response_class=HTMLResponse)
def privacy_policy():
    return f"""<html><head><title>Privacy Policy — 6Frame Studio</title>{LEGAL_PAGE_STYLE}</head><body>
    <h1>Privacy Policy</h1>
    <p class="updated">Last updated: 2026</p>
    <p>6Frame Studio ("we", "us", "our") operates this marketing automation application, which uses
    generative AI to create and publish video and text content to connected social media accounts
    (Twitter/X, LinkedIn, Instagram, TikTok, YouTube, Facebook) on behalf of the account owner.</p>

    <h2>Information We Collect</h2>
    <ul>
      <li>OAuth access and refresh tokens for the social media accounts you explicitly connect,
      used solely to publish content to those accounts on your behalf.</li>
      <li>Publicly available social media content (post text, video links, engagement metrics)
      retrieved via each platform's official API for the purpose of trend research and analytics.</li>
      <li>Content you upload or generate within the application (videos, captions, prompts).</li>
    </ul>

    <h2>How We Use Information</h2>
    <ul>
      <li>To publish content to the social media accounts you have connected and authorized.</li>
      <li>To analyze engagement metrics on published content and refine future content strategy.</li>
      <li>To identify publicly trending content relevant to the account owner's niche for the
      purpose of creating original, credited, brand-voice adaptations.</li>
    </ul>

    <h2>Data Sharing</h2>
    <p>We do not sell or share your data with third parties, except as required to operate the
    connected platform integrations themselves (e.g. sending a video to TikTok's API to publish it)
    or as required by law.</p>

    <h2>Data Retention &amp; Security</h2>
    <p>Access tokens are stored securely on our hosting infrastructure and are only used to
    perform actions you have explicitly configured (e.g. autonomous posting). You may revoke
    access at any time from the connected platform's own account settings, which immediately
    invalidates our stored token.</p>

    <h2>AI-Generated Content</h2>
    <p>This application uses generative AI (including Google's Gemini and Veo models) to create
    video and text content. Where a platform requires disclosure of AI-generated content, we label
    published content accordingly.</p>

    <h2>Contact</h2>
    <p>Questions about this policy can be directed to {os.environ.get("CONTACT_EMAIL", "the account owner")}.</p>
    </body></html>"""

@app.get("/terms-of-service", response_class=HTMLResponse)
def terms_of_service():
    return f"""<html><head><title>Terms of Service — 6Frame Studio</title>{LEGAL_PAGE_STYLE}</head><body>
    <h1>Terms of Service</h1>
    <p class="updated">Last updated: 2026</p>
    <p>By using 6Frame Studio's marketing automation application, you agree to the following terms.</p>

    <h2>Purpose</h2>
    <p>This application is an internal marketing automation tool that generates AI-driven video
    and text content and publishes it to social media accounts explicitly connected and authorized
    by the account owner. It is operated for 6Frame Studio's own marketing use.</p>

    <h2>Acceptable Use</h2>
    <p>Connected platform integrations (Twitter/X, LinkedIn, Instagram, TikTok, YouTube, Facebook)
    are used strictly in accordance with each platform's own developer terms, API terms of service,
    and content policies, including any required disclosure of AI-generated or synthetic content.</p>

    <h2>Content Ownership</h2>
    <p>All content generated and published through this application is owned by 6Frame Studio.
    Where content is inspired by or recreates a publicly trending post, the original creator is
    credited within the published caption.</p>

    <h2>No Warranty</h2>
    <p>This application is provided as-is, without warranty of any kind, for the purpose of
    automating 6Frame Studio's own marketing content pipeline.</p>

    <h2>Changes</h2>
    <p>These terms may be updated from time to time to reflect changes in how the application
    operates or in connected platforms' own requirements.</p>

    <h2>Contact</h2>
    <p>Questions about these terms can be directed to {os.environ.get("CONTACT_EMAIL", "the account owner")}.</p>
    </body></html>"""

# Mount durable generated assets before the broader static folder so legacy
# /static/assets/generated/... URLs resolve to the Railway volume.
app.mount("/static/assets/generated", StaticFiles(directory=GENERATED_DIR), name="generated_assets")
app.mount("/reports", StaticFiles(directory=REPORTS_DIR), name="reports")

# Mount static files folder
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

@app.get("/")
def read_root():
    return FileResponse(
        os.path.join(BASE_DIR, "static", "index.html"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
    )
