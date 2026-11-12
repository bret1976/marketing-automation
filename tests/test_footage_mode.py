import json
import os
import shutil
import subprocess

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
        assert 2 <= len(t["vo"]) <= 3 and all(v["say"] and v["show"] for v in t["vo"])
        assert sum(len(v["say"].split()) for v in t["vo"]) <= 20  # fits the 8 s formula


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
    bounds = [0.0] + cuts + [fm.TOTAL_SECONDS]
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
    info = {"media_source": "archival_footage", "format": "single_8s", "landscape_path": land, "cost_usd": 0.0}

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
    # exactly 8.0 s at 30 fps
    n = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
                        "stream=nb_read_frames", "-of", "csv=p=0", out], capture_output=True, text=True).stdout.strip()
    assert int(n) == int(fm.TOTAL_SECONDS * fm.FPS)


def test_focus_x_reads_local_files(tmp_path):
    # regression: -user_agent on a local file made ffmpeg fail, so every shot fell back to centre (0.5)
    src = str(tmp_path / "wide.mp4")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30", "-t", "3",
                    "-pix_fmt", "yuv420p", src], check=True)
    assert fm._ua(src) == [] and fm._ua("https://x/y.webm")
    assert len(fm._gray_frames(src, 0.0, seconds=2.0, fps=2)) >= 2
