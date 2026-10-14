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
