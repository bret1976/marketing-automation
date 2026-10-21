"""x-safe-v1: X path only - no replies, no threads, <=2 per 20h window >=4h apart, hook-first <=280, 0-1 tag, no links, no dupes."""
import asyncio
import importlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def xp(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.delenv("X_SAFE_MODE", raising=False)
    monkeypatch.delenv("X_REPLIES_ENABLED", raising=False)
    import x_policy
    return importlib.reload(x_policy)


def _post(i=1, caption="Veo 3.1 froze a lightning bolt in acrylic\nHigh voltage trapped in clear plastic.", path=None):
    return {"id": f"p{i}", "platform": "twitter", "text": caption, "platform_captions": {"twitter": caption},
            "video_path": path or f"/static/assets/generated/clip{i}.mp4"}


def test_replies_and_threads_off_by_default(xp, monkeypatch):
    s = xp.summary()
    assert s["enabled"] and not s["replies_enabled"] and not s["threads_enabled"]
    assert s["max_posts_per_day"] == 2 and s["min_gap_hours"] == 4 and s["max_hashtags"] == 1
    monkeypatch.setenv("X_MAX_POSTS_PER_DAY", "9")
    monkeypatch.setenv("X_MIN_GAP_HOURS", "1")
    assert xp.max_posts_per_day() == 2 and xp.min_gap_hours() == 4  # env can only tighten


def test_caption_rules(xp):
    raw = ("Public TikTok original https://www.tiktok.com/@x/video/1 #6FrameStudio #AIFilmmaking #AICinema")
    out = xp.sanitize_caption(raw)
    assert "http" not in out and "tiktok.com" not in out
    assert len(re.findall(r"#\w+", out)) <= 1
    long = "Frozen Disc Glides On Magnets: " + "superconductor flux pinning " * 30 + "#Physics #Quantum #Science"
    out = xp.sanitize_caption(long)
    assert len(out) <= 280 and out.startswith("Frozen Disc Glides On Magnets") and len(re.findall(r"#\w+", out)) <= 1
    out = xp.sanitize_caption("So every shot starts from the last frame.\nmarketing-automation-production-dbd5.up.railway.app/x",
                              "Veo 3.1 + Seedance: 1 subject, 4 shots")
    assert out.splitlines()[0] == "Veo 3.1 + Seedance: 1 subject, 4 shots" and "railway.app" not in out


def test_link_only_and_text_only_refused(xp):
    ok, why, _ = xp.check(_post(caption=xp.sanitize_caption("https://www.tiktok.com/@a/video/1 #AICinema")), "")
    assert not ok
    p = _post()
    p.pop("video_path")
    ok, why, _ = xp.check(p, xp.sanitize_caption(p["text"]))
    assert not ok and "video" in why


def test_cadence_two_per_day_four_hours(xp):
    p1 = _post(1)
    assert xp.check(p1, p1["text"])[0]
    xp.record(p1, p1["text"])
    p2 = _post(2, "Seedance 2.5 drops a glacier onto a city\nOne shot, zero stock.")
    ok, why, retry = xp.check(p2, p2["text"])
    assert not ok and retry is not None and retry - datetime.now(timezone.utc) > timedelta(hours=3, minutes=59)
    data = json.load(open(xp.ledger_path()))
    data["posts"][0]["at"] = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    json.dump(data, open(xp.ledger_path(), "w"))
    assert xp.check(p2, p2["text"])[0]
    xp.record(p2, p2["text"])
    data = json.load(open(xp.ledger_path()))
    data["posts"][1]["at"] = (datetime.now(timezone.utc) - timedelta(hours=4, minutes=30)).isoformat()
    json.dump(data, open(xp.ledger_path(), "w"))
    p3 = _post(3, "Kling 4.0 turns a paper plane into a jet\nWatch the fold.")
    ok, why, retry = xp.check(p3, p3["text"])
    assert not ok and "2/20h" in why  # 2 in the window -> wait for the oldest to age out


def test_slots_do_not_drift_later_each_day(xp, monkeypatch):
    """8 AM / 5 PM PT slots (15:00 / 00:00 UTC) + up to 53 min jitter must stay on their slots.
    The old strict 24h window made each day's 8 AM X wait for yesterday's jittered time (ratchet)."""
    day0 = datetime(2026, 10, 22, 15, 0, tzinfo=timezone.utc)
    clock = {"now": day0}
    monkeypatch.setattr(xp, "_now", lambda: clock["now"])
    monkeypatch.setattr(xp.random, "randint", lambda a, b: b)  # worst-case jitter every time
    posted = []
    for i in range(10):  # 5 days of slots
        slot = day0 + timedelta(hours=9 * ((i + 1) // 2) + 15 * (i // 2))
        clock["now"] = slot
        allowed = xp.next_allowed_at()
        at = max(slot, allowed) + timedelta(minutes=53, seconds=59)
        clock["now"] = at
        p = _post(100 + i, f"Distinct physics short number {i}\nnew words {i}", path=f"/c/{i}.mp4")
        assert xp.check(p, p["text"])[0], (i, at)
        xp.record(p, p["text"])
        posted.append(at - slot)
    assert max(posted) < timedelta(hours=1), posted


def test_recovers_from_drifted_ledger(xp, monkeypatch):
    """Real Oct 20-21 2026 ledger: X went out 21:20 and 06:09 UTC. Next 8 AM PT slot must not wait till 2:20 PM PT."""
    data = {"posts": [{"at": "2026-10-20T21:20:14+00:00", "post_id": "a"}, {"at": "2026-10-21T06:09:18+00:00", "post_id": "b"}]}
    json.dump(data, open(xp.ledger_path(), "w"))
    now = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(xp, "_now", lambda: now)
    assert xp.next_allowed_at() <= datetime(2026, 10, 21, 17, 21, tzinfo=timezone.utc)  # was 21:20 UTC


def test_cap_window_env_only_tightens(xp, monkeypatch):
    assert xp.cap_window_hours() == 20
    monkeypatch.setenv("X_CAP_WINDOW_HOURS", "6")
    assert xp.cap_window_hours() == 20
    monkeypatch.setenv("X_CAP_WINDOW_HOURS", "48")
    assert xp.cap_window_hours() == 24


def test_never_duplicates_or_reposts(xp):
    p1 = _post(1)
    xp.record(p1, p1["text"])
    data = json.load(open(xp.ledger_path()))
    data["posts"][0]["at"] = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    json.dump(data, open(xp.ledger_path(), "w"))
    same_caption = _post(9, p1["text"] + " #AIvideo")
    assert not xp.check(same_caption, same_caption["text"])[0]
    same_video = _post(10, "A different caption entirely\nnew words", path=p1["video_path"])
    assert not xp.check(same_video, same_video["text"])[0]
    assert not xp.check(p1, "fresh text for the same post id\nmore")[0]


def test_schedule_varies_minutes(xp):
    planned = datetime(2026, 10, 17, 4, 4, 37)
    mins = {xp.schedule_time(planned).minute for _ in range(40)}
    assert len(mins) > 5


@pytest.fixture()
def main_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("DISABLE_BACKGROUND_SCHEDULER", "true")
    import x_policy
    importlib.reload(x_policy)
    import main
    # main may already be imported by another test with a different STATE_DIR;
    # point its state files at this test's tmp dir so runs never share/leak state.
    monkeypatch.setattr(main, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(main, "SCHEDULED_POSTS_FILE", str(tmp_path / "scheduled_posts.json"))
    monkeypatch.setattr(main, "POSTPROXY_STATE_FILE", str(tmp_path / "postproxy_state.json"))
    return main


def test_publish_gate_only_touches_x(main_mod, monkeypatch):
    m = main_mod
    calls = []
    monkeypatch.setattr(m, "publish_via_postproxy", lambda post, settings, platforms: (calls.append((list(platforms), json.loads(json.dumps(post)))) or {"successes": list(platforms), "errors": []}))
    monkeypatch.setattr(m, "enforce_postproxy_daily_limit", lambda *a, **k: None)
    monkeypatch.setattr(m.publish_guard, "check_publish", lambda post, force=False: {"blocked": False})
    settings = {"postproxy_enabled": True, "postproxy_api_key": "k"}
    tiktok_cap = "Hook line\nstory\n#AIvideo #AIFilmmaking #AIshortfilm https://example.com"
    post = {"id": "a", "platform": "twitter,tiktok", "text": "x", "video_path": "/static/assets/generated/a.mp4",
            "hook": "Veo 3.1 froze lightning", "thread": ["one", "two"],
            "platform_captions": {"twitter": "How did we do it? https://tiktok.com/@a/video/1 #A #B #C", "tiktok": tiktok_cap}}
    res = asyncio.run(m.publish_post_to_platforms(post, settings))
    assert "twitter" in res["successes"]
    sent = calls[0][1]
    assert sent["platform_captions"]["tiktok"] == tiktok_cap  # other platform unchanged
    x = sent["platform_captions"]["twitter"]
    assert x.splitlines()[0] == "Veo 3.1 froze lightning" and "http" not in x and len(re.findall(r"#\w+", x)) <= 1
    assert sent["thread"] is None
    # second X post right away: X is held + queued separately, TikTok still goes now
    post2 = dict(post, id="b", video_path="/static/assets/generated/b.mp4",
                 platform_captions={"twitter": "Seedance 2.5 drops a glacier", "tiktok": tiktok_cap + " 2"})
    res2 = asyncio.run(m.publish_post_to_platforms(post2, settings))
    assert calls[1][0] == ["tiktok"] and any("x_policy" in e for e in res2["errors"])
    queued = [p for p in m.load_scheduled_posts() if p.get("platform") == "twitter" and p.get("status") == "PENDING"]
    assert len(queued) == 1 and datetime.fromisoformat(queued[0]["scheduled_time"]) > datetime.now() + timedelta(hours=3, minutes=59)
    # an X-only post that is held gets rescheduled, not failed
    res3 = asyncio.run(m.publish_post_to_platforms(dict(queued[0]), settings))
    p = dict(queued[0])
    m.apply_publish_result_to_post(p, res3)
    assert p["status"] == "PENDING" and res3.get("x_deferred_until")


def test_x_reply_send_blocked_and_drafting_off(main_mod, monkeypatch):
    m = main_mod
    from fastapi import HTTPException
    monkeypatch.setattr(m, "load_engagement_queue", lambda: [{"id": "e1", "platform": "twitter", "status": "PENDING_REVIEW",
                                                              "drafted_reply": "hi", "source_tweet_id": "1"}])
    monkeypatch.setattr(m, "load_settings", lambda: {"mock_mode": False, "twitter_consumer_key": "a"})
    with pytest.raises(HTTPException) as e:
        m.send_engagement_reply("e1")
    assert e.value.status_code == 403
    called = []
    monkeypatch.setattr(m, "fetch_and_draft_mention_replies_with_bearer", lambda s: called.append(1))
    m.fetch_and_draft_mention_replies({k: "x" for k in ("twitter_consumer_key", "twitter_consumer_secret", "twitter_access_token", "twitter_access_token_secret")})
    assert not called
    assert m.health()["x_policy"]["replies_enabled"] is False
