from __future__ import annotations

from datetime import datetime, timedelta, timezone

import last30days_scan as ls


NOW = datetime(2026, 11, 17, 16, 0, tzinfo=timezone.utc)


def _fresh(hours, object_id="99", title="Fresh HN story"):
    published = NOW - timedelta(hours=hours)
    return {
        "objectID": object_id,
        "title": title,
        "url": "https://example.com/post",
        "author": "pg",
        "points": 1200,
        "num_comments": 200,
        "created_at_i": int(published.timestamp()),
    }


def test_age_filter_drops_items_older_than_24h(monkeypatch):
    stale = _fresh(30, object_id="1", title="Old")
    fresh = _fresh(5)

    def fake_request(method, url, timeout=30):
        return {"hits": [stale, fresh]}

    ls._ensure_vendor_on_path()
    import lib.http as last_http
    monkeypatch.setattr(last_http, "request", fake_request)
    monkeypatch.setattr(ls, "_fetch_google_trends", lambda now, window_hours=24: ([], "ok"))
    monkeypatch.setattr(ls, "_fetch_reddit_last30days", lambda now, window_hours=24: ([], "schema-drift"))
    found = ls.scan(now=NOW)
    assert found["gemini_calls"] == 0
    assert [it["url"] for it in found["items"]] == ["https://news.ycombinator.com/item?id=99"]
    assert found["items"][0]["age_hours"] == 5.0


def test_to_trends_and_ingest_shapes(monkeypatch):
    found = {
        "engine": "last30days", "window_hours": 24, "sources": {"hackernews": "ok"}, "errors": [],
        "items": [{
            "title": "Qwen4-Max", "url": "https://news.ycombinator.com/item?id=5",
            "platform": "hackernews", "author": "x", "published_at": "2026-11-17T10:00:00Z",
            "age_hours": 6, "score": 1400, "comments": 90, "pace": 233,
            "why": "1,400 points in 6 hours on Hacker News.",
        }],
    }
    trends = ls.to_trends(found, brand="6Frame Studio")
    assert trends[0]["url"].startswith("https://")
    for key in ("platform", "url", "author", "title", "viral_metrics", "original_concept", "recreated_video_prompt"):
        assert key in trends[0]
    ingest = ls.to_ingest(found)
    assert ingest[0]["source"] == "last30days" and ingest[0]["url"] == trends[0]["url"]


def test_scanner_default_does_not_call_gemini(monkeypatch):
    import orchestrator
    monkeypatch.delenv("VIRAL_SOURCE", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    found = {
        "engine": "last30days", "window_hours": 24, "sources": {"hackernews": "ok"}, "errors": [],
        "items": [{
            "title": "TEST last 24h item", "url": "https://news.ycombinator.com/item?id=1",
            "platform": "hackernews", "author": "pg", "published_at": "2026-11-17T10:00:00Z",
            "age_hours": 2.0, "score": 400, "comments": 80, "pace": 200, "why": "400 points in 2 hours",
        }],
    }
    monkeypatch.setattr(ls, "scan", lambda **k: found)
    orchestrator.run_live_trend_scanner("job-l30", {"brand_name": "6Frame Studio"})
    st = orchestrator.get_job_status("job-l30")
    assert st["status"] == "SUCCESS", st
    assert st["result"]["source"] == "last30days"
    assert st["result"]["gemini_calls"] == 0
    assert st["result"]["engine"] == "last30days"
