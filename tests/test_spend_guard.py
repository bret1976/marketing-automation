import importlib
import os

import pytest
from fastapi.testclient import TestClient

os.environ["DISABLE_BACKGROUND_SCHEDULER"] = "true"
os.environ.pop("REQUIRE_ADMIN_AUTH", None)

import spend_guard  # noqa: E402

main = importlib.import_module("main")
client = TestClient(main.app)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    for key in list(os.environ):
        if key.startswith("SPEND_GUARD"):
            monkeypatch.delenv(key, raising=False)
    spend_guard.reset_for_tests()
    yield
    spend_guard.reset_for_tests()


def test_classify_routes():
    assert spend_guard.classify("POST", "/api/generate-video") == "video_gen"
    assert spend_guard.classify("POST", "/api/approval-queue/abc-123/approve") == "publish"
    assert spend_guard.classify("POST", "/api/engagement-queue/x1/send") == "publish"
    assert spend_guard.classify("POST", "/api/trigger-autopilot") == "autopilot"
    assert spend_guard.classify("POST", "/api/upload") == "upload"
    assert spend_guard.classify("GET", "/api/generate-video") is None
    assert spend_guard.classify("GET", "/") is None
    assert spend_guard.classify("POST", "/api/approval-queue/abc/reject") is None
    assert spend_guard.classify("POST", "/api/yt-proxy/register") is None


def test_per_client_window_blocks_then_other_client_ok(monkeypatch):
    monkeypatch.setenv("SPEND_GUARD_VIDEO_GEN_PER_CLIENT", "2")
    t = 1_800_000_000.0
    assert spend_guard.reserve("video_gen", "1.2.3.4", now=t)["ok"]
    assert spend_guard.reserve("video_gen", "1.2.3.4", now=t + 1)["ok"]
    blocked = spend_guard.reserve("video_gen", "1.2.3.4", now=t + 2)
    assert not blocked["ok"] and blocked["reason"] == "client_window"
    assert 3500 < blocked["retry_after"] <= 3601
    assert spend_guard.reserve("video_gen", "5.6.7.8", now=t + 3)["ok"]
    # window slides
    assert spend_guard.reserve("video_gen", "1.2.3.4", now=t + 3601)["ok"]


def test_daily_cap_is_global_and_resets_next_utc_day(monkeypatch):
    monkeypatch.setenv("SPEND_GUARD_AUTOPILOT_DAILY", "2")
    t = 1_800_000_000.0  # 2027-01-15 08:00 UTC
    assert spend_guard.reserve("autopilot", "a", now=t)["ok"]
    assert spend_guard.reserve("autopilot", "b", now=t)["ok"]
    blocked = spend_guard.reserve("autopilot", "c", now=t)
    assert not blocked["ok"] and blocked["reason"] == "daily_cap"
    assert spend_guard.summary(now=t)["classes"]["autopilot"]["blocked_today"] == 1
    assert spend_guard.reserve("autopilot", "c", now=t + 86400)["ok"]


def test_daily_ledger_survives_restart(monkeypatch, tmp_path):
    monkeypatch.setenv("SPEND_GUARD_AI_CALL_DAILY", "3")
    t = 1_800_000_000.0
    for _ in range(3):
        assert spend_guard.reserve("ai_call", "x", now=t)["ok"]
    assert (tmp_path / "spend_guard_ledger.json").exists()
    spend_guard.reset_for_tests()  # simulates process restart (memory cleared)
    assert not spend_guard.reserve("ai_call", "y", now=t)["ok"]


def test_refund_returns_budget():
    t = 1_800_000_000.0
    r = spend_guard.reserve("publish", "x", now=t)
    assert spend_guard.summary(now=t)["classes"]["publish"]["used_today"] == 1
    spend_guard.refund("publish", "x", r["ts"], now=t)
    assert spend_guard.summary(now=t)["classes"]["publish"]["used_today"] == 0


def test_zero_means_no_cap(monkeypatch):
    monkeypatch.setenv("SPEND_GUARD_UPLOAD_PER_CLIENT", "0")
    monkeypatch.setenv("SPEND_GUARD_UPLOAD_DAILY", "0")
    for i in range(300):
        assert spend_guard.reserve("upload", "x", now=1_800_000_000.0 + i)["ok"]


def test_client_key_prefers_x_real_ip():
    assert spend_guard.client_key({"x-real-ip": "9.9.9.9", "x-forwarded-for": "1.1.1.1"}, "10.0.0.1") == "9.9.9.9"
    assert spend_guard.client_key({"x-forwarded-for": "1.1.1.1, 10.0.0.2"}, "10.0.0.1") == "1.1.1.1"
    assert spend_guard.client_key({}, "10.0.0.1") == "10.0.0.1"


def test_middleware_blocks_paid_route_with_429(monkeypatch):
    monkeypatch.setenv("SPEND_GUARD_VIDEO_GEN_PER_CLIENT", "1")
    calls = []
    monkeypatch.setattr(main, "run_video_generation", lambda **kw: calls.append(kw))
    h = {"x-real-ip": "203.0.113.7"}
    ok = client.post("/api/generate-video", json={"prompt": "a"}, headers=h)
    assert ok.status_code == 200
    blocked = client.post("/api/generate-video", json={"prompt": "b"}, headers=h)
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0
    assert blocked.json()["spend_guard"]["reason"] == "client_window"
    assert len(calls) == 1  # the blocked request never reached the provider
    other = client.post("/api/generate-video", json={"prompt": "c"}, headers={"x-real-ip": "198.51.100.9"})
    assert other.status_code == 200


def test_middleware_refunds_failed_requests(monkeypatch):
    monkeypatch.setenv("SPEND_GUARD_AI_CALL_PER_CLIENT", "1")
    h = {"x-real-ip": "203.0.113.8"}
    # invalid body -> 422 from FastAPI, refunded, so the next call is not blocked
    assert client.post("/api/analyze", json={}, headers=h).status_code == 422
    assert client.post("/api/analyze", json={}, headers=h).status_code == 422
    assert spend_guard.summary()["classes"]["ai_call"]["used_today"] == 0


def test_kill_switch_and_bypass(monkeypatch):
    monkeypatch.setenv("SPEND_GUARD_VIDEO_GEN_PER_CLIENT", "1")
    monkeypatch.setattr(main, "run_video_generation", lambda **kw: None)
    h = {"x-real-ip": "203.0.113.9"}
    assert client.post("/api/generate-video", json={"prompt": "a"}, headers=h).status_code == 200
    assert client.post("/api/generate-video", json={"prompt": "a"}, headers=h).status_code == 429
    monkeypatch.setenv("SPEND_GUARD_BYPASS_TOKEN", "s3cret")
    assert client.post("/api/generate-video", json={"prompt": "a"},
                       headers={**h, "x-spend-guard-bypass": "wrong"}).status_code == 429
    assert client.post("/api/generate-video", json={"prompt": "a"},
                       headers={**h, "x-spend-guard-bypass": "s3cret"}).status_code == 200
    monkeypatch.setenv("SPEND_GUARD", "0")
    assert client.post("/api/generate-video", json={"prompt": "a"}, headers=h).status_code == 200


def test_unguarded_routes_untouched(monkeypatch):
    monkeypatch.setenv("SPEND_GUARD_AI_CALL_PER_CLIENT", "1")
    for _ in range(5):
        assert client.get("/health").status_code == 200
        assert client.get("/api/publish-guard/summary").status_code == 200


def test_health_and_summary_routes():
    body = client.get("/health").json()
    assert body["packs"]["spend_guard"] == "spend-guard-v1"
    assert body["spend_guard"]["enabled"] is True
    s = client.get("/api/spend-guard/summary").json()
    assert s["pack"] == "spend-guard-v1"
    assert s["classes"]["video_gen"]["per_client_limit"] == 6
    assert s["classes"]["video_gen"]["daily_limit"] == 40
    assert any(r["path"] == "/api/trigger-autopilot" for r in s["routes"])


def test_recent_blocks_mask_ip(monkeypatch):
    monkeypatch.setenv("SPEND_GUARD_PUBLISH_PER_CLIENT", "1")
    t = 1_800_000_000.0
    spend_guard.reserve("publish", "203.0.113.50", now=t)
    spend_guard.reserve("publish", "203.0.113.50", now=t + 1)
    blocks = spend_guard.summary(now=t + 2)["recent_blocks"]
    assert blocks and blocks[-1]["client"] == "203.0.x.x"
