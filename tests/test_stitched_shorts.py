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
    assert set(caps) == {"youtube", "tiktok", "instagram", "linkedin"}
    texts = [c["caption"] for c in caps.values()]
    assert len(set(texts)) == 4
    for name, c in caps.items():
        assert 3 <= len(c["hashtags"]) <= 5, name
        assert "#fyp" not in c["caption"].lower() and "#viral" not in c["caption"].lower()
        assert "http" not in c["caption"]
    assert len(caps["linkedin"]["hashtags"]) == 3
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
    assert set(info["platform_captions"]) == {"youtube", "tiktok", "instagram", "linkedin"}


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
