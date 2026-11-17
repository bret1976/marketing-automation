"""Last-24h viral scan powered by mvanhorn/last30days-skill (MIT).

Replaces Gemini Google-Search grounding. Only keyless/free sources are used:
Hacker News (Algolia, via the vendored last30days collectors) plus Google
Trends RSS. X, TikTok, and Instagram stay off unless a paid key is present —
this adapter never sends those keys. Reddit's official listings currently
schema-drift/403 from datacenter IPs; that failure is recorded, not guessed.

Vendored snapshot: vendor/last30days/ (v4.3.2, commit 488e50d).
"""
from __future__ import annotations

import json
import logging
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

logger = logging.getLogger("last30days_scan")

ENGINE = "last30days"
ENGINE_VERSION = "4.3.2"
WINDOW_HOURS = 24
TRENDS_RSS = "https://trends.google.com/trending/rss?geo=US"
USER_AGENT = "Mozilla/5.0 (compatible; 6frame-last30days-scan/1.0; +https://6framestudio.com)"
SCAN_FIELDS = (
    "title",
    "source_url",
    "platform",
    "why",
    "topic_tags",
    "suggested_start",
    "suggested_duration",
    "notes",
)

# Paid/authenticated env vars last30days would otherwise spend. Never forward them.
_PAID_ENV = (
    "SCRAPECREATORS_API_KEY",
    "OPENAI_API_KEY",
    "XAI_API_KEY",
    "X_BEARER_TOKEN",
    "OPENROUTER_API_KEY",
    "PERPLEXITY_API_KEY",
    "PARALLEL_API_KEY",
    "BRAVE_API_KEY",
    "APIFY_API_TOKEN",
    "GEMINI_API_KEY",
    "AUTH_TOKEN",
    "CT0",
)


def _vendor_scripts() -> Path:
    here = Path(__file__).resolve()
    # app/last30days_scan.py -> repo/vendor/...  or repo/last30days_scan.py -> repo/vendor/...
    for root in (here.parent, here.parent.parent):
        scripts = root / "vendor" / "last30days" / "scripts"
        if (scripts / "lib" / "hackernews.py").exists():
            return scripts
    raise RuntimeError("vendored last30days scripts not found")


def _ensure_vendor_on_path() -> Path:
    scripts = _vendor_scripts()
    path = str(scripts)
    if path not in sys.path:
        sys.path.insert(0, path)
    return scripts


def now_utc(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def age_hours(published: datetime, now: datetime) -> float:
    return max(0.0, (now - published).total_seconds() / 3600.0)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        text = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return parsedate_to_datetime(value).astimezone(timezone.utc)
        except Exception:
            return None


def _within_window(published: datetime | None, now: datetime, hours: int = WINDOW_HOURS) -> bool:
    if published is None:
        return False
    return 0 <= (now - published).total_seconds() <= hours * 3600


def _item(
    *,
    title: str,
    url: str,
    platform: str,
    author: str = "",
    published_at: datetime | None = None,
    now: datetime,
    score: float = 0.0,
    comments: int = 0,
    why: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    age = age_hours(published_at, now) if published_at else None
    pace = (score / age) if age and age > 0 else score
    out: dict[str, Any] = {
        "title": (title or "").strip(),
        "url": (url or "").strip(),
        "source_url": (url or "").strip(),
        "platform": platform,
        "author": author or "",
        "published_at": published_at.strftime("%Y-%m-%dT%H:%M:%SZ") if published_at else "",
        "age_hours": None if age is None else round(age, 2),
        "score": score,
        "comments": comments,
        "pace": round(pace, 2),
        "why": why,
        "source": ENGINE,
    }
    if extra:
        out.update(extra)
    return out


def _fetch_hackernews(now: datetime, niche: str = "", window_hours: int = WINDOW_HOURS) -> tuple[list[dict[str, Any]], str]:
    _ensure_vendor_on_path()
    from lib import hackernews
    from lib import http as last_http

    to_day = now.date()
    from_day = (now - timedelta(hours=window_hours)).date()
    from_ts = int((now - timedelta(hours=window_hours)).timestamp())
    to_ts = int(now.timestamp()) + 1
    limit = 30
    urls = [
        f"{hackernews.ALGOLIA_SEARCH_URL}?tags=front_page&hitsPerPage={limit}",
        (
            f"{hackernews.ALGOLIA_SEARCH_URL}?tags=story"
            f"&numericFilters=created_at_i>{from_ts},created_at_i<{to_ts}"
            f"&hitsPerPage={limit}"
        ),
    ]
    if niche.strip():
        from urllib.parse import urlencode
        params = {
            "query": niche.strip(),
            "tags": "story",
            "numericFilters": f"created_at_i>{from_ts},created_at_i<{to_ts}",
            "hitsPerPage": str(limit),
        }
        urls.append(f"{hackernews.ALGOLIA_SEARCH_URL}?{urlencode(params)}")

    hits: list[dict[str, Any]] = []
    errors: list[str] = []
    for url in urls:
        try:
            response = last_http.request("GET", url, timeout=30)
            hits.extend(response.get("hits") or [])
        except Exception as exc:  # noqa: BLE001 — one source must not abort the scan
            errors.append(str(exc))

    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for hit in hits:
        object_id = str(hit.get("objectID") or "")
        if not object_id or object_id in seen:
            continue
        seen.add(object_id)
        created = hit.get("created_at_i")
        published = datetime.fromtimestamp(int(created), tz=timezone.utc) if created else _parse_iso(hit.get("created_at"))
        if not _within_window(published, now, window_hours):
            continue
        hn_url = f"https://news.ycombinator.com/item?id={object_id}"
        points = float(hit.get("points") or 0)
        comments = int(hit.get("num_comments") or 0)
        title = str(hit.get("title") or "").strip()
        items.append(
            _item(
                title=title,
                url=hn_url,
                platform="hackernews",
                author=str(hit.get("author") or ""),
                published_at=published,
                now=now,
                score=points,
                comments=comments,
                why=f"{int(points):,} points and {comments} comments in {age_hours(published, now):.1f} hours on Hacker News.",
                extra={"article_url": hit.get("url") or "", "id": object_id},
            )
        )
    status = "ok" if items else ("error" if errors else "no-results")
    return items, status if not errors else f"{status}: {errors[0][:160]}"


def _fetch_google_trends(now: datetime, window_hours: int = WINDOW_HOURS) -> tuple[list[dict[str, Any]], str]:
    try:
        req = Request(TRENDS_RSS, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml"})
        with urlopen(req, timeout=20) as resp:
            raw = resp.read()
        root = ET.fromstring(raw)
    except Exception as exc:  # noqa: BLE001
        return [], f"error: {exc}"[:200]

    ns = {"ht": "https://trends.google.com/trending/rss"}
    items: list[dict[str, Any]] = []
    for node in root.findall(".//item"):
        title = (node.findtext("title") or "").strip()
        pub = _parse_iso(node.findtext("pubDate") or "")
        if not title or not _within_window(pub, now, window_hours):
            continue
        traffic_raw = node.findtext("ht:approx_traffic", default="", namespaces=ns) or ""
        traffic = 0.0
        digits = "".join(ch for ch in traffic_raw if ch.isdigit())
        if digits:
            traffic = float(digits)
        news = node.findall("ht:news_item", ns)
        url = ""
        source = ""
        if news:
            url = (news[0].findtext("ht:news_item_url", default="", namespaces=ns) or "").strip()
            source = (news[0].findtext("ht:news_item_source", default="", namespaces=ns) or "").strip()
            news_title = (news[0].findtext("ht:news_item_title", default="", namespaces=ns) or "").strip()
            if news_title and news_title not in title:
                title = f"{title}: {news_title}"
        if not url.startswith("http"):
            continue
        items.append(
            _item(
                title=title,
                url=url,
                platform="google_trends",
                author=source,
                published_at=pub,
                now=now,
                score=traffic,
                why=f"Google Trends daily search ({traffic_raw or 'trending'}) in the last {age_hours(pub, now):.1f} hours.",
                extra={"approx_traffic": traffic_raw},
            )
        )
    return items, "ok" if items else "no-results"


def _fetch_reddit_last30days(now: datetime, window_hours: int = WINDOW_HOURS) -> tuple[list[dict[str, Any]], str]:
    """Best-effort: last30days listing collector. Datacenter IPs often see schema-drift/403."""
    try:
        _ensure_vendor_on_path()
        from lib import reddit_listing
        raw = reddit_listing.fetch_discovery_listings(["all"], query="", depth="quick")
    except Exception as exc:  # noqa: BLE001
        return [], f"error: {exc}"[:200]
    items: list[dict[str, Any]] = []
    for post in raw.get("items") or []:
        created = post.get("created_utc")
        published = datetime.fromtimestamp(int(created), tz=timezone.utc) if created else _parse_iso(str(post.get("date") or ""))
        if not _within_window(published, now, window_hours):
            continue
        url = str(post.get("url") or "")
        if "reddit.com" not in url or "/comments/" not in url:
            # prefer permalink-like urls only
            continue
        score = float((post.get("engagement") or {}).get("score") or post.get("score") or 0)
        comments = int((post.get("engagement") or {}).get("num_comments") or post.get("num_comments") or 0)
        items.append(
            _item(
                title=str(post.get("title") or ""),
                url=url,
                platform="reddit",
                author=str(post.get("author") or ""),
                published_at=published,
                now=now,
                score=score,
                comments=comments,
                why=f"{int(score):,} upvotes in {age_hours(published, now):.1f} hours on Reddit.",
                extra={"id": post.get("id") or "", "subreddit": post.get("subreddit") or ""},
            )
        )
    errors = raw.get("errors") or []
    if items:
        return items, "ok"
    if errors:
        return [], f"schema-drift-or-blocked: {errors[0][:160]}"
    return [], "no-results"



def _safe_url(url: str) -> bool:
    if not url or not url.startswith("http"):
        return False
    if any(ch.isspace() or ch in "<>" for ch in url):
        return False
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        return False
    if not host or any(ord(ch) > 127 for ch in host):
        return False
    if any(bad in url.lower() for bad in ("javascript:", "data:", "hub.aratntx")):
        return False
    return True


def scan(
    niche: str = "",
    *,
    now: datetime | None = None,
    window_hours: int = WINDOW_HOURS,
    summarize: bool = False,
) -> dict[str, Any]:
    """Run a last-24h keyless scan. Never calls Gemini or a paid API."""
    here = now_utc(now)
    sources: dict[str, str] = {}
    errors: list[str] = []
    collected: list[dict[str, Any]] = []

    for name, fetcher in (
        ("hackernews", lambda: _fetch_hackernews(here, niche=niche, window_hours=window_hours)),
        ("google_trends", lambda: _fetch_google_trends(here, window_hours=window_hours)),
        ("reddit", lambda: _fetch_reddit_last30days(here, window_hours=window_hours)),
    ):
        try:
            items, status = fetcher()
        except Exception as exc:  # noqa: BLE001
            items, status = [], f"error: {exc}"[:200]
            logger.warning("last30days source %s failed: %s", name, exc)
        sources[name] = status
        if status.startswith("error") or status.startswith("schema-drift"):
            errors.append(f"{name}: {status}")
        collected.extend(items)

    # Dedup by URL, drop junk/spam links, rank by engagement pace, cap per source
    # so one feed cannot bury the others.
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in collected:
        url = item.get("url") or ""
        if not url or url in seen or not _safe_url(url):
            continue
        seen.add(url)
        unique.append(item)
    unique.sort(key=lambda it: float(it.get("pace") or 0), reverse=True)
    per_source: dict[str, int] = {}
    mixed: list[dict[str, Any]] = []
    for item in unique:
        platform = str(item.get("platform") or "other")
        if per_source.get(platform, 0) >= 8:
            continue
        per_source[platform] = per_source.get(platform, 0) + 1
        mixed.append(item)
    unique = mixed

    if summarize:
        unique = _maybe_summarize(unique, niche)

    return {
        "engine": ENGINE,
        "engine_version": ENGINE_VERSION,
        "repo": "https://github.com/mvanhorn/last30days-skill",
        "window_hours": window_hours,
        "generated_at": here.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "niche": niche or "",
        "gemini_calls": 0,
        "items": unique,
        "top": unique,
        "sources": sources,
        "errors": errors,
        "paid_sources_disabled": list(_PAID_ENV),
    }


def _maybe_summarize(items: list[dict[str, Any]], niche: str) -> list[dict[str, Any]]:
    """Optional Cloudflare rewrite of `why`. Failures leave the template text."""
    if not items:
        return items
    try:
        from content_team.llm import complete_json, provider_status
    except Exception:
        return items
    try:
        if (provider_status().get("active") or "") != "cloudflare":
            return items
    except Exception:
        return items

    snippet = json.dumps(
        [{"title": it["title"], "platform": it["platform"], "why": it["why"]} for it in items[:8]],
        ensure_ascii=False,
    )
    prompt = (
        "Rewrite each item's 'why' in one short sentence for a social-content scout. "
        "Keep facts (points, comments, hours, platform). Do not invent URLs. "
        f"Niche hint: {niche or 'general trending'}. JSON array in, JSON array of "
        '{"title":..., "why":...} out.\n' + snippet
    )
    try:
        data = complete_json(prompt, fallback=[], system="Return only JSON.")
    except Exception:
        return items
    by_title = {}
    if isinstance(data, list):
        for row in data:
            if isinstance(row, dict) and row.get("title") and row.get("why"):
                by_title[str(row["title"]).strip().lower()] = str(row["why"]).strip()
    for item in items:
        rewritten = by_title.get(item["title"].strip().lower())
        if rewritten:
            item["why"] = rewritten
            item["why_source"] = "cloudflare"
    return items


def to_scan_candidates(found: dict[str, Any], *, exclude_urls: Iterable[str] | None = None) -> list[dict[str, Any]]:
    skip = {u for u in (exclude_urls or []) if u}
    notes = (
        f"last30days-skill {ENGINE_VERSION} last-{found.get('window_hours') or WINDOW_HOURS}h keyless scan. "
        "No Gemini. X/TikTok require paid keys and were not used."
    )
    out: list[dict[str, Any]] = []
    for item in found.get("items") or []:
        url = str(item.get("source_url") or item.get("url") or "").strip()
        if not url.startswith("http") or url in skip:
            continue
        out.append(
            {
                "title": item.get("title") or "",
                "source_url": url,
                "platform": item.get("platform") or "",
                "why": item.get("why") or "",
                "topic_tags": [item.get("platform")] if item.get("platform") else [],
                "suggested_start": 0,
                "suggested_duration": 45,
                "notes": notes,
                "age_hours": item.get("age_hours"),
                "published_at": item.get("published_at"),
                "author": item.get("author") or "",
                "grounded": True,
                "engine": ENGINE,
                "gemini_calls": 0,
            }
        )
    return out


def to_scan_result(found: dict[str, Any], *, exclude_urls: Iterable[str] | None = None) -> dict[str, Any]:
    candidates = to_scan_candidates(found, exclude_urls=exclude_urls)
    top = candidates[0] if candidates else {}
    result = {key: top.get(key) for key in SCAN_FIELDS}
    if not result.get("topic_tags"):
        result["topic_tags"] = []
    if not result.get("title"):
        result["title"] = ""
        result["source_url"] = ""
        result["platform"] = ""
        result["why"] = "No keyless last-24h items cleared the window."
        result["notes"] = "last30days-skill found nothing in the last 24 hours (Reddit listings may be blocked from this host)."
        result["suggested_start"] = 0
        result["suggested_duration"] = 45
    result["candidates"] = candidates
    result["engine"] = ENGINE
    result["gemini_calls"] = 0
    result["viral_window_hours"] = found.get("window_hours") or WINDOW_HOURS
    result["viral_sources"] = found.get("sources")
    result["errors"] = found.get("errors") or []
    return result


def to_trends(found: dict[str, Any], brand: str = "") -> list[dict[str, Any]]:
    trends: list[dict[str, Any]] = []
    for item in found.get("items") or []:
        age = item.get("age_hours")
        metrics = []
        if item.get("platform") == "hackernews":
            metrics.append(f"{int(item.get('score') or 0):,} points")
            metrics.append(f"{int(item.get('comments') or 0)} comments")
        elif item.get("platform") == "google_trends":
            traffic = item.get("approx_traffic") or item.get("score")
            metrics.append(f"Google Trends {traffic}")
        elif item.get("score"):
            metrics.append(f"{int(item.get('score') or 0):,} score")
        if age is not None:
            metrics.append(f"{age:.1f}h old")
            if item.get("pace"):
                metrics.append(f"{item['pace']:.0f}/h")
        title = item.get("title") or ""
        trends.append(
            {
                "platform": item.get("platform") or "",
                "url": item.get("url") or "",
                "author": item.get("author") or "",
                "title": title,
                "viral_metrics": ", ".join(metrics),
                "published_at": item.get("published_at"),
                "original_concept": item.get("why") or title,
                "recreated_video_prompt": (
                    f"Original short about: {title}. Keep the facts; do not invent footage of private people."
                    + (f" Brand: {brand}." if brand else "")
                ),
                "age_hours": age,
            }
        )
    return trends


def to_ingest(found: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in found.get("items") or []:
        items.append(
            {
                "title": item.get("title") or "",
                "text": item.get("title") or "",
                "url": item.get("url") or "",
                "source": ENGINE,
                "platform": item.get("platform") or "",
                "author": item.get("author") or "",
                "published_at": item.get("published_at") or "",
                "age_hours": item.get("age_hours"),
                "why": item.get("why") or "",
                "metrics": {
                    "score": item.get("score"),
                    "comments": item.get("comments"),
                    "pace": item.get("pace"),
                },
            }
        )
    return items


def to_viral(found: dict[str, Any]) -> dict[str, Any]:
    """Shape compatible with footage_mode.todays_viral / open_viral.find_viral."""
    top = []
    for item in found.get("items") or []:
        age = float(item.get("age_hours") or 0) or 1.0
        score = float(item.get("score") or 0)
        top.append(
            {
                "title": item.get("title") or "",
                "url": item.get("url") or "",
                "views": int(score),
                "views_per_hour": float(item.get("pace") or (score / age)),
                "age_hours": item.get("age_hours"),
                "channel": item.get("author") or item.get("platform") or "",
                "platform": item.get("platform") or "",
            }
        )
    return {
        "source": ENGINE,
        "window_hours": found.get("window_hours") or WINDOW_HOURS,
        "sources": found.get("sources"),
        "errors": found.get("errors") or [],
        "top": top,
        "gemini_calls": 0,
    }
