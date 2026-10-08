"""Residential pull proxy for bot-walled source clips (yt-pull-proxy on Bret's Mac Mini).

Railway's datacenter IP gets YouTube's "Sign in to confirm you're not a bot" and
Reddit 403s. The Mini (residential IP) runs an authenticated pull service behind a
Cloudflare tunnel - the same one TrendPilot (autopilot-mcp) uses. We ask it for the
trimmed window of an original and hand the mp4 to the existing pipeline.

Routing lives in bin/ytdlp-shim/yt-dlp, which main.py puts first on PATH, so every
download path (main.py, the cockpit overlay, V9/V10 isolate patches) goes through it
without monkey-patching their closures:
  YouTube                         -> Mini first, local yt-dlp if the Mini fails
  Reddit / X / IG / TikTok / Vimeo -> local yt-dlp first, Mini if that fails

Config:
  YT_DOWNLOAD_PROXY_TOKEN  shared secret (X-Proxy-Token), required
  YT_DOWNLOAD_PROXY_URL    tunnel base URL (fallback)
  STATE_DIR/yt_proxy.json  URL registered by the Mini watchdog (preferred; quick
                           tunnel URLs change on restart, so the Mini re-registers
                           without a Railway redeploy)
"""
from __future__ import annotations

import hmac
import json
import os
import time
from typing import Any, Dict, List
from urllib.parse import urlparse

PULL_TIMEOUT_SECONDS = 150.0
STATE_FILE = "yt_proxy.json"
LOG_FILE = "yt_proxy_pulls.jsonl"
MINI_HOSTS = (
    "youtube.com", "youtu.be", "youtube-nocookie.com",
    "reddit.com", "redd.it",
    "x.com", "twitter.com", "tiktok.com", "instagram.com", "vimeo.com",
)
YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")


def state_dir() -> str:
    base = os.environ.get("STATE_DIR") or os.environ.get("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
    return base


def _path(name: str) -> str:
    return os.path.join(state_dir(), name)


def token() -> str:
    return (os.environ.get("YT_DOWNLOAD_PROXY_TOKEN") or "").strip()


def registered() -> Dict[str, Any]:
    try:
        with open(_path(STATE_FILE), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def urls() -> List[str]:
    """Candidate base URLs: registered (fresh) first, then the env var."""
    out: List[str] = []
    for value in (registered().get("url"), os.environ.get("YT_DOWNLOAD_PROXY_URL")):
        value = str(value or "").strip().rstrip("/")
        if value.startswith("https://") and value not in out:
            out.append(value)
    return out


def configured() -> bool:
    return bool(token() and urls())


def token_ok(candidate: Any) -> bool:
    expected = token()
    return bool(expected and candidate) and hmac.compare_digest(str(candidate).strip(), expected)


def register(url: str) -> Dict[str, Any]:
    url = (url or "").strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path not in ("", "/"):
        raise ValueError("url must be an https base URL")
    record = {"url": url, "registered_at": int(time.time())}
    os.makedirs(state_dir(), exist_ok=True)
    tmp = _path(STATE_FILE + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(record, fh)
    os.replace(tmp, _path(STATE_FILE))
    return record


def host_of(url: str) -> str:
    try:
        host = (urlparse(str(url or "").strip()).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _host_in(url: str, hosts) -> bool:
    host = host_of(url)
    return bool(host) and any(host == h or host.endswith("." + h) for h in hosts)


def is_youtube(url: str) -> bool:
    return _host_in(url, YOUTUBE_HOSTS)


def mini_supports(url: str) -> bool:
    return str(url or "").startswith(("https://", "http://")) and _host_in(url, MINI_HOSTS)


def log_pull(entry: Dict[str, Any]) -> None:
    """Append one pull outcome (no secrets) for /api/yt-proxy/status."""
    try:
        entry = {"ts": int(time.time()), **entry}
        os.makedirs(state_dir(), exist_ok=True)
        path = _path(LOG_FILE)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        if os.path.getsize(path) > 512_000:
            with open(path, "r", encoding="utf-8") as fh:
                tail = fh.readlines()[-500:]
            with open(path, "w", encoding="utf-8") as fh:
                fh.writelines(tail)
    except Exception:  # noqa: BLE001 - logging must never break a download
        pass


def recent_pulls(limit: int = 10) -> List[Dict[str, Any]]:
    try:
        with open(_path(LOG_FILE), "r", encoding="utf-8") as fh:
            lines = fh.readlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def summary() -> Dict[str, Any]:
    reg = registered()
    last = recent_pulls(1)
    return {
        "configured": configured(),
        "registered_at": reg.get("registered_at"),
        "env_url_set": bool((os.environ.get("YT_DOWNLOAD_PROXY_URL") or "").strip()),
        "last_pull": last[0] if last else None,
    }


class ProxyError(RuntimeError):
    pass


def pull(source_url: str, dest: str, *, start: float = 0.0, duration: float = 60.0,
         timeout: float = PULL_TIMEOUT_SECONDS) -> bool:
    """Download the trimmed window of source_url to dest through the Mini.

    Returns True if the file is already sectioned (starts at `start`), False if it is
    the full video. Raises ProxyError when no proxy URL works.
    """
    import requests

    secret = token()
    if not secret:
        raise ProxyError("YT_DOWNLOAD_PROXY_TOKEN is not set")
    errors: List[str] = []
    for base in urls():
        netloc = urlparse(base).netloc
        try:
            with requests.post(
                f"{base}/pull",
                headers={"x-proxy-token": secret, "content-type": "application/json"},
                json={"url": source_url, "start": start, "duration": duration},
                stream=True,
                timeout=(15, timeout),
            ) as response:
                if response.status_code != 200:
                    body = response.raw.read(400, decode_content=True) or b""
                    errors.append(f"{netloc}: HTTP {response.status_code} {body.decode('utf-8', 'ignore')[:300]}")
                    continue
                tmp = dest + ".part"
                with open(tmp, "wb") as fh:
                    for chunk in response.iter_content(chunk_size=256 * 1024):
                        if chunk:
                            fh.write(chunk)
                sectioned = response.headers.get("x-ytpp-sectioned", "1") == "1"
            if os.path.exists(tmp) and os.path.getsize(tmp) > 1000:
                os.replace(tmp, dest)
                return sectioned
            errors.append(f"{netloc}: empty file")
        except Exception as exc:  # noqa: BLE001 - try the next URL, then local yt-dlp
            errors.append(f"{netloc}: {type(exc).__name__}: {str(exc)[:200]}")
        finally:
            try:
                if os.path.exists(dest + ".part"):
                    os.remove(dest + ".part")
            except OSError:
                pass
    raise ProxyError("; ".join(errors) or "no YT download proxy URL configured")
