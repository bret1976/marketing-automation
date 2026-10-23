"""Gemini Omni 1.1 Flash as the primary 8 s video engine (Veo 3.1 Preview retired 2026-10-22)."""
import asyncio
import base64
import os
import types

import pytest
import requests

os.environ.setdefault("DISABLE_BACKGROUND_SCHEDULER", "true")
import growth_v2 as g
import orchestrator as o


def test_cost_table_matches_google_pricing():
    # $17.50 / 1M video tokens, 5,792 tokens per second at 720p
    assert o.omni_expected_cost(8) == 0.8109
    assert g.single_clip_cost("gemini_omni") == 0.8109
    assert abs(g.OMNI_USD_PER_SECOND - 0.10136) < 1e-9
    # hard token cap: one clip can never cost more than $0.85, even if all output were billed as video
    assert o.omni_max_cost() <= g.OMNI_MAX_CLIP_USD <= 0.85
    assert o.OMNI_MAX_OUTPUT_TOKENS >= 8 * o.OMNI_VIDEO_TOKENS_PER_SECOND
    assert g.single_cost_cap({}) == 0.85
    # Veo numbers stay for history / overrides
    assert g.single_clip_cost("google_veo_lite") == 0.40


def test_cap_never_above_085_and_can_be_lowered(monkeypatch):
    monkeypatch.setenv("GROWTH_COST_CAP_USD", "3")
    assert g.single_cost_cap({}) == 0.85
    monkeypatch.setenv("GROWTH_COST_CAP_USD", "0.5")
    assert g.single_cost_cap({}) == 0.5


def test_request_body_is_vertical_720p_8s_token_capped():
    body = o.omni_request_body("A glass drop shatters.", "9:16")
    assert body["model"] == "gemini-omni-1.1-flash"
    assert body["response_format"] == {"type": "video", "aspect_ratio": "9:16", "resolution": "720p"}
    assert body["generation_config"]["max_output_tokens"] == o.OMNI_MAX_OUTPUT_TOKENS
    assert body["generation_config"]["thinking_level"] == "low"  # Omni accepts only low|high
    assert "exactly 8 seconds" in body["input"] and body["input"].endswith("A glass drop shatters.")
    assert body["store"] is False and body["background"] is False


def test_cost_from_usage():
    usage = {"total_input_tokens": 200, "total_output_tokens": 46336, "total_thought_tokens": 100,
             "output_tokens_by_modality": [{"modality": "video", "tokens": 46336}]}
    assert o.omni_cost_from_usage(usage) == round(46336 * 17.5e-6 + 100 * 9e-6 + 200 * 1.5e-6, 4)
    # no modality split -> conservative: all output at the video rate
    assert o.omni_cost_from_usage({"total_output_tokens": 46336}) == 0.8109
    assert o.omni_cost_from_usage({}) is None


class _Resp:
    def __init__(self, code, body=None, text="", content=b""):
        self.status_code, self._body, self.text, self.content = code, body, text, content

    def json(self):
        return self._body


def _status(monkeypatch):
    jobs = {}
    monkeypatch.setattr(o, "update_job_status", lambda jid, st, pr, msg, result=None: jobs.__setitem__(jid, {"status": st, "message": msg, "result": result}))
    return jobs


def test_run_omni_success_inline(monkeypatch, tmp_path):
    jobs = _status(monkeypatch)
    monkeypatch.setattr(o, "GENERATED_DIR", str(tmp_path))
    sent = {}

    def post(url, headers, json, timeout):
        sent.update(url=url, json=json, headers=headers)
        return _Resp(200, {"status": "completed", "usage": {"total_output_tokens": 46336, "output_tokens_by_modality": [{"modality": "video", "tokens": 46336}]},
                           "steps": [{"type": "model_output", "content": [{"type": "video", "mime_type": "video/mp4", "data": base64.b64encode(b"MP4").decode()}]}]})

    o.run_omni_generation("j1", "p", {"gemini_api_key": "k"}, "9:16", post=post)
    assert jobs["j1"]["status"] == "SUCCESS" and jobs["j1"]["result"]["cost_usd"] == 0.8109
    assert (tmp_path / "j1.mp4").read_bytes() == b"MP4"
    assert sent["url"].endswith("/v1beta/interactions") and "key=" not in sent["url"]


def test_run_omni_uri_delivery(monkeypatch, tmp_path):
    jobs = _status(monkeypatch)
    monkeypatch.setattr(o, "GENERATED_DIR", str(tmp_path))
    post = lambda *a, **k: _Resp(200, {"status": "completed", "steps": [{"content": [{"type": "video", "uri": "https://x/f"}]}]})
    o.run_omni_generation("j2", "p", {"gemini_api_key": "k"}, post=post, get=lambda *a, **k: _Resp(200, content=b"V"))
    assert jobs["j2"]["status"] == "SUCCESS" and (tmp_path / "j2.mp4").read_bytes() == b"V"


def test_run_omni_http_error_and_timeout_messages(monkeypatch):
    jobs = _status(monkeypatch)
    o.run_omni_generation("e1", "p", {"gemini_api_key": "k"}, post=lambda *a, **k: _Resp(400, text="bad request"))
    assert jobs["e1"]["status"] == "FAILED" and "400" in jobs["e1"]["message"]
    assert not g.veo_failure_billed(jobs["e1"]["message"])

    def boom(*a, **k):
        raise requests.exceptions.Timeout()
    o.run_omni_generation("e2", "p", {"gemini_api_key": "k"}, post=boom)
    assert "timed out" in jobs["e2"]["message"] and g.veo_failure_billed(jobs["e2"]["message"])
    o.run_omni_generation("e3", "p", {"gemini_api_key": "k"}, post=lambda *a, **k: _Resp(200, {"status": "completed", "steps": []}))
    assert g.veo_failure_billed(jobs["e3"]["message"])  # returned but no video: may still be billed


def test_run_video_generation_dispatches_omni(monkeypatch):
    calls = []
    monkeypatch.setattr(o, "run_omni_generation", lambda *a: calls.append(a))
    o.run_video_generation("d1", "p", {}, "gemini_omni", 5, allow_fallback=False, aspect_ratio="9:16")
    assert calls and calls[0][3] == "9:16" and calls[0][4] == 8


def _omni_env(monkeypatch, tmp_path, text_usd=0.004):
    for k in ("AUTOPILOT_FORMAT", "STITCHED_SHORTS", "SHORTS_COST_CAP_USD", "AUTOPILOT_TRANSFORM_FALLBACK", "GROWTH_VIDEO_ENGINES", "GROWTH_COST_CAP_USD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GROWTH_RETRY_BACKOFF", "0")
    usage = {"usd": 0.0}
    fake = types.SimpleNamespace(GENERATED_DIR=str(tmp_path), text_usage_reset=lambda: usage.__setitem__("usd", 0.0),
                                 text_usage_usd=lambda: usage["usd"])
    fake.spend = lambda: usage.__setitem__("usd", usage["usd"] + text_usd)
    monkeypatch.setattr(g, "_main", lambda: fake)
    monkeypatch.setattr(g, "ensure_opening_sound", lambda raw: (raw, {"sfx_added": False}))
    monkeypatch.setattr(g, "render_vertical", lambda *a, **k: {"width": 1080, "height": 1920, "duration": 8.0})
    monkeypatch.setattr(g, "build_transformed_clip", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no repost")))
    monkeypatch.setattr(g, "produce_stitched", lambda *a, **k: (_ for _ in ()).throw(AssertionError("stitched path must be off")))
    return fake


def _concept(topic="drop"):
    return {"topic": topic, "topic_key": topic, "veo_prompt": "p", "on_screen_hook": "3,000 mph glass crack",
            "hook": "Snapping its tail destroys this glass drop", "lines": ["The bulb survives a hammer."]}


def test_default_slot_is_one_omni_clip_under_085(monkeypatch, tmp_path):
    fake = _omni_env(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(g, "plan_concept", lambda s, st, extra_avoid=None: fake.spend() or _concept())
    monkeypatch.setattr(g, "generate_original_clip", lambda c, s, e: calls.append(e) or ("/tmp/raw.mp4", {"engine": e, "actual_cost_usd": 0.8118}))
    monkeypatch.setattr(g, "ensure_caption_matches", lambda raw, c, s: fake.spend() or (c, {"caption_check": "match"}))
    _, _, info = asyncio.run(g.produce({}, {}))
    assert calls == ["gemini_omni"]
    assert info["cost"]["cap_usd"] == 0.85
    assert info["cost_usd"] == round(0.004 + 0.8118 + 0.004, 4) <= 0.85


def test_unbilled_omni_failure_retries_on_omni(monkeypatch, tmp_path):
    fake = _omni_env(monkeypatch, tmp_path)
    calls = []

    def gen(c, s, e):
        calls.append(e)
        if len(calls) == 1:
            raise RuntimeError("gemini_omni: Video generation failed: 400 safety filter")
        return "/tmp/raw.mp4", {"engine": e}
    monkeypatch.setattr(g, "plan_concept", lambda s, st, extra_avoid=None: fake.spend() or _concept(f"t{len(extra_avoid or [])}"))
    monkeypatch.setattr(g, "generate_original_clip", gen)
    monkeypatch.setattr(g, "ensure_caption_matches", lambda raw, c, s: (c, {"caption_check": "match"}))
    _, concept, info = asyncio.run(g.produce({}, {}))
    assert calls == ["gemini_omni", "gemini_omni"] and concept["topic"] == "t1"
    assert info["cost_usd"] <= 0.85


def test_billed_omni_failure_never_buys_a_second_clip(monkeypatch, tmp_path):
    fake = _omni_env(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(g, "plan_concept", lambda s, st, extra_avoid=None: fake.spend() or _concept(f"t{len(extra_avoid or [])}"))
    monkeypatch.setattr(g, "generate_original_clip", lambda c, s, e: calls.append(e) or (_ for _ in ()).throw(RuntimeError("gemini_omni: request timed out after 600s")))
    with pytest.raises(RuntimeError, match="cost cap"):
        asyncio.run(g.produce({}, {}))
    assert calls == ["gemini_omni"]


def test_expensive_planning_blocks_the_paid_call(monkeypatch, tmp_path):
    fake = _omni_env(monkeypatch, tmp_path, text_usd=0.05)
    calls = []
    monkeypatch.setattr(g, "plan_concept", lambda s, st, extra_avoid=None: fake.spend() or _concept(f"t{len(extra_avoid or [])}"))
    monkeypatch.setattr(g, "generate_original_clip", lambda c, s, e: calls.append(e))
    with pytest.raises(RuntimeError, match="cost cap"):
        asyncio.run(g.produce({}, {}))
    assert calls == []


def test_text_call_cost_uses_real_tokens():
    import main
    meta = types.SimpleNamespace(prompt_token_count=2000, candidates_token_count=500, thoughts_token_count=500)
    assert main.text_call_cost(types.SimpleNamespace(usage_metadata=meta)) == round(2000 * 0.75e-6 + 1000 * 3.75e-6, 5)
    assert main.text_call_cost(types.SimpleNamespace(usage_metadata=None)) == main.TEXT_CALL_FALLBACK_USD
    main.text_usage_reset()
    main.note_text_usage(types.SimpleNamespace(usage_metadata=meta))
    assert main.text_usage_usd() > 0 and main.text_usage_calls() == 1
