import os
from datetime import datetime, timedelta, timezone

import open_viral as ov

NOW = datetime(2026, 11, 11, 21, 0, tzinfo=timezone.utc)


def _iso(h_ago):
    return (NOW - timedelta(hours=h_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _videos(ids, key):
    table = {
        "fast": dict(title="AI short film: the last astronaut #aivideo", author="A", published_at=_iso(2), views=40_000, seconds=30),
        "slow": dict(title="Veo 3 cinematic trailer", author="B", published_at=_iso(20), views=100_000, seconds=45),
        "old": dict(title="AI video from yesterday", author="C", published_at=_iso(30), views=9_000_000, seconds=20),
        "long": dict(title="AI film full movie", author="D", published_at=_iso(3), views=500_000, seconds=3600),
        "offniche": dict(title="cat compilation", author="E", published_at=_iso(1), views=900_000, seconds=20),
        "dupe": dict(title="AI short film: The Last Astronaut", author="A", published_at=_iso(3), views=10_000, seconds=30),
    }
    return {i: {"platform": "YouTube", "id": i, "url": f"https://www.youtube.com/watch?v={i}", "description": "", **table[i]}
            for i in ids if i in table}


def _run(monkeypatch, key="k", **kw):
    monkeypatch.setenv("YOUTUBE_API_KEY", key) if key else monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    return ov.find_viral(queries=["q"], subreddits=[], now=NOW,
                         yt_search=lambda q, k, since: ["fast", "slow", "old"],
                         yt_list=_videos,
                         dlp_search=lambda q: [{"id": "long"}, {"id": "offniche"}, {"id": "dupe"}], **kw)


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("VIRAL_SOURCE", raising=False)
    assert ov.enabled() is False
    monkeypatch.setenv("VIRAL_SOURCE", "open")
    assert ov.enabled() is True


def test_ranks_by_views_per_hour_last_24h_only_and_dedupes(monkeypatch):
    r = _run(monkeypatch)
    ids = [x["id"] for x in r["top"]]
    assert ids == ["fast", "slow"]  # 20k/h beats 5k/h; old (>24h), long (>180s), off-niche and dupe dropped
    assert r["top"][0]["views_per_hour"] == 20_000.0
    assert r["top"][1]["age_hours"] == 20.0


def test_no_key_uses_ytdlp_probe(monkeypatch):
    probed = []

    def probe(vid):
        probed.append(vid)
        return _videos([vid], None).get(vid)
    r = _run(monkeypatch, key="", dlp_probe=probe)
    assert r["sources"]["youtube_api"] == "no YOUTUBE_API_KEY"
    assert probed == ["long", "offniche", "dupe"]
    assert [x["id"] for x in r["top"]] == ["dupe"]


def test_reddit_ranked_separately_by_upvotes(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)

    def reddit(sub, token):
        return [{"platform": "Reddit", "id": "a", "url": "https://www.reddit.com/r/aivideo/comments/a/x/", "title": "t",
                 "author": "u/x", "published_at": _iso(4), "upvotes": 800, "comments": 10, "views": None},
                {"platform": "Reddit", "id": "b", "url": "https://www.reddit.com/r/aivideo/comments/b/y/", "title": "t2",
                 "author": "u/y", "published_at": _iso(26), "upvotes": 9000, "comments": 1, "views": None}]
    r = ov.find_viral(queries=["q"], subreddits=["aivideo"], now=NOW, dlp_search=lambda q: [], reddit=reddit,
                      reddit_token=lambda: None)
    assert [x["id"] for x in r["reddit"]] == ["a"] and r["reddit"][0]["upvotes_per_hour"] == 200.0
    assert r["top"] == []


def test_errors_never_raise(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setenv("YOUTUBE_API_KEY", "k")
    r = ov.find_viral(queries=["q"], subreddits=["aivideo"], now=NOW, yt_search=boom, dlp_search=boom, reddit=boom,
                      reddit_token=lambda: None)
    assert r["top"] == [] and r["sources"]["youtube_api"] == "failed" and r["sources"]["reddit"] == "failed"
    assert len(r["errors"]) == 3


def test_to_trends_shape(monkeypatch):
    trends = ov.to_trends(_run(monkeypatch))
    t = trends[0]
    for k in ("platform", "url", "author", "title", "viral_metrics", "original_concept", "recreated_video_prompt"):
        assert k in t
    assert "views/h" in t["viral_metrics"] and t["url"].startswith("https://www.youtube.com/watch?v=")


def test_iso_duration():
    assert ov.iso_duration_seconds("PT1M5S") == 65 and ov.iso_duration_seconds("PT45S") == 45
    assert ov.iso_duration_seconds("") is None


def test_trend_scanner_uses_open_source_without_gemini(monkeypatch):
    os.environ.setdefault("DISABLE_BACKGROUND_SCHEDULER", "true")
    import orchestrator
    monkeypatch.setenv("VIRAL_SOURCE", "open")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    result = _run(monkeypatch)
    monkeypatch.setattr(ov, "find_viral", lambda: result)
    orchestrator.run_live_trend_scanner("job-open", {"brand_name": "6Frame Studio"})
    st = orchestrator.get_job_status("job-open")
    assert st["status"] == "SUCCESS", st
    assert st["result"]["source"] == "open"
    assert [t["url"] for t in st["result"]["trends"]][:1] == ["https://www.youtube.com/watch?v=fast"]


def test_trend_scanner_default_path_unchanged(monkeypatch):
    import orchestrator
    monkeypatch.delenv("VIRAL_SOURCE", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    called = []
    monkeypatch.setattr(ov, "find_viral", lambda: called.append(1))
    orchestrator.run_live_trend_scanner("job-default", {})
    st = orchestrator.get_job_status("job-default")
    assert st["status"] == "FAILED" and "Gemini" in st["message"] and not called
