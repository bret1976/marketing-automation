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


def test_auth_is_required_by_default_when_admin_password_is_set():
    assert client.get("/api/auth/status").json()["auth_required"] is True
    assert client.get("/api/settings").status_code == 401
    assert client.post("/api/settings", json={}).status_code == 401
    assert client.post("/api/trigger-autopilot").status_code == 401
    assert client.post("/api/growth-v2/dry-run").status_code == 401
    assert client.get("/api/analytics/summary").status_code == 401
    assert client.get("/api/publish-guard/summary").status_code == 401
    assert client.get("/api/spend-guard/summary").status_code == 401
    assert client.post(
        "/api/analyze",
        json={"video_path": "/etc/passwd", "website_url": "https://example.com"},
    ).status_code == 401
    assert client.post("/api/publish/twitter", json={"text": "blocked"}).status_code == 401
    # Login screen (HTML) and health stay reachable.
    home = client.get("/")
    assert home.status_code == 200
    assert "6Frame Studio" in home.text
    assert client.get("/health").status_code == 200
    assert client.get("/api/health").status_code == 200


def test_public_dashboard_opt_out():
    os.environ["ALLOW_PUBLIC_DASHBOARD"] = "1"
    try:
        assert client.get("/api/settings").status_code == 200
    finally:
        os.environ.pop("ALLOW_PUBLIC_DASHBOARD", None)


def test_http_basic_auth_unlocks_admin():
    assert client.get("/api/settings", headers=basic_headers()).status_code == 200
    assert client.get("/api/settings", headers=basic_headers(password="wrong")).status_code == 401


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
