import os
import time
from pathlib import Path

import publish_guard as pg


def test_fingerprint_stable(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLISH_GUARD_LEDGER", str(tmp_path / "ledger.json"))
    a = {"platform": "twitter,linkedin", "text": "Hello World!", "video_path": "/x/clip.mp4"}
    b = {"platform": "linkedin,twitter", "text": "  hello   world! ", "video_path": "clip.mp4"}
    assert pg.content_fingerprint(a) == pg.content_fingerprint(b)


def test_blocks_duplicate_publish(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLISH_GUARD_LEDGER", str(tmp_path / "ledger.json"))
    monkeypatch.setenv("PUBLISH_GUARD_BLOCK", "1")
    post = {"id": "p1", "platform": "twitter", "text": "Unique scout caption 42", "video_path": "a.mp4"}
    assert pg.check_publish(post)["blocked"] is False
    pg.record_publish(post, now=time.time())
    again = pg.check_publish(post, now=time.time() + 10)
    assert again["blocked"] is True
    assert again["reason"] == "duplicate_fingerprint"
    forced = pg.check_publish(post, force=True)
    assert forced["blocked"] is False


def test_schedule_queue_dedupe(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLISH_GUARD_LEDGER", str(tmp_path / "ledger.json"))
    monkeypatch.setenv("PUBLISH_GUARD_BLOCK", "1")
    existing = [
        {"id": "q1", "status": "PENDING", "platform": "twitter", "text": "Queue dupe test", "video_path": ""}
    ]
    post = {"platform": "twitter", "text": "Queue dupe test", "video_path": ""}
    result = pg.check_schedule(post, existing)
    assert result["blocked"] is True
    assert "queue" in result["reason"]


def test_summary_pack_name(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLISH_GUARD_LEDGER", str(tmp_path / "ledger.json"))
    s = pg.summary()
    assert s["pack"] == "publish-guard-v1"
    assert s["blocking"] is True
