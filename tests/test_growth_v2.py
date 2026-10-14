import os
import sys
import importlib
from datetime import datetime, timezone

os.environ.setdefault("DISABLE_BACKGROUND_SCHEDULER", "true")
import growth_v2 as g


def test_youtube_title_never_uses_junk_campaign_title():
    post = {"campaign_title": "Autonomous: Public TikTok original", "text": "This liquid grows spikes near a magnet\nmore"}
    title = g.youtube_title_for(post)
    assert title == "This liquid grows spikes near a magnet"
    assert len(g.youtube_title_for({"hook": "x" * 200})) <= 60
    assert g.youtube_title_for({"campaign_title": "Autopilot: Rockets make diamonds"}) == "Rockets make diamonds"


def test_captions_short_no_links_no_brand_opener():
    concept = dict(g.FALLBACK_CONCEPTS[0])
    concept["lines"] = concept["lines"] + ["see https://example.com now"]
    caps = g.build_captions(concept, credit={"creator": "someone", "url": "https://tiktok.com/@someone/video/1"})
    for p in ("tiktok", "youtube", "instagram", "twitter"):
        assert "http" not in caps[p] and "www." not in caps[p]
        assert "At 6Frame Studio" not in caps[p]
    tags = [w for w in caps["tiktok"].split() if w.startswith("#")]
    assert 3 <= len(tags) <= 5
    body_lines = [l for l in caps["tiktok"].split("\n\n")[0].splitlines() if l]
    assert len(body_lines) <= 4  # hook + up to 2 lines + credit
    assert len(caps["twitter"]) <= 280
    assert "https://tiktok.com" in caps["linkedin"]


def test_hashtags_normalized_and_brand_removed():
    tags = g.normalize_hashtags(["#6FrameStudio", "science", "#Physics", "physics", "123"])
    assert "#6FrameStudio" not in tags and 3 <= len(tags) <= 5
    assert len(g.normalize_hashtags(["a", "b", "c", "d", "e", "f", "g"])) == 5


def test_due_slot_stops_after_max_attempts(monkeypatch, tmp_path):
    monkeypatch.setattr(g, "slot_hours", lambda s: [15])
    monkeypatch.setattr(g, "slot_succeeded", lambda slot: False)
    now = datetime(2026, 10, 14, 15, 5)
    slot = "2026-10-14-15"
    assert g.due_slot(now, {}, {"slots": {}}) == slot
    assert g.due_slot(now, {}, {"slots": {slot: {"attempts": 2}}}) is None
    assert g.due_slot(now, {}, {"slots": {slot: {"status": "gave_up"}}}) is None
    assert g.due_slot(datetime(2026, 10, 14, 16, 5), {}, {"slots": {}}) is None


def test_legacy_cockpit_import_is_stubbed():
    assert g.stub_legacy_boot_patches()
    mod = importlib.import_module("cockpit_v10_isolate")
    assert mod.STUBBED_BY == g.MARKER
    mod.apply()
    assert "cockpit_v10_isolate" in g.skipped_legacy_modules()


def test_performance_requires_real_data():
    assert g.post_performance({"metrics": {}}) is None
    perf = g.post_performance({"metrics": {"public": {"tiktok": {"views": 120, "likes": 3}},
                                           "postproxy": {"youtube": {"impressions": 40}}}})
    assert perf["views"] == 160 and perf["engagements"] == 3


def test_youtube_bot_wall_is_not_reported_as_unavailable():
    html = '"playabilityStatus":{"status":"LOGIN_REQUIRED","reason":"Sign in to confirm you\u2019re not a bot"}'
    assert g.parse_youtube_html(html) is None
    gone = g.parse_youtube_html('"playabilityStatus":{"status":"ERROR","reason":"Video unavailable"}')
    assert gone["available"] is False
    assert g.health()["media_tools"]["ffmpeg"] is True


def test_transform_fallback_tries_next_source_and_prefers_non_reddit(monkeypatch):
    import types
    import growth_v2 as g
    trends = [
        {"url": "https://www.reddit.com/r/aivideo/comments/aaa/x/", "title": "r1"},
        {"url": "https://www.youtube.com/watch?v=dead", "title": "y1"},
        {"url": "https://www.youtube.com/watch?v=good", "title": "y2"},
    ]
    tried = []

    def dl(url, title, *a, **k):
        tried.append(url)
        if "good" not in url:
            raise RuntimeError("blocked")
        return "/tmp/in.mp4"

    fake = types.SimpleNamespace(
        load_growth_os=lambda: {"last_trend_scan": trends},
        download_and_trim_original_video=dl,
        call_gemini_json=lambda *a, **k: {"hook": "Why this works", "on_screen_hook": "Why", "commentary": ["Line one fact", "Line two fact"]},
        GENERATED_DIR="/tmp",
    )
    monkeypatch.setattr(g, "_main", lambda: fake)
    monkeypatch.setattr(g, "render_vertical", lambda *a, **k: {"width": 1080, "height": 1920})
    state = {}
    out, concept, info = g.build_transformed_clip({}, state)
    assert tried == ["https://www.youtube.com/watch?v=dead", "https://www.youtube.com/watch?v=good"]
    assert info["source"]["url"].endswith("good")
    assert "https://www.youtube.com/watch?v=dead" in state["failed_source_urls"]


def test_transform_copy_uses_frames_when_available(monkeypatch):
    import types
    import growth_v2 as g
    fake = types.SimpleNamespace(
        load_growth_os=lambda: {"last_trend_scan": [{"url": "https://www.youtube.com/watch?v=ok", "title": "Animal physics"}]},
        download_and_trim_original_video=lambda *a, **k: "/tmp/in.mp4",
        call_gemini_json=lambda *a, **k: (_ for _ in ()).throw(AssertionError("title-only path should not run")),
        GENERATED_DIR="/tmp",
    )
    seen = {}
    monkeypatch.setattr(g, "_main", lambda: fake)
    monkeypatch.setattr(g, "extract_frames", lambda path, n: [b"a", b"b", b"c", b"d", b"e"])

    def vision(settings, prompt, frames):
        seen["frames"] = len(frames)
        seen["prompt"] = prompt
        return {"hook": "Social apps on a desk", "on_screen_hook": "Social apps", "commentary": ["Woman at an iMac", "Facebook and Instagram icons"]}

    monkeypatch.setattr(g, "call_gemini_vision_json", vision)
    captured = {}
    monkeypatch.setattr(g, "render_vertical", lambda src, dst, hook, commentary, footer, *a, **k: captured.update(hook=hook, commentary=commentary, footer=footer) or {})
    out, concept, info = g.build_transformed_clip({}, {})
    assert seen["frames"] == 5 and "Animal physics" in seen["prompt"]
    assert info["copy_mode"] == "vision" and info["frames_sent"] == 5
    assert captured["commentary"] == ["Woman at an iMac", "Facebook and Instagram icons"]
    assert captured["footer"].startswith("Commentary: 6Frame Studio") or "6Frame Studio" in captured["footer"]


def test_transform_copy_falls_back_to_title_when_no_frames(monkeypatch):
    import types
    import growth_v2 as g
    fake = types.SimpleNamespace(
        load_growth_os=lambda: {"last_trend_scan": [{"url": "https://www.youtube.com/watch?v=ok", "title": "T"}]},
        download_and_trim_original_video=lambda *a, **k: "/tmp/in.mp4",
        call_gemini_json=lambda *a, **k: {"hook": "Title hook", "on_screen_hook": "T", "commentary": ["one", "two"]},
        GENERATED_DIR="/tmp",
    )
    monkeypatch.setattr(g, "_main", lambda: fake)
    monkeypatch.setattr(g, "extract_frames", lambda path, n: [])
    monkeypatch.setattr(g, "render_vertical", lambda *a, **k: {})
    _, _, info = g.build_transformed_clip({}, {})
    assert info["copy_mode"] == "title_only"


def test_x_quota_detection_matches_real_postproxy_text():
    import growth_v2 as g
    real = "24 hour Twitter quota exceeded. Top up your wallet to keep posting or consider using BYOK."
    assert g.is_x_quota_error("twitter: " + real)
    assert g.is_x_quota_error(real)
    assert g.is_x_quota_error("Daily limit hit, 24 hour window", ["twitter"])
    assert not g.is_x_quota_error("linkedin: token expired")
    state = {}
    g.inspect_publish_errors(state, {"id": "p"}, ["twitter: " + real]) if False else None
