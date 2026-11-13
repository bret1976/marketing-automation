import json
import os
import shutil
import subprocess

import re

import pytest

os.environ.setdefault("ADMIN_PASSWORD", "test-password")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("DISABLE_BACKGROUND_SCHEDULER", "true")

import footage_mode as fm
import footage_topics as ft


def test_flag_is_off_by_default(monkeypatch):
    monkeypatch.delenv("VIDEO_ENGINE", raising=False)
    assert fm.enabled() is False
    monkeypatch.setenv("VIDEO_ENGINE", "footage")
    assert fm.enabled() is True


def test_topic_library_is_complete():
    keys = set()
    for t in ft.TOPICS:
        assert t["key"] not in keys
        keys.add(t["key"])
        for f in ("topic", "title_line", "on_screen_hook", "hook", "vo", "lines", "hashtags", "match", "require", "queries"):
            assert t.get(f), (t["key"], f)
        assert 7 <= len(t["vo"]) <= 14 and all(v["say"] and v["show"] for v in t["vo"])
        # long enough to carry a 31-55 s Short at an unhurried read (~2.3 words/s), short enough to fit 55 s
        words = sum(len(v["say"].split()) for v in t["vo"])
        assert 70 <= words <= 125, (t["key"], words)
        assert all(len(v["show"]) <= 60 for v in t["vo"]), t["key"]
        assert not any("8 second" in x or "8-second" in x for x in t["lines"] + [t["hook"]])


def test_score_topics_uses_views_per_hour():
    items = [{"title": "World War II: Behind Enemy Lines | Official Trailer", "views_per_hour": 30000},
             {"title": "D-Day 1944 - Real Footage", "views_per_hour": 2},
             {"title": "Minecraft Titanic Sinking", "views_per_hour": 1188}]
    ranked = fm.score_topics(items)
    by = {r["topic"]["key"]: r["score"] for r in ranked}
    assert by["titanic-1912"] == 1188
    assert by["dday-1944"] == pytest.approx(30000 * 0.25 + 2)
    assert by["apollo11-1969"] == 0


def test_pick_topic_skips_recent():
    items = [{"title": "Titanic", "views_per_hour": 100}]
    assert fm.pick_topic(items)["topic"]["key"] == "titanic-1912"
    assert fm.pick_topic(items, avoid_keys={"titanic-1912"})["topic"]["key"] != "titanic-1912"


def test_beat_cuts_snap_to_beats():
    beats = [0.0, 0.557, 1.115, 1.672, 2.252, 2.81, 3.367, 3.924, 4.505, 5.062, 5.619, 6.177, 6.734, 7.3]
    cuts = fm.beat_cuts(beats)
    assert cuts == [2.81, 5.062]  # nearest beats to 2.667 and 5.333
    assert all(c in beats for c in cuts)


def test_music_manifest_is_cc0_with_beats():
    with open(os.path.join(fm.MUSIC_DIR, "manifest.json")) as fh:
        tracks = json.load(fh)
    assert len(tracks) >= 3
    for t in tracks:
        assert t["license"] == "CC0 1.0" and "freesound.org" in t["source"]
        assert os.path.exists(os.path.join(fm.MUSIC_DIR, t["file"]))
        assert len([b for b in t["beats"] if b < 8]) >= 8
        # beds cover the longest allowed clip without looping
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                    os.path.join(fm.MUSIC_DIR, t["file"])], capture_output=True, text=True).stdout or 0)
        assert dur >= fm.MAX_SECONDS and t["seconds"] >= fm.MAX_SECONDS
        assert t["beats"][-1] >= 40


@pytest.mark.parametrize("name,ok", [("Public domain", True), ("CC0", True), ("CC BY 2.0", True), ("CC BY 4.0", True),
                                     ("CC BY-SA 4.0", False), ("CC BY-NC 2.0", False), ("CC BY-ND 3.0", False),
                                     ("Fair use", False), ("", False)])
def test_license_filter(name, ok):
    assert fm.license_ok(name) is ok


class _Resp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


def test_commons_search_keeps_only_reusable_video_and_strips_query():
    pages = {
        "1": {"title": "File:Normandy Invasion.webm", "index": 1, "imageinfo": [{
            "url": "https://upload.wikimedia.org/x/Normandy_Invasion.webm?utm_source=commons", "width": 1920, "height": 1080,
            "duration": 1237.8, "descriptionurl": "https://commons.wikimedia.org/wiki/File:Normandy_Invasion.webm",
            "extmetadata": {"LicenseShortName": {"value": "Public domain"}, "Artist": {"value": "<a>US Army</a>"}}}]},
        "2": {"title": "File:Some talk.webm", "index": 2, "imageinfo": [{
            "url": "https://upload.wikimedia.org/x/talk.webm", "extmetadata": {"LicenseShortName": {"value": "CC BY-SA 4.0"}}}]},
    }
    out = fm.search_commons("normandy", "video", get=lambda *a, **k: _Resp({"query": {"pages": pages}}))
    assert len(out) == 1
    assert out[0]["url"].endswith(".webm") and "?" not in out[0]["url"]
    assert out[0]["license"] == "Public domain" and out[0]["author"] == "US Army"


def test_archive_trusts_only_curated_public_domain_collections():
    docs = [{"identifier": "a", "title": "Eve of Battle, 1944/06/06", "collection": ["universal_newsreels"]},
            {"identifier": "b", "title": "Titanic 1997 Deleted Scenes", "collection": ["opensource_movies"],
             "licenseurl": "http://creativecommons.org/publicdomain/mark/1.0/"}]
    out = fm.search_archive("x", get=lambda *a, **k: _Resp({"response": {"docs": docs}}))
    assert [o["id"] for o in out] == ["a"]
    assert "public domain" in out[0]["license"].lower()


def test_gather_filters_offtopic_and_talking_heads():
    topic = dict(ft.BY_KEY["dday-1944"])
    topic["queries"] = {"commons": ["q"], "archive": [], "nasa": []}
    vids = [
        {"provider": "wikimedia_commons", "kind": "video", "id": "1", "title": "Normandy Invasion.webm", "description": "", "height": 1080, "license": "Public domain"},
        {"provider": "wikimedia_commons", "kind": "video", "id": "2", "title": "D-Day 82 Anniversary ceremony.webm", "description": "", "height": 1080, "license": "Public domain"},
        {"provider": "wikimedia_commons", "kind": "video", "id": "3", "title": "Amtrak through Omaha, NE.webm", "description": "", "height": 1080, "license": "CC BY 3.0"},
        {"provider": "wikimedia_commons", "kind": "video", "id": "4", "title": "D-Day 1944 Office of War Information.webm", "description": "", "height": 1080, "license": "Public domain"},
    ]
    orig = fm.search_commons
    try:
        fm.search_commons = lambda q, media="video", get=None: vids if media == "video" else []
        kept, errors = fm.gather_candidates(topic)
    finally:
        fm.search_commons = orig
    assert [c["id"] for c in kept] == ["1", "4"]


def _frames(fn, n=8, w=fm.FRAME_W, h=fm.FRAME_H):
    import numpy as np
    return [fn(i, np).astype("uint8").tobytes() for i in range(n)]


def test_segment_score_rejects_black_and_frozen_accepts_textured_motion():
    import numpy as np
    yy, xx = np.mgrid[0:fm.FRAME_H + 8, 0:fm.FRAME_W + 8]
    base = 128 + 70 * np.sin(xx / 6.0) * np.cos(yy / 5.0)  # smooth, detailed texture; a 1px pan is real motion
    black = _frames(lambda i, np: np.zeros((fm.FRAME_H, fm.FRAME_W)))
    frozen = _frames(lambda i, np: base[:fm.FRAME_H, :fm.FRAME_W])
    moving = _frames(lambda i, np: base[0:fm.FRAME_H, i:i + fm.FRAME_W])
    assert fm.segment_score(black) is None
    assert fm.segment_score(frozen) is None
    assert fm.segment_score(moving) is not None


def test_bars_detection_finds_pillarbox():
    import numpy as np
    arr = np.full((4, 180, 320), 120.0)
    arr[:, :, :40] = 20.0
    arr[:, :, -40:] = 20.0
    arr += np.random.default_rng(1).normal(0, 20, arr.shape) * (arr > 50)
    left, right, top, bottom = fm.bars_from_frames(arr)
    assert left == pytest.approx(40 / 320) and right == pytest.approx(40 / 320) and top == 0 and bottom == 0


def test_concept_has_everything_build_captions_needs():
    import growth_v2
    src = [{"provider": "nasa", "credit_short": "NASA (public domain)"}]
    concept = fm.build_concept(ft.BY_KEY["artemis1-2022"], src, {"score": 727.4, "reason": "viral_match", "matched": []})
    caps = growth_v2.build_captions(concept)
    assert set(caps) == {"tiktok", "youtube", "instagram", "linkedin", "twitter"}
    assert "NASA (public domain)" in caps["linkedin"]
    assert "AI" not in concept["hook"] and "generative" not in " ".join(concept["lines"]).lower()
    assert len(caps["twitter"]) <= 280


def test_origin_of_archival_is_original():
    import growth_v2
    assert growth_v2.origin_of("archival_footage") == "original"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_render_is_capcut_and_meta_safe(tmp_path):
    shots = []
    for i, src in enumerate(("testsrc2=size=640x480:rate=24", "mandelbrot=size=960x540:rate=25")):
        p = str(tmp_path / f"s{i}.mp4")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", src, "-t", "4", "-pix_fmt", "yuv420p", p], check=True)
        shots.append({"path": p, "kind": "video", "start": 0.0, "dur": 0.0})
    still = str(tmp_path / "still.png")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=800x600", "-frames:v", "1", still], check=True)
    shots.append({"path": still, "kind": "image", "start": 0.0, "dur": 0.0})
    music = fm.load_music("cinematic")
    cuts = fm.beat_cuts(music["beats"])
    bounds = [0.0] + cuts + [8.0]
    for s, a, b in zip(shots, bounds, bounds[1:]):
        s["dur"] = round(b - a, 3)
    caps = [{"text": "June 6, 1944.", "start": 0.35, "end": 2.6}, {"text": "One day.", "start": 2.8, "end": 4.0}]
    for orient, (w, h) in (("vertical", (1080, 1920)), ("landscape", (1920, 1080))):
        out = str(tmp_path / f"{orient}.mp4")
        info = fm.render(shots, None, caps, music, "D-DAY · 1944", "Footage: test", out, orient, str(tmp_path))
        assert (info["width"], info["height"]) == (w, h)
        assert info["vcodec"] == "h264" and info["profile"] == "Main" and info["level"] == 40
        assert info["pix_fmt"] == "yuv420p" and info["fps"] == "30/1"
        assert info["acodec"] == "aac" and info["sample_rate"] == "44100" and info["channels"] == 2
        assert abs(info["duration"] - 8.0) < 0.1
        with open(out, "rb") as fh:  # +faststart: moov before mdat
            head = fh.read(4096)
        assert head.find(b"moov") != -1 and (head.find(b"mdat") == -1 or head.find(b"moov") < head.find(b"mdat"))


def test_postproxy_routes_landscape_to_linkedin_and_x(monkeypatch):
    import main
    captured = []

    def fake_post(settings, path, payload, timeout=180):
        captured.append(payload)
        return {"id": f"pp_{len(captured)}", "platforms": [{"platform": p, "status": "published"} for p in payload["profiles"]]}

    monkeypatch.setattr(main, "postproxy_post", fake_post)
    monkeypatch.setattr(main, "ensure_vertical_video_variant", lambda post, *a, **k: post.get("vertical_video_path"))
    monkeypatch.setattr(main, "resolve_postproxy_platform_params", lambda *a, **k: {})
    monkeypatch.setattr(main, "load_postproxy_state", lambda: {})
    post = {"id": "p1", "text": "Long LinkedIn body.", "video_path": "/static/assets/generated/g.mp4",
            "vertical_video_path": "/static/assets/generated/g.mp4", "landscape_video_path": "/static/assets/generated/g_16x9.mp4",
            "media_source": "archival_footage",
            "platform_captions": {"instagram": "short", "youtube": "short", "tiktok": "short", "linkedin": "long", "twitter": "tweet"}}
    settings = {"postproxy_enabled": True, "postproxy_api_key": "t", "public_base_url": "https://app.example.com"}
    res = main.publish_via_postproxy(post, settings, ["instagram", "youtube", "linkedin", "twitter"])
    assert set(res["successes"]) == {"instagram", "youtube", "linkedin", "twitter"}
    media = {tuple(p["profiles"]): p["media"][0] for p in captured}
    assert media[("twitter",)].endswith("g_16x9.mp4")
    assert media[("linkedin",)].endswith("g_16x9.mp4")
    assert media[("instagram", "youtube")].endswith("/g.mp4")
    # archival footage is not AI-generated: no made_with_ai / synthetic flags
    ig = [p for p in captured if "instagram" in p["profiles"]][0]
    assert not (ig.get("platforms") or {}).get("instagram", {}).get("made_with_ai")


def test_postproxy_without_landscape_is_unchanged(monkeypatch):
    import main
    captured = []
    monkeypatch.setattr(main, "postproxy_post", lambda s, path, payload, timeout=180: captured.append(payload) or {"id": "x", "platforms": []})
    monkeypatch.setattr(main, "postproxy_media_url", lambda *a, **k: "https://app.example.com/clip.mp4")
    monkeypatch.setattr(main, "resolve_postproxy_platform_params", lambda *a, **k: {})
    monkeypatch.setattr(main, "load_postproxy_state", lambda: {})
    post = {"id": "p2", "text": "t", "video_path": "/v.mp4", "platform_captions": {"linkedin": "a", "youtube": "a"}}
    main.publish_via_postproxy(post, {"postproxy_enabled": True, "postproxy_api_key": "t"}, ["linkedin", "youtube"])
    assert len(captured) == 1 and captured[0]["media"] == ["https://app.example.com/clip.mp4"]


def test_produce_uses_footage_engine_without_gemini(monkeypatch, tmp_path):
    import asyncio
    import growth_v2
    monkeypatch.setenv("VIDEO_ENGINE", "footage")
    vert, land = str(tmp_path / "g2_footage_x.mp4"), str(tmp_path / "g2_footage_x_16x9.mp4")
    concept = fm.build_concept(ft.BY_KEY["titanic-1912"], [{"credit_short": "Wikimedia Commons (public domain)"}])
    info = {"media_source": "archival_footage", "format": "footage_short", "landscape_path": land, "cost_usd": 0.0}

    def fake_produce(out_dir, **kw):
        return vert, land, dict(concept), dict(info)

    def boom(*a, **k):
        raise AssertionError("Gemini must not be called in footage mode")

    monkeypatch.setattr(fm, "produce_footage", fake_produce)
    monkeypatch.setattr(growth_v2, "plan_concept", boom)
    monkeypatch.setattr(growth_v2, "generate_original_clip", boom)
    monkeypatch.setattr(growth_v2, "_main", lambda: type("M", (), {"GENERATED_DIR": str(tmp_path)}))
    out, c, i = asyncio.run(growth_v2.produce({}, {"history": []}))
    assert out == vert and i["media_source"] == "archival_footage"
    post = growth_v2.build_post(out, c, i, ["instagram", "linkedin"])
    assert post["landscape_video_path"].endswith("_16x9.mp4")
    assert post["vertical_video_path"].endswith("g2_footage_x.mp4") and post["origin"] == "original"


def test_fit_caption_never_orphans():
    assert fm.fit_caption("It sank in under 3 hours.", 26, 56) == ("It sank in under 3 hours.", 56)
    one, size = fm.fit_caption("Titanic sinks in under 3 hours.", 26, 56)
    assert "\n" not in one and size < 56
    two, _ = fm.fit_caption("The largest landslide in recorded history.", 26, 56)
    a, b = two.split("\n")
    assert len(b.split()) >= 2 and abs(len(a) - len(b)) <= 6


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_vertical_is_full_bleed_not_padded(tmp_path):
    import numpy as np
    src = str(tmp_path / "wide.mp4")  # 16:9 source with detail everywhere
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30", "-t", "9",
                    "-pix_fmt", "yuv420p", src], check=True)
    shots = [{"path": src, "kind": "video", "start": float(i * 3), "dur": d, "focus": 0.5}
             for i, d in enumerate((2.7, 2.7, 2.6))]
    out = str(tmp_path / "v.mp4")
    fm.render(shots, None, [], fm.load_music("cinematic"), "HOOK", "", out, "vertical", str(tmp_path))
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", "4", "-i", out, "-frames:v", "1", "-vf", "format=gray",
                          "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    img = np.frombuffer(raw, dtype=np.uint8).reshape(1920, 1080).astype(np.float32)
    # the picture runs to the top and bottom edges: same column structure as the middle (a blurred or
    # black pad band would have almost no horizontal detail compared with the picture)
    detail = lambda band: np.abs(np.diff(band, axis=1)).mean()
    mid = detail(img[800:1100])
    for band in (img[:250], img[-250:]):
        assert detail(band) > 0.4 * mid
    assert img[:40].mean() > 30 and img[-40:].mean() > 30  # no black letterbox
    # exactly sum(dur) = 8.0 s at 30 fps
    n = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
                        "stream=nb_read_frames", "-of", "csv=p=0", out], capture_output=True, text=True).stdout.strip()
    assert int(n) == int(8.0 * fm.FPS)


def test_focus_x_reads_local_files(tmp_path):
    # regression: -user_agent on a local file made ffmpeg fail, so every shot fell back to centre (0.5)
    src = str(tmp_path / "wide.mp4")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30", "-t", "3",
                    "-pix_fmt", "yuv420p", src], check=True)
    assert fm._ua(src) == [] and fm._ua("https://x/y.webm")
    assert len(fm._gray_frames(src, 0.0, seconds=2.0, fps=2)) >= 2


def test_modern_offtopic_clips_rejected_for_historic_topics():
    topic = next(t for t in ft.TOPICS if t["key"] == "pearl-harbor-1941")
    fake = [
        {"provider": "wikimedia_commons", "id": "1", "kind": "video", "title": "Reporter- Why didn't you inform allies before attacking Iran- Trump-....webm", "description": "attack", "license": "Public domain"},
        {"provider": "wikimedia_commons", "id": "2", "kind": "video", "title": "National Pearl Harbor Remembrance Day QIISsZYm1SA.webm", "description": "", "license": "CC BY 3.0"},
        {"provider": "wikimedia_commons", "id": "3", "kind": "video", "title": "Pearl Harbor 2016 drone tour.webm", "description": "", "license": "Public domain"},
        {"provider": "internet_archive", "id": "4", "kind": "video", "title": "Avenge December 7 (Pearl Harbor)", "description": "", "license": "Public domain"},
    ]
    orig = {k: getattr(fm, k) for k in ("search_nasa", "search_commons", "search_archive", "search_pexels", "search_pixabay")}
    try:
        fm.search_commons = lambda q, kind, get: fake if kind == "video" else []
        for k in ("search_nasa", "search_pexels", "search_pixabay"):
            setattr(fm, k, lambda *a, **kw: [])
        fm.search_archive = lambda q, get: []
        kept, _ = fm.gather_candidates(topic)
    finally:
        for k, v in orig.items():
            setattr(fm, k, v)
    assert [c["id"] for c in kept] == ["4"]
    assert fm.topic_year(topic) == 1941


def test_screen_credit_fits_frame():
    long = ("Footage: Internet Archive (public domain); Wikimedia Commons (public domain); "
            "The U.S. National Archives (public domain); Jane Doe / Wikimedia Commons (CC BY 4.0)")
    text, size = fm.screen_credit(long, int(1000 / (26 * 0.52)), 26)
    width = int(1000 / (26 * 0.52))
    for line in text.split("\n"):
        assert len(line) * size <= width * 26 + 1
    assert "Jane Doe" in text and text.count("public domain") == 1


def test_off_era():
    assert fm.off_era("Time-lapse images of Mount St. Helens dome growth 2004-2008.webm", 1980)
    assert fm.off_era("GOLDEN GATE - The Nobility of Bridges (HD).webm", 1937)
    assert not fm.off_era("Mount St. Helens--Legacies of the 1980 eruption", 1980)
    assert not fm.off_era("Titanic Disaster - Genuine Footage (1911-1912)(enhanced).webm", 1912)
    assert not fm.off_era("Artemis I Launch (KSC-20221116).webm", 2022)


# --------------------------------------------------------------------------- 31-55 s length rule (Bret 2026-11-12)

def test_length_constants():
    assert fm.MIN_SECONDS == 31.0 and fm.MAX_SECONDS == 55.0
    assert fm.MIN_SECONDS <= fm.TARGET_SECONDS <= fm.MAX_SECONDS
    assert fm.length_ok(31.0) and fm.length_ok(45) and fm.length_ok(55.0)
    assert not fm.length_ok(8.0) and not fm.length_ok(30.99) and not fm.length_ok(55.01) and not fm.length_ok(None)


def test_target_ignores_stale_8s_setting_and_is_clamped(monkeypatch):
    monkeypatch.delenv("FOOTAGE_TARGET_SECONDS", raising=False)
    monkeypatch.setenv("AUTONOMOUS_VIDEO_DURATION", "8")
    assert fm.target_seconds() == 45.0
    for raw, want in (("8", 31.0), ("120", 55.0), ("40", 40.0), ("junk", 45.0)):
        monkeypatch.setenv("FOOTAGE_TARGET_SECONDS", raw)
        assert fm.target_seconds() == want


@pytest.mark.parametrize("vo,want", [(0.0, 45.0), (8.0, 45.0), (20.0, 45.0), (43.4, 45.0), (46.0, 47.6), (60.0, 55.0)])
def test_plan_total_covers_vo_within_bounds(monkeypatch, vo, want):
    monkeypatch.delenv("FOOTAGE_TARGET_SECONDS", raising=False)
    assert fm.plan_total(vo) == pytest.approx(want)
    assert fm.length_ok(fm.plan_total(vo))


def test_fit_total_fails_instead_of_shipping_short():
    assert fm.fit_total(45.0, 11) == 45.0
    assert fm.fit_total(45.0, 5) == pytest.approx(min(45.0, 5 * fm.MAX_SHOT_SECONDS))
    assert fm.fit_total(45.0, 3) == pytest.approx(min(45.0, 3 * fm.MAX_SHOT_SECONDS))  # 3 x 11 s = 33 s
    with pytest.raises(RuntimeError, match="31"):
        fm.fit_total(45.0, 2)  # 2 x 11 s = 22 s < 31 s
    with pytest.raises(RuntimeError):
        fm.fit_total(45.0, 0)


@pytest.mark.parametrize("total", [31.0, 38.2, 45.0, 48.37, 55.0])
def test_shot_durations_sum_exactly_and_respect_shot_limits(total):
    music = fm.load_music("emotional")
    n = fm.shot_count(total)
    cuts, durs = fm.shot_durations(fm.music_beats(music, total), n, total)
    assert len(durs) == n and sum(durs) == pytest.approx(total, abs=1e-6)
    assert all(fm.MIN_SHOT_SECONDS - 1e-6 <= d <= fm.MAX_SHOT_SECONDS + 1e-6 for d in durs), durs
    beats = set(fm.music_beats(music, total))
    assert sum(c in beats for c in cuts) >= len(cuts) - 1  # cut on the beat


def test_synth_vo_skips_a_hung_line_and_keeps_the_rest(monkeypatch, tmp_path):
    import numpy as np
    import time as _time

    class FakeKokoro:
        def get_voice_style(self, name):
            return np.ones(4, dtype=np.float32)

        def create(self, text, voice, speed, lang):
            if "HANG" in text:
                _time.sleep(2.0)
            return np.full(int(24000 * 0.2 * len(text.split())), 0.2, dtype=np.float32), 24000

    monkeypatch.setattr(fm, "_kokoro", lambda: FakeKokoro())
    monkeypatch.setenv("FOOTAGE_VO_LINE_TIMEOUT", "0.3")
    lines = [
        {"say": "one two three four five", "show": "one"},
        {"say": "HANG this line", "show": "hang"},
        {"say": "six seven eight nine ten", "show": "six"},
    ]
    wav, caps, info = fm.synth_vo(lines, str(tmp_path))
    assert wav and info["lines_used"] == 2
    assert [c["text"] for c in caps] == ["one", "six"]


def test_three_long_shots_can_fill_the_31s_floor():
    assert fm.min_shots() <= 3
    assert 3 * fm.MAX_SHOT_SECONDS >= fm.MIN_SECONDS
    assert fm.VERSION == "2026-11-12.5"


def test_choose_segments_falls_back_when_quality_probes_return_nothing(monkeypatch):
    monkeypatch.setattr(fm, "segment_score", lambda *a, **k: None)
    monkeypatch.setattr(fm, "_gray_frames", lambda *a, **k: [])
    got = fm.choose_segments("https://example/film.webm", 296.0, need=5, shot_len=11.5)
    assert len(got) >= 3
    starts = [t for t, _ in got]
    assert min(starts) >= 0 and max(starts) + 11.5 <= 296.0


def test_localize_retries_longer_on_429(monkeypatch, tmp_path):
    sleeps = []
    monkeypatch.setattr(fm.time, "sleep", lambda s: sleeps.append(s))
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise RuntimeError("ffmpeg failed rc=8: stderr=Server returned 429 Too Many Requests")

    monkeypatch.setattr(fm, "_run", boom)
    c = {"kind": "video", "crop": None, "title": "x"}
    try:
        fm.localize(c, "https://upload.wikimedia.org/x.webm", 10.0, 11.0, str(tmp_path), 0)
    except RuntimeError as exc:
        assert "429" in str(exc)
    else:
        raise AssertionError("expected localize to raise")
    assert calls["n"] == 5
    assert sleeps == [15, 30, 45, 60]

def test_short_bed_is_looped_on_a_bar():
    music = {"beats": [round(i * 0.5, 3) for i in range(20)], "seconds": 10.0}
    assert fm.loop_seconds(music) == 8.0  # 16 beats = 4 bars
    grid = fm.music_beats(music, 45.0)
    assert grid[-1] > 44 and 8.0 in grid and 16.5 in grid


def test_vo_is_never_sped_up_and_drops_trailing_lines(monkeypatch, tmp_path):
    import numpy as np
    speeds = []

    class FakeKokoro:
        def get_voice_style(self, name):
            return np.ones(4, dtype=np.float32)

        def create(self, text, voice, speed, lang):
            speeds.append(speed)
            return np.full(int(24000 * 0.2 * len(text.split())), 0.2, dtype=np.float32), 24000

    monkeypatch.setattr(fm, "_kokoro", lambda: FakeKokoro())
    lines = [{"say": "one two three four five six seven eight nine ten", "show": f"line {i}"} for i in range(30)]
    wav, caps, info = fm.synth_vo(lines, str(tmp_path))
    assert set(speeds) == {fm.VO_SPEED} and fm.VO_SPEED <= 1.0
    assert info["seconds"] <= fm.MAX_SECONDS - fm.VO_TAIL
    assert 0 < len(caps) < 30 and caps[-1]["end"] <= fm.MAX_SECONDS


def test_plan_shots_returns_what_exists_and_fails_below_minimum(tmp_path):
    cands = [{"provider": "wikimedia_commons", "kind": "video", "title": f"Titanic reel {i}", "url": f"https://x/{i}.webm",
              "page": "p", "author": "a", "license": "Public domain", "credit_short": "Wikimedia Commons (public domain)"}
             for i in range(3)]
    probe_fn = lambda src: {"width": 1280, "height": 720, "duration": 300.0}
    segs = lambda src, dur, need, shot_len, crop=None: [(10.0 + 20 * k, 5.0 - k * 0.1) for k in range(need)]
    loc = lambda c, src, start, dur, workdir, i: {"path": f"{src}@{start}", "kind": "video", "start": 0.0, "dur": dur}
    kw = dict(probe_fn=probe_fn, segment_fn=segs, crop_fn=lambda *a: None, localize_fn=loc)
    got = fm.plan_shots(cands, 11, str(tmp_path), [], min_need=fm.min_shots(), **kw)
    assert len(got) == 11 and all(s["dur"] == fm.SEGMENT_SECONDS for s, _ in got)
    with pytest.raises(RuntimeError, match="usable on-topic shots"):
        fm.plan_shots(cands[:1], 11, str(tmp_path), [], min_need=fm.min_shots(),
                      **{**kw, "segment_fn": lambda *a, **k: [(10.0, 5.0), (40.0, 4.0)]})


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_render_long_clip_both_orientations_in_bounds(tmp_path):
    """A real ffmpeg render of a planned 45 s footage clip: both files measure 31-55 s with ffprobe."""
    total = fm.plan_total(40.0)
    n = fm.shot_count(total)
    shots = []
    for i in range(n):
        p = str(tmp_path / f"s{i}.mp4")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=25",
                        "-t", f"{fm.SEGMENT_SECONDS + 0.4}", "-pix_fmt", "yuv420p", p], check=True)
        shots.append({"path": p, "kind": "video", "start": 0.0, "dur": 0.0})
    music = fm.load_music("dark_build")
    _, durs = fm.shot_durations(fm.music_beats(music, total), n, total)
    for s, d in zip(shots, durs):
        s["dur"] = d
    caps = [{"text": f"Line {i}", "start": 0.35 + 4 * i, "end": 3.9 + 4 * i} for i in range(10)]
    for orient in ("vertical", "landscape"):
        out = str(tmp_path / f"{orient}.mp4")
        info = fm.render(shots, None, caps, music, "TITANIC · 1912", "Footage: test", out, orient, str(tmp_path))
        assert fm.length_ok(info["duration"]) and abs(info["duration"] - total) < 0.1, info["duration"]
        a = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=duration",
                            "-of", "csv=p=0", out], capture_output=True, text=True).stdout.strip()
        assert abs(float(a) - total) < 0.15  # music covers the whole clip


def test_produce_footage_rejects_out_of_bounds_render(monkeypatch, tmp_path):
    topic = ft.BY_KEY["titanic-1912"]
    rec = {"provider": "wikimedia_commons", "credit_short": "Wikimedia Commons (public domain)"}
    monkeypatch.setattr(fm, "gather_candidates", lambda t: ([{"title": "x"}], []))
    monkeypatch.setattr(fm, "synth_vo", lambda lines, wd, max_total=None: (None, [], {"seconds": 40.0}))
    monkeypatch.setattr(fm, "plan_shots", lambda c, need, wd, log, **k: [({"path": "p", "kind": "video", "dur": 7.0}, dict(rec))
                                                                       for _ in range(need)])
    monkeypatch.setattr(fm, "focus_x", lambda *a: 0.5)
    seen = {}

    def fake_render(shots, *a, **k):
        seen["total"] = sum(s["dur"] for s in shots)
        return {"duration": 8.0}
    monkeypatch.setattr(fm, "render", fake_render)
    with pytest.raises(RuntimeError, match="outside 31-55"):
        fm.produce_footage(str(tmp_path), topic_key=topic["key"], workdir=str(tmp_path / "w"))
    assert seen["total"] == pytest.approx(45.0)


def test_produce_footage_fails_when_footage_too_thin(monkeypatch, tmp_path):
    monkeypatch.setattr(fm, "gather_candidates", lambda t: ([], []))
    monkeypatch.setattr(fm, "synth_vo", lambda lines, wd, max_total=None: (None, [], {"seconds": 40.0}))

    def thin(c, need, wd, log, min_need=None, **k):
        raise RuntimeError(f"only 2 usable on-topic shots (need {min_need} for 31s)")
    monkeypatch.setattr(fm, "plan_shots", thin)
    monkeypatch.setattr(fm, "render", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not render")))
    with pytest.raises(RuntimeError, match="no usable footage"):
        fm.produce_footage(str(tmp_path), topic_key="titanic-1912", workdir=str(tmp_path / "w"))


def test_no_8_second_copy_left_in_footage_path():
    import inspect
    for mod in (fm, ft):
        src = inspect.getsource(mod)
        assert "cut to 8 seconds" not in src and "single_8s" not in src and "TOTAL_SECONDS = 8" not in src


def test_apollo11_rejects_other_apollo_missions():
    t = ft.BY_KEY["apollo11-1969"]
    cands = [{"provider": "wikimedia_commons", "kind": "video", "id": i, "title": title, "description": "Apollo 11 launch"}
             for i, title in enumerate(["Apollo 13 Launch and Post-Launch (KSC-19700411-MH-NAS01-0001).webm",
                                        "Apollo 8 Saturn V liftoff.webm",
                                        "Apollo 11 launch, video of engines at 500 fps (camera E-8).ogv",
                                        "The Eagle Has Landed, The Flight of Apollo 11, 1969.webm"])]

    def fake_get(*a, **k):
        raise RuntimeError("offline")
    import unittest.mock as um
    with um.patch.object(fm, "search_nasa", lambda *a, **k: []), um.patch.object(fm, "search_archive", lambda *a, **k: []), \
            um.patch.object(fm, "search_commons", lambda q, media="video", get=None: cands if media == "video" else []):
        kept, _ = fm.gather_candidates(t, get=fake_get)
    titles = [c["title"] for c in kept]
    assert not any("Apollo 13" in x or "Apollo 8" in x for x in titles)
    assert any("Apollo 11 launch" in x for x in titles) and any("Eagle Has Landed" in x for x in titles)


# --------------------------------------------------------------------------- viral-first topic selection (Bret 2026-11-12)

class _Resp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


_LEAD = ("The Hindenburg disaster was an airship accident that occurred on May 6, 1937, in Manchester Township, New Jersey. "
         "The German passenger airship LZ 129 Hindenburg caught fire and was destroyed during its attempt to dock with its "
         "mooring mast at Naval Air Station Lakehurst. There were 97 people on board, and 36 of them died, along with one "
         "worker on the ground. The disaster was the subject of newsreel coverage, photographs and Herbert Morrison's recorded "
         "radio eyewitness reports from the landing field, which were broadcast the next day. A variety of theories have been "
         "put forward for both the cause of ignition and the initial fuel for the ensuing fire. The event shattered public "
         "confidence in the giant, passenger-carrying rigid airship and marked the abrupt end of the airship era.\n\n\n"
         "Flight\n"
         "The Hindenburg left Frankfurt on the evening of May 3, 1937. "
         "Strong headwinds slowed the crossing of the Atlantic. "
         "Storms over the landing field delayed the arrival by several hours. "
         "At 7:25 p.m., the airship caught fire and quickly became engulfed in flames. "
         "The tail struck the ground first, followed by the burning hull. "
         "Ground crew and survivors ran from the wreck as the frame collapsed. "
         "Within a minute, the giant ship lay burning on the field. "
         "Newsreel cameras recorded the whole fire from start to finish.")


def _fake_wiki(pages):
    def get(url, params=None, **k):
        assert "wikipedia.org" in url
        if params.get("list") == "search":
            q = params["srsearch"].lower()
            return _Resp({"query": {"search": [{"title": t} for t in pages if any(w in q for w in t.lower().split())]}})
        t = params["titles"]
        p = pages[t]
        return _Resp({"query": {"pages": {"1": {"title": t, "description": p[0], "extract": p[1]}}}})
    return get


def test_year_words_and_spoken():
    assert fm.year_words(1912) == "nineteen twelve" and fm.year_words(1903) == "nineteen oh three"
    assert fm.year_words(2022) == "twenty twenty-two" and fm.year_words(2000) == "two thousand"
    assert fm.spoken("On May 6, 1937, 36 died.") == "On May 6, nineteen thirty-seven, 36 died."


def test_script_from_text_is_long_enough_and_caption_sized():
    vo = fm.script_from_text(_LEAD, name="Hindenburg disaster")
    shows = [v["show"] for v in vo]
    assert 60 <= sum(len(v.split()) for v in shows) <= 100
    # every caption is one whole, tightened sentence that fits on screen; no headings, no "p.m." split, no asides
    assert all(v["parts"] == [v["show"]] and len(v["show"]) <= fm.CAPTION_MAX_CHARS for v in vo)
    assert all(s.endswith(".") and s[0].isupper() for s in shows) and "(" not in " ".join(shows)
    assert "Hindenburg" in shows[0] and not any(s.startswith("Flight") for s in shows)
    assert "At 7:25 pm, the airship caught fire and quickly became engulfed in flames." in shows
    assert fm.script_from_text("Too short. Not enough here to narrate.") == []


def test_viral_scan_determines_topic_not_the_library():
    pages = {"Chernobyl disaster": ("1986 nuclear accident in the Soviet Union", _LEAD.replace("Hindenburg", "Chernobyl")),
             "MrBeast": ("American YouTuber (born 1998)", "Jimmy Donaldson (born May 7, 1998) is an American YouTuber.")}
    items = [{"title": "I Gave Away 100 Cars | MrBeast", "views_per_hour": 900000},
             {"title": "Trump speech reaction LIVE", "views_per_hour": 500000},
             {"title": "Chernobyl disaster: rare footage #shorts", "views_per_hour": 40000},
             {"title": "Titanic wreck new scan", "views_per_hour": 1000}]
    log = []
    picks = fm.viral_topics(items, get=_fake_wiki(pages), log=log)
    keys = [p["topic"]["key"] for p in picks]
    assert keys[0] == "viral-chernobyl-disaster"  # most-viewed usable viral subject wins
    assert picks[0]["reason"] == "viral_subject" and picks[0]["topic"]["source"] == "viral_scan"
    assert "titanic-1912" in keys  # a library topic only when a viral title names it (better script)
    assert not any("mrbeast" in k or "trump" in k for k in keys)
    assert any("news/politics" in l for l in log)
    t = picks[0]["topic"]
    assert re.search(t["require"], "Chernobyl Nuclear Power Plant 1986.webm", re.I)
    assert t["text_credit"] and t["wikipedia"].startswith("https://en.wikipedia.org/wiki/")


def test_no_viral_subject_means_no_canned_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(fm, "wiki_subjects", lambda title, get=None, limit=2: [])
    monkeypatch.setattr(fm, "gather_candidates", lambda t: (_ for _ in ()).throw(AssertionError("no library fallback")))
    with pytest.raises(RuntimeError, match="not falling back"):
        fm.produce_footage(str(tmp_path), viral={"top": [{"title": "Cute cat compilation", "views_per_hour": 5}]},
                           workdir=str(tmp_path / "w"))


def test_thin_viral_topic_is_skipped_for_the_next_viral_topic(monkeypatch, tmp_path):
    tried = []
    a = {**ft.BY_KEY["titanic-1912"]}
    b = {**ft.BY_KEY["hindenburg-1937"]}
    monkeypatch.setattr(fm, "viral_topics", lambda items, **k: [{"topic": a, "score": 9.0, "matched": []},
                                                                {"topic": b, "score": 5.0, "matched": []}])
    monkeypatch.setattr(fm, "gather_candidates", lambda t: (tried.append(t["key"]) or [], []))
    monkeypatch.setattr(fm, "synth_vo", lambda lines, wd, max_total=None: (None, [], {"seconds": 40.0}))
    rec = {"provider": "wikimedia_commons", "credit_short": "Wikimedia Commons (public domain)"}

    def plan(c, need, wd, log, min_need=None, **k):
        if len(tried) == 1:
            raise RuntimeError("only 2 usable on-topic shots")
        return [({"path": "p", "kind": "video", "dur": 7.0}, dict(rec)) for _ in range(need)]
    monkeypatch.setattr(fm, "plan_shots", plan)
    monkeypatch.setattr(fm, "focus_x", lambda *a: 0.5)
    monkeypatch.setattr(fm, "render", lambda shots, *a, **k: {"duration": round(sum(s["dur"] for s in shots), 3)})
    v, l, concept, info = fm.produce_footage(str(tmp_path), viral={"top": [{"title": "x"}]}, workdir=str(tmp_path / "w"))
    assert tried == ["titanic-1912", "hindenburg-1937"] and info["topic_key"] == "hindenburg-1937"
    assert fm.length_ok(info["duration_s"])


def test_vo_with_caption_parts_times_each_piece(monkeypatch, tmp_path):
    import numpy as np

    class FakeKokoro:
        def get_voice_style(self, name):
            return np.ones(4, dtype=np.float32)

        def create(self, text, voice, speed, lang):
            return np.full(24000 * 3, 0.2, dtype=np.float32), 24000

    monkeypatch.setattr(fm, "_kokoro", lambda: FakeKokoro())
    lines = [{"say": "a b c", "show": "aaaa bbbb", "parts": ["aaaa", "bbbb"]}, {"say": "d", "show": "dd"}]
    wav, caps, info = fm.synth_vo(lines, str(tmp_path))
    assert wav and [c["text"] for c in caps] == ["aaaa", "bbbb", "dd"]
    assert caps[0]["end"] == pytest.approx(caps[1]["start"]) and caps[1]["end"] < caps[2]["start"]


def test_scanned_subject_rejects_homonym_clips():
    t = fm.topic_from_subject({"title": "Wolf", "description": "Species of canine", "url": "https://en.wikipedia.org/wiki/Wolf",
                               "lead": _LEAD.replace("Hindenburg", "wolf")})
    ex = re.compile(t["exclude"], re.I)
    assert ex.search("Chief Little Wolf vs Fred Atkins, Sydney, 1947.webm") and ex.search("Kamala Harris questions Chad Wolf.webm")
    assert t["title_only"]
    assert not ex.search("Gray wolf pack in Yellowstone.webm") and re.search(t["require"], "Gray wolf pack in Yellowstone.webm", re.I)


def test_scanned_subject_searches_its_commons_category_only():
    t = fm.topic_from_subject({"title": "Cheetah", "description": "Species of large cat", "commons_category": "Acinonyx jubatus",
                               "url": "https://en.wikipedia.org/wiki/Cheetah", "lead": _LEAD.replace("Hindenburg", "cheetah")})
    assert t["queries"]["commons_cat"] == ["Acinonyx jubatus"] and t["queries"]["commons"] == [] and t["queries"]["archive"] == []
    assert t["max_per_source"] == 2 and re.search(t["require"], "Juvenile Cheetahs at a kill.webm", re.I)


def test_scanned_subject_caps_clips_per_source(tmp_path):
    cands = [{"provider": "wikimedia_commons", "kind": "video", "title": "Cheetah run", "url": "https://x/1.webm",
              "page": "p", "author": "a", "license": "Public domain", "credit_short": "Wikimedia Commons (public domain)"}]
    kw = dict(probe_fn=lambda src: {"width": 1280, "height": 720, "duration": 600.0}, crop_fn=lambda *a: None,
              segment_fn=lambda src, dur, need, shot_len, crop=None: [(10.0 + 30 * k, 5.0) for k in range(need)],
              localize_fn=lambda c, src, start, dur, workdir, i: {"path": "p", "kind": "video", "start": 0.0, "dur": dur})
    assert len(fm.plan_shots(cands, 11, str(tmp_path), [], min_need=1, **kw)) == 11  # library topics: unchanged
    with pytest.raises(RuntimeError, match="usable on-topic shots"):
        fm.plan_shots(cands, 11, str(tmp_path), [], min_need=fm.min_shots(), max_per_source=2, **kw)


def test_tighten_keeps_main_clause():
    assert fm.tighten("The wolf, also known as the grey wolf or gray wolf, is a canine native to Eurasia.") == \
        "The wolf is a canine native to Eurasia."
    assert fm.tighten("More than thirty subspecies have been recognized, including the dog and dingo, though grey "
                      "wolves include only wild subspecies.") == "More than thirty subspecies have been recognized."


def test_scanned_subject_copy_matches_its_kind_not_history():
    lead = _LEAD.replace("Hindenburg disaster was an airship accident", "cheetah is a large cat").replace("Hindenburg", "cheetah")
    t = fm.topic_from_subject({"title": "Cheetah", "description": "Large cat species", "commons_category": "Acinonyx jubatus",
                               "url": "https://en.wikipedia.org/wiki/Cheetah", "lead": lead, "text": lead})
    assert t["kind"] == "wildlife" and "·" not in t["on_screen_hook"]
    c = fm.build_concept(t, [{"credit_short": "Wikimedia Commons (CC BY 4.0)"}])
    assert "#History" not in c["hashtags"] and "#Wildlife" in c["hashtags"] and "history" not in c["comment_question"].lower()
    assert len(c["lines"]) == 2 and "Wikipedia (CC BY-SA 4.0)" in c["lines"][0] and len(c["lines"][0]) <= 110
    lib = fm.build_concept(fm.footage_topics.TOPICS[0], [])
    assert "#History" in lib["hashtags"]


def test_fit_caption_three_lines_for_long_sentence():
    text, size = fm.fit_caption("The global cheetah population was estimated at 6,517 mature individuals in 2021.", 26, 56)
    assert text.count("\n") == 2 and size >= 48


def test_run_includes_returncode_when_stderr_is_empty(tmp_path):
    # Production 4pm 2026-11-12 failed with "ffmpeg failed: " and nothing after the colon.
    script = tmp_path / "boom.py"
    script.write_text("import sys; sys.exit(3)\n")
    try:
        fm._run(["python3", str(script)], timeout=10)
        assert False, "should have raised"
    except RuntimeError as exc:
        msg = str(exc)
        assert "rc=3" in msg
        assert "(empty)" in msg


def test_ensure_render_space_deletes_old_unreferenced_only(tmp_path, monkeypatch):
    gen = tmp_path / "generated"
    gen.mkdir()
    posts = tmp_path / "scheduled_posts.json"
    keep_name = "keep_me.mp4"
    posts.write_text('[{"id":"p1","video_path":"/static/assets/generated/%s"}]' % keep_name)
    old = gen / "old_clip.mp4"
    kept_ref = gen / keep_name
    recent = gen / "recent.mp4"
    old.write_bytes(b"x" * 2000)
    kept_ref.write_bytes(b"y" * 2000)
    recent.write_bytes(b"z" * 2000)
    old_st = old.stat()
    os.utime(old, (old_st.st_atime, old_st.st_mtime - 10 * 86400))
    log = []
    # pretend the volume is tight so cleanup runs
    monkeypatch.setattr(fm, "disk_free", lambda path: 100 if (path == str(gen) and old.exists()) else 2_000_000_000)
    fm.ensure_render_space(str(gen), log, need=1_000_000_000, keep_days=3)
    assert not old.exists()
    assert kept_ref.exists()
    assert recent.exists()
    assert any("removed 1 files" in line for line in log)


def test_run_names_sigkill_when_returncode_is_negative(monkeypatch, tmp_path):
    class Fake:
        returncode = -9
        stdout = b""
        stderr = b""
    monkeypatch.setattr(fm.subprocess, "run", lambda *a, **k: Fake())
    with pytest.raises(RuntimeError, match=r"rc=-9 \(SIGKILL\)"):
        fm._run(["ffmpeg", "-y"], fail_log=str(tmp_path / "fail.log"))
