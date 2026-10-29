import os

os.environ.setdefault("ADMIN_PASSWORD", "test-password")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("DISABLE_BACKGROUND_SCHEDULER", "true")

import main


def test_twitter_caption_is_truncated_to_280():
    long_text = "Vegas crypto after dark. " * 20
    assert len(long_text) > 280
    trimmed = main.truncate_twitter_text(long_text)
    assert len(trimmed) <= 280
    assert trimmed.endswith("…")


def test_twitter_caption_is_split_into_thread_parts():
    text = ("Hook one. " * 20) + "\n\n" + ("Second beat. " * 20)
    parts = main.split_twitter_caption(text)
    assert len(parts) >= 2
    assert all(len(part) <= 280 for part in parts)
    assert parts[0]


def test_existing_thread_parts_are_truncated():
    parts = main.split_twitter_caption("ignored", existing_thread=["ok", "x" * 400])
    assert parts[0] == "ok"
    assert len(parts[1]) <= 280


def test_placement_picker_skips_null_ids():
    assert main.pick_placement_id([{"id": None, "name": "Personal"}, {"id": "12345", "name": "Page"}]) == "12345"
    assert main.pick_placement_id([{"id": "", "name": "Empty"}]) is None


def test_facebook_params_require_cached_page_id(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "POSTPROXY_STATE_FILE", str(tmp_path / "postproxy_state.json"))
    main.save_postproxy_state({
        "profiles": [{"id": "prof_fb", "platform": "facebook", "status": "active", "name": "Page"}],
        "placements": {"facebook": {"placement_id": "998877", "param": "page_id"}},
    })
    params = main.resolve_postproxy_platform_params({}, ["facebook", "twitter"])
    assert params["facebook"]["page_id"] == "998877"
    assert "twitter" not in params


def test_capabilities_requires_auth():
    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    assert client.get("/api/capabilities").status_code == 401
    assert client.get("/api/credentials").status_code == 401
    assert client.post("/api/postproxy/sync").status_code == 401
    assert client.get("/api/postproxy/status").status_code == 401


def test_postproxy_callback_is_public():
    from fastapi.testclient import TestClient
    client = TestClient(main.app, follow_redirects=False)
    res = client.get("/api/postproxy/callback")
    assert res.status_code in (302, 307)
    assert "postproxy=ok" in res.headers.get("location", "")
    fail = client.get("/api/postproxy/callback?error=access_denied")
    assert "postproxy=failure" in fail.headers.get("location", "")
    alias = client.get("/oauth/postproxy/callback")
    assert alias.status_code in (302, 307)
    assert "postproxy=ok" in alias.headers.get("location", "")


def test_postproxy_status_has_no_secrets(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main, "POSTPROXY_STATE_FILE", str(tmp_path / "postproxy_state.json"))
    main.save_postproxy_state({
        "key_valid": True,
        "profile_group_id": "grp_demo",
        "profiles": [{"id": "prof_x", "name": "@studio", "platform": "twitter", "status": "active"}],
        "placements": {"twitter": {"placement_id": None, "placements": []}},
        "synced_at": "2026-08-19T00:00:00",
    })
    client = TestClient(main.app)
    token = __import__("base64").b64encode(b"admin:test-password").decode()
    res = client.get("/api/postproxy/status", headers={"Authorization": f"Basic {token}"})
    assert res.status_code == 200
    payload = res.json()
    blob = str(payload).lower()
    assert "bearer " not in blob
    assert "postproxy_api_key" not in payload
    assert payload["configured"] in (True, False)
    assert "channels" in payload
    assert payload["channels"]["twitter"]["live"] is True


def test_inactive_postproxy_profile_is_not_live():
    channels = main.build_social_channel_status({}, {
        "profiles": [{"id": "p1", "platform": "linkedin", "status": "inactive", "name": "Old"}],
        "placements": {},
    })
    linkedin = next(item for item in channels if item["platform"] == "linkedin")
    assert linkedin["live"] is False
    assert linkedin["status"] == "INACTIVE"


# --- ported from VegasCryptoAgent/marketing-automation#17 / #18 ---

def _pp_post():
    return {
        "id": "q1",
        "text": "Hook line\n\n#Physics",
        "video_path": "/static/assets/generated/clip.mp4",
        "platform_captions": {
            "twitter": "Hook tweet #Physics",
            "linkedin": "Hook line.\\n\\n\\#Physics \\#Science",
            "youtube": "Hook short\n\n#Physics",
        },
    }


def test_twitter_quota_does_not_abort_other_postproxy_batches(monkeypatch):
    captured = []

    def fake_post(settings, path, payload, timeout=180):
        captured.append(payload)
        if payload["profiles"] == ["twitter"]:
            raise ValueError("PostProxy POST /posts failed HTTP 422: 24 hour Twitter quota exceeded")
        return {"id": f"pp_{len(captured)}", "platforms": [{"platform": p, "status": "published"} for p in payload["profiles"]]}

    monkeypatch.setattr(main, "postproxy_post", fake_post)
    monkeypatch.setattr(main, "postproxy_media_url", lambda *a, **k: "https://app.example.com/clip.mp4")
    monkeypatch.setattr(main, "resolve_postproxy_platform_params", lambda *a, **k: {})
    monkeypatch.setattr(main, "load_postproxy_state", lambda: {})
    result = main.publish_via_postproxy(_pp_post(), {"postproxy_enabled": True, "postproxy_api_key": "t"}, ["twitter", "linkedin", "youtube"])
    assert "linkedin" in result["successes"] and "youtube" in result["successes"]
    assert any("twitter" in e for e in result["errors"])
    assert result["postproxy_post_ids"]
    li = [p for p in captured if p["profiles"] == ["linkedin"]][0]["post"]["body"]
    assert li == "Hook line.\n\n#Physics #Science"


def test_apply_publish_result_persists_all_postproxy_ids():
    post = {"status": "PUBLISHING"}
    main.apply_publish_result_to_post(post, {
        "successes": ["linkedin", "youtube"],
        "errors": ["twitter: 24 hour Twitter quota exceeded"],
        "postproxy_post_id": "tw_1",
        "postproxy_post_ids": ["tw_1", "li_2", "yt_3"],
        "postproxy_result": {"platforms": []},
        "blocked_by_publish_guard": True,
    })
    assert post["status"] == "PARTIAL_SUCCESS"
    assert post["postproxy_post_ids"] == ["tw_1", "li_2", "yt_3"]


def test_reconcile_does_not_fail_whole_post_when_only_twitter_id_is_stored(monkeypatch):
    posts = [{"id": "q1", "platform": "twitter,linkedin,instagram,tiktok,youtube", "status": "SUCCESS",
              "postproxy_post_id": "tw_only", "error_message": None}]
    monkeypatch.setattr(main, "postproxy_get", lambda settings, path, timeout=30: {
        "platforms": [{"platform": "twitter", "status": "failed", "error": "24 hour Twitter quota exceeded"}]})
    assert main.reconcile_postproxy_posts(posts, {"postproxy_enabled": True, "postproxy_api_key": "t"}) is True
    assert posts[0]["status"] == "PARTIAL_SUCCESS"
    assert "twitter" in posts[0]["error_message"]


def test_pins_saved_per_platform_and_preferred(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "POSTPROXY_PINS_FILE", str(tmp_path / "pins.json"))
    main.save_postproxy_pin("pinterest", "board_9", "Recipes")
    main.save_postproxy_pin("telegram", "-100123", "My Channel")
    pins = main.load_postproxy_pins()
    assert pins["pinterest"]["placement_id"] == "board_9"
    assert pins["telegram"]["placement_id"] == "-100123"


def test_connect_flow_map_covers_special_platforms():
    assert main.POSTPROXY_CONNECT_FLOW["bluesky"] == "app_password"
    assert main.POSTPROXY_CONNECT_FLOW["telegram"] == "bot_token"
    assert main.POSTPROXY_CONNECT_FLOW["google_business"] == "dashboard"
    assert main.POSTPROXY_PICK_PLACEMENT == {"pinterest": "board", "telegram": "chat"}


def test_dashboard_js_lists_all_platforms():
    js = open(os.path.join(os.path.dirname(main.__file__), "static", "index.js")).read()
    for p in ["tiktok", "instagram", "linkedin", "twitter", "facebook", "youtube", "threads",
              "pinterest", "bluesky", "telegram", "google_business"]:
        assert f'platform:"{p}"' in js, p
