"""Conservative public-web fetch. Never calls the X API or x.com/twitter.com."""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser
from urllib.parse import urlparse

logger = logging.getLogger("content_team.web")

BLOCKED_HOSTS = {
    "api.twitter.com",
    "api.x.com",
    "twitter.com",
    "www.twitter.com",
    "x.com",
    "www.x.com",
    "mobile.twitter.com",
    "pro.x.com",
}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"} and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._skip:
            return
        text = re.sub(r"\s+", " ", data or "").strip()
        if text:
            self._chunks.append(text)

    def text(self, limit: int = 4000) -> str:
        return " ".join(self._chunks)[:limit]


def host_allowed(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    return host not in BLOCKED_HOSTS


def fetch_text(url: str, *, timeout: int = 15) -> dict:
    """GET a public http(s) page and return extracted text. Never hits X API hosts."""
    if not url or not url.lower().startswith(("http://", "https://")):
        return {"ok": False, "url": url, "error": "only http(s) URLs are allowed", "text": ""}
    if not host_allowed(url):
        return {"ok": False, "url": url, "error": "x.com / twitter.com and the X API are blocked", "text": ""}
    try:
        import requests

        res = requests.get(url, timeout=timeout, headers={"User-Agent": "6FrameContentTeam/1.0"})
        if res.status_code >= 400:
            return {"ok": False, "url": url, "error": f"HTTP {res.status_code}", "text": ""}
        parser = _TextExtractor()
        parser.feed(res.text or "")
        return {"ok": True, "url": url, "error": None, "text": parser.text()}
    except Exception as exc:  # noqa: BLE001
        logger.warning("public fetch failed for %s: %s", url, exc)
        return {"ok": False, "url": url, "error": str(exc), "text": ""}
