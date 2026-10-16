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
    monkeypatch.setenv("AUTOPILOT_TRANSFORM_FALLBACK", "1")
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
    monkeypatch.setenv("AUTOPILOT_TRANSFORM_FALLBACK", "1")
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
    monkeypatch.setenv("AUTOPILOT_TRANSFORM_FALLBACK", "1")
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


def test_transform_fallback_off_by_default(monkeypatch):
    import growth_v2 as g
    monkeypatch.delenv("AUTOPILOT_TRANSFORM_FALLBACK", raising=False)
    assert g.transform_fallback_enabled() is False
    assert g.build_transformed_clip({}, {}) is None


def _fake_main(tmp_path):
    import types
    return types.SimpleNamespace(GENERATED_DIR=str(tmp_path))


def test_failed_veo_retries_with_new_topic_then_succeeds(monkeypatch, tmp_path):
    monkeypatch.setenv("STITCHED_SHORTS", "0")  # these cover the legacy single-clip ladder
    import asyncio
    import growth_v2 as g
    monkeypatch.delenv("AUTOPILOT_TRANSFORM_FALLBACK", raising=False)
    monkeypatch.setenv("GROWTH_RETRY_BACKOFF", "0")
    monkeypatch.setattr(g, "_main", lambda: _fake_main(tmp_path))
    plans, engines = [], []

    def plan(settings, state, extra_avoid=None):
        n = len(plans)
        plans.append([c["topic"] for c in (extra_avoid or [])])
        return {"topic": f"topic{n}", "topic_key": f"k{n}", "veo_prompt": "p", "on_screen_hook": "h", "hook": "h"}

    def gen(concept, settings, engine):
        engines.append(engine)
        if concept["topic"] in ("topic0", "topic1"):
            raise RuntimeError("Veo 503")
        return "/tmp/raw.mp4", {"engine": engine}

    monkeypatch.setattr(g, "plan_concept", plan)
    monkeypatch.setattr(g, "ensure_caption_matches", lambda raw, c, s: (c, {"caption_check": "match"}))
    monkeypatch.setattr(g, "generate_original_clip", gen)
    monkeypatch.setattr(g, "render_vertical", lambda *a, **k: {"width": 1080, "height": 1920})
    monkeypatch.setattr(g, "build_transformed_clip", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no repost")))
    out, concept, info = asyncio.run(g.produce({}, {}))
    assert concept["topic"] == "topic2" and info["attempt"] == 3
    assert plans == [[], ["topic0"], ["topic0", "topic1"]]
    assert engines == ["google_veo_lite"] * 3
    assert info["media_source"] == "ai_generated"


def test_all_attempts_fail_walks_lite_then_fast_and_never_reposts(monkeypatch, tmp_path):
    monkeypatch.setenv("STITCHED_SHORTS", "0")  # these cover the legacy single-clip ladder
    import asyncio
    import pytest
    import growth_v2 as g
    monkeypatch.delenv("AUTOPILOT_TRANSFORM_FALLBACK", raising=False)
    monkeypatch.setenv("GROWTH_RETRY_BACKOFF", "0")
    monkeypatch.setattr(g, "_main", lambda: _fake_main(tmp_path))
    engines = []
    monkeypatch.setattr(g, "plan_concept", lambda s, st, extra_avoid=None: {"topic": f"t{len(extra_avoid or [])}", "topic_key": f"k{len(extra_avoid or [])}", "veo_prompt": "p", "on_screen_hook": "h", "hook": "h"})
    monkeypatch.setattr(g, "generate_original_clip", lambda c, s, e: engines.append(e) or (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(g, "build_transformed_clip", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no repost")))
    with pytest.raises(RuntimeError, match="All 5 original video attempts failed"):
        asyncio.run(g.produce({}, {}))
    assert engines == ["google_veo_lite"] * 3 + ["google_veo_fast"] * 2


def _concept(topic="drop"):
    return {"topic": topic, "topic_key": topic, "veo_prompt": "p", "on_screen_hook": "3,000 mph glass crack",
            "hook": "Snapping its tail destroys this glass drop", "lines": ["The bulb survives a hammer."]}


def test_caption_mismatch_is_rewritten_from_frames(monkeypatch):
    import growth_v2 as g
    calls = []

    def verify(raw, concept, settings):
        calls.append(concept["hook"])
        if len(calls) == 1:
            return {"matches": False, "mismatches": ["no hammer", "bulb stays intact"], "visible_summary": "pliers snap the tail; dust cloud",
                    "rewrite": {"on_screen_hook": "Snap the tail, watch", "hook": "Pliers snap the tail of a glass drop",
                                "lines": ["Fine glass dust bursts from the tail as stress releases."]}}
        return {"matches": True, "visible_summary": "pliers snap the tail; dust cloud"}

    monkeypatch.setattr(g, "verify_clip_matches", verify)
    fixed, info = g.ensure_caption_matches("/tmp/x.mp4", _concept(), {})
    assert fixed["hook"] == "Pliers snap the tail of a glass drop"
    assert fixed["caption_rewritten_from_frames"] and fixed["original_caption"]["hook"].startswith("Snapping")
    assert info["caption_check"] == "rewritten_from_frames" and len(calls) == 2


def test_caption_still_mismatched_raises(monkeypatch):
    import pytest
    import growth_v2 as g
    monkeypatch.setattr(g, "verify_clip_matches", lambda raw, c, s: {"matches": False, "mismatches": ["x"], "rewrite": {"hook": "Some new hook here", "lines": ["l"]}})
    with pytest.raises(RuntimeError, match="mismatch after rewrite"):
        g.ensure_caption_matches("/tmp/x.mp4", _concept(), {})


def test_mismatch_counts_toward_retry_with_new_topic(monkeypatch, tmp_path):
    monkeypatch.setenv("STITCHED_SHORTS", "0")  # these cover the legacy single-clip ladder
    import asyncio
    import growth_v2 as g
    monkeypatch.delenv("AUTOPILOT_TRANSFORM_FALLBACK", raising=False)
    monkeypatch.setenv("GROWTH_RETRY_BACKOFF", "0")
    monkeypatch.setattr(g, "_main", lambda: _fake_main(tmp_path))
    monkeypatch.setattr(g, "plan_concept", lambda s, st, extra_avoid=None: _concept(f"t{len(extra_avoid or [])}"))
    monkeypatch.setattr(g, "generate_original_clip", lambda c, s, e: ("/tmp/raw.mp4", {"engine": e}))

    def ensure(raw, c, s):
        if c["topic"] == "t0":
            raise RuntimeError("caption mismatch after rewrite")
        return c, {"caption_check": "match"}

    monkeypatch.setattr(g, "ensure_caption_matches", ensure)
    monkeypatch.setattr(g, "render_vertical", lambda *a, **k: {})
    _, concept, info = asyncio.run(g.produce({}, {}))
    assert concept["topic"] == "t1" and info["attempt"] == 2 and info["caption_check"] == "match"


def test_sanitize_concept_appends_shot_list_to_prompt():
    import growth_v2 as g
    c = g.sanitize_concept({"veo_prompt": "Macro shot of a glass drop.", "hook": "Pliers snap a glass drop tail",
                            "shot_list": [{"t": "0-2s", "shows": "pliers grip the thin tail"}, {"t": "2-5s", "shows": "tail snaps"},
                                          {"t": "5-8s", "shows": "whole drop bursts into powder"}]})
    assert len(c["shot_list"]) == 3 and "Shot list, in order: 0-2s: pliers grip the thin tail." in c["veo_prompt"]


def test_topic_similarity_catches_rupert_variants():
    import growth_v2 as g
    recent = ["Prince Rupert's Drop Explosion", "prince ruperts drop glass physics"]
    for variant in ["Prince Rupert's Drop Shatter", "prince ruperts drop explosive shatter", "Rupert drops: unbreakable glass"]:
        assert g.is_recent_topic({"topic": variant, "topic_key": variant}, recent), variant
    assert not g.is_recent_topic({"topic": "Nitinol Shape Memory Alloy", "topic_key": "nitinol shape memory wire"}, recent)
    assert not g.topics_similar("Glass drop tail", "Mantis shrimp punch")


def test_plan_concept_rejects_recent_post_topic(monkeypatch):
    import types
    from datetime import datetime, timezone
    import growth_v2 as g
    now = datetime.now(timezone.utc).isoformat()
    posts = [{"posted_at": now, "hook": "Snapping its tail destroys this bulletproof glass drop",
              "concept": {"topic": "Prince Rupert's Drop Explosion", "topic_key": "prince ruperts drop glass physics"}}]
    replies = iter([
        {"topic": "Prince Rupert's Drop Shatter", "topic_key": "prince ruperts drop explosive shatter", "veo_prompt": "p", "hook": "Clipping this glass tail cracks it at 3,000 mph"},
        {"topic": "Nitinol Shape Memory Alloy", "topic_key": "nitinol shape memory wire", "veo_prompt": "p", "hook": "This bent wire snaps back at 70 degrees C"},
    ])
    prompts = []

    def gem(settings, prompt, fallback, use_search=False):
        prompts.append(prompt)
        return next(replies)

    fake = types.SimpleNamespace(load_scheduled_posts=lambda: posts, load_growth_os=lambda: {}, call_gemini_json=gem)
    monkeypatch.setattr(g, "_main", lambda: fake)
    c = g.plan_concept({}, {"history": []})
    assert c["topic"] == "Nitinol Shape Memory Alloy"
    assert "Prince Rupert's Drop Explosion" in prompts[0]


def test_veo_vertical_config():
    import orchestrator
    assert orchestrator.gemini_veo_config_kwargs("9:16") == {"aspect_ratio": "9:16", "number_of_videos": 1}
    assert orchestrator.gemini_veo_config_kwargs("weird")["aspect_ratio"] == "16:9"


def test_captions_title_line_and_question_no_series():
    import growth_v2 as g
    c = {"hook": "This drop cracks at 3,000 mph", "lines": ["Stress is locked in by fast cooling."], "hashtags": ["#Physics"],
         "comment_question": "Would you hold one while it snaps", "episode": 4, "title_line": "Glass That Explodes"}
    caps = g.build_captions(c)
    assert set(caps) == {"tiktok", "youtube", "instagram", "linkedin", "twitter"}
    for text in caps.values():
        assert text.startswith("Glass That Explodes")  # one shared top line per run
        assert "Engineering at the Extreme" not in text and "#4" not in text
    assert caps["tiktok"].startswith("Glass That Explodes\nThis drop cracks at 3,000 mph")
    assert "Would you hold one while it snaps?" in caps["tiktok"] and "Would you hold one" in caps["linkedin"]
    assert len(caps["twitter"]) <= 280
    rep = g.build_captions(c, {"creator": "someone", "url": "u"})
    assert "Glass That Explodes" not in rep["tiktok"] and "?" not in rep["tiktok"].split("\n\n")[0][-1:]
    assert not hasattr(g, "SERIES_NAME") and not hasattr(g, "next_episode")


def test_title_clean_and_repeat_check():
    import growth_v2 as g
    assert g.clean_title("Episode 4: Molten Rail Magic #4") == "Molten Rail Magic"
    assert g.clean_title("Engineering at the Extreme #7") == ""
    assert g.title_repeats("Molten rail magic!", ["molten Rail Magic"])
    assert g.title_repeats("The Molten Rail Magic", ["Molten Rail Magic"])  # 3/3 overlap
    assert g.title_repeats("Glass That Fights Back", ["Molten Rail Magic"]) is None
    assert g.title_repeats("", []) == "(empty)"


def test_plan_concept_rejects_repeated_title(monkeypatch):
    import types
    import growth_v2 as g
    replies = iter([
        {"topic": "Liquid Metal Porcupine", "topic_key": "ferrofluid spikes", "veo_prompt": "p", "hook": "Spikes at 1 tesla", "on_screen_hook": "1 tesla spikes", "title_line": "Liquid Metal Porcupine"},
        {"topic": "Tungsten", "topic_key": "tungsten melting point", "veo_prompt": "p", "hook": "Tungsten melts at 3,422C", "on_screen_hook": "Melts at 3,422°C", "title_line": "The Metal That Refuses"},
    ])
    fake = types.SimpleNamespace(load_scheduled_posts=lambda: [{"concept": {"title_line": "Liquid metal porcupine"}}],
                                 load_growth_os=lambda: {}, call_gemini_json=lambda *a, **k: next(replies))
    monkeypatch.setattr(g, "_main", lambda: fake)
    c = g.plan_concept({}, {"history": []})
    assert c["title_line"] == "The Metal That Refuses"


def test_number_required_in_on_screen_hook(monkeypatch):
    import types
    import growth_v2 as g
    replies = iter([
        {"topic": "Ferrofluid", "topic_key": "ferrofluid spikes", "veo_prompt": "p", "hook": "Liquid grows spikes", "on_screen_hook": "Liquid grows spikes"},
        {"topic": "Tungsten", "topic_key": "tungsten melting point", "veo_prompt": "p", "hook": "Tungsten melts at 3,422C", "on_screen_hook": "Melts at 3,422°C"},
    ])
    fake = types.SimpleNamespace(load_scheduled_posts=lambda: [], load_growth_os=lambda: {}, call_gemini_json=lambda *a, **k: next(replies))
    monkeypatch.setattr(g, "_main", lambda: fake)
    c = g.plan_concept({}, {"history": []})
    assert c["topic"] == "Tungsten" and g.has_number(c["on_screen_hook"])


def test_origin_tags():
    import growth_v2 as g
    assert g.origin_of("ai_generated") == "original"
    assert g.origin_of("original_download") == "repost" and g.origin_of("transformed_repost") == "repost"
    assert g.origin_of(None) == "unknown"


def _ffmpeg():
    import shutil
    return shutil.which("ffmpeg") and shutil.which("ffprobe")


def test_render_native_vertical_no_blur_and_audio_fade(tmp_path):
    import subprocess
    import pytest
    import growth_v2 as g
    if not _ffmpeg():
        pytest.skip("ffmpeg not installed")
    src = str(tmp_path / "v.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=720x1280:rate=24:duration=2",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-shortest", "-c:v", "libx264", "-c:a", "aac", src], check=True)
    out = str(tmp_path / "o.mp4")
    info = g.render_vertical(src, out, "", None, "", 15.0, False)
    assert (info["width"], info["height"]) == (1080, 1920) and info["native_vertical_source"] is True and info["has_audio"]
    tail = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-sseof", "-0.08", "-i", out, "-vn", "-af", "volumedetect", "-f", "null", "-"],
                          capture_output=True, text=True).stderr
    head = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-t", "0.5", "-i", out, "-vn", "-af", "volumedetect", "-f", "null", "-"],
                          capture_output=True, text=True).stderr
    import re
    mv = lambda t: float(re.search(r"mean_volume: (-?[\d.]+)", t).group(1))
    assert mv(tail) < mv(head) - 6


def test_followers_and_post_stats_parsing(monkeypatch):
    import types
    import growth_v2 as g
    responses = {
        "/profiles": {"data": [{"id": "yt", "platform": "youtube", "name": "Bret"}, {"id": "li", "platform": "linkedin", "name": "Bret"}]},
        "/profiles/yt": {"latest_stats": [{"placement_id": None, "stats": {"subscriberCount": 75}, "recorded_at": "t"}]},
        "/profiles/li": {"latest_stats": [{"placement_id": "112382931", "stats": {"followerCount": 42}, "recorded_at": "t"}]},
    }

    def get(settings, path, timeout=30):
        if path.startswith("/posts/stats"):
            return {"data": {"pp1": {"platforms": [{"platform": "tiktok", "records": [
                {"stats": {"impressions": 100, "likes": 1}, "recorded_at": "a"}, {"stats": {"impressions": 306, "likes": 3, "comments": 1}, "recorded_at": "b"}]}]}}}
        return responses[path]

    monkeypatch.setattr(g, "_main", lambda: types.SimpleNamespace(postproxy_get=get))
    state = {}
    snap = g.snapshot_followers({}, state, force=True)
    assert snap["by_platform"]["youtube"]["followers"] == 75 and snap["by_platform"]["linkedin:112382931"]["followers"] == 42
    assert state["follower_history"][-1]["youtube"] == 75
    assert g.snapshot_followers({}, state) is None  # throttled
    st = g.fetch_post_stats({}, ["pp1"])
    assert st["pp1"]["tiktok"]["impressions"] == 306 and st["pp1"]["tiktok"]["comments"] == 1


def test_opening_sound_added_when_silent(tmp_path):
    import subprocess
    import pytest
    import growth_v2 as g
    if not _ffmpeg():
        pytest.skip("ffmpeg not installed")
    src = str(tmp_path / "quiet.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=720x1280:rate=24:duration=3",
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "3", "-c:v", "libx264", "-c:a", "aac", src], check=True)
    out, info = g.ensure_opening_sound(src)
    assert info["sfx_added"] and out != src
    assert info["opening_db_after"] >= g.OPENING_SOUND_MIN_DB
    assert abs(g.probe_video(out)["duration"] - 3.0) < 0.1


def test_rewrite_keeps_number(monkeypatch):
    import growth_v2 as g
    calls = []

    def verify(raw, concept, settings):
        calls.append(concept["on_screen_hook"])
        if len(calls) == 1:
            return {"matches": False, "mismatches": ["x"], "rewrite": {"on_screen_hook": "Heat without contact", "hook": "Watch metal glow without any flame", "lines": ["Coils heat it."]}}
        return {"matches": True}

    monkeypatch.setattr(g, "verify_clip_matches", verify)
    c = dict(_concept(), on_screen_hook="2,000°C without contact")
    fixed, info = g.ensure_caption_matches("/x.mp4", c, {})
    assert g.has_number(fixed["on_screen_hook"]) and fixed["on_screen_hook"].startswith("2,000°C")
