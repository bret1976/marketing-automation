import os
import shutil
import subprocess
from datetime import datetime

import pytest

import stitched_shorts as ss


def _plan(**over):
    plan = {
        "topic": "Desert robot sunrise", "topic_key": "desert robot sunrise", "title_line": "Rust At Dawn",
        "style": "Small rusty robot, orange desert dunes, golden hour, 35mm anamorphic.",
        "on_screen_hook": "1 robot, 4 shots, 0 cameras",
        "shots": [
            {"role": "hook", "seconds": 5, "prompt": "Robot slams into sand with a metallic clang", "caption": ""},
            {"role": "broll", "seconds": 5, "prompt": "Robot walks across a dune", "caption": "Locked style prompt"},
            {"role": "broll", "seconds": 5, "prompt": "Close-up of robot eye lens", "caption": "Same lens every shot"},
            {"role": "broll", "seconds": 5, "prompt": "Wide shot ending on the opening frame", "caption": "Ends where it began"},
        ],
        "comment_question": "Which shot is AI?",
        "platforms": {"youtube": {"title": "1 robot, 4 shots: how it was made", "hashtags": ["#AIFilmmaking", "#Veo3", "#fyp", "#Seedance"]},
                      "tiktok": {"caption": "This robot took 4 prompts", "hashtags": ["#AIVideo", "#CinematicAI", "#viral"]},
                      "instagram": {"caption": "Line one\nSave for later", "hashtags": ["#AIFilmmaking"]},
                      "linkedin": {"caption": "Process post", "hashtags": ["#AI", "#Film", "#Workflow", "#Extra"]}},
    }
    plan.update(over)
    return plan


def test_sanitize_plan_fits_20_30s_and_hook_rules():
    p = ss.sanitize_plan(_plan())
    total = sum(s["seconds"] for s in p["shots"])
    assert 20 <= total <= 30
    assert p["shots"][0]["role"] == "hook" and p["shots"][0]["seconds"] in (4, 6)
    assert all(ss.BROLL_MIN <= s["seconds"] <= ss.BROLL_MAX for s in p["shots"][1:])
    assert all("9:16" in s["prompt"] for s in p["shots"])
    # style is locked into every prompt for consistency
    assert all(p["style"][:40].lower() in s["prompt"].lower() for s in p["shots"])


def test_sanitize_plan_rejects_hook_without_number_and_too_few_shots():
    with pytest.raises(ValueError):
        ss.sanitize_plan(_plan(on_screen_hook="No digits here"))
    with pytest.raises(ValueError):
        ss.sanitize_plan(_plan(shots=_plan()["shots"][:2]))


def test_fit_durations_shrinks_long_plans():
    shots = [{"role": "hook", "seconds": 6}] + [{"role": "broll", "seconds": 8} for _ in range(3)]
    ss.fit_durations(shots)
    assert sum(s["seconds"] for s in shots) <= 30


def test_plan_short_falls_back_to_curated_when_llm_fails():
    def bad_llm(settings, prompt):
        raise RuntimeError("down")
    ledger = ss.CostLedger(1.5)
    p = ss.plan_short({}, llm=bad_llm, ledger=ledger)
    assert p["plan_source"] == "curated_fallback"
    assert ss.has_number(p["on_screen_hook"])


def test_plan_prompt_is_ai_filmmaking_niche_not_science():
    assert "AI filmmaking" in ss.PLAN_PROMPT and "how this shot was made" in ss.PLAN_PROMPT
    assert "Engineering at the Extreme" not in ss.PLAN_PROMPT


def test_platform_captions_distinct_and_3_to_5_hashtags():
    caps = ss.build_platform_captions(ss.sanitize_plan(_plan()))
    assert set(caps) == {"youtube", "tiktok", "instagram", "linkedin", "twitter"}
    texts = [c["caption"] for c in caps.values()]
    assert len(set(texts)) == 5
    for name, c in caps.items():
        if name == "twitter":  # x-safe-v1: X gets 0-1 hashtag
            assert len(c["hashtags"]) <= 1
        else:
            assert 3 <= len(c["hashtags"]) <= 8, name
        assert "#fyp" not in c["caption"].lower() and "#viral" not in c["caption"].lower()
        assert "http" not in c["caption"]
    assert len(caps["linkedin"]["hashtags"]) in (3, 4, 5)
    assert any(ch.isdigit() for ch in caps["youtube"]["title"])


def test_staggered_schedule_never_simultaneous():
    start = datetime(2026, 10, 16, 9, 0, 0)
    sched = ss.staggered_schedule(["linkedin", "instagram", "youtube", "tiktok"], start)
    assert len(set(sched.values())) == 4
    assert sched["youtube"] == start.isoformat(timespec="seconds")


def test_cost_cap_blocks_before_paid_call():
    ledger = ss.CostLedger(0.5)
    ledger.charge("x", 0.45)
    with pytest.raises(ss.CostCapExceeded):
        ledger.reserve("veo", 0.4)
    assert ss.shot_cost("veo_fast", 4) == pytest.approx(0.40)
    assert ss.shot_cost("seedance_fast", 5) == pytest.approx(0.108, abs=0.001)


def test_cost_cap_env_override(monkeypatch):
    monkeypatch.setenv("SHORTS_COST_CAP_USD", "0.9")
    assert ss.cost_cap({}) == 0.9
    monkeypatch.delenv("SHORTS_COST_CAP_USD")
    assert ss.cost_cap({}) == ss.DEFAULT_COST_CAP == 0.75


def test_generate_shot_retries_max_2_then_veo_lite_fallback(tmp_path):
    calls = []

    def failing(name):
        def f(s, p, sec, d, **kw):
            calls.append(name)
            raise RuntimeError("boom")
        return f

    def ok(name):
        def f(s, p, sec, d, **kw):
            calls.append(name)
            open(d, "wb").write(b"x")
            return {"model": name, "billed_seconds": sec}
        return f

    gens = {"seedance_fast": failing("seedance_fast"), "veo_lite": ok("veo_lite"), "veo_fast": failing("veo_fast")}
    shot = {"index": 2, "role": "broll", "seconds": 5, "prompt": "p"}
    ledger = ss.CostLedger(1.5)
    res = ss.generate_shot(shot, {}, str(tmp_path), ledger, gens, sleep=lambda s: None)
    assert calls == ["seedance_fast", "veo_lite"] and res["engine"] == "veo_lite"
    assert res["cost_usd"] == pytest.approx(0.30)  # Veo renders 6s for a 5s shot

    calls.clear()
    hook = {"index": 1, "role": "hook", "seconds": 4, "prompt": "p"}
    with pytest.raises(RuntimeError):
        ss.generate_shot(hook, {}, str(tmp_path), ss.CostLedger(1.5), gens, sleep=lambda s: None)
    assert calls == ["veo_fast"] * 3  # 1 try + 2 retries, never more


def test_kill_switch(monkeypatch):
    assert ss.enabled({})
    assert not ss.enabled({"autopilot_format": "single"})
    monkeypatch.setenv("STITCHED_SHORTS", "0")
    assert not ss.enabled({})


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg missing")
def test_produce_short_end_to_end_with_fake_generators(tmp_path):
    def fake(color, audio):
        def f(settings, prompt, sec, dest, **kw):
            cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=720x1280:r=24:d={sec}"]
            if audio:
                cmd += ["-f", "lavfi", "-i", f"sine=f=440:d={sec}", "-shortest"]
            cmd += ["-c:v", "libx264", "-preset", "ultrafast", dest]
            subprocess.run(cmd, check=True, capture_output=True)
            return {"model": color, "billed_seconds": sec}
        return f

    gens = {"veo_fast": fake("red", True), "seedance_fast": fake("blue", False), "veo_lite": fake("green", True)}
    out, plan, info = ss.produce_short({"shorts_cost_cap_usd": 1.5}, str(tmp_path), llm=lambda s, p: _plan(), generators=gens, sleep=lambda s: None)
    assert os.path.exists(out)
    assert (info["width"], info["height"]) == (1080, 1920)
    assert 20 <= info["duration"] <= 30.5
    assert info["has_audio"]
    assert info["cost_usd"] <= 1.50
    assert len(info["shot_list"]) == 4
    assert info["shot_list"][0]["engine"] == "veo_fast"
    assert all(s["engine"] == "seedance_fast" for s in info["shot_list"][1:])
    assert set(info["platform_captions"]) == {"youtube", "tiktok", "instagram", "linkedin", "twitter"}


def test_growth_v2_staggered_posts_per_platform():
    os.environ.setdefault("DISABLE_BACKGROUND_SCHEDULER", "true")
    import growth_v2 as g
    caps = ss.build_platform_captions(ss.sanitize_plan(_plan()))
    info = {"format": "stitched_short", "platform_captions": caps}
    post = {"id": "abc", "platform": "youtube,tiktok,instagram,linkedin", "text": "x", "status": "PUBLISHING"}
    out = g.build_staggered_posts(post, info, ["linkedin", "tiktok", "youtube", "instagram"], datetime(2026, 10, 16, 9))
    assert [p["platform"] for p in out] == ["youtube", "tiktok", "instagram", "linkedin"]
    assert out[0]["id"] == "abc" and out[0]["status"] == "PUBLISHING"
    assert all(p["status"] == "PENDING" for p in out[1:])
    assert len({p["scheduled_time"] for p in out}) == 4
    assert len({p["text"] for p in out}) == 4
    assert out[0]["youtube_title"] == caps["youtube"]["title"]
    assert all(p["stagger_group"] == "abc" for p in out)


def test_growth_v2_produce_routes_to_stitched(monkeypatch):
    import asyncio
    import growth_v2 as g
    monkeypatch.setattr(g, "produce_stitched", lambda settings, state: ("/tmp/x.mp4", {"topic": "t"}, {"format": "stitched_short"}))
    out = asyncio.run(g.produce({}, {}))
    assert out[2]["format"] == "stitched_short"


def test_hashtags_drop_tools_we_did_not_use():
    tags = ss.clean_hashtags(["#SoraAI", "#AIFilmmaking", "#Kling", "#Veo3"])
    assert "#SoraAI" not in tags and "#Kling" not in tags and "#Veo3" in tags


def test_tool_tags_follow_engines_actually_used():
    plan = ss.sanitize_plan(_plan())
    caps = ss.build_platform_captions(plan, {"veo_fast", "veo_lite"})
    assert all("#Seedance" not in c["caption"] for c in caps.values())
    caps = ss.build_platform_captions(plan, {"veo_fast", "seedance_fast"})
    assert any("#Seedance" in c["caption"] for c in caps.values())


def test_test_mode_zero_retries_no_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("SHORTS_TEST_MODE", "1")
    calls = []

    def fail(s, p, sec, d, **kw):
        calls.append(1)
        raise RuntimeError("x")
    gens = {"seedance_fast": fail, "veo_lite": fail, "veo_fast": fail}
    with pytest.raises(RuntimeError):
        ss.generate_shot({"index": 2, "role": "broll", "seconds": 5, "prompt": "p"}, {}, str(tmp_path), ss.CostLedger(0.75), gens, sleep=lambda s: None)
    assert len(calls) == 1


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg missing")
def test_forced_durations_are_exact(tmp_path, monkeypatch):
    monkeypatch.setenv("SHORTS_TEST_MODE", "1")
    seen = []

    def fake(settings, prompt, sec, dest, **kw):
        seen.append(sec)
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=red:s=720x1280:r=24:d={sec}", "-c:v", "libx264", "-preset", "ultrafast", dest], check=True, capture_output=True)
        return {"model": "f", "billed_seconds": sec}
    gens = {"veo_fast": fake, "seedance_fast": fake, "veo_lite": fake}
    out, plan, info = ss.produce_short({"shorts_forced_durations": [4, 5, 5, 5]}, str(tmp_path), llm=lambda s, p: _plan(), generators=gens, sleep=lambda s: None)
    assert seen == [4, 5, 5, 5]
    assert info["cost_usd"] <= 0.75


def test_copy_audit_fixes_wrong_model_and_fake_techniques():
    plan = ss.sanitize_plan(_plan(on_screen_hook="1 robot, 4 shots"))
    shots = [dict(s, engine=("veo_fast" if s["role"] == "hook" else "seedance_fast"), chained_from_previous_frame=s["role"] == "broll") for s in plan["shots"]]
    shots[2]["caption"] = "Simulating underwater lighting in Veo."
    caps = {"youtube": {"title": "1 robot, made with Sora", "caption": "1 robot, made with Sora\nWe used consistent prompt seeds in Veo. Same robot.\n\n#AIFilmmaking #Veo3 #Seedance", "hashtags": ["#AIFilmmaking", "#Veo3", "#Seedance"]},
            "tiktok": {"caption": "Kept the drone identical across 4 shots. Cut 30% of renders.\n\n#AIFilmmaking #Veo3 #Seedance", "hashtags": ["#AIFilmmaking", "#Veo3", "#Seedance"]}}
    log = ss.audit_and_fix_copy(plan, shots, caps)
    assert log["clean"]
    assert shots[2]["caption"] == "Shot 3: Seedance 1 Pro Fast"
    assert "seeds" not in caps["youtube"]["caption"] and "Sora" not in caps["youtube"]["caption"]
    assert "identical" not in caps["tiktok"]["caption"] and "30%" not in caps["tiktok"]["caption"]
    assert "Seedance 1 Pro Fast" in caps["tiktok"]["caption"]  # factual credit derived from shot records
    assert "#Seedance" in caps["tiktok"]["caption"]
    assert ss.copy_problems("Locked style line in every prompt.", {"veo_fast"}) == []


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg missing")
def test_broll_chains_from_previous_last_frame(tmp_path, monkeypatch):
    monkeypatch.setenv("SHORTS_TEST_MODE", "1")
    images = []

    def fake(settings, prompt, sec, dest, **kw):
        images.append(kw.get("image"))
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc2=s=720x1280:r=24:d={sec}", "-c:v", "libx264", "-preset", "ultrafast", dest], check=True, capture_output=True)
        return {"model": "f", "billed_seconds": sec}
    gens = {"veo_fast": fake, "seedance_fast": fake, "veo_lite": fake}
    out, plan, info = ss.produce_short({"shorts_forced_durations": [4, 5, 5, 5]}, str(tmp_path), llm=lambda s, p: _plan(), generators=gens, sleep=lambda s: None)
    assert images[0] is None and all(images[1:])
    assert all(s["chained_from_previous_frame"] for s in info["shot_list"][1:])
    assert info["duration"] >= 20.0  # 19s generated + 1s held loop frame
    assert abs(info["integrated_lufs"] - (-14.0)) <= 1.0
    assert info["cost_usd"] <= 0.75


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg missing")
def test_baked_in_letterbox_is_removed(tmp_path):
    src, dst = str(tmp_path / "b.mp4"), str(tmp_path / "n.mp4")
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=s=720x1280:r=24:d=2", "-vf",
                    "drawbox=0:0:720:200:black:t=fill,drawbox=0:1080:720:200:black:t=fill", "-c:v", "libx264", "-preset", "ultrafast", src],
                   check=True, capture_output=True)
    assert ss.detect_letterbox(src) is not None
    ss.normalize_shot(src, dst, 2, False)
    assert ss.detect_letterbox(dst) is None
    assert (ss.probe(dst)["width"], ss.probe(dst)["height"]) == (1080, 1920)


def _fake_video(sec, dest, src="testsrc2"):
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"{src}=s=720x1280:r=24:d={sec}", "-c:v", "libx264", "-preset", "ultrafast", dest], check=True, capture_output=True)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg missing")
def test_plan_saved_immediately_and_resume_reuses_everything(tmp_path, monkeypatch):
    import json as _json
    monkeypatch.setenv("SHORTS_TEST_MODE", "1")
    llm_calls, gen_calls = [], []

    def llm(s, p):
        llm_calls.append(1)
        return _plan()

    def gen_fail_on_3(settings, prompt, sec, dest, **kw):
        gen_calls.append(("first", sec))
        if len(gen_calls) == 3:
            raise RuntimeError("provider down")
        if kw.get("on_submit"):
            kw["on_submit"](f"req-{len(gen_calls)}")
        _fake_video(sec, dest)
        return {"model": "m", "billed_seconds": sec, "request_id": f"req-{len(gen_calls)}"}
    gens = {"veo_fast": gen_fail_on_3, "seedance_fast": gen_fail_on_3, "veo_lite": gen_fail_on_3}
    st = {"shorts_forced_durations": [4, 5, 5, 5]}
    runs = str(tmp_path / "runs")
    with pytest.raises(RuntimeError):
        ss.produce_short(st, str(tmp_path), llm=llm, generators=gens, sleep=lambda s: None, run_id="r1", runs_dir=runs)
    saved = _json.load(open(tmp_path / "runs" / "r1" / "run.json"))
    assert saved["plan"]["on_screen_hook"] and len(saved["plan"]["shots"]) == 4
    assert set(saved["plan"]["draft_platform_captions"]) == {"youtube", "tiktok", "instagram", "linkedin", "twitter"}
    assert set(saved["shots"]) == {"1", "2"} and saved["shots"]["1"]["request_id"] == "req-1"
    assert saved["status"] == "stopped"

    resumed = []

    def gen_ok(settings, prompt, sec, dest, **kw):
        resumed.append(sec)
        _fake_video(sec, dest)
        return {"model": "m", "billed_seconds": sec, "request_id": f"new-{len(resumed)}"}
    gens2 = {"veo_fast": gen_ok, "seedance_fast": gen_ok, "veo_lite": gen_ok}
    out, plan, info = ss.produce_short(st, str(tmp_path), llm=llm, generators=gens2, sleep=lambda s: None, run_id="r1", runs_dir=runs)
    assert len(llm_calls) == 1          # no second planning call
    assert resumed == [5, 5]            # only shots 3 and 4 generated
    final = _json.load(open(tmp_path / "runs" / "r1" / "run.json"))
    assert final["status"] == "done" and final["resumed"] == 1 and os.path.exists(out)
    assert info["cost_usd"] <= 0.75


def test_timed_out_request_is_fetched_by_id_not_resubmitted(tmp_path, monkeypatch):
    monkeypatch.setenv("SHORTS_TEST_MODE", "1")
    state = ss.RunState(str(tmp_path), "r2")
    calls = []

    def timeout_gen(settings, prompt, sec, dest, **kw):
        kw["on_submit"]("fal-123")
        raise RuntimeError("Seedance timed out after 1500s")
    shot = {"index": 2, "role": "broll", "seconds": 5, "prompt": "p"}
    ledger = ss.CostLedger(0.75)
    with pytest.raises(RuntimeError, match="rerun with run_id=r2"):
        ss.generate_shot(shot, {}, state.dir, ledger, {"seedance_fast": timeout_gen}, sleep=lambda s: None, state=state)
    assert state.pending(2)["request_id"] == "fal-123"
    assert ledger.total == pytest.approx(ss.seedance_cost(5))  # counted as spent

    def fetch(settings, prompt, sec, dest, **kw):
        calls.append(kw.get("resume_id"))
        open(dest, "wb").write(b"x")
        return {"model": "m", "billed_seconds": sec, "request_id": kw.get("resume_id")}
    state2 = ss.RunState(str(tmp_path), "r2")
    ledger2 = ss.CostLedger(0.75)
    ledger2.items = [dict(i) for i in state2.data["cost_items"]]
    rec = ss.generate_shot(shot, {}, state2.dir, ledger2, {"seedance_fast": fetch}, sleep=lambda s: None, state=state2)
    assert calls == ["fal-123"] and rec["resumed_from_request"]
    assert ledger2.total == pytest.approx(ss.seedance_cost(5))  # not double counted
    assert state2.done_shot(2) and not state2.pending(2)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg missing")
def test_long_plan_is_shrunk_to_budget_not_failed(tmp_path):
    def fake(settings, prompt, sec, dest, **kw):
        _fake_video(sec, dest)
        return {"model": "m", "billed_seconds": sec}
    long_plan = _plan(shots=[dict(s, seconds=8) for s in _plan()["shots"]])
    gens = {"veo_fast": fake, "seedance_fast": fake, "veo_lite": fake}
    out, plan, info = ss.produce_short({}, str(tmp_path), llm=lambda s, p: long_plan, generators=gens, sleep=lambda s: None)
    assert [s["seconds"] for s in plan["shots"]] == [4, 5, 5, 5]
    assert info["cost_usd"] <= 0.75 and info["duration"] >= 20


def test_viral_research_ranks_short_refs_and_feeds_prompt():
    import viral_research as vr
    fake = lambda q: [{"title": f"AI {q} #AIvideo", "views": len(q) * 100000, "seconds": 30, "url": "https://youtube.com/shorts/u" + q, "channel": "c", "query": q},
                      {"title": "long film", "views": 10**9, "seconds": 900, "url": "https://youtube.com/watch/L" + q, "channel": "c", "query": q}]
    res = vr.research(queries=("a", "bbb"), search=fake)
    assert [r["url"] for r in res["refs"]] == ["https://youtube.com/shorts/ubbb", "https://youtube.com/shorts/ua"] and res["cost_usd"] == 0
    assert "#aivideo" in res["hashtags"] and "#shorts" not in res["hashtags"]
    seen = {}
    def llm(settings, prompt):
        seen["p"] = prompt
        return dict(_plan(), viral_angle="number hook", viral_refs_used=["bbb #AIvideo"], pinned_comment="Part 2?")
    plan = ss.plan_short({}, llm=llm, viral=res)
    assert "CURRENTLY VIRAL" in seen["p"] and "AI bbb #AIvideo" in seen["p"]
    assert plan["viral_angle"] == "number hook" and plan["pinned_comment"] == "Part 2?" and plan["viral_refs"]


def test_research_never_raises():
    import viral_research as vr
    def boom(q):
        raise RuntimeError("offline")
    res = vr.research(queries=("x",), search=boom)
    assert res["refs"] == [] and res["errors"]
    assert "No live viral references" in vr.prompt_block(res)


def test_twitter_caption_fits_280():
    caps = ss.build_platform_captions(dict(_plan(), platforms={"twitter": {"caption": "x " * 300, "hashtags": ["#AIvideo"]}}), {"veo_fast"})
    assert len(caps["twitter"]["caption"]) <= 280 and len(caps["twitter"]["hashtags"]) <= 1


def test_hashtag_counts_mix_and_tool_cap():
    caps = ss.build_platform_captions(dict(ss.sanitize_plan(_plan()), viral_hashtags=["#aishortfilm", "#mango", "#shortsfeed"]),
                                      {"veo_fast", "seedance_fast"})
    n = {k: len(v["hashtags"]) for k, v in caps.items()}
    assert 6 <= n["tiktok"] <= 8 and n["instagram"] == 5 and n["twitter"] <= 1 and 3 <= n["linkedin"] <= 5
    assert "#Shorts" in caps["youtube"]["hashtags"] and 4 <= n["youtube"] <= 6
    for k, v in caps.items():
        low = [t.lower() for t in v["hashtags"]]
        assert sum(t in ("#veo3", "#seedance") for t in low) <= 2, k
        assert "#mango" not in low and "#shortsfeed" not in low
    assert "#aishortfilm" in [t.lower() for t in caps["tiktok"]["hashtags"]]
    assert len(caps["twitter"]["caption"]) <= 280


def test_viral_filter_drops_offtopic_cartoon():
    import viral_research as vr
    t = "Dadaji Ka Mango Tree #shorts #shortsfeed #shortvideo #short #shortsvideo #mango #ai #dadaji #cartoon"
    assert not vr.NICHE_RE.search(t)
    assert vr.NICHE_RE.search("POOF | AI Short Film") and vr.NICHE_RE.search("THE TITAN AWAKENS | Seedance 2.5 AI Short")


def test_broll_retries_seedance_when_veo_lite_would_break_cap(tmp_path):
    # Live 2026-10-18 00:03 UTC: Seedance shot 3 was moderated, the Veo Lite fallback ($0.30) did not fit
    # under the $0.75 cap after $0.518 spent, and the whole Short failed. A Seedance retry ($0.108) fits.
    calls = []

    def seedance(s, p, sec, d, **kw):
        calls.append("seedance_fast")
        if len(calls) == 1:
            raise RuntimeError("Seedance result HTTP 422: content_policy_violation")
        open(d, "wb").write(b"x")
        return {"model": "seedance", "billed_seconds": sec}

    def veo_lite(s, p, sec, d, **kw):
        calls.append("veo_lite")
        open(d, "wb").write(b"x")
        return {"model": "veo_lite", "billed_seconds": sec}

    gens = {"seedance_fast": seedance, "veo_lite": veo_lite}
    ledger = ss.CostLedger(0.75)
    ledger.charge("spent", 0.518)
    shot = {"index": 3, "role": "broll", "seconds": 5, "prompt": "p"}
    res = ss.generate_shot(shot, {}, str(tmp_path), ledger, gens, sleep=lambda s: None)
    assert calls == ["seedance_fast", "seedance_fast"] and res["engine"] == "seedance_fast"
    assert ledger.total <= 0.75


def test_broll_still_raises_cap_when_nothing_fits(tmp_path):
    def failing(s, p, sec, d, **kw):
        raise RuntimeError("boom")

    gens = {"seedance_fast": failing, "veo_lite": failing}
    ledger = ss.CostLedger(0.75)
    ledger.charge("spent", 0.70)
    shot = {"index": 3, "role": "broll", "seconds": 5, "prompt": "p"}
    with pytest.raises(ss.CostCapExceeded):
        ss.generate_shot(shot, {}, str(tmp_path), ledger, gens, sleep=lambda s: None)


# --------------------------------------------------------------------------
# slot retry reuses paid shots (2026-10-18, approved by Bret)
# --------------------------------------------------------------------------

class _Gens:
    """Fake generators ($0): record every call, optionally fail some shots."""

    def __init__(self, fail=None):
        self.calls = []          # (engine, seconds, prompt, image)
        self.fail = fail or (lambda engine, prompt, n: None)

    def make(self, engine):
        def gen(settings, prompt, sec, dest, **kw):
            n = sum(1 for c in self.calls if c[2] == prompt)
            self.calls.append((engine, sec, prompt, kw.get("image")))
            err = self.fail(engine, prompt, n)
            if err:
                raise RuntimeError(err)
            if kw.get("on_submit"):
                kw["on_submit"](f"{engine}-{len(self.calls)}")
            _fake_video(sec, dest)
            return {"model": engine, "billed_seconds": sec, "request_id": f"{engine}-{len(self.calls)}"}
        return gen

    def table(self):
        return {e: self.make(e) for e in ("veo_fast", "seedance_fast", "veo_lite")}


SLOT_SETTINGS = {"shorts_forced_durations": [4, 5, 5, 5]}
SHOT3 = "Close-up of robot eye lens"


def _moderated_shot3(engine, prompt, n):
    return "Seedance result HTTP 422: content_policy_violation" if SHOT3 in prompt else None


def _fail_slot_attempt_1(tmp_path, slot="2026-10-18-0", settings=None):
    """Live 2026-10-17 5 PM PT: shots 1-2 paid ($0.518 with planning), Seedance shot 3 moderated, Short failed."""
    llm_calls = []

    def llm(s, p):
        llm_calls.append(1)
        return _plan()
    g1 = _Gens(fail=_moderated_shot3)
    with pytest.raises(RuntimeError, match="shot 3 failed"):
        ss.produce_short(dict(settings or SLOT_SETTINGS), str(tmp_path), llm=llm, generators=g1.table(),
                         sleep=lambda s: None, runs_dir=str(tmp_path / "runs"), slot=slot)
    return llm, llm_calls, g1


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg missing")
def test_slot_retry_reuses_paid_shots_and_generates_only_missing(tmp_path):
    llm, llm_calls, g1 = _fail_slot_attempt_1(tmp_path)
    spent_1 = round(ss.LLM_CALL_USD + ss.shot_cost("veo_fast", 4) + ss.seedance_cost(5), 4)  # 0.518
    assert (tmp_path / "runs" / "slot-2026-10-18-0" / "run.json").exists()
    assert [c[0] for c in g1.calls] == ["veo_fast", "seedance_fast", "seedance_fast", "seedance_fast", "seedance_fast"]

    g2 = _Gens()  # slot attempt 2: everything works
    out, plan, info = ss.produce_short(dict(SLOT_SETTINGS), str(tmp_path), llm=llm, generators=g2.table(),
                                       sleep=lambda s: None, runs_dir=str(tmp_path / "runs"), slot="2026-10-18-0")
    assert len(llm_calls) == 1                                   # no second planning call
    assert [(c[0], c[1]) for c in g2.calls] == [("seedance_fast", 5), ("seedance_fast", 5)]  # only shots 3 + 4
    assert SHOT3 in g2.calls[0][2]
    assert g2.calls[0][3] and os.path.exists(g2.calls[0][3])   # shot 3 chains from the reused shot 2's last frame
    assert info["reused_shots"] == [1, 2] and info["slot_retry_reused"] is True
    # reused shots count toward this slot's spend: attempt 1 ($0.518) + 2 new Seedance shots
    assert info["cost_usd"] == pytest.approx(spent_1 + 2 * ss.seedance_cost(5), abs=1e-4)
    assert info["cost_usd"] <= 0.75
    labels = [i["label"] for i in info["cost"]["items"] if i["usd"] > 0]
    assert labels.count("shot1:veo_fast") == 1 and labels.count("plan_llm") == 1
    assert [s["cost_usd"] for s in info["shot_list"]][:2] == [ss.shot_cost("veo_fast", 4), ss.seedance_cost(5)]
    assert os.path.exists(out) and info["duration"] >= 19.5


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg missing")
def test_slot_retry_cap_counts_combined_spend(tmp_path):
    """Attempt 2's ledger starts at attempt 1's spend: Veo Lite ($0.30) never fits, Seedance retries do."""
    llm, _, _ = _fail_slot_attempt_1(tmp_path)
    first_try = {"n": 0}

    def flaky_shot3_once(engine, prompt, n):
        if SHOT3 in prompt and first_try["n"] == 0:
            first_try["n"] += 1
            return "Seedance result HTTP 422: content_policy_violation"
    g2 = _Gens(fail=flaky_shot3_once)
    out, plan, info = ss.produce_short(dict(SLOT_SETTINGS), str(tmp_path), llm=llm, generators=g2.table(),
                                       sleep=lambda s: None, runs_dir=str(tmp_path / "runs"), slot="2026-10-18-0")
    assert "veo_lite" not in [c[0] for c in g2.calls]           # 0.518 + 0.30 would break the cap
    assert [c[0] for c in g2.calls] == ["seedance_fast"] * 3
    assert info["cost_usd"] <= 0.75 + 1e-9                      # whole slot, both attempts, under the cap


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg missing")
def test_slot_retry_that_cannot_fit_cap_starts_fresh(tmp_path):
    """Corrupt cached shot 2 -> regenerating it plus 3+4 would push the slot over $0.75, so the saved run
    is dropped and attempt 2 is a fresh Short under its own cap (the pre-change behavior)."""
    llm, llm_calls, _ = _fail_slot_attempt_1(tmp_path)
    run_dir = tmp_path / "runs" / "slot-2026-10-18-0"
    shot2 = next(run_dir.glob("shot2_*.mp4"))
    shot2.write_bytes(shot2.read_bytes()[:20_000])               # truncated / corrupt
    assert not ss.shot_file_ok(str(shot2))
    g2 = _Gens()
    out, plan, info = ss.produce_short(dict(SLOT_SETTINGS), str(tmp_path), llm=llm, generators=g2.table(),
                                       sleep=lambda s: None, runs_dir=str(tmp_path / "runs"), slot="2026-10-18-0")
    assert len(llm_calls) == 2 and len(g2.calls) == 4 and info["reused_shots"] == []
    assert info["slot_retry_reused"] is False and info["cost_usd"] <= 0.75


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg missing")
def test_slot_retry_regenerates_corrupt_cached_shot_and_keeps_its_cost(tmp_path):
    st = dict(SLOT_SETTINGS, shorts_cost_cap_usd=1.5)
    llm, llm_calls, _ = _fail_slot_attempt_1(tmp_path, settings=st)
    run_dir = tmp_path / "runs" / "slot-2026-10-18-0"
    next(run_dir.glob("shot2_*.mp4")).write_bytes(b"\x00" * 50_000)   # big enough, but not a video
    g2 = _Gens()
    out, plan, info = ss.produce_short(dict(st), str(tmp_path), llm=llm, generators=g2.table(),
                                       sleep=lambda s: None, runs_dir=str(tmp_path / "runs"), slot="2026-10-18-0")
    assert len(llm_calls) == 1 and info["reused_shots"] == [1] and len(g2.calls) == 3
    # the corrupt shot 2 was still paid for in attempt 1 -> stays in the slot total
    expected = ss.LLM_CALL_USD + ss.shot_cost("veo_fast", 4) + 4 * ss.seedance_cost(5)
    assert info["cost_usd"] == pytest.approx(expected, abs=1e-4)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg missing")
def test_successful_first_try_is_unchanged_with_slot(tmp_path):
    runs = []
    for slot in (None, "2026-10-18-15"):
        g = _Gens()
        sub = tmp_path / (slot or "manual")
        out, plan, info = ss.produce_short(dict(SLOT_SETTINGS), str(sub), llm=lambda s, p: _plan(), generators=g.table(),
                                           sleep=lambda s: None, runs_dir=str(sub / "runs"), slot=slot)
        runs.append((g.calls, info, plan))
    (calls_a, info_a, plan_a), (calls_b, info_b, plan_b) = runs
    assert [(c[0], c[1], c[2]) for c in calls_a] == [(c[0], c[1], c[2]) for c in calls_b]
    assert info_a["cost_usd"] == info_b["cost_usd"] <= 0.75
    strip = lambda sl: [{k: v for k, v in s.items() if k != "request_id"} for s in sl]
    assert strip(info_a["shot_list"]) == strip(info_b["shot_list"])
    assert info_a["platform_captions"] == info_b["platform_captions"]
    assert info_b["reused_shots"] == [] and info_b["slot_retry_reused"] is False
    assert info_b["run_id"] == "slot-2026-10-18-15" and not info_a["run_id"].startswith("slot-")


def test_slot_cache_cleanup_keeps_current_and_manual_runs(tmp_path):
    for name in ("slot-2026-10-17-15", "slot-2026-10-18-0", "20261017-120000-abcdef"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "run.json").write_text("{}")
    removed = ss.clear_slot_cache(str(tmp_path), keep_run_id="slot-2026-10-18-0")
    assert removed == ["slot-2026-10-17-15"]
    assert sorted(os.listdir(tmp_path)) == ["20261017-120000-abcdef", "slot-2026-10-18-0"]
    assert ss.clear_slot_cache(str(tmp_path / "missing")) == []
    assert ss.slot_run_id("2026-10-18-0") == "slot-2026-10-18-0" and ss.slot_run_id("../x y") == "slot-x_y"


def test_growth_v2_slot_passes_slot_and_cleans_cache_after_post(tmp_path, monkeypatch):
    import asyncio
    import growth_v2 as g
    seen = {}

    async def fake_autopilot(settings, dry_run=False, slot=None):
        seen["slot"] = slot
        return {"ok": True, "post_id": "p1"}
    (tmp_path / "slot-2026-10-18-15").mkdir()
    (tmp_path / "keep-manual").mkdir()
    monkeypatch.setattr(g, "run_autopilot", fake_autopilot)
    monkeypatch.setattr(g, "load_state", lambda: {})
    monkeypatch.setattr(g, "save_state", lambda st: None)
    monkeypatch.setattr(g, "mark_slot_success", lambda slot: None)
    monkeypatch.setattr(g, "stitched_runs_dir", lambda: str(tmp_path))
    assert asyncio.run(g.run_slot("2026-10-18-15", {})) is True
    assert seen["slot"] == "2026-10-18-15"
    assert os.listdir(tmp_path) == ["keep-manual"]


def test_growth_v2_failed_slot_keeps_cache_for_retry(tmp_path, monkeypatch):
    import asyncio
    import growth_v2 as g

    async def boom(settings, dry_run=False, slot=None):
        raise RuntimeError("shot 3 failed after 3 attempt(s)")
    (tmp_path / "slot-2026-10-18-15").mkdir()
    monkeypatch.setattr(g, "run_autopilot", boom)
    monkeypatch.setattr(g, "load_state", lambda: {})
    monkeypatch.setattr(g, "save_state", lambda st: None)
    monkeypatch.setattr(g, "add_alert", lambda *a, **k: None)
    monkeypatch.setattr(g, "stitched_runs_dir", lambda: str(tmp_path))
    assert asyncio.run(g.run_slot("2026-10-18-15", {})) is False
    assert os.listdir(tmp_path) == ["slot-2026-10-18-15"]


def test_growth_v2_produce_passes_slot_only_for_scheduled_slots(monkeypatch):
    import asyncio
    import growth_v2 as g
    got = []
    monkeypatch.setattr(g, "produce_stitched", lambda settings, state, slot=None: got.append(slot) or ("/tmp/x.mp4", {}, {}))
    monkeypatch.setattr(g.stitched_shorts, "enabled", lambda s=None: True)
    asyncio.run(g.produce({}, {}, slot="2026-10-18-15"))
    asyncio.run(g.produce({}, {}))                               # manual / dry run: no slot cache
    assert got == ["2026-10-18-15", None]
