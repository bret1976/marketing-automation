import footage_clip as fc


class _FakeImg:
    pass


def test_clip_requires_clear_winner_but_keeps_exact_entity(monkeypatch):
    """Assigned line must beat other lines by 0.03; a slightly tighter caption is allowed.

    The Eisenhower letter (0.253 vs description 0.265) is kept via exact-entity when the
    margin vs other lines is tight. A line that loses clearly is rejected.
    """
    def scores_for(values):
        def _score(image, texts):
            return list(values[:len(texts)])
        return _score

    img = _FakeImg()

    # Clear winner: line 0.28, desc 0.27, best other 0.22 (margin 0.06)
    monkeypatch.setattr(fc, "score_image_texts", scores_for([0.28, 0.27, 0.22, 0.19]))
    got = fc.confirm_line(img, "artificial Mulberry harbours", "Mulberry harbour caisson",
                          other_lines=["Eisenhower was appointed commander", "winds blew the landing craft"])
    assert got["ok"] is True
    assert got["best_other"] == 0.22

    # v4 sectors: line 0.237 trails desc 0.245 by 0.008 (under slack 0.02) and wins by 0.04
    monkeypatch.setattr(fc, "score_image_texts", scores_for([0.237, 0.245, 0.197]))
    got = fc.confirm_line(img, "divided into five sectors", "D-Day Sectors Chart",
                          other_lines=["Mulberry harbours"])
    assert got["ok"] is True

    # Not a clear winner (margin 0.01) and no exact entity
    monkeypatch.setattr(fc, "score_image_texts", scores_for([0.26, 0.25, 0.25]))
    got = fc.confirm_line(img, "an airborne landing had occurred", "British airborne troops in a village",
                          other_lines=["soldiers in a Normandy village"], exact_entity=False)
    assert got["ok"] is False

    # Eisenhower letter: 0.253 trails desc 0.265 by 0.012, margin vs other is 0.013 — keep via entity
    monkeypatch.setattr(fc, "score_image_texts", scores_for([0.253, 0.265, 0.240]))
    got = fc.confirm_line(img, "Eisenhower was appointed SHAEF commander",
                          "General Eisenhower letter to the Allied Expeditionary Force",
                          other_lines=["Mulberry harbours were built"], exact_entity=True)
    assert got["ok"] is True

    # Trails the description by more than entity slack (solar-energy v4 reject 0.203 vs 0.324)
    monkeypatch.setattr(fc, "score_image_texts", scores_for([0.203, 0.324, 0.18]))
    got = fc.confirm_line(img, "Solar energy can be captured", "a glass greenhouse",
                          other_lines=["a palm house"], exact_entity=True)
    assert got["ok"] is False


def test_run_fallback_respects_topic_budget(monkeypatch):
    import footage_verified as fv
    monkeypatch.setattr(fv, "FALLBACK_TOPIC_BUDGET_S", 0)
    monkeypatch.setattr(fv, "cached_fallback_subjects", lambda *a, **k: [])
    monkeypatch.setattr(fv, "library_fallback_subjects", lambda *a, **k: [
        (fv.NORMANDY if hasattr(fv, "NORMANDY") else {"title": "Normandy landings"},
         {"title": "Normandy landings"}, "v2-dday-1944-fb")
    ])
    # Use the test fixture subject
    from tests.test_footage_verified import NORMANDY
    monkeypatch.setattr(fv, "library_fallback_subjects", lambda *a, **k: [
        (NORMANDY, {"title": "Normandy landings"}, "v2-dday-1944-fb")
    ])
    log = []
    # budget 0 is exhausted before the first topic
    assert fv.run_fallback(set(), "", "/tmp", log) is None
    assert any("budget exhausted" in line or "skipping slot" in line for line in log)
    assert any(line == "ai fallback (cf 429)" for line in log)
