import os

import numpy as np
import pytest

os.environ.setdefault("ADMIN_PASSWORD", "test-password")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("DISABLE_BACKGROUND_SCHEDULER", "true")

import cf_ai
import footage_tts
import footage_verified as fv

REALM_PAGE = {"title": "Realm (database)", "description": "Embedded database", "lead": "Realm is an open-source object database.",
              "text": "Realm is an open-source object database. MongoDB acquired Realm in 2019.", "commons_category": "",
              "url": "https://en.wikipedia.org/wiki/Realm_(database)"}


def test_realm_fantasy_short_is_rejected_when_resolver_says_fiction(monkeypatch):
    monkeypatch.setattr(cf_ai, "chat_json", lambda *a, **k: {"real_world": False, "subject": "", "wikipedia_title": "",
                                                             "confidence": 1.0, "why": "fantasy short film"})
    monkeypatch.setattr(fv.fm, "_wiki_page", lambda *a, **k: pytest.fail("must not look up a page for fiction"))
    log = []
    assert fv.resolve_trend({"title": "In Service of the Realm | Seedance AI Fantasy Short Film (2026)"}, log=log) is None
    assert "not a real-world subject" in log[-1]


def test_keyword_page_is_rejected_when_verifier_disagrees(monkeypatch):
    answers = iter([{"real_world": True, "subject": "Realm", "wikipedia_title": "Realm (database)", "confidence": 0.9},
                    {"same_subject": False, "confidence": 0.95, "why": "film vs database"}])
    monkeypatch.setattr(cf_ai, "chat_json", lambda *a, **k: next(answers))
    monkeypatch.setattr(cf_ai, "embed", lambda texts: [[1.0, 0.0], [1.0, 0.0]])
    monkeypatch.setattr(fv.fm, "_wiki_page", lambda *a, **k: dict(REALM_PAGE))
    log = []
    assert fv.resolve_trend({"title": "In Service of the Realm | AI Short Film"}, log=log) is None
    assert "verify failed" in log[-1]


def test_low_embedding_similarity_rejects(monkeypatch):
    answers = iter([{"real_world": True, "subject": "Realm", "wikipedia_title": "Realm (database)", "confidence": 0.9},
                    {"same_subject": True, "confidence": 0.9}])
    monkeypatch.setattr(cf_ai, "chat_json", lambda *a, **k: next(answers))
    monkeypatch.setattr(cf_ai, "embed", lambda texts: [[1.0, 0.0], [0.0, 1.0]])
    monkeypatch.setattr(fv.fm, "_wiki_page", lambda *a, **k: dict(REALM_PAGE))
    assert fv.resolve_trend({"title": "Realm"}, log=[]) is None


def test_grounded_numbers():
    src = "Voyager 1 launched in 1977 and crossed the heliopause in 2012."
    assert fv._grounded("It launched in 1977.", src)
    assert not fv._grounded("It launched in 1979.", src)


def test_timeline_cuts_in_the_middle_of_gaps():
    caps, shots, total = fv.timeline([3.0, 4.0, 2.5])
    assert caps[0]["start"] == fv.LEAD
    assert caps[1]["start"] == pytest.approx(fv.LEAD + 3.0 + fv.GAP)
    # first cut sits halfway through the silence between line 1 and line 2
    assert shots[0] == pytest.approx(caps[1]["start"] - fv.GAP / 2)
    assert sum(shots) == pytest.approx(total)
    assert total == pytest.approx(caps[-1]["start"] + 2.5 + fv.END_CARD)


def test_vision_rejects_text_talkshow_and_low_match(monkeypatch):
    line = {"show": "Voyager 1 launched from Cape Canaveral.", "visual": "Titan rocket launch"}
    cases = [({"match": 9, "on_topic": True, "has_text": True, "type": "footage"}, False),
             ({"match": 9, "on_topic": True, "has_text": False, "type": "talk_show"}, False),
             ({"match": 4, "on_topic": True, "has_text": False, "type": "footage"}, False),
             ({"match": 9, "on_topic": False, "has_text": False, "type": "footage"}, False),
             ({"match": 8, "on_topic": True, "has_text": False, "type": "footage"}, True)]
    for ans, ok in cases:
        monkeypatch.setattr(cf_ai, "vision_json", lambda *a, ans=ans, **k: dict(ans))
        assert fv.vision_check(b"x", "Voyager 1", line, "a frame")["ok"] is ok, ans


def test_tts_trim_keeps_tail_and_never_changes_speed():
    sr = footage_tts.SR
    audio = np.concatenate([np.zeros(sr), 0.5 * np.ones(sr // 2, dtype=np.float32), np.zeros(sr)]).astype(np.float32)
    out = footage_tts.trim(audio, sr)
    assert len(out) == pytest.approx(sr // 2 + int(footage_tts.HEAD_PAD * sr) + int(footage_tts.TAIL_PAD * sr), abs=2)


def test_tts_rejects_blended_voice(monkeypatch):
    monkeypatch.setenv("FOOTAGE_VOICE", "am_michael:0.6,am_onyx:0.4")
    assert footage_tts.voice() == footage_tts.DEFAULT_VOICE


def test_produce_without_cloudflare_refuses(monkeypatch, tmp_path):
    monkeypatch.setattr(cf_ai, "ready", lambda: False)
    with pytest.raises(RuntimeError):
        fv.produce(str(tmp_path), viral={"top": []})


def test_produce_raises_instead_of_wrong_video(monkeypatch, tmp_path):
    monkeypatch.setattr(cf_ai, "ready", lambda: True)
    monkeypatch.setattr(fv, "resolve_trend", lambda it, log=None: None)
    with pytest.raises(RuntimeError, match="not rendering a wrong video"):
        fv.produce(str(tmp_path), viral={"top": [{"title": "Some AI Short Film", "views_per_hour": 10}]})
