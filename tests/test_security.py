import base64
import importlib
import os

from fastapi.testclient import TestClient


os.environ["ADMIN_PASSWORD"] = "test-password"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["DISABLE_BACKGROUND_SCHEDULER"] = "true"
os.environ.pop("REQUIRE_ADMIN_AUTH", None)

main = importlib.import_module("main")
client = TestClient(main.app)


def basic_headers(username="admin", password="test-password"):
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_dashboard_is_open_without_authentication_by_default():
    response = client.get("/")
    assert response.status_code == 200
    assert "6Frame Studio" in response.text
    assert client.get("/api/auth/status").json() == {"auth_required": False, "authenticated": True}
    assert client.get("/api/settings").status_code == 200


def test_opt_in_admin_auth_locks_sensitive_apis():
    os.environ["REQUIRE_ADMIN_AUTH"] = "true"
    try:
        assert client.get("/api/settings").status_code == 401
        assert client.post("/api/settings", json={}).status_code == 401
        assert client.post("/api/trigger-autopilot").status_code == 401
        assert client.post(
            "/api/analyze",
            json={"video_path": "/etc/passwd", "website_url": "https://example.com"},
        ).status_code == 401
        assert client.post("/api/publish/twitter", json={"text": "blocked"}).status_code == 401
        assert client.post("/api/publish/linkedin", json={"text": "blocked"}).status_code == 401
        # The dashboard HTML itself stays reachable so visitors are not blocked
        # by a browser basic-auth prompt before they can use the platform.
        home = client.get("/")
        assert home.status_code == 200
        assert "6Frame Studio" in home.text
    finally:
        os.environ.pop("REQUIRE_ADMIN_AUTH", None)


def test_http_basic_auth_unlocks_admin_when_auth_is_required():
    os.environ["REQUIRE_ADMIN_AUTH"] = "true"
    try:
        response = client.get("/api/settings", headers=basic_headers())
        assert response.status_code == 200
    finally:
        os.environ.pop("REQUIRE_ADMIN_AUTH", None)


def test_arbitrary_filesystem_paths_are_rejected_even_when_authenticated():
    response = client.post(
        "/api/analyze",
        headers=basic_headers(),
        json={"video_path": "/etc/passwd", "website_url": "https://example.com"},
    )
    assert response.status_code == 400
    assert "uploaded or generated media" in response.json()["detail"]


def test_allowed_uploaded_video_path_is_accepted_by_resolver(tmp_path, monkeypatch):
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    video = upload_dir / "clip.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(main, "UPLOAD_DIR", str(upload_dir))
    assert main.resolve_local_video_path(str(video)) == str(video.resolve())
