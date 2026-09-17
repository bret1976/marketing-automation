import asyncio
import importlib
import os

os.environ.setdefault("ADMIN_PASSWORD", "test-password")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("DISABLE_BACKGROUND_SCHEDULER", "true")

main = importlib.import_module("main")


KLING_URL = "https://www.youtube.com/watch?v=c8huoAtXGew"
KLING_TITLE = "Kling 3.0 Full Tutorial | How to Make Cinematic AI Short Films"


def test_placeholder_url_is_refused_without_fallback():
    try:
        main.download_and_trim_original_video("https://x.com/DigitalDreams/status/12345", allow_fallback=False)
    except main.OriginalVideoDownloadError as exc:
        assert "placeholder" in str(exc).lower() or "refusing" in str(exc).lower()
    else:
        raise AssertionError("placeholder URL should have been refused")


def test_empty_url_is_refused():
    try:
        main.download_and_trim_original_video("   ")
    except main.OriginalVideoDownloadError as exc:
        assert "no source video url" in str(exc).lower()
    else:
        raise AssertionError("empty URL should have been refused")


def test_resolve_autopilot_copy_uses_scan_fields(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("repurpose_video_link_copy should not run when scan copy exists")

    monkeypatch.setattr(main, "repurpose_video_link_copy", boom)
    text, thread = main.resolve_autopilot_copy(
        {
            "url": KLING_URL,
            "title": KLING_TITLE,
            "recreated_linkedin_post": "Scan LinkedIn copy",
            "recreated_twitter_thread": ["tweet one", "tweet two"],
        },
        {},
    )
    assert text == "Scan LinkedIn copy"
    assert thread == ["tweet one", "tweet two"]


def test_resolve_autopilot_copy_falls_back_to_repurpose(monkeypatch):
    class Fake:
        repurposed_linkedin_post = "Repurposed LI"
        repurposed_twitter_thread = ["r1"]

    monkeypatch.setattr(main, "repurpose_video_link_copy", lambda url, settings: Fake())
    text, thread = main.resolve_autopilot_copy({"url": KLING_URL, "title": KLING_TITLE}, {})
    assert text == "Repurposed LI"
    assert thread == ["r1"]


def test_execute_autonomous_autopost_downloads_original_not_generate(monkeypatch):
    calls = {"gen": 0, "dl": 0, "scan": 0}

    def fake_scan(job_id, settings):
        calls["scan"] += 1
        main.update_job_status(
            job_id,
            "SUCCESS",
            100,
            "ok",
            result={
                "trends": [
                    {
                        "title": KLING_TITLE,
                        "url": KLING_URL,
                        "recreated_linkedin_post": "Kling in 6Frame words",
                        "recreated_twitter_thread": ["Kling hook"],
                        "recreated_video_prompt": "should not be used",
                    }
                ]
            },
        )

    def fake_download(url, title=None, max_duration_sec=60, allow_fallback=False, timeout_sec=180):
        calls["dl"] += 1
        assert url == KLING_URL
        assert max_duration_sec == 60
        assert allow_fallback is False
        return "/tmp/generated/original_kling.mp4"

    def fake_gen(*args, **kwargs):
        calls["gen"] += 1
        raise AssertionError("run_video_generation must not be called on the Autopilot repurpose path")

    saved = {}
    monkeypatch.setattr(main, "run_live_trend_scanner", fake_scan)
    monkeypatch.setattr(main, "download_and_trim_original_video", fake_download)
    monkeypatch.setattr(main, "run_video_generation", fake_gen)
    monkeypatch.setattr(main, "persist_trend_scan_result", lambda result: None)
    monkeypatch.setattr(main, "load_scheduled_posts", lambda: [])
    monkeypatch.setattr(main, "save_scheduled_posts", lambda posts: saved.setdefault("posts", posts))

    asyncio.run(
        main.execute_autonomous_autopost(
            {
                "require_autopilot_approval": True,
                "autonomous_platforms": ["twitter", "linkedin"],
                "autonomous_video_engine": "google_veo_lite",
            }
        )
    )

    assert calls["scan"] == 1
    assert calls["dl"] == 1
    assert calls["gen"] == 0
    post = saved["posts"][0]
    assert post["status"] == "AWAITING_APPROVAL"
    assert post["video_path"] == "/static/assets/generated/original_kling.mp4"
    assert post["media_source"] == "original_download"
    assert post["source_url"] == KLING_URL
    assert post["text"] == "Kling in 6Frame words"
    assert post["error_message"] is None


def test_execute_autonomous_autopost_stages_copy_when_download_fails(monkeypatch):
    calls = {"gen": 0}

    def fake_download(*args, **kwargs):
        raise main.OriginalVideoDownloadError("yt-dlp blocked")

    def fake_gen(*args, **kwargs):
        calls["gen"] += 1
        raise AssertionError("must not generate a replacement clip")

    saved = {}
    monkeypatch.setattr(
        main,
        "download_and_trim_original_video",
        fake_download,
    )
    monkeypatch.setattr(main, "run_video_generation", fake_gen)
    monkeypatch.setattr(main, "load_scheduled_posts", lambda: [])
    monkeypatch.setattr(main, "save_scheduled_posts", lambda posts: saved.setdefault("posts", posts))

    asyncio.run(
        main.execute_autonomous_autopost(
            {
                "require_autopilot_approval": True,
                "autonomous_platforms": ["twitter", "linkedin", "instagram", "tiktok", "youtube", "facebook"],
            },
            {
                "title": KLING_TITLE,
                "url": KLING_URL,
                "recreated_linkedin_post": "copy",
                "recreated_twitter_thread": ["t"],
            },
        )
    )

    assert calls["gen"] == 0
    post = saved["posts"][0]
    assert post["status"] == "AWAITING_APPROVAL"
    assert post["video_path"] is None
    assert post["media_source"] == "missing"
    assert "yt-dlp blocked" in post["error_message"]
    assert post["text"] == "copy"


def test_load_original_video_endpoint_requests_60s_trim(monkeypatch):
    seen = {}

    def fake_download(url, title=None, max_duration_sec=60, allow_fallback=False, timeout_sec=180):
        seen["max_duration_sec"] = max_duration_sec
        seen["url"] = url
        return "/tmp/generated/original_workshop.mp4"

    monkeypatch.setattr(main, "download_and_trim_original_video", fake_download)
    from fastapi.testclient import TestClient

    client = TestClient(main.app)
    token = __import__("base64").b64encode(b"admin:test-password").decode()
    res = client.post(
        "/api/load-original-video",
        headers={"Authorization": f"Basic {token}"},
        json={"url": KLING_URL, "title": KLING_TITLE, "allow_fallback": False},
    )
    assert res.status_code == 200
    assert seen["max_duration_sec"] == 60
    assert seen["url"] == KLING_URL
