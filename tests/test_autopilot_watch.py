import json
import logging
from datetime import datetime, timedelta, timezone

import autopilot_watch as aw

HOURS = "15,0"  # 8 AM / 5 PM PT


def _now(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def _ledger(tmp_path, slots, last_at="2026-10-07T00:01:18Z"):
    (tmp_path / "autonomous_slot.json").write_text(json.dumps({
        "success_slots": slots,
        "last_success_slot": slots[-1] if slots else None,
        "last_success_at": last_at,
    }))


def _env(monkeypatch, tmp_path):
    aw.reset_for_tests()
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("AUTONOMOUS_HOURS", HOURS)
    monkeypatch.delenv("AUTOPILOT_WATCH", raising=False)
    monkeypatch.delenv("AUTOPILOT_WATCH_GRACE_MIN", raising=False)
    monkeypatch.delenv("AUTOPILOT_WATCH_LOOKBACK_HOURS", raising=False)


def test_parse_hours_shapes():
    assert aw.parse_hours("15,0") == [15, 0]
    assert aw.parse_hours("[15, 0, 15]") == [15, 0]
    assert aw.parse_hours(9) == [9]
    assert aw.parse_hours("x,25") == [1]
    assert aw.parse_hours("") == []


def test_expected_slot_ids_match_overlay_format():
    slots = aw.expected_slots(_now("2026-10-07T13:00:00"), [15, 0], 48)
    assert [s["slot"] for s in slots] == ["2026-10-05-15", "2026-10-06-0", "2026-10-06-15", "2026-10-07-0"]


def test_all_ok(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    _ledger(tmp_path, ["2026-10-05-15", "2026-10-06-0", "2026-10-06-15", "2026-10-07-0"])
    r = aw.report(settings={"autonomous_posting": True}, posts=[], now=_now("2026-10-07T13:00:00"), state_dir=tmp_path)
    assert r["status"] == "ok"
    assert r["consecutive_missed"] == 0
    assert r["last_success_slot"] == "2026-10-07-0"
    assert r["schedule"]["next_slot_utc"] == "2026-10-07T15:00:00Z"
    assert r["read_only"] is True


def test_oct5_outage_is_down(tmp_path, monkeypatch):
    """Replays the real 2026-10-05 outage: three slots in a row with no success."""
    _env(monkeypatch, tmp_path)
    _ledger(tmp_path, ["2026-10-04-0", "2026-10-04-15", "2026-10-05-0"], last_at="2026-10-05T00:00:54Z")
    r = aw.report(settings={"autonomous_posting": True}, posts=[], now=_now("2026-10-06T18:00:00"), state_dir=tmp_path)
    assert r["status"] == "down"
    assert r["consecutive_missed"] == 3
    assert r["missed_slots"] == ["2026-10-05-15", "2026-10-06-0", "2026-10-06-15"]
    assert r["last_success_age_hours"] > 40


def test_single_miss_is_late_and_current_slot_in_grace_is_due(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    _ledger(tmp_path, ["2026-10-06-15"])
    # 2026-10-07-0 missed; 2026-10-07-15 started 20 min ago (inside grace)
    r = aw.report(settings={"autonomous_posting": True}, posts=[], now=_now("2026-10-07T15:20:00"), state_dir=tmp_path)
    states = {s["slot"]: s["state"] for s in r["slots"]}
    assert states["2026-10-07-15"] == "due"
    assert states["2026-10-07-0"] == "missed"
    assert r["status"] == "late"
    assert r["consecutive_missed"] == 1


def test_attempted_slot_shows_running(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    _ledger(tmp_path, ["2026-10-07-0"])
    aw.observe("Autonomous autoposting slot 2026-10-07-15 reached. Initiating pipeline...")
    r = aw.report(settings={"autonomous_posting": True}, posts=[], now=_now("2026-10-07T15:05:00"), state_dir=tmp_path)
    assert r["slots"][-1] == {**r["slots"][-1], "slot": "2026-10-07-15", "state": "running"}
    assert r["status"] == "ok"


def test_disabled_autoposting(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    r = aw.report(settings={"autonomous_posting": False}, posts=[], now=_now("2026-10-07T13:00:00"), state_dir=tmp_path)
    assert r["status"] == "disabled"
    assert r["slots"] == []


def test_kill_switch(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("AUTOPILOT_WATCH", "0")
    assert aw.report() == {"pack": aw.PACK, "enabled": False}
    assert aw.install() is False


def test_classify_real_log_lines():
    gem = ("Google Search grounding failed inside trend scanner: 402 RESOURCE_EXHAUSTED. {'error': {'code': 402, "
           "'message': 'Your prepayment credits are depleted. Please go to AI Studio'}}")
    assert aw.classify(gem) == ["gemini_credits_depleted"]
    assert aw.classify("ERROR: [youtube] WKO-YgMQhLI: Sign in to confirm you\u2019re not a bot.") == ["youtube_bot_wall"]
    assert aw.classify("V10 isolated publish errors=['facebook: Missing required page_id. Refresh']") == ["facebook_page_id_missing"]
    assert aw.classify("429 RESOURCE_EXHAUSTED quota") == ["gemini_rate_limited"]
    assert "no_original_video" in aw.classify("No original video could be downloaded. Tried 3 source(s). Did not call PostProxy. Last error: x")
    assert aw.classify("Publishing post abc through PostProxy for platforms") == []


def test_redacts_secrets():
    out = aw.redact("GET https://x.test/v1?key=AIzaSECRET123&alt=json token=abc")
    assert "AIzaSECRET123" not in out and "abc" not in out
    assert "[redacted]" in out


def test_log_tap_and_post_scan_merge(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    _ledger(tmp_path, ["2026-10-07-0"])
    assert aw.install() is True
    logging.getLogger("main").warning("Google Search grounding failed: 402 RESOURCE_EXHAUSTED prepay credits are depleted")
    # Relative dates so the 72h lookback window keeps covering the recent row
    # (a fixed 2026-10-06 date aged out of the window and broke this test).
    recent = (datetime.now() - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%S")
    posts = [{"status": "FAILED", "scheduled_time": recent,
              "error_message": "No original video could be downloaded. Tried 3 source(s). Last error: Sign in to confirm you're not a bot"},
             {"status": "FAILED", "scheduled_time": "2026-09-01T16:00:00", "error_message": "Missing required page_id"}]
    r = aw.report(settings={"autonomous_posting": True}, posts=posts, state_dir=tmp_path)
    ids = {i["id"] for i in r["issues"]}
    assert {"gemini_credits_depleted", "no_original_video", "youtube_bot_wall"} <= ids
    assert "facebook_page_id_missing" not in ids  # outside lookback window
    gem = next(i for i in r["issues"] if i["id"] == "gemini_credits_depleted")
    assert gem["log_count"] >= 1 and "AI Studio" in gem["hint"]
    assert r["failed_rows"] == 1
    assert r["tap"]["installed"] is True
    aw._persist(force=True)
    saved = json.loads((tmp_path / "autopilot_watch.json").read_text())
    assert saved["signatures"]["gemini_credits_depleted"]["count"] >= 1
    aw.reset_for_tests()


def test_observe_never_raises():
    assert aw.observe(None) == []
    assert aw.observe("nothing interesting") == []


def test_health_and_summary_endpoints(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient
    import main
    client = TestClient(main.app)
    h = client.get("/health")
    assert h.status_code == 200
    body = h.json()
    assert body["packs"]["autopilot_watch"] == aw.PACK
    assert body["packs"]["publish_guard"]  # existing pack marker unchanged
    assert "status" in body["autopilot_watch"]
    s = client.get("/api/autopilot-watch/summary")
    assert s.status_code == 200
    assert s.json()["pack"] == aw.PACK


import pytest as _pytest


@_pytest.fixture(autouse=True)
def _public_dashboard_for_this_module(monkeypatch):
    # Auth is on by default when ADMIN_PASSWORD is set (other test modules set it).
    monkeypatch.setenv("ALLOW_PUBLIC_DASHBOARD", "1")


OCT22_404 = ("google_veo_lite: Video generation failed: 404 NOT_FOUND. {'error': {'code': 404, 'message': "
             "'This model models/veo-3.1-lite-generate-preview is no longer available.', 'status': 'NOT_FOUND'}}")


def test_oct22_slot_that_ran_and_gave_up_is_failed_not_missed(tmp_path, monkeypatch):
    """Replays 2026-10-23-0 (Oct 22 5 PM PT): it ran twice and gave up on a retired Veo model.
    The watch used to call that 'missed' (never ran); after a restart the log tap is empty too."""
    _env(monkeypatch, tmp_path)
    _ledger(tmp_path, ["2026-10-20-15", "2026-10-21-0", "2026-10-21-15", "2026-10-22-0", "2026-10-22-15"],
            last_at="2026-10-22T15:03:00Z")
    (tmp_path / "growth_v2_state.json").write_text(json.dumps({"slots": {
        "2026-10-23-0": {"attempts": 2, "status": "gave_up", "last_error": OCT22_404}}}))
    r = aw.report(settings={"autonomous_posting": True}, posts=[], now=_now("2026-10-23T13:55:00"), state_dir=tmp_path)
    row = {s["slot"]: s for s in r["slots"]}["2026-10-23-0"]
    assert row["state"] == "failed"
    assert row["attempts"] == 2
    assert "no longer available" in row["last_error"]
    assert r["failed_slots"] == ["2026-10-23-0"]
    assert r["missed_slots"] == ["2026-10-23-0"]  # still counts toward late/down
    assert r["status"] == "late" and r["consecutive_missed"] == 1


def test_classify_retired_video_model():
    assert "video_model_retired" in aw.classify(OCT22_404)
    assert "video_model_retired" not in aw.classify("404 page not found")
