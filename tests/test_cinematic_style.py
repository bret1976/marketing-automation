import os

os.environ.setdefault("ADMIN_PASSWORD", "test-password")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("DISABLE_BACKGROUND_SCHEDULER", "true")

import growth_v2 as g
import orchestrator


def test_style_is_widescreen_blur_padded_recreation():
    assert g.STYLE_ASPECT == "16:9"
    for word in ("RECREATION", "16:9", "newsreel", "orchestral", "2-3 hard cuts", "Place Year"):
        assert word in g.CONCEPT_PROMPT
    assert "loops cleanly" not in g.CONCEPT_PROMPT


def test_fallbacks_follow_formula():
    for c in g.FALLBACK_CONCEPTS:
        s = g.sanitize_concept(c)
        assert g.has_number(s["on_screen_hook"])
        assert any(ch.isdigit() for ch in s["title_line"])
        assert "#CinematicAI" in s["hashtags"] and len(s["hashtags"]) <= 5
        assert s["lines"][0].startswith("We recreated")
        assert len(s["shot_list"]) == 3


def test_omni_prompt_follows_aspect():
    assert "widescreen 16:9" in orchestrator.omni_prompt("x", 8, "16:9")
    assert "vertical 9:16" in orchestrator.omni_prompt("x", 8)
    assert orchestrator.omni_request_body("x", "16:9")["response_format"]["aspect_ratio"] == "16:9"


def test_captions_use_reference_structure():
    caps = g.build_captions(g.sanitize_concept(g.FALLBACK_CONCEPTS[0]))
    first = caps["tiktok"].splitlines()
    assert first[0] == "Omaha Beach 1944"
    assert "#CinematicAI" in caps["tiktok"]
