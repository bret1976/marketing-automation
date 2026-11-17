import os
from datetime import datetime
from zoneinfo import ZoneInfo

os.environ.setdefault("DISABLE_BACKGROUND_SCHEDULER", "true")
os.environ["CONTENT_TEAM_LLM"] = "stub"
os.environ["SPEND_GUARD"] = "0"

import content_team
from content_team import guard, scheduler, store, voices
from content_team.llm import STUB_MARKER, complete_json
from content_team.roles import (
    run_analytics,
    run_bookmarks,
    run_chief,
    run_collabs,
    run_competitors,
    run_factcheck,
    run_qt_scout,
    run_reply_scout,
    run_repurposer,
    run_researcher,
    run_sparring,
    run_sponsors,
    run_visuals,
    run_writer,
)
from content_team.web import fetch_text, host_allowed
from growth_v2 import approval_required


PT = ZoneInfo("America/Los_Angeles")


def _seed_all():
    store.append_ingest("trending", [
        {"id": "t1", "author": "maker", "text": "I lit a ship in a bottle with one practical bulb.", "url": "https://example.com/t1", "metrics": {"likes": 20}},
        {"id": "t2", "author": "lens", "text": "Anamorphic flares aren't the story. The cut is.", "url": "https://example.com/t2"},
    ] + [
        {"id": f"t{i}", "author": "feed", "text": f"Trending post {i} about cinematic AI film.", "url": f"https://example.com/t{i}"}
        for i in range(3, 12)
    ])
    store.append_ingest("competitor", [
        {"id": "c1", "author": "runwayml", "competitor": "runwayml", "text": "New gen video model demo.", "url": "https://example.com/c1"},
    ])
    store.append_ingest("bookmark", [
        {"id": "b1", "author": "editor", "text": "Bookmarked a pacing breakdown.", "url": "https://example.com/b1", "bookmarked_at": "2026-11-16T18:00:00-08:00"},
    ])
    store.append_ingest("qt_candidate", [
        {"id": "q1", "author": "bigaccount", "text": "Everyone is chasing hooks. Start with the frame.", "url": "https://example.com/q1", "why_qt": "craft"},
        {"id": "q2", "author": "studio", "text": "We over-grade when the light was already good.", "url": "https://example.com/q2"},
        {"id": "q3", "author": "dp", "text": "One lens, one room, ten stories.", "url": "https://example.com/q3"},
    ])
    store.append_ingest("reply_target", [
        {"id": "r1", "author": "hugecreator", "text": "What's the one lighting rule you won't break?", "url": "https://example.com/r1", "account_size": "large"},
    ])


def test_guard_blocks_publish_and_x_api_strings(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_TEAM_STATE_DIR", str(tmp_path / "ct"))
    hits = guard.scan_package()
    assert hits == []
    guard.assert_no_publish_hooks()
    try:
        guard.blocked()
        assert False, "blocked() should raise"
    except guard.ContentTeamGuardError as exc:
        assert "forbidden" in str(exc).lower()


def test_ingest_and_ideas(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_TEAM_STATE_DIR", str(tmp_path / "ct"))
    store.state_root()
    added = store.append_ingest("trending", [{"text": "hello", "author": "bret", "url": "https://example.com/x"}])
    assert len(added) == 1
    assert store.load_ingest("trending")[0]["author"] == "bret"
    idea = store.add_idea("A short about practical light in a bottle", source="dashboard", tags=["light"])
    assert idea["id"] and store.load_ideas()[0]["text"].startswith("A short")


def test_all_roles_with_stub_llm(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_TEAM_STATE_DIR", str(tmp_path / "ct"))
    monkeypatch.setenv("CONTENT_TEAM_LLM", "stub")
    _seed_all()
    reports = {
        "researcher": run_researcher(),
        "competitors": run_competitors(),
        "bookmarks": run_bookmarks(),
        "qt_scout": run_qt_scout(),
        "analytics": run_analytics(),
        "sparring": run_sparring("Ship in a bottle, one bulb"),
        "writer": run_writer("A 45s archival piece about Vostok 1"),
        "visuals": run_visuals("Vostok 1 launch"),
        "factcheck": run_factcheck("The clip reached 2 million views in 24 hours."),
        "repurposer": run_repurposer("Start with the frame, not the hook."),
        "reply_scout": run_reply_scout(),
        "sponsors": run_sponsors(urls=[], fetch=lambda url: {"ok": True, "url": url, "text": "Cinematic tools studio", "error": None}),
        "collabs": run_collabs(urls=[], fetch=lambda url: {"ok": True, "url": url, "text": "AI filmmakers", "error": None}),
    }
    reports["chief"] = run_chief()
    # 30 ideas, 3 QTs
    assert len(reports["researcher"]["payload"]["ideas"]) == 30
    assert len(reports["qt_scout"]["payload"]["candidates"]) == 3
    for role, report in reports.items():
        assert report["draft_id"]
        assert report["status"] == "AWAITING_REVIEW"
        if role != "analytics":
            assert report["llm_status"] in {"stub", "fallback", "real"}
            if role != "analytics" and report["llm_status"] == "stub":
                # stub marker appears in payload or body
                blob = str(report["payload"]) + report.get("body", "")
                assert STUB_MARKER in blob or role in {"analytics"}
    drafts = store.load_drafts()
    assert all(d["status"] == "AWAITING_REVIEW" for d in drafts)
    # reviewing a draft must not publish
    reviewed = store.set_draft_status(drafts[0]["id"], "APPROVED")
    assert reviewed["status"] == "APPROVED"
    assert "publish" not in reviewed


def test_llm_failure_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_TEAM_STATE_DIR", str(tmp_path / "ct"))
    monkeypatch.setenv("CONTENT_TEAM_LLM", "gemini")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("GROK_API_KEY", raising=False)
    store.append_ingest("trending", [{"text": "A practical bulb in a bottle.", "author": "maker", "id": "z1"}])
    report = run_researcher()
    assert report["provider"] == "fallback"
    assert report["llm_status"] == "fallback"
    assert len(report["payload"]["ideas"]) == 30
    assert "ingested" in report["payload"]["ideas"][0]["why"] or "Placeholder" in report["payload"]["ideas"][0]["title"] or "practical bulb" in report["payload"]["ideas"][0]["title"].lower() or "maker" in str(report["payload"]["ideas"][0])


def test_web_blocks_x_hosts():
    assert host_allowed("https://6framestudio.com/") is True
    assert host_allowed("https://api.twitter.com/2/tweets") is False
    assert host_allowed("https://api.x.com/2/users/me") is False
    assert host_allowed("https://x.com/someone/status/1") is False
    blocked = fetch_text("https://api.x.com/2/tweets")
    assert blocked["ok"] is False


def test_scheduler_fires_once_per_day_in_pt(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_TEAM_STATE_DIR", str(tmp_path / "ct"))
    morning = datetime(2026, 11, 16, 7, 5, tzinfo=PT)
    evening = datetime(2026, 11, 16, 19, 5, tzinfo=PT)
    noon = datetime(2026, 11, 16, 12, 0, tzinfo=PT)
    calls = []
    assert scheduler.due_jobs(noon) == []
    ran = scheduler.tick(morning, morning=lambda: calls.append("morning"), evening=lambda: calls.append("evening"))
    assert ran == ["morning"] and calls == ["morning"]
    ran2 = scheduler.tick(morning, morning=lambda: calls.append("morning"), evening=lambda: calls.append("evening"))
    assert ran2 == []  # once per date
    ran3 = scheduler.tick(evening, morning=lambda: calls.append("morning"), evening=lambda: calls.append("evening"))
    assert ran3 == ["evening"] and calls[-1] == "evening"


def test_voices_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_TEAM_STATE_DIR", str(tmp_path / "ct"))
    docs = voices.load_voices()
    assert "writer" in docs and "Don't" not in docs["writer"]["donts"] or docs["writer"]["donts"]
    updated = voices.update_voice("writer", {"donts": "Never auto-post."})
    assert updated["donts"] == "Never auto-post."
    assert voices.load_voices()["writer"]["donts"] == "Never auto-post."


def test_fastapi_endpoints(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_TEAM_STATE_DIR", str(tmp_path / "ct"))
    monkeypatch.setenv("CONTENT_TEAM_LLM", "stub")
    monkeypatch.setenv("ALLOW_PUBLIC_DASHBOARD", "1")
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    from fastapi.testclient import TestClient
    import main

    client = TestClient(main.app)
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["packs"]["content_team"] == content_team.PACK
    assert health.json()["content_team"]["publish_blocked"] is True

    seeded = client.post("/api/content-team/ingest", json={
        "kind": "trending",
        "source": "test",
        "items": [{"id": "e1", "author": "maker", "text": "Practical light in a bottle.", "url": "https://example.com/e1"}],
    })
    assert seeded.status_code == 200 and seeded.json()["added"] == 1

    docs = client.get("/api/content-team/docs/ingest")
    assert docs.status_code == 200 and "items" in docs.json()["payload"]

    idea = client.post("/api/content-team/ideas", json={"text": "Texted idea about Dover Patrol", "source": "api"})
    assert idea.status_code == 200

    for role in ("researcher", "competitors", "bookmarks", "qt_scout", "analytics", "chief", "reply_scout"):
        res = client.post(f"/api/content-team/run/{role}", json={})
        assert res.status_code == 200, res.text
        report = res.json()["report"]
        assert report["status"] == "AWAITING_REVIEW"

    for role, payload in (
        ("sparring", {"idea": "Bottle ship"}),
        ("writer", {"brief": "Bottle ship"}),
        ("visuals", {"subject": "Bottle ship"}),
        ("factcheck", {"text": "It did 1 million views."}),
        ("repurposer", {"text": "Start with the frame."}),
        ("sponsors", {"urls": []}),
        ("collabs", {"urls": []}),
    ):
        res = client.post(f"/api/content-team/run/{role}", json=payload)
        assert res.status_code == 200, (role, res.text)

    for path in (
        "/api/content-team/researcher",
        "/api/content-team/competitors",
        "/api/content-team/bookmarks",
        "/api/content-team/qt-scout",
        "/api/content-team/analytics",
        "/api/content-team/chief",
        "/api/content-team/ideas",
        "/api/content-team/drafts",
        "/api/content-team/voices",
        "/api/content-team/reports",
    ):
        res = client.get(path)
        assert res.status_code == 200, path

    drafts = client.get("/api/content-team/drafts").json()["items"]
    draft_id = drafts[0]["id"]
    reviewed = client.post(f"/api/content-team/drafts/{draft_id}/review", json={"status": "APPROVED"})
    assert reviewed.status_code == 200
    assert reviewed.json()["published"] is False
    assert reviewed.json()["sent"] is False

    # token gate: with auth on, a bad token is 401 and a good token works
    monkeypatch.setenv("ADMIN_PASSWORD", "secret-test")
    monkeypatch.setenv("CONTENT_TEAM_TOKEN", "routine-token")
    monkeypatch.delenv("ALLOW_PUBLIC_DASHBOARD", raising=False)
    # auth_enabled reads env at request time
    denied = client.get("/api/content-team/researcher")
    assert denied.status_code == 401
    ok = client.get("/api/content-team/researcher", headers={"X-Content-Team-Token": "routine-token"})
    assert ok.status_code == 200


def test_growth_v2_default_still_imported():
    assert approval_required({}) is True
