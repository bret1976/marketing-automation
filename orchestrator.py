import os
import time
import json
import logging
import urllib.parse
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field
from google import genai
from google.genai import types
import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("orchestrator")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("STATE_DIR") or os.environ.get("DATA_DIR") or BASE_DIR
GENERATED_DIR = os.path.join(STATE_DIR, "generated")
os.makedirs(GENERATED_DIR, exist_ok=True)

def resolve_local_generated_path(video_path: str) -> str:
    if video_path.startswith("/static/assets/generated/"):
        candidate = os.path.join(GENERATED_DIR, os.path.basename(video_path))
    elif video_path.startswith("static/assets/generated/"):
        candidate = os.path.join(GENERATED_DIR, os.path.basename(video_path))
    elif video_path.startswith("/static/"):
        candidate = os.path.join(BASE_DIR, video_path.lstrip("/"))
    elif video_path.startswith("static/"):
        candidate = os.path.join(BASE_DIR, video_path)
    else:
        candidate = video_path
    resolved = os.path.realpath(candidate)
    allowed_roots = (
        os.path.realpath(GENERATED_DIR),
        os.path.realpath(os.path.join(BASE_DIR, "static")),
    )
    if not any(os.path.commonpath((resolved, root)) == root for root in allowed_roots):
        raise ValueError("Video path must reference generated media.")
    if os.path.splitext(resolved)[1].lower() not in {".mp4", ".mov", ".m4v", ".webm"}:
        raise ValueError("Video path must reference a supported video file.")
    return resolved

FAL_TEXT_VIDEO_ENGINES = {
    "fal_hailuo_23": {
        "model": "fal-ai/minimax/hailuo-2.3/standard/text-to-video",
        "label": "FAL MiniMax Hailuo 2.3 Standard",
        "durations": [6, 10],
        "payload": lambda prompt, duration: {
            "prompt": prompt,
            "duration": str(duration),
            "prompt_optimizer": True,
        },
    },
    "fal_hailuo_02": {
        "model": "fal-ai/minimax/hailuo-02/standard/text-to-video",
        "label": "FAL MiniMax Hailuo 02 Standard",
        "durations": [6, 10],
        "payload": lambda prompt, duration: {
            "prompt": prompt,
            "duration": str(duration),
            "prompt_optimizer": True,
        },
    },
    "fal_seedance_fast": {
        "model": "fal-ai/bytedance/seedance/v1/pro/fast/text-to-video",
        "label": "FAL Seedance 1.0 Pro Fast",
        "durations": list(range(8, 13)),
        "payload": lambda prompt, duration: {
            "prompt": prompt,
            "aspect_ratio": "16:9",
            "resolution": "720p",
            "duration": str(duration),
            "enable_safety_checker": True,
        },
    },
    "fal_ltx_fast": {
        "model": "fal-ai/ltx-2/text-to-video/fast",
        "label": "FAL LTX Video 2.0 Fast",
        "durations": [8, 10, 12, 14, 16, 18, 20],
        "payload": lambda prompt, duration: {
            "prompt": prompt,
            "duration": duration,
            "resolution": "1080p",
            "fps": 25,
            "generate_audio": True,
        },
    },
}

def get_binary_path(name: str) -> str:
    import shutil
    brew_path = f"/opt/homebrew/bin/{name}"
    if os.path.exists(brew_path):
        return brew_path
    which_path = shutil.which(name)
    if which_path:
        return which_path
    return name

def get_font_path() -> str:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # Debian/Railway container
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",     # macOS
        "/opt/homebrew/share/fonts/DejaVuSans-Bold.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return ""

def escape_ffmpeg_text(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace("%", "\\%")

# Structured output schemas using Pydantic
class TwitterThread(BaseModel):
    tweets: List[str] = Field(description="A list of 1 to 3 tweets representing a thread. Each tweet must be strictly under 280 characters.")

class SocialCopyResponse(BaseModel):
    linkedin_post: str = Field(description="A professional, behind-the-scenes LinkedIn post. Detail the AI tools, rendering techniques, and styling.")
    twitter_post: str = Field(description="A single punchy tweet (under 280 characters) to summarize the video.")
    twitter_thread: List[str] = Field(description="A list of 2 to 3 tweets representing a thread. Each tweet must be strictly under 280 characters. The first tweet should contain a strong hook.")
    instagram_caption: str = Field(description="An aesthetic, visually-driven Instagram caption. Under 150 words. Focus on mood, cinematography, and styling.")
    suggested_hashtags: List[str] = Field(description="5 to 8 highly relevant hashtags (e.g. #AIArt, #Sora, #RunwayML, #6FrameStudio).")

class ViralityScore(BaseModel):
    score: int = Field(description="Predicted virality score from 0 to 100, where 100 is maximum viral potential.")
    reasoning: str = Field(description="Brief explanation of the score, covering hook strength, visual novelty, and shareability.")
    suggested_improvements: List[str] = Field(description="2 to 4 concrete, specific suggestions to increase the score.")

class ContextBrief(BaseModel):
    video_summary: str = Field(description="Detailed description of what happens in the video (visuals, subjects, motions, style, transitions).")
    key_themes: List[str] = Field(description="List of core thematic concepts represented in the video.")
    brand_alignment: str = Field(description="How the video aligns with the brand context extracted from the URL.")
    visual_style_tags: List[str] = Field(description="Keywords describing the visual style (e.g. cinematic, cyberpunk, photorealistic, abstract).")
    recreation_motion_prompt: str = Field(description="A detailed, optimized Text-to-Video motion prompt for Runway Gen-3 or Google Veo 3.1 to recreate the visual scene, subject, lighting, and camera movement.")

class ViralConcept(BaseModel):
    platform: str = Field(description="Platform name, e.g. Reddit, YouTube, Twitter/X, LinkedIn, Instagram")
    url: str = Field(description="The REAL direct URL of the original viral post on its source platform (e.g. instagram.com/reel/..., x.com/.../status/..., youtube.com/watch?v=..., reddit.com/r/.../comments/...), exactly as found in search results — never a substitute video from a different platform, and never invented.")
    author: str = Field(description="The username, channel name or author of the content")
    title: str = Field(description="Summary or title of the trending/viral content")
    viral_metrics: str = Field(description="Engagement metrics, e.g. views, likes, retweets, comments, or general virality notes")
    original_concept: str = Field(description="Detailed description of the original video's exact visual content: subject, camera movement, editing technique, and pacing.")
    studio_adaptation_concept: str = Field(description="The literal recreation plan: how 6Frame Studio will recreate this SAME viral video shot-for-shot (same subject, scene, and motion), finished in 6Frame Studio's premium cinematic style.")
    recreated_video_prompt: str = Field(description="A precise Image-to-Video/Text-to-Video motion prompt that recreates the SAME viral video as closely as possible (same subject, camera motion, lighting, pacing) — not a new or loosely-inspired concept.")
    recreated_linkedin_post: str = Field(description="The ORIGINAL viral post's LinkedIn copy, reworded in 6Frame Studio's brand voice — same core message, hook, and story — framed as 6Frame Studio's own recreation, crediting the original creator's handle.")
    recreated_twitter_thread: List[str] = Field(description="The ORIGINAL viral post reworded as a Twitter/X thread in 6Frame Studio's brand voice (2-3 tweets, under 280 chars each) — same message and hook as the original, just reworded and credited.")
    recreated_instagram_caption: str = Field(description="The ORIGINAL viral post reworded as an Instagram caption in 6Frame Studio's brand voice, keeping the same core message and hook, with hashtags.")
    original_post_text: str = Field(description="Reconstructed or summarized text/copy of the original viral post — the source material the recreated_* copy fields are reworded from.")


class ViralSearchResponse(BaseModel):
    trends: List[ViralConcept] = Field(description="A list of viral video concepts found on social media from the last 24 hours. The app will only publish entries with verified direct source URLs.")

# In-memory job status cache
jobs_status: Dict[str, Dict[str, Any]] = {}

def get_job_status(job_id: str) -> Dict[str, Any]:
    return jobs_status.get(job_id, {"status": "NOT_FOUND"})

def update_job_status(job_id: str, status: str, progress: int, message: str, result: Any = None):
    jobs_status[job_id] = {
        "status": status,
        "progress": progress,
        "message": message,
        "result": result,
        "updated_at": time.time()
    }
    logger.info(f"Job {job_id} [{progress}%]: {message}")

def run_multi_agent_pipeline(
    job_id: str,
    video_path: str,
    website_url: str,
    settings: Dict[str, Any]
):
    try:
        api_key = settings.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY")
        if not api_key:
            update_job_status(job_id, "FAILED", 0, "Missing Gemini API Key. Please configure it in Settings.")
            return

        # Initialize GenAI client
        update_job_status(job_id, "PROCESSING", 10, "Initializing Gemini Client...")
        client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=180000)
        )

        # Step 1: Upload Video file to Gemini File API
        update_job_status(job_id, "PROCESSING", 20, "Uploading video to Gemini API (this may take a minute for larger files)...")
        
        # Verify file exists
        if not os.path.exists(video_path):
            update_job_status(job_id, "FAILED", 0, f"Video file not found at path: {video_path}")
            return
            
        video_file = client.files.upload(file=video_path)
        
        # Step 2: Poll file processing status
        update_job_status(job_id, "PROCESSING", 30, "Waiting for Gemini to process the video asset...")
        while video_file.state.name == "PROCESSING":
            time.sleep(5)
            video_file = client.files.get(name=video_file.name)
            
        if video_file.state.name == "FAILED":
            update_job_status(job_id, "FAILED", 0, "Gemini video processing failed.")
            return

        update_job_status(job_id, "PROCESSING", 45, "Video processed. Querying Context Agent (Gemini 2.5 Pro)...")

        # Step 3: Call Context Agent (Gemini 2.5 Pro)
        context_prompt = f"""
        You are the Research & Context Agent for 6Frame Studio.
        We have uploaded a video asset: {video_file.name}.
        The corresponding website/brand context is: {website_url}.

        Analyze this video file and the website context. Perform a deep multimodal analysis of the video frames, pacing, mood, and characters.
        Extract a unified context brief outlining:
        1. A description of the visuals and motion.
        2. The key aesthetic themes.
        3. How it aligns with 6Frame Studio's high-end, cinematic, AI-generative mission.
        4. Key visual style descriptors.
        5. A detailed, optimized Text-to-Video motion prompt (recreation_motion_prompt) for Runway Gen-3 or Google Veo 3.1 to recreate the visual scene, subject, lighting, and camera movement.
        """

        context_response = client.models.generate_content(
            model='gemini-3.1-pro-preview',
            contents=[video_file, context_prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ContextBrief,
                system_instruction="You are a cinematic research assistant that extracts visual details from video files and websites."
            )
        )
        
        try:
            brief: ContextBrief = context_response.parsed
        except Exception as e:
            logger.error(f"Failed to parse Context Brief: {e}. Raw text: {context_response.text}")
            update_job_status(job_id, "FAILED", 0, "Failed to parse context brief from Gemini Pro.")
            return

        update_job_status(job_id, "PROCESSING", 65, "Context Brief generated. Invoking Platform Copy Agents (Gemini 2.5 Flash)...")

        # Step 4: Call Copy Agents (Gemini 2.5 Flash) using Context Brief and brand settings
        brand_voice = settings.get("brand_voice", "Cinematic, minimalist, premium, technical yet artistic.")
        copy_prompt = f"""
        You are the Platform Copy Agent for 6Frame Studio.
        Based on the following Context Brief and Brand Voice guidelines, generate social media copy.

        ### BRAND VOICE GUIDELINES
        {brand_voice}

        ### CONTEXT BRIEF
        - Video Summary: {brief.video_summary}
        - Key Themes: {", ".join(brief.key_themes)}
        - Brand Alignment: {brief.brand_alignment}
        - Visual Style: {", ".join(brief.visual_style_tags)}

        ### TARGET OUTPUTS REQUIRED
        1. **LinkedIn Post**: Behind-the-scenes breakdown, focusing on the AI generation tools (Sora, Runway Gen-3, Kling, Midjourney), rendering techniques, artistic direction, and production insights. Professional and sophisticated.
        2. **Twitter/X Single Post**: Under 280 characters, highly hooky, driving engagement.
        3. **Twitter/X Thread**: A sequence of 2-3 tweets expanding on the cinematic process. Each tweet must be under 280 characters.
        4. **Instagram Caption**: Mood-oriented, visual, under 150 words, clean style, matching the video's aesthetic.
        5. **Suggested Hashtags**: Relevant industry and brand hashtags.

        ### POST FORMATTING AND SPACING RULES (CRITICAL)
        - **Double Line Breaks**: You MUST separate all paragraphs and bullet groups with a blank line (double newlines `\n\n`). Do not write long blocks of single-spaced text.
        - **NO ASTERISKS / NO BOLD**: Do NOT use markdown bold asterisks (`**`) or headers in the post text. All text must be clean plain text so it displays properly on Twitter and LinkedIn.
        - **Social handles / links**: Always credit the original creator using their actual social media handle (e.g. @CuriousRefuge) or a link to their channel/profile.
        - **Lists**: Format list items with clear dashes (`- item`) and separate the list block from paragraphs with empty lines.
        """

        copy_response = client.models.generate_content(
            model='gemini-3.6-flash',
            contents=[copy_prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=SocialCopyResponse,
                system_instruction="You are an expert social media copywriter specialized in marketing high-end cinematic AI assets."
            )
        )

        try:
            copy_results: SocialCopyResponse = copy_response.parsed
        except Exception as e:
            logger.error(f"Failed to parse Social Copy: {e}. Raw text: {copy_response.text}")
            update_job_status(job_id, "FAILED", 0, "Failed to parse social copy from Gemini Flash.")
            return

        # Step 5: Clean up video from Gemini File API
        update_job_status(job_id, "PROCESSING", 90, "Cleaning up assets and finalizing...")
        try:
            client.files.delete(name=video_file.name)
        except Exception as cleanup_err:
            logger.warning(f"Could not delete temporary file {video_file.name}: {cleanup_err}")

        # Success!
        final_result = {
            "brief": brief.model_dump(),
            "copy": copy_results.model_dump()
        }
        update_job_status(job_id, "SUCCESS", 100, "Multi-agent pipeline complete!", result=final_result)

    except Exception as e:
        logger.exception("Error in multi-agent pipeline")
        update_job_status(job_id, "FAILED", 0, f"Pipeline execution failed: {str(e)}")

MOCK_URL_PATTERNS = ["example", "status/1234", "DigitalDreams", "AIVoyager", "ChronoDrifter", "abcdef", "examplecyber", "dQw4w9WgXcQ", "watch?v=12345", "your_post_id"]

# Platforms whose post URLs sit behind login walls: yt-dlp can't probe them, so they are
# validated by domain match against the claimed platform instead of a download simulation.
PLATFORM_DOMAINS = {
    "instagram": ("instagram.com",),
    "tiktok": ("tiktok.com",),
    "linkedin": ("linkedin.com",),
    "twitter": ("twitter.com", "x.com"),
    "youtube": ("youtube.com", "youtu.be"),
    "reddit": ("reddit.com",),
}

def _youtube_video_id(url: str) -> str:
    """Return the video id for a well-formed youtube.com/watch or youtu.be URL."""
    import urllib.parse as _up
    parsed = _up.urlparse(url)
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    vid = ""
    if host == "youtube.com" or host.endswith(".youtube.com"):
        if parsed.path.rstrip("/") == "/watch":
            vid = (_up.parse_qs(parsed.query).get("v") or [""])[0]
    elif host == "youtu.be":
        trimmed = parsed.path.strip("/")
        vid = trimmed.split("/")[0] if trimmed else ""
    if vid and len(vid) >= 11 and all(c.isalnum() or c in "-_" for c in vid):
        return vid
    return ""

def check_url_valid(url: str, platform: str = "") -> bool:
    import subprocess
    import urllib.parse as _up
    if not url or not url.startswith("http"):
        return False
    parsed = _up.urlparse(url)
    host = parsed.netloc.lower()
    if host in {"google.com", "www.google.com"} or "/search" in parsed.path:
        return False
    if any(p in url for p in MOCK_URL_PATTERNS):
        return False
    # Well-formed YouTube watch URLs are valid without yt-dlp. Railway bot-blocks
    # or times out `yt-dlp --simulate`, which previously rejected real videos.
    if _youtube_video_id(url):
        return True
    platform_lower = (platform or "").lower()
    for key, domains in PLATFORM_DOMAINS.items():
        if key in platform_lower or (key == "twitter" and "x" in platform_lower.replace("twitter/x", "twitter")):
            if not any(host == d or host.endswith("." + d) for d in domains):
                return False  # URL domain contradicts the claimed platform
            if key in ("youtube", "reddit"):
                break  # downloadable platforms: also probe with yt-dlp below
            return True  # login-walled platforms: domain match is the best possible check
    cmd = [
        get_binary_path("yt-dlp"),
        "--simulate",
        url
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        return res.returncode == 0
    except:
        return False

def resolve_trend_mock_url(title: str, platform: str = "") -> str:
    import subprocess
    platform_lower = (platform or "").lower()
    # For login-walled platforms, the honest fallback is a search for the post on ITS OWN
    # platform (via Google site-search) — never a substitute video from another platform.
    for key, domains in PLATFORM_DOMAINS.items():
        if key in ("youtube", "reddit"):
            continue
        if key in platform_lower:
            return "unknown"
    cmd = [
        get_binary_path("yt-dlp"),
        "--no-playlist",
        "--flat-playlist",
        "--print", "webpage_url",
        f"ytsearch1:{title}"
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if res.returncode == 0 and res.stdout.strip():
            url = res.stdout.strip()
            logger.info(f"Resolved trend title '{title}' to real video URL: {url}")
            return url
    except Exception as e:
        logger.error(f"Failed resolving mock URL for trend: {title}. Error: {e}")
    return "unknown"

def fetch_realtime_news_context() -> str:
    import xml.etree.ElementTree as ET
    import requests
    url = "https://news.google.com/rss/search?q=AI+filmmaking+OR+AI+video+OR+Sora+OR+Runway+Gen-3+OR+Veo+OR+Kling&hl=en-US&gl=US&ceid=US:en"
    try:
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            root = ET.fromstring(res.content)
            items = root.findall(".//item")
            lines = []
            for item in items[:15]:
                title = item.find("title").text
                link = item.find("link").text
                pub_date = item.find("pubDate").text
                lines.append(f"- Title: {title}\n  Published: {pub_date}\n  URL: {link}")
            return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error fetching Google News context: {e}")
    return ""

INSTAGRAM_HASHTAG_STATE_FILE = os.path.join(BASE_DIR, "instagram_hashtag_state.json")
DEFAULT_INSTAGRAM_SEARCH_HASHTAGS = [
    "aivideo", "runwaygen3", "soraai", "aifilmmaking",
    "generativevideo", "klingai", "aicinematography", "midjourneyvideo",
]
INSTAGRAM_HASHTAG_ROLLING_WINDOW_DAYS = 7
INSTAGRAM_HASHTAG_ROLLING_CAP = 30  # Meta's hard limit: unique hashtags queried per IG User ID per 7 days

def load_instagram_hashtag_state() -> dict:
    if os.path.exists(INSTAGRAM_HASHTAG_STATE_FILE):
        try:
            with open(INSTAGRAM_HASHTAG_STATE_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error reading Instagram hashtag state file: {e}")
    return {"hashtag_ids": {}, "usage": {}}

def save_instagram_hashtag_state(state: dict):
    try:
        with open(INSTAGRAM_HASHTAG_STATE_FILE, "w") as f:
            json.dump(state, f, indent=4)
    except Exception as e:
        logger.error(f"Error saving Instagram hashtag state file: {e}")

def fetch_instagram_hashtag_context(settings: Dict[str, Any]) -> str:
    """Uses the Instagram Business Graph API's Hashtag Search (the only real content-discovery
    endpoint Meta exposes, even to authenticated business accounts) to pull real, verifiable
    posts for a handful of niche-relevant hashtags. Meta caps queries to 30 unique hashtags per
    rolling 7 days per IG User ID, so hashtag lookups (not media fetches) are tracked and capped
    here; already-queried hashtags within the window are reused from cache at no cost."""
    import requests
    from datetime import datetime, timedelta

    access_token = settings.get("instagram_access_token")
    ig_user_id = settings.get("instagram_business_account_id")
    if not access_token or not ig_user_id:
        return ""

    hashtags = settings.get("instagram_search_hashtags") or DEFAULT_INSTAGRAM_SEARCH_HASHTAGS
    if isinstance(hashtags, str):
        hashtags = [h.strip().lstrip("#") for h in hashtags.split(",") if h.strip()]

    state = load_instagram_hashtag_state()
    hashtag_ids = state.get("hashtag_ids", {})
    usage = state.get("usage", {})

    # Prune usage entries older than the rolling window
    cutoff = datetime.now() - timedelta(days=INSTAGRAM_HASHTAG_ROLLING_WINDOW_DAYS)
    usage = {h: ts for h, ts in usage.items() if datetime.fromisoformat(ts) > cutoff}

    # Meta hashtag search has a rolling quota and often rejects large top_media requests.
    # Rotate a small window per run and let Google grounding fill the rest of the scan.
    max_hashtags = int(settings.get("instagram_hashtag_scan_limit") or 4)
    lines = []
    for hashtag in hashtags[:max(1, max_hashtags)]:
        hashtag = hashtag.lower()
        try:
            hashtag_id = hashtag_ids.get(hashtag)
            if not hashtag_id:
                if hashtag not in usage and len(usage) >= INSTAGRAM_HASHTAG_ROLLING_CAP:
                    logger.warning(
                        f"Instagram hashtag search cap ({INSTAGRAM_HASHTAG_ROLLING_CAP}/7 days) reached — "
                        f"skipping new hashtag '#{hashtag}' this round."
                    )
                    continue
                search_res = requests.get(
                    "https://graph.facebook.com/v21.0/ig_hashtag_search",
                    params={"user_id": ig_user_id, "q": hashtag, "access_token": access_token},
                    timeout=8,
                )
                if search_res.status_code != 200:
                    logger.warning(f"Instagram hashtag search failed for '#{hashtag}': {search_res.text[:200]}")
                    continue
                data = search_res.json().get("data", [])
                if not data:
                    continue
                hashtag_id = data[0]["id"]
                hashtag_ids[hashtag] = hashtag_id
                usage[hashtag] = datetime.now().isoformat()

            media_res = requests.get(
                f"https://graph.facebook.com/v21.0/{hashtag_id}/top_media",
                params={
                    "user_id": ig_user_id,
                    "fields": "id,caption,permalink,like_count,comments_count",
                    "limit": 1,
                    "access_token": access_token,
                },
                timeout=8,
            )
            if media_res.status_code != 200:
                logger.warning(f"Instagram top_media fetch failed for '#{hashtag}', retrying smaller payload: {media_res.text[:200]}")
                media_res = requests.get(
                    f"https://graph.facebook.com/v21.0/{hashtag_id}/top_media",
                    params={
                        "user_id": ig_user_id,
                        "fields": "id,caption,permalink",
                        "limit": 1,
                        "access_token": access_token,
                    },
                    timeout=8,
                )
                if media_res.status_code != 200:
                    logger.warning(f"Instagram compact top_media fetch failed for '#{hashtag}': {media_res.text[:200]}")
                    continue
            for post in media_res.json().get("data", [])[:1]:
                caption = (post.get("caption") or "").replace("\n", " ")[:200]
                lines.append(
                    f"- Platform: Instagram | URL: {post.get('permalink')} | "
                    f"Likes: {post.get('like_count', 0)} | Comments: {post.get('comments_count', 0)} | "
                    f"Posted: {post.get('timestamp', '')} | Caption: {caption}"
                )
        except Exception as e:
            logger.warning(f"Error fetching Instagram hashtag data for '#{hashtag}': {e}")
            continue

    save_instagram_hashtag_state({"hashtag_ids": hashtag_ids, "usage": usage})
    return "\n".join(lines)

def run_live_trend_scanner(
    job_id: str,
    settings: Dict[str, Any]
):
    try:
        api_key = settings.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY")
        if not api_key:
            update_job_status(job_id, "FAILED", 0, "Missing Gemini API Key. Please configure it in Settings.")
            return

        update_job_status(job_id, "PROCESSING", 10, "Fetching supplementary real-time AI news context...")
        news_context = fetch_realtime_news_context()

        update_job_status(job_id, "PROCESSING", 15, "Querying Instagram Hashtag Search for verified real posts...")
        instagram_context = fetch_instagram_hashtag_context(settings)

        update_job_status(job_id, "PROCESSING", 20, "Searching Reddit, YouTube, Twitter/X, LinkedIn, and Instagram via Google Search grounding (last 24 hours)...")

        instagram_context_block = ""
        if instagram_context:
            instagram_context_block = (
                "Here are REAL, verified Instagram posts pulled directly via the Instagram Hashtag "
                "Search API — each URL below is a confirmed, real permalink to the actual post (not a "
                "search result to interpret). When one of these aligns with 6Frame Studio's niche, prefer "
                "citing it AS THE INSTAGRAM TREND using its exact URL, caption, and engagement numbers as given:\n"
                + instagram_context
            )

        search_prompt = f"""
        Search across Reddit (specifically r/aivideo, r/midjourney, r/StableDiffusion, r/ChatGPT), YouTube, Twitter/X, LinkedIn, and Instagram for the top 10 most viral or trending AI video posts from the last 24 hours.
        Focus specifically on content in categories aligned with 6Frame Studio's niche:
        - AI filmmaking and cinematic AI trailers (e.g. Sora, Runway Gen-3, Kling, Luma, Veo)
        - AI logo animations, visual loops, and motion design
        - AI music videos and audio-visual experiments

        Here is supplementary real-time AI news context you may use as additional seed material — but you must still actively search the social platforms above directly rather than relying on this alone:
        {news_context}

        {instagram_context_block}

        URL RULES (CRITICAL): The URL you report for each trend MUST be the REAL, direct link to the original viral post on its source platform, exactly as it appears in your search results (or in the verified Instagram post list above) — an instagram.com/reel/... link for an Instagram trend, an x.com/.../status/... link for a Twitter/X trend, a youtube.com/watch?v=... link for a YouTube trend, and so on. NEVER substitute a video from a different platform, and NEVER fabricate, guess, or reconstruct a URL (no placeholder IDs like 'examplecyber', 'abcdef', 'status/12345', 'your_post_id'). Return more than 10 candidate trends if needed, because the app will filter out anything without a verified direct original URL. If you cannot find the exact post URL in your search results, write "unknown" as the URL and still report the trend as a candidate.

        For each trend candidate identified, describe:
        1. The EXACT source platform where it was found (Reddit, YouTube, Twitter/X, LinkedIn, or Instagram). DO NOT use generic labels like "public news" or "news".
        2. The direct URL of the original post, following the URL rules above.
        3. The author or creator's username
        4. The title/description of the video
        5. Viral metrics (views, likes, retweets, comments, or upvotes)
        6. A detailed description of the exact visual content, subject, camera movement, and editing technique — and why it went viral
        7. The reconstructed original post text/caption the creator used
        """

        client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=360000) # Prevents indefinite hangs (360s safe limit)
        )

        try:
            response = client.models.generate_content(
                model='gemini-3.1-pro-preview',
                contents=search_prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    system_instruction="You are a real-time social media trend researcher specialized in finding trending cinematic AI contents across Reddit, YouTube, Twitter/X, LinkedIn, and Instagram."
                )
            )
        except Exception as grounding_err:
            logger.warning(f"Google Search grounding failed inside trend scanner: {grounding_err}. Falling back to news-context-only generation.")
            update_job_status(job_id, "PROCESSING", 30, "Search grounding unavailable, falling back to AI news context...")
            response = client.models.generate_content(
                model='gemini-3.6-flash',
                contents=search_prompt,
                config=types.GenerateContentConfig(
                    system_instruction="You are a real-time social media trend researcher specialized in finding trending cinematic AI contents across Reddit, YouTube, Twitter/X, LinkedIn, and Instagram."
                )
            )
        search_text = response.text

        update_job_status(job_id, "PROCESSING", 60, "Recreating viral posts in 6Frame Studio's voice (generating structured copies)...")

        adaptation_prompt = f"""
        You are 6Frame Studio, literally recreating viral AI video content found across social media.
        We have researched viral AI video trend candidates from the last 24 hours:

        {search_text}

        For each of these trending concepts, your goal is a LITERAL recreation, not a loosely-inspired new idea:
        1. Keep the EXACT platform name (Reddit, YouTube, Twitter/X, LinkedIn, or Instagram), author, title, and viral metrics. NEVER replace the platform name with "public news", "news", or any generic label. Keep the URL EXACTLY as reported in the research notes — the original post's link on its source platform. If the research notes say the URL is "unknown", output "unknown" as the URL. Never invent, alter, or substitute URLs.
        2. Describe the original video's exact visual content, subject, camera movement, and editing technique (original_concept).
        3. Describe the literal recreation plan: how 6Frame Studio will recreate the SAME video shot-for-shot — same subject, scene, and motion — finished in 6Frame Studio's premium cinematic style (studio_adaptation_concept).
        4. Write a precise Image-to-Video/Text-to-Video motion prompt (recreated_video_prompt) that recreates that SAME video as closely as possible — not a new or different concept.
        5. Reconstruct the original creator's post text/caption (original_post_text).
        6. Reword the ORIGINAL POST TEXT itself into 6Frame Studio's brand voice for LinkedIn, a Twitter/X thread, and Instagram (recreated_linkedin_post, recreated_twitter_thread, recreated_instagram_caption). Preserve the exact same hook, excitement, and core message as the original caption — just translate its tone and wording into 6Frame Studio's cinematic, refined voice, as if 6Frame Studio itself is captioning its OWN recreated video. Do NOT write it as a reaction to, review of, or commentary about someone else's post ("We were captivated by...", "Check out this creator's..."). It must read as 6Frame Studio's own first-person post about their own video. End with one brief, secondary line crediting the original creator/trend as the inspiration (e.g. "Inspired by a viral moment from @handle on Platform").

        ### POST FORMATTING AND SPACING RULES (CRITICAL)
        - **Double Line Breaks**: You MUST separate all paragraphs, bullet point blocks, and commentary highlights with an empty line (double newlines `\n\n`). Do not write long blocks of single-spaced text.
        - **NO ASTERISKS / NO BOLD**: Do NOT use markdown bold asterisks (`**`) or headers in the post text. All text must be clean plain text so it displays properly on Twitter and LinkedIn.
        - **Social handles / links**: Always credit the original creator using their actual social media handle (e.g. @CuriousRefuge) or a link to their channel/profile.
        - **Bullet Points**: Format lists with clean bullet dashes (`- item`) and leave spaces above and below list blocks.

        Return the results matching the required JSON schema structure.
        """

        # Step 2: Structure as JSON using Gemini 2.5 Flash
        copy_response = client.models.generate_content(
            model='gemini-3.6-flash',
            contents=adaptation_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ViralSearchResponse,
                system_instruction="You are an expert social media copywriter specialized in recreating viral cinematic AI content for 6Frame Studio."
            )
        )

        update_job_status(job_id, "PROCESSING", 90, "Resolving search links and verifying video availability...")
        try:
            results: ViralSearchResponse = copy_response.parsed
        except Exception as e:
            logger.error(f"Failed to parse Viral Search results: {e}. Raw text: {copy_response.text}")
            update_job_status(job_id, "FAILED", 0, "Failed to parse structured JSON from Gemini Pro.")
            return

        verified_trends = []
        unresolved_count = 0
        for trend in results.trends:
            if not check_url_valid(trend.url, trend.platform):
                logger.info(f"Invalid or mock URL detected: {trend.url}. Resolving dynamically for: {trend.title}")
                trend.url = resolve_trend_mock_url(trend.title, trend.platform)
                if _youtube_video_id(trend.url):
                    trend.platform = "YouTube"
            if check_url_valid(trend.url, trend.platform):
                verified_trends.append(trend)
            else:
                unresolved_count += 1

        if not verified_trends:
            update_job_status(
                job_id,
                "FAILED",
                0,
                "Trend search found candidates, but none had verified direct original post URLs. Try again with different hashtags or broader platform access.",
            )
            return

        final_result = {
            "trends": [trend.model_dump() for trend in verified_trends[:10]],
            "verified_count": len(verified_trends),
            "filtered_unresolved_count": unresolved_count,
        }
        update_job_status(job_id, "SUCCESS", 100, "Live trend search complete with verified direct URLs.", result=final_result)

    except Exception as e:
        logger.exception("Error in live trend scanner")
        update_job_status(job_id, "FAILED", 0, f"Search pipeline failed: {str(e)}")

def run_runway_rendering(job_id: str, prompt: str, settings: Dict[str, Any], duration: int = 10):
    try:
        api_key = settings.get("runway_api_key")
        if not api_key:
            update_job_status(job_id, "FAILED", 0, "Missing Runway API Key. Please configure it in Settings.")
            return

        import requests
        import subprocess
        
        headers = {
            "Authorization": f"Bearer {api_key}",
            "X-Runway-Version": "2024-11-06",
            "Content-Type": "application/json"
        }
        
        assets_dir = GENERATED_DIR
        os.makedirs(assets_dir, exist_ok=True)
        dest_path = os.path.join(assets_dir, f"{job_id}.mp4")

        if duration == 30:
            update_job_status(job_id, "PROCESSING", 5, "Initializing 30s cinematic cut (3 parallel tasks)...")
            
            # Formulate the 3 prompts for the cuts
            prompts = [
                f"{prompt}, cinematic wide establishing shot",
                f"{prompt}, medium camera shot, panning side angle",
                f"{prompt}, cinematic close-up shot, detail focus"
            ]
            
            task_ids = []
            url = "https://api.dev.runwayml.com/v1/text_to_video"
            
            for idx, p in enumerate(prompts):
                payload = {
                    "model": "gen4.5",
                    "promptText": p,
                    "ratio": "1280:720",
                    "duration": 10
                }
                update_job_status(job_id, "PROCESSING", 10 + (idx * 5), f"Submitting scene {idx + 1}/3 to Runway...")
                response = requests.post(url, headers=headers, json=payload)
                if response.status_code not in [200, 201, 202]:
                    update_job_status(job_id, "FAILED", 0, f"Runway API Error on scene {idx + 1}: {response.text}")
                    return
                data = response.json()
                tid = data.get("id")
                if not tid:
                    update_job_status(job_id, "FAILED", 0, f"No task ID returned for scene {idx + 1}.")
                    return
                task_ids.append(tid)

            update_job_status(job_id, "PROCESSING", 25, "All scenes queued. Polling status in parallel...")
            
            # Polling loop
            poll_count = 0
            statuses = ["PENDING", "PENDING", "PENDING"]
            urls = [f"https://api.dev.runwayml.com/v1/tasks/{tid}" for tid in task_ids]
            video_urls = [None, None, None]
            
            while True:
                time.sleep(5)
                poll_count += 1
                progress = min(25 + (poll_count * 3), 90)
                
                all_done = True
                any_failed = False
                fail_msg = ""
                
                for idx, status_url in enumerate(urls):
                    if statuses[idx] in ["SUCCEEDED", "FAILED"]:
                        continue
                    
                    try:
                        res = requests.get(status_url, headers=headers)
                        if res.status_code == 200:
                            task_data = res.json()
                            status = task_data.get("status")
                            statuses[idx] = status
                            
                            if status == "SUCCEEDED":
                                outputs = task_data.get("output", [])
                                if outputs:
                                    video_urls[idx] = outputs[0]
                                else:
                                    statuses[idx] = "FAILED"
                                    any_failed = True
                                    fail_msg = f"Scene {idx + 1} returned empty output."
                            elif status == "FAILED":
                                any_failed = True
                                error_msg = task_data.get("error", "Unknown Runway API error")
                                fail_msg = f"Scene {idx + 1} failed: {error_msg}"
                        else:
                            logger.error(f"Error polling scene {idx + 1}: {res.text}")
                    except Exception as poll_err:
                        logger.error(f"Connection error on scene {idx + 1}: {poll_err}")
                
                # Check overall status
                if any_failed:
                    update_job_status(job_id, "FAILED", 0, fail_msg)
                    return
                
                all_done = all(s == "SUCCEEDED" for s in statuses)
                if all_done:
                    break
                
                # Display status message in UI
                msg = f"Rendering: Scene 1 ({statuses[0]}), Scene 2 ({statuses[1]}), Scene 3 ({statuses[2]})"
                update_job_status(job_id, "PROCESSING", progress, msg)
                
            # Download and stitch clips
            update_job_status(job_id, "PROCESSING", 92, "Downloading scene clips...")
            clip_paths = []
            
            for idx, vurl in enumerate(video_urls):
                update_job_status(job_id, "PROCESSING", 92 + idx, f"Downloading clip {idx + 1}/3...")
                download_response = requests.get(vurl)
                if download_response.status_code != 200:
                    update_job_status(job_id, "FAILED", 0, f"Failed to download clip {idx + 1}. HTTP status: {download_response.status_code}")
                    return
                
                clip_path = os.path.join(assets_dir, f"{job_id}_clip_{idx + 1}.mp4")
                with open(clip_path, "wb") as f:
                    f.write(download_response.content)
                clip_paths.append(clip_path)

            update_job_status(job_id, "PROCESSING", 96, "Stitching 3 scenes together using ffmpeg...")
            
            # Write ffmpeg concat file
            concat_file_path = os.path.join(assets_dir, f"concat_{job_id}.txt")
            with open(concat_file_path, "w") as cf:
                for cp in clip_paths:
                    cf.write(f"file '{cp}'\n")
            
            try:
                # Concatenate videos without re-encoding
                subprocess.run([
                    get_binary_path("ffmpeg"), "-y", "-f", "concat", "-safe", "0", 
                    "-i", concat_file_path, "-c", "copy", dest_path
                ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except Exception as ffmpeg_err:
                logger.exception("ffmpeg concatenation failed")
                update_job_status(job_id, "FAILED", 0, f"Failed to stitch video clips: {str(ffmpeg_err)}")
                return
            finally:
                # Clean up temporary files
                if os.path.exists(concat_file_path):
                    os.remove(concat_file_path)
                for cp in clip_paths:
                    if os.path.exists(cp):
                        os.remove(cp)

        else:
            # Single clip generation (10s typical; 5s previews retired)
            update_job_status(job_id, "PROCESSING", 10, f"Submitting video task to Runway ({duration}s)...")
            
            url = "https://api.dev.runwayml.com/v1/text_to_video"
            payload = {
                "model": "gen4.5",
                "promptText": prompt,
                "ratio": "1280:720",
                "duration": duration
            }
            
            response = requests.post(url, headers=headers, json=payload)
            if response.status_code not in [200, 201, 202]:
                update_job_status(job_id, "FAILED", 0, f"Runway API Error: {response.text}")
                return

            data = response.json()
            task_id = data.get("id")
            if not task_id:
                update_job_status(job_id, "FAILED", 0, "No task ID returned from Runway API.")
                return

            update_job_status(job_id, "PROCESSING", 25, "Runway task queued. Polling status...")

            poll_count = 0
            status_url = f"https://api.dev.runwayml.com/v1/tasks/{task_id}"
            
            while True:
                time.sleep(5)
                poll_count += 1
                progress = min(25 + (poll_count * 4), 95)
                
                try:
                    res = requests.get(status_url, headers=headers)
                    if res.status_code == 200:
                        task_data = res.json()
                        status = task_data.get("status")
                        
                        if status == "SUCCEEDED":
                            outputs = task_data.get("output", [])
                            if not outputs:
                                update_job_status(job_id, "FAILED", 0, "Runway task succeeded but returned empty output.")
                                return
                            video_url = outputs[0]
                            break
                        elif status == "FAILED":
                            error_msg = task_data.get("error", "Unknown Runway API error")
                            update_job_status(job_id, "FAILED", 0, f"Runway Task Failed: {error_msg}")
                            return
                        else:
                            msg = f"Runway status: {status}..."
                            update_job_status(job_id, "PROCESSING", progress, msg)
                    else:
                        logger.error(f"Error polling Runway status: {res.text}")
                except Exception as poll_err:
                    logger.error(f"Error connecting to Runway polling endpoint: {poll_err}")

            update_job_status(job_id, "PROCESSING", 98, "Downloading video file from Runway...")
            
            download_response = requests.get(video_url)
            if download_response.status_code != 200:
                update_job_status(job_id, "FAILED", 0, f"Failed to download video from Runway. HTTP status: {download_response.status_code}")
                return

            with open(dest_path, "wb") as f:
                f.write(download_response.content)

        # Success!
        video_web_path = f"/static/assets/generated/{job_id}.mp4"
        update_job_status(
            job_id, 
            "SUCCESS", 
            100, 
            "Runway rendering complete!", 
            result={"video_path": video_web_path}
        )

    except Exception as e:
        logger.exception("Error in Runway rendering")
        update_job_status(job_id, "FAILED", 0, f"Runway generation failed: {str(e)}")

def _closest_supported_duration(duration: int, supported: List[int]) -> int:
    return min(supported, key=lambda candidate: abs(candidate - duration))

def _extract_fal_video_url(result: Dict[str, Any]) -> str:
    data = result.get("data") if isinstance(result.get("data"), dict) else result
    video = data.get("video") if isinstance(data, dict) else None
    if isinstance(video, dict) and video.get("url"):
        return video["url"]
    if isinstance(data, dict) and data.get("video_url"):
        return data["video_url"]
    if isinstance(data, dict) and isinstance(data.get("videos"), list) and data["videos"]:
        first = data["videos"][0]
        if isinstance(first, dict) and first.get("url"):
            return first["url"]
        if isinstance(first, str):
            return first
    raise ValueError("FAL result did not include a downloadable video URL.")

def run_fal_video_generation(job_id: str, prompt: str, settings: Dict[str, Any], engine: str, duration: int = 10):
    try:
        engine_config = FAL_TEXT_VIDEO_ENGINES.get(engine)
        if not engine_config:
            update_job_status(job_id, "FAILED", 0, f"Unsupported FAL video engine: {engine}")
            return

        api_key = settings.get("fal_api_key") or os.environ.get("FAL_API_KEY") or os.environ.get("FAL_KEY")
        if not api_key:
            update_job_status(job_id, "FAILED", 0, "Missing FAL API Key. Add FAL_KEY or configure it in Settings.")
            return

        model = engine_config["model"]
        label = engine_config["label"]
        render_duration = _closest_supported_duration(int(duration or 6), engine_config["durations"])
        payload = engine_config["payload"](prompt, render_duration)
        headers = {
            "Authorization": f"Key {api_key}",
            "Content-Type": "application/json",
        }

        update_job_status(job_id, "PROCESSING", 10, f"Submitting {render_duration}s render to {label}...")
        submit_url = f"https://queue.fal.run/{model}"
        submit_response = requests.post(submit_url, headers=headers, json=payload, timeout=60)
        if submit_response.status_code not in [200, 201, 202]:
            update_job_status(job_id, "FAILED", 0, f"{label} submit error: {submit_response.text}")
            return

        submit_data = submit_response.json()
        request_id = submit_data.get("request_id") or submit_data.get("requestId")
        if not request_id:
            update_job_status(job_id, "FAILED", 0, f"{label} did not return a request id.")
            return

        status_url = submit_data.get("status_url") or f"https://queue.fal.run/{model}/requests/{request_id}/status"
        result_url = submit_data.get("response_url") or f"https://queue.fal.run/{model}/requests/{request_id}/response"
        update_job_status(job_id, "PROCESSING", 25, f"{label} queued. Polling render status...")

        poll_count = 0
        while True:
            time.sleep(5)
            poll_count += 1
            progress = min(25 + (poll_count * 4), 94)
            status_response = requests.get(status_url, headers=headers, params={"logs": "1"}, timeout=30)
            if status_response.status_code not in [200, 202]:
                logger.warning("FAL status polling error for %s: %s", request_id, status_response.text)
                update_job_status(job_id, "PROCESSING", progress, f"{label} is rendering...")
                continue

            status_data = status_response.json()
            status = status_data.get("status") or status_data.get("state") or ""
            if status == "COMPLETED":
                break
            if status in {"FAILED", "ERROR", "CANCELLED"}:
                update_job_status(job_id, "FAILED", 0, f"{label} failed: {status_data}")
                return

            log_message = ""
            logs = status_data.get("logs") or []
            if logs and isinstance(logs[-1], dict):
                log_message = logs[-1].get("message") or ""
            update_job_status(job_id, "PROCESSING", progress, log_message or f"{label} status: {status or 'IN_PROGRESS'}")

        update_job_status(job_id, "PROCESSING", 96, f"{label} render complete. Fetching result...")
        result_response = requests.get(result_url, headers=headers, timeout=60)
        if result_response.status_code != 200:
            update_job_status(job_id, "FAILED", 0, f"{label} result error: {result_response.text}")
            return

        video_url = _extract_fal_video_url(result_response.json())
        download_response = requests.get(video_url, timeout=120)
        if download_response.status_code != 200:
            update_job_status(job_id, "FAILED", 0, f"Failed to download video from {label}. HTTP status: {download_response.status_code}")
            return

        assets_dir = GENERATED_DIR
        os.makedirs(assets_dir, exist_ok=True)
        dest_path = os.path.join(assets_dir, f"{job_id}.mp4")
        with open(dest_path, "wb") as f:
            f.write(download_response.content)

        video_web_path = f"/static/assets/generated/{job_id}.mp4"
        update_job_status(
            job_id,
            "SUCCESS",
            100,
            f"{label} rendering complete!",
            result={"video_path": video_web_path, "engine": engine, "engine_label": label},
        )

    except Exception as e:
        logger.exception("Error in FAL video generation")
        update_job_status(job_id, "FAILED", 0, f"FAL video generation failed: {str(e)}")

def normalize_video_duration(engine: str, duration: int) -> int:
    """No 5s previews. Veo is 8s. Other engines 8–10 default, model max if documented higher."""
    engine = engine or ""
    try:
        duration = int(duration)
    except (TypeError, ValueError):
        duration = 0
    if engine.startswith("google_veo"):
        return 8
    if engine == "runway_gen3":
        return 30 if duration == 30 else 10
    cfg = FAL_TEXT_VIDEO_ENGINES.get(engine)
    if cfg:
        supported = list(cfg["durations"])
        preferred = [d for d in supported if d >= 8] or supported
        target = duration if duration >= 8 else (10 if 10 in preferred else preferred[-1])
        return min(preferred, key=lambda candidate: abs(candidate - target))
    return max(8, min(duration or 10, 15))



def gemini_veo_config_kwargs() -> Dict[str, Any]:
    """Gemini API Veo rejects duration_seconds. Keep only supported fields."""
    return {
        "aspect_ratio": "16:9",
        "number_of_videos": 1,
    }


def build_gemini_veo_videos_config():
    return types.GenerateVideosConfig(**gemini_veo_config_kwargs())


def veo_prompt_with_duration_policy(prompt: str, duration: int) -> str:
    prompt = prompt or ""
    if int(duration or 0) == 8 and "8-second cinematic clip" not in prompt.lower():
        return f"8-second cinematic clip. {prompt}".strip()
    return prompt


def _error_mentions_duration_seconds(err: Exception) -> bool:
    return "duration_seconds" in str(err).lower()


def _looks_like_veo_model_id_error(err: Exception) -> bool:
    text = str(err).lower()
    if "duration_seconds" in text:
        return False
    needles = (
        "model",
        "not found",
        "not supported",
        "not available",
        "unknown model",
        "invalid model",
        "does not exist",
    )
    return any(n in text for n in needles)


def fal_key_present(settings: Dict[str, Any]) -> bool:
    return bool(
        (settings or {}).get("fal_api_key")
        or os.environ.get("FAL_API_KEY")
        or os.environ.get("FAL_KEY")
    )


def next_video_fallback_engine(engine: str, error_text: str, fal_available: bool) -> Optional[str]:
    """Labeled fallback only: Veo Lite model-id -> google_veo, then FAL if a key already exists."""
    engine = engine or ""
    err = (error_text or "").lower()
    if "duration_seconds" in err:
        return None
    if engine == "google_veo_lite" and _looks_like_veo_model_id_error(Exception(error_text or "")):
        return "google_veo"
    if engine.startswith("google_veo") and fal_available:
        return "fal_hailuo_23"
    return None


def run_video_generation(
    job_id: str,
    prompt: str,
    settings: Dict[str, Any],
    engine: str = "google_veo",
    duration: int = 8
):
    duration = normalize_video_duration(engine, duration)
    if engine == "runway_gen3":
        run_runway_rendering(job_id, prompt, settings, duration)
        return
    if engine in FAL_TEXT_VIDEO_ENGINES:
        run_fal_video_generation(job_id, prompt, settings, engine, duration)
        return

    prompt = veo_prompt_with_duration_policy(prompt, duration)

    try:
        api_key = settings.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY")
        if not api_key:
            update_job_status(job_id, "FAILED", 0, "Missing Gemini API Key. Please configure it in Settings.")
            return

        update_job_status(job_id, "PROCESSING", 10, "Initializing Gemini Client...")
        client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=180000)
        )

        if engine == "google_veo_lite":
            model_name = "veo-3.1-lite-generate-preview"
            engine_label = "Google Veo 3.1 Lite"
        elif engine == "google_veo_fast":
            model_name = "veo-3.1-fast-generate-preview"
            engine_label = "Google Veo 3.1 Fast"
        else:
            model_name = "veo-3.1-generate-preview"
            engine_label = "Google Veo 3.1"
        
        update_job_status(job_id, "PROCESSING", 25, f"Submitting video generation request to {engine_label}...")
        
        # Gemini API Veo rejects duration_seconds; Lite is a fixed 8s clip.
        config = build_gemini_veo_videos_config()

        try:
            operation = client.models.generate_videos(
                model=model_name,
                prompt=prompt,
                config=config
            )
        except Exception as gen_err:
            if _error_mentions_duration_seconds(gen_err):
                update_job_status(job_id, "PROCESSING", 25, f"{engine_label} rejected duration_seconds; retrying without it...")
                operation = client.models.generate_videos(
                    model=model_name,
                    prompt=prompt,
                    config=build_gemini_veo_videos_config(),
                )
            else:
                fallback = next_video_fallback_engine(engine, str(gen_err), fal_key_present(settings))
                if fallback:
                    update_job_status(
                        job_id,
                        "PROCESSING",
                        15,
                        f"{engine_label} failed ({gen_err}); labeled fallback to {fallback}.",
                    )
                    run_video_generation(job_id, prompt, settings, fallback, duration)
                    return
                raise

        update_job_status(job_id, "PROCESSING", 45, "Video rendering started. Polling status...")

        poll_count = 0
        while not operation.done:
            time.sleep(10)
            poll_count += 1
            # Simple progress simulation up to 95%
            progress = min(45 + (poll_count * 4), 95)
            update_job_status(job_id, "PROCESSING", progress, "Veo is rendering frames...")
            operation = client.operations.get(operation)

        update_job_status(job_id, "PROCESSING", 95, "Video rendered! Retrieving output URI...")
        
        # Retrieve download URI with robust dict/object checking to handle SDK parsing bugs
        video_uri = None
        
        # 1. Check if response is a dictionary
        if hasattr(operation, "response") and operation.response:
            resp = operation.response
            if isinstance(resp, dict):
                samples = resp.get("generateVideoResponse", {}).get("generatedSamples", [])
                if samples:
                    video_uri = samples[0].get("video", {}).get("uri")
            else:
                try:
                    if hasattr(resp, "generate_video_response") and resp.generate_video_response:
                        samples = resp.generate_video_response.generated_samples
                        if samples:
                            video_uri = samples[0].video.uri
                except Exception:
                    pass

        # 2. Check if result has generated_videos (SDK fallback)
        if not video_uri and hasattr(operation, "result") and operation.result:
            res_obj = operation.result
            if res_obj and hasattr(res_obj, "generated_videos") and res_obj.generated_videos:
                generated_video = res_obj.generated_videos[0]
                if hasattr(generated_video, "video") and hasattr(generated_video.video, "uri"):
                    video_uri = generated_video.video.uri
                elif hasattr(generated_video, "uri"):
                    video_uri = generated_video.uri

        if not video_uri:
            update_job_status(job_id, "FAILED", 0, "No video returned or download URI found in Veo API response.")
            return
        
        update_job_status(job_id, "PROCESSING", 98, "Downloading video file from Google server...")
        # Download the file using the authenticated SDK client
        try:
            video_content = client.files.download(file=video_uri)
        except Exception as sdk_err:
            logger.warning(f"SDK download failed, falling back to authenticated requests: {sdk_err}")
            # Fallback: request with x-goog-api-key header or key parameter
            import requests
            headers = {"x-goog-api-key": api_key}
            response = requests.get(video_uri, headers=headers)
            if response.status_code != 200:
                # Also try passing as query parameter
                separator = "&" if "?" in video_uri else "?"
                response = requests.get(f"{video_uri}{separator}key={api_key}")
                
            if response.status_code != 200:
                update_job_status(job_id, "FAILED", 0, f"Failed to download video file. HTTP status: {response.status_code}")
                return
            video_content = response.content
            
        # Target path
        assets_dir = GENERATED_DIR
        os.makedirs(assets_dir, exist_ok=True)
        dest_path = os.path.join(assets_dir, f"{job_id}.mp4")
        
        with open(dest_path, "wb") as f:
            f.write(video_content)
            
        # Success!
        video_web_path = f"/static/assets/generated/{job_id}.mp4"
        update_job_status(
            job_id, 
            "SUCCESS", 
            100, 
            "Video generation complete!", 
            result={"video_path": video_web_path, "engine": engine, "engine_label": engine_label}
        )

    except Exception as e:
        logger.exception("Error in video generation")
        fallback = next_video_fallback_engine(engine, str(e), fal_key_present(settings))
        if fallback:
            update_job_status(
                job_id,
                "PROCESSING",
                15,
                f"Video generation failed ({e}); labeled fallback to {fallback}.",
            )
            run_video_generation(job_id, prompt, settings, fallback, duration)
            return
        update_job_status(job_id, "FAILED", 0, f"Video generation failed: {str(e)}")

def build_reframe_filter(width: int, height: int, text: str, font_path: str, y_offset: int) -> str:
    """Blur-pad reframe: scale+crop a blurred background to fill the target frame,
    then overlay the properly-scaled (letterboxed) original video on top, plus a
    burned-in hook-text card near the bottom — the on-screen-text equivalent of
    captions for non-narrated cinematic AI clips."""
    filt = (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},gblur=sigma=20[bg];"
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2"
    )
    if text and font_path:
        escaped = escape_ffmpeg_text(text)
        filt += (
            f",drawtext=fontfile='{font_path}':text='{escaped}':fontcolor=white:fontsize=54:"
            f"line_spacing=8:box=1:boxcolor=black@0.55:boxborderw=16:"
            f"x=(w-text_w)/2:y=h-{y_offset}"
        )
    return filt + "[outv]"

def run_ffmpeg_variant(input_path: str, output_path: str, filter_complex: str):
    import subprocess
    cmd = [
        get_binary_path("ffmpeg"), "-y",
        "-i", input_path,
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-c:a", "aac", "-shortest",
        output_path
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg variant render failed: {res.stderr[-800:]}")

def render_variant_with_fallback(input_path: str, output_path: str, width: int, height: int,
                                  text: str, font_path: str, y_offset: int) -> bool:
    """Renders a reframed variant. If the installed ffmpeg build lacks the drawtext filter
    (some minimal builds omit libfreetype/fontconfig), retries without the text overlay
    rather than failing the whole variant. Returns whether text was actually burned in."""
    filt = build_reframe_filter(width, height, text, font_path, y_offset)
    try:
        run_ffmpeg_variant(input_path, output_path, filt)
        return bool(text and font_path)
    except RuntimeError as e:
        if text and font_path and "drawtext" in str(e).lower():
            logger.warning("ffmpeg build lacks drawtext filter support — rendering variant without burned-in text.")
            filt_no_text = build_reframe_filter(width, height, "", font_path, y_offset)
            run_ffmpeg_variant(input_path, output_path, filt_no_text)
            return False
        raise

def generate_video_variants(job_id: str, video_path: str, hook_text: str):
    try:
        abs_video_path = resolve_local_generated_path(video_path)
        if not os.path.exists(abs_video_path):
            update_job_status(job_id, "FAILED", 0, f"Source video not found at {abs_video_path}")
            return

        update_job_status(job_id, "PROCESSING", 10, "Preparing platform variants (9:16, 1:1, 16:9)...")
        assets_dir = GENERATED_DIR
        os.makedirs(assets_dir, exist_ok=True)
        font_path = get_font_path()
        if not font_path:
            logger.warning("No font found for text overlay — variants will render without burned-in hook text.")

        variants = {}
        text_applied = True
        specs = [
            ("vertical_9x16", 1080, 1920, 320, 30, "Rendering 9:16 vertical (Reels/TikTok/Shorts)..."),
            ("square_1x1", 1080, 1080, 220, 60, "Rendering 1:1 square (feed post)..."),
            ("landscape_16x9", 1920, 1080, 160, 90, "Rendering 16:9 landscape with hook text..."),
        ]
        for key, w, h, y_offset, progress, message in specs:
            update_job_status(job_id, "PROCESSING", progress, message)
            out_path = os.path.join(assets_dir, f"{job_id}_{key}.mp4")
            applied = render_variant_with_fallback(abs_video_path, out_path, w, h, hook_text, font_path, y_offset)
            text_applied = text_applied and applied
            variants[key] = f"/static/assets/generated/{job_id}_{key}.mp4"

        message = "Platform variants ready!" if text_applied else "Platform variants ready (text overlay unsupported by this server's ffmpeg build, rendered without it)."
        update_job_status(
            job_id, "SUCCESS", 100, message,
            result={"variants": variants, "captioned": text_applied}
        )
    except Exception as e:
        logger.exception("Error generating video variants")
        update_job_status(job_id, "FAILED", 0, f"Video variant generation failed: {str(e)}")

def generate_virality_score(post_text: str, video_prompt: str, platform: str, settings: Dict[str, Any]) -> ViralityScore:
    api_key = settings.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("Missing Gemini API Key. Please configure it in Settings.")

    client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=60000))
    prompt = f"""
    You are a social media virality analyst. Score the predicted virality potential of this post on a 0-100 scale, where 100 is maximum viral potential.

    Platform: {platform}
    Post copy: {post_text}
    Video concept / motion prompt: {video_prompt}

    Consider: hook strength in the first line, emotional resonance, visual novelty/spectacle described in the
    video prompt, shareability, and alignment with what's currently trending in AI-generated video content.
    Provide a numeric score, brief reasoning, and 2-4 concrete, specific suggestions to increase the score.
    """
    response = client.models.generate_content(
        model='gemini-3.6-flash',
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ViralityScore,
            system_instruction="You are an expert social media virality predictor and growth strategist."
        )
    )
    try:
        return response.parsed
    except Exception as e:
        logger.error(f"Failed to parse virality score: {e}. Raw text: {response.text}")
        raise ValueError("Failed to parse virality score from Gemini.")

class DraftedReply(BaseModel):
    reply_text: str = Field(description="A short, in-character reply under 280 characters, written in the brand voice, that directly addresses the mention.")

def draft_engagement_reply(mention_text: str, mention_author: str, settings: Dict[str, Any]) -> str:
    api_key = settings.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("Missing Gemini API Key. Please configure it in Settings.")

    brand_voice = settings.get("brand_voice", "")
    client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=60000))
    prompt = f"""
    You are 6Frame Studio's social media community manager, replying to a mention on Twitter/X.

    ### BRAND VOICE GUIDELINES
    {brand_voice}

    ### THE MENTION
    From @{mention_author}: "{mention_text}"

    Draft a short, warm, genuine reply (under 280 characters) in the brand voice above. Directly address
    what they said — do not write a generic thank-you. No hashtags unless truly natural. No markdown.
    """
    response = client.models.generate_content(
        model='gemini-3.6-flash',
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=DraftedReply,
            system_instruction="You are a warm, on-brand social media community manager who writes concise replies."
        )
    )
    try:
        return response.parsed.reply_text
    except Exception as e:
        logger.error(f"Failed to parse drafted reply: {e}. Raw text: {response.text}")
        raise ValueError("Failed to parse drafted reply from Gemini.")

class RepurposedContent(BaseModel):
    author: str = Field(description="Creator's username/channel name")
    original_post_text: str = Field(description="Summary or text of the original post")
    repurposed_linkedin_post: str = Field(description="Staged B2B LinkedIn post copy for 6Frame Studio commenting on the video and citing the link/author")
    repurposed_twitter_thread: List[str] = Field(description="Staged Twitter thread (2-3 tweets, under 280 chars each)")
    repurposed_instagram_caption: str = Field(description="Staged Instagram caption with hashtags")

def repurpose_video_link_copy(url: str, settings: Dict[str, Any]) -> RepurposedContent:
    api_key = settings.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("Missing Gemini API Key. Please configure it in Settings.")
        
    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=180000)
    )
    brand_voice = settings.get("brand_voice", "")
    
    # Phase 1: Search grounding to find metadata for this specific URL
    search_prompt = f"""
    Search for information about the following viral video link:
    URL: {url}
    
    Determine:
    1. The platform it is on (e.g. YouTube, X/Twitter, Instagram, Reddit)
    2. The creator/author's username or channel
    3. The title or description of the video
    4. Reconstruct what the original post text or copy says
    5. The main visual concept, technique, or content of the video.
    """
    
    try:
        search_res = client.models.generate_content(
            model='gemini-3.1-pro-preview',
            contents=search_prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                system_instruction="You are a social media research assistant specialized in finding information about links."
            )
        )
    except Exception as e:
        logger.warning(f"Google Search grounding failed inside repurposer: {e}. Falling back to standard generation.")
        search_res = client.models.generate_content(
            model='gemini-3.1-pro-preview',
            contents=search_prompt,
            config=types.GenerateContentConfig(
                system_instruction="You are a social media research assistant specialized in finding information about links."
            )
        )
    
    # Phase 2: Structure and draft repurposed copies using Gemini 2.5 Pro JSON schema
    adaptation_prompt = f"""
    Analyze the research details of this viral video link:
    URL: {url}
    Research Details: {search_res.text}
    
    6Frame Studio Brand Voice Guidelines:
    {brand_voice}
    
    Based on this information, perform the following tasks:
    1. Extract the author/creator's username.
    2. Reconstruct or summarize the original post text.
    3. Write repurposed social copy in the brand voice of 6Frame Studio (cinematic, refined, artistic, technical but premium) reacting to/commenting on the original video. You must explicitly credit the creator/author (by username) and direct the audience to check out the original clip (using the URL {url} or a citation).
    
    Draft:
    - repurposed_linkedin_post: A professional, cinematic review/commentary for LinkedIn citing the creator.
    - repurposed_twitter_thread: A structured X thread (2-3 tweets, each under 280 characters).
    - repurposed_instagram_caption: A premium caption with hashtags.

    ### POST FORMATTING AND SPACING RULES (CRITICAL)
    - **Double Line Breaks**: You MUST separate all paragraphs, highlights, and bullet groups with a blank line (double newlines `\n\n`). Do not write long blocks of single-spaced text.
    - **NO ASTERISKS / NO BOLD**: Do NOT use markdown bold asterisks (`**`) or headers in the post text. All text must be clean plain text so it displays properly on Twitter and LinkedIn.
    - **Social handles / links**: Always credit the original creator using their actual social media handle (e.g. @CuriousRefuge) or a link to their channel/profile.
    - **Bullet Points**: Format lists with clean bullet dashes (`- item`) and leave spaces above and below list blocks.
    """
    
    copy_res = client.models.generate_content(
        model='gemini-3.1-pro-preview',
        contents=adaptation_prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=RepurposedContent,
            system_instruction="You are an expert social media copywriter specialized in marketing high-end cinematic AI assets."
        )
    )
    
    try:
        return copy_res.parsed
    except Exception as e:
        logger.error(f"Failed to parse repurposed copy structure: {e}. Raw: {copy_res.text}")
        raise ValueError("Failed to parse structured JSON from Gemini Pro.")
