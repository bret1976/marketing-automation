import os

import orchestrator


def test_normalize_veo_duration_stays_8():
    assert orchestrator.normalize_video_duration("google_veo_lite", 5) == 8
    assert orchestrator.normalize_video_duration("google_veo", 15) == 8
    assert orchestrator.normalize_video_duration("google_veo_fast", 4) == 8


def test_gemini_veo_config_omits_duration_seconds():
    kwargs = orchestrator.gemini_veo_config_kwargs()
    assert "duration_seconds" not in kwargs
    assert kwargs == {"aspect_ratio": "16:9", "number_of_videos": 1}


def test_prompt_gets_8_second_cinematic_hint():
    prompt = orchestrator.veo_prompt_with_duration_policy("cinematic drone over neon streets", 8)
    assert prompt.startswith("8-second cinematic clip")
    assert "cinematic drone over neon streets" in prompt
    assert orchestrator.veo_prompt_with_duration_policy(prompt, 8).count("8-second cinematic clip") == 1
    assert "8-second cinematic clip" not in orchestrator.veo_prompt_with_duration_policy("keep me", 10)


def test_fallback_chain_is_labeled_and_key_gated(monkeypatch):
    monkeypatch.delenv("FAL_API_KEY", raising=False)
    monkeypatch.delenv("FAL_KEY", raising=False)
    duration_err = "duration_seconds parameter is not supported in Gemini API."
    assert orchestrator.next_video_fallback_engine("google_veo_lite", duration_err, True) is None
    assert orchestrator.next_video_fallback_engine(
        "google_veo_lite", "model veo-3.1-lite-generate-preview is not found", True
    ) == "google_veo"
    assert orchestrator.next_video_fallback_engine("google_veo", "quota exceeded", True) == "fal_hailuo_23"
    assert orchestrator.next_video_fallback_engine("google_veo", "quota exceeded", False) is None
    assert orchestrator.fal_key_present({}) is False
    monkeypatch.setenv("FAL_API_KEY", "present")
    assert orchestrator.fal_key_present({}) is True
