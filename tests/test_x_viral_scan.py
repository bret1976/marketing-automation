"""X viral scan (Bret 2026-11-13 t752u): last 24 h on X, top 10 viral AI videos (no prompt-sharing posts),
easiest to recreate, recreated at $0, held for Bret's approval. Fixtures mirror the real 2026-11-13 scan."""
import asyncio
import json
import os
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import brand_gate  # noqa: E402
import growth_v2 as g  # noqa: E402
import x_recreate_recipes as rr  # noqa: E402
import x_viral_scan as xs  # noqa: E402

NOW = datetime(2026, 11, 13, 18, 52, tzinfo=timezone.utc)


def _post(pid, user, text, views, hours_old, video_s=30.0, likes=100, media="video"):
    created = (NOW - timedelta(hours=hours_old)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    mk = f"13_{pid}"
    media_obj = {"media_key": mk, "type": media}
    if media == "video" and video_s:
        media_obj["duration_ms"] = int(video_s * 1000)
    return ({"id": pid, "author_id": f"u{user}", "created_at": created, "text": text,
             "attachments": {"media_keys": [mk]} if media else {},
             "public_metrics": {"impression_count": views, "like_count": likes, "retweet_count": 1, "reply_count": 1}},
            {"id": f"u{user}", "username": user, "name": user}, media_obj if media else None)


def _snapshot(posts, stories=(), fetched_at=NOW):
    data, users, media = [], [], []
    for p, u, m in posts:
        data.append(p)
        users.append(u)
        if m:
            media.append(m)
    return {"fetched_at": fetched_at.isoformat(), "fetched_by": "test", "stories": list(stories),
            "post_payloads": [{"data": data, "includes": {"users": users, "media": media}}]}


SHACK = _post("2121022587580871144", "layoproperty", "At this point, AI need to be banned https://t.co/x", 1763518, 19.4, 162.2, 15287)
JENSEN = _post("2120962452137431338", "JesseCohenInv", "Whoever made this is a genius", 1765856, 23.4, 154.5, 31722)
H1KEY = _post("2121166189326180674", "H1KEY_official", "H1-KEY Digital Single Unbelievable Official M/V", 226739, 9.9, 26.7, 409)
OLD = _post("2120914062473155009", "MandelDuck", "The Office AI: Jensen Huang gets a new job at Dunder Mifflin", 4591336, 26.6, 154.5)
PROMPT = _post("2121276514524086384", "8clerk8", "How to write effective prompts for Grok Imagine Video", 23345, 2.6, 38.2)
PROMPT2 = _post("2121258786233188568", "DasSprompts", "Grok Imagine prompt: The video references a close-up portrait", 900000, 3.7, 15.0)
PHOTO = _post("2120944241878651267", "DiscussingFilm", "First look at an AI horror film", 2167735, 5.0, None, media="photo")
STORIES = [
    {"id": "s-shack", "name": "AI Video of Woman Building Shack Sparks Viewer Debate",
     "hook": "A woman in a bright orange outfit hammers together a wooden shack in a grassy field.",
     "cluster_posts_results": [{"post_id": SHACK[0]["id"]}]},
    {"id": "s-jensen", "name": "AI Video Puts Nvidia's Jensen Huang in The Office as New Hire",
     "hook": "Picture Nvidia CEO Jensen Huang stepping into Dunder Mifflin's chaos.",
     "cluster_posts_results": [{"post_id": JENSEN[0]["id"]}, {"post_id": OLD[0]["id"]}]},
    {"id": "s-h1key", "name": "H1-KEY's AI Music Video Sparks Fan Backlash",
     "cluster_posts_results": [{"post_id": H1KEY[0]["id"]}]},
    {"id": "s-film", "name": "First Look at AI Horror Thriller 'ANSWR'", "cluster_posts_results": [{"post_id": PHOTO[0]["id"]}]},
]


def _scan(posts, stories=STORIES, tmp_path=None, now=NOW):
    path = os.path.join(str(tmp_path), "snap.json")
    with open(path, "w") as fh:
        json.dump(_snapshot(posts, stories), fh)
    return xs.run_scan(source=xs.SnapshotSource(path), now=now)


# --------------------------------------------------------------------------- 24 h window
def test_24h_window_drops_posts_older_than_24h(tmp_path):
    res = _scan([SHACK, OLD], tmp_path=tmp_path)
    urls = [t["url"] for t in res["top"]]
    assert all("2120914062473155009" not in u for u in urls), "4.59M-view post 26.6 h old must be out of the window"
    assert any("outside the last 24 h (26.6 h old)" in line for line in res["log"])
    assert res["window_hours"] == 24.0


def test_brand_gate_rechecks_x_item_age_at_publish():
    prov = {"platform": "X", "url": "https://x.com/a/status/1", "title": "x", "published_at": (NOW - timedelta(hours=26)).isoformat(),
            "views": 10, "genre_evidence": "X News story: AI Video of Woman Building Shack", "video_duration_s": 10}
    ok, why, _ = brand_gate.check_viral_item(prov, now=NOW, max_age=25)
    assert not ok and "not within" in why
    prov["published_at"] = (NOW - timedelta(hours=3)).isoformat()
    ok, why, _ = brand_gate.check_viral_item(prov, now=NOW, max_age=25)
    assert ok, why


def test_stale_snapshot_is_refused(tmp_path):
    path = tmp_path / "snap.json"
    path.write_text(json.dumps(_snapshot([SHACK], STORIES, fetched_at=NOW - timedelta(hours=8))))
    with pytest.raises(xs.XScanUnavailable, match="stale"):
        xs.SnapshotSource(str(path)).fetch(NOW)


def test_no_credential_and_no_snapshot_skips_with_named_credential(monkeypatch, tmp_path):
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("TWITTER_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("X_SCAN_SNAPSHOT", str(tmp_path / "missing.json"))
    with pytest.raises(xs.XScanUnavailable, match="X_BEARER_TOKEN") as exc:
        xs.source_from_env({})
    assert isinstance(exc.value, brand_gate.BrandGateSkip)  # -> the slot is skipped, never filled


def test_bearer_token_selects_x_api_source(monkeypatch):
    monkeypatch.setenv("X_BEARER_TOKEN", "test-token")
    assert isinstance(xs.source_from_env({}), xs.XApiSource)


# --------------------------------------------------------------------------- prompt-sharing exclusion
def test_prompt_sharing_posts_are_excluded_even_when_most_viewed(tmp_path):
    res = _scan([SHACK, PROMPT, PROMPT2], tmp_path=tmp_path)
    authors = [t["author"] for t in res["top"]]
    assert "8clerk8" not in authors and "DasSprompts" not in authors
    assert sum("prompt-sharing post" in line for line in res["log"]) == 2


def test_brand_gate_blocks_prompt_post_at_publish():
    prov = {"platform": "X", "url": "https://x.com/a/status/1", "title": "Grok Imagine prompt: a cat",
            "published_at": (NOW - timedelta(hours=1)).isoformat(), "views": 99, "genre_evidence": "post text: 'grok imagine'",
            "video_duration_s": 6}
    ok, why, _ = brand_gate.check_viral_item(prov, now=NOW)
    assert not ok and "prompt" in why


def test_posts_without_native_video_or_genre_are_dropped(tmp_path):
    off = _post("2121041516147638410", "SonnyFani", "HOLY SHIT! A POLICE OFFICER JUMPS FROM A TOWER", 2208, 18, 10.0)
    res = _scan([SHACK, PHOTO, off], tmp_path=tmp_path)
    assert [t["author"] for t in res["top"]] == ["layoproperty"]
    assert any("no native video (photo)" in line for line in res["log"])
    assert any("not AI-video genre" in line for line in res["log"])


# --------------------------------------------------------------------------- top-10 ranking
def test_top_10_ranked_by_views_with_required_fields(tmp_path):
    posts = [_post(str(3000 + i), f"maker{i}", f"AI short film number {i} made with AI", 1000 * (i + 1), 2 + i, 8.0 + i)
             for i in range(14)]
    res = _scan(posts, stories=[], tmp_path=tmp_path)
    top = res["top"]
    assert len(top) == 10
    assert [t["views"] for t in top] == sorted([1000 * (i + 1) for i in range(14)], reverse=True)[:10]
    assert [t["rank"] for t in top] == list(range(1, 11))
    for t in top:
        for k in ("url", "author", "created_at", "views", "likes", "video_duration_s"):
            assert t[k] not in (None, ""), k
        assert t["url"].startswith("https://x.com/")


def test_same_video_reshared_is_counted_once(tmp_path):
    reshare = _post("2121231982113763733", "OGAFredd", "Lol this AI shack", 2000, 4, 162.2)
    stories = [dict(STORIES[0], cluster_posts_results=[{"post_id": SHACK[0]["id"]}, {"post_id": reshare[0]["id"]}])]
    res = _scan([SHACK, reshare], stories=stories, tmp_path=tmp_path)
    assert len(res["top"]) == 1 and res["top"][0]["author"] == "layoproperty"
    assert res["top"][0]["reshares"][0]["views"] == 2000


# --------------------------------------------------------------------------- recreate scoring
def test_real_person_and_copyrighted_ip_are_not_recreatable(tmp_path):
    res = _scan([SHACK, JENSEN, H1KEY], tmp_path=tmp_path)
    by = {t["author"]: t for t in res["top"]}
    assert by["JesseCohenInv"]["recreatable"] is False and by["JesseCohenInv"]["recreate_score"] == 0
    assert any("Jensen Huang" in b for b in by["JesseCohenInv"]["blocks"])
    assert any("The Office" in b or "Dunder Mifflin" in b for b in by["JesseCohenInv"]["blocks"])
    assert by["H1KEY_official"]["recreatable"] is False
    assert by["layoproperty"]["recreatable"] is True and by["layoproperty"]["recipe"] == "x-ai-shack-vs-real-cabin"


def test_pick_is_the_easiest_recreatable_not_the_most_viewed(tmp_path):
    res = _scan([SHACK, JENSEN, H1KEY], tmp_path=tmp_path)
    assert res["top"][0]["author"] == "JesseCohenInv"  # most viewed
    assert res["pick"]["author"] == "layoproperty"  # but not recreatable -> next best recreatable
    assert any(line.startswith("PICK #") for line in res["log"])


def test_rubric_points_are_logged_and_short_single_scene_scores_higher():
    short = {"text": "An AI video of a man building a cabin", "video_duration_s": 12, "stories": []}
    long_ = {"text": "AI trailer: a man building a cabin, episode 2", "video_duration_s": 140, "stories": []}
    a, b = xs.recreate_score(short), xs.recreate_score(long_)
    assert a["points"] == {"duration": 3, "single_scene": 2, "few_characters": 2, "zero_cost_recipe": 3}
    assert a["score"] == 10 and b["score"] < a["score"]
    assert any("duration 12" in w for w in a["why"])


def test_no_recipe_means_not_recreatable_at_zero_cost():
    r = xs.recreate_score({"text": "AI video of a dragon over a city", "video_duration_s": 10, "stories": []})
    assert r["recreatable"] is False and r["score"] == 0 and r["recipe"] is None


def test_nothing_recreatable_skips_the_slot(monkeypatch, tmp_path):
    res = _scan([JENSEN, H1KEY], tmp_path=tmp_path)
    assert res["pick"] is None
    with pytest.raises(brand_gate.BrandGateSkip, match="none of the top 10"):
        g.produce_x_recreate({}, {}, scan=res)


def test_recipe_topic_speaks_the_real_view_count():
    t = rr.build_topic("x-ai-shack-vs-real-cabin", {"views": 1763518, "url": "u"})
    assert t["vo"][0]["say"].startswith("one point eight million people")
    assert t["vo"][0]["show"].startswith("1.8M views")
    assert rr._views(23345) == ("twenty-three thousand", "23K")


# --------------------------------------------------------------------------- approval hold (real path, render faked)
def _fake_produce_footage(calls):
    def fake(out_dir, topic=None, viral_pick=None, name=None, **k):
        calls.append({"topic": topic, "viral_pick": viral_pick})
        v = os.path.join(out_dir, f"{name or 'g2_x'}.mp4")
        open(v, "wb").write(b"\0")
        clips = [{"ok": True, "clip": f"Building a Log Cabin {i}.jpg", "reason": "named", "shot": i, "narration": ["x"]}
                 for i in range(4)]
        gate = {"brand": dict(brand_gate.PINNED_BRAND), "viral_item": viral_pick["matched"][0], "clips": clips}
        concept = {"topic": topic["topic"], "topic_key": topic["key"], "title_line": topic["title_line"],
                   "on_screen_hook": topic["on_screen_hook"], "hook": topic["hook"], "lines": topic["lines"],
                   "hashtags": topic["hashtags"], "vo": topic["vo"]}
        info = {"engine": "footage", "media_source": "archival_footage", "captions": [{"text": "x", "start": 0, "end": 2}],
                "brand_gate": gate, "landscape_path": None}
        return v, None, concept, info
    return fake


def test_x_pick_is_rendered_and_held_for_approval_never_published(monkeypatch, tmp_path):
    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps(_snapshot([SHACK, JENSEN, H1KEY, PROMPT], STORIES, fetched_at=datetime.now(timezone.utc))))
    # keep the fixture posts inside the real 24 h window by shifting them to "now"
    data = json.loads(snap.read_text())
    shift = datetime.now(timezone.utc) - NOW
    for p in data["post_payloads"][0]["data"]:
        p["created_at"] = (datetime.fromisoformat(p["created_at"].replace("Z", "+00:00")) + shift).isoformat()
    snap.write_text(json.dumps(data))
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("TWITTER_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("X_SCAN_SNAPSHOT", str(snap))
    monkeypatch.setenv("VIDEO_ENGINE", "footage")
    monkeypatch.delenv("VIRAL_SCAN", raising=False)
    monkeypatch.setenv("GROWTH_PLATFORMS", "tiktok,instagram,linkedin")
    calls = []
    monkeypatch.setattr(g.footage_mode, "produce_footage", _fake_produce_footage(calls))
    published, posts = [], []

    async def publish(post, settings, **k):
        published.append(post["id"])
        return {"successes": ["tiktok"], "errors": []}
    fake = types.SimpleNamespace(
        STATE_DIR=str(tmp_path), GENERATED_DIR=str(tmp_path),
        load_settings=lambda: {"public_base_url": "https://app.example", "require_autopilot_approval": False},
        load_scheduled_posts=lambda: [dict(p) for p in posts],
        save_scheduled_posts=lambda ps: (posts.clear(), posts.extend(ps)),
        publish_post_to_platforms=publish, fetch_twitter_bearer_token=lambda s: None)
    monkeypatch.setattr(g, "_main", lambda: fake)
    assert asyncio.run(g.run_slot("2026-11-14-0", fake.load_settings())) is True
    assert published == [], "nothing may publish without Bret's explicit approval"
    assert calls and calls[0]["topic"]["key"] == "x-ai-shack-vs-real-cabin"
    held = [p for p in posts if p["status"] == "AWAITING_APPROVAL"]
    assert len(held) == 1
    pkg = json.load(open(tmp_path / "approvals" / "2026-11-14-0" / "approval.json"))
    assert pkg["status"] == "AWAITING_APPROVAL"
    assert pkg["viral_source"]["platform"] == "X" and "layoproperty" in pkg["viral_source"]["url"]
    assert pkg["viral_source"]["views"] == 1763518 and pkg["viral_source"]["video_duration_s"] == 162.2
    assert len(pkg["x_scan"]["top"]) == 3 and pkg["x_scan"]["pick"]["author"] == "layoproperty"
    assert all(t["author"] != "8clerk8" for t in pkg["x_scan"]["top"])
    assert pkg["recreation"]["cost_usd"] == 0.0 and "NOT AI-generated" in pkg["recreation"]["method"]
    assert pkg["script"] and pkg["platform_captions"] and pkg["approve"].endswith("/approve")


def test_x_scan_is_default_and_youtube_only_on_explicit_opt_out(monkeypatch):
    monkeypatch.delenv("VIRAL_SCAN", raising=False)
    assert g.x_scan_enabled() is True
    monkeypatch.setenv("VIRAL_SCAN", "youtube")
    assert g.x_scan_enabled() is False
    monkeypatch.setenv("REQUIRE_AUTOPILOT_APPROVAL", "false")
    assert g.approval_required({"require_autopilot_approval": False}) is True


def test_snapshot_upload_endpoint_disabled_without_token():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")).read()
    block = src[src.index('@app.post("/api/x-scan/snapshot")'):][:700]
    assert 'os.environ.get("X_SCAN_INGEST_TOKEN", "")' in block and "if not token or not hmac.compare_digest" in block
