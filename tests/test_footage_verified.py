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


@pytest.fixture(autouse=True)
def _no_wiki_categories(monkeypatch):
    monkeypatch.setattr(fv, "_wiki_categories", lambda *a, **k: [])


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


def test_produce_without_cloudflare_falls_back_and_skips_without_guaranteed_clips(monkeypatch, tmp_path):
    monkeypatch.setattr(cf_ai, "ready", lambda: False)
    monkeypatch.setattr(fv, "run_fallback", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="ai fallback"):
        fv.produce(str(tmp_path), viral={"top": []})


def test_cfquota_during_resolve_switches_to_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(cf_ai, "ready", lambda: True)
    monkeypatch.setattr(cf_ai, "reset_usage", lambda: None)

    def boom(*_a, **_k):
        raise cf_ai.CFQuota("glm HTTP 429: daily free allocation of 10,000 neurons")

    monkeypatch.setattr(fv, "resolve_trend", boom)
    monkeypatch.setattr(fv, "run_fallback", lambda *a, **k: None)
    monkeypatch.setenv("FOOTAGE_TOPIC_POOL", "all")
    with pytest.raises(RuntimeError, match=r"ai fallback \(cf 429\)"):
        fv.produce(str(tmp_path), viral={"top": [{"title": "Apollo 11 launch documentary", "views_per_hour": 10}]})


def test_produce_raises_instead_of_wrong_video(monkeypatch, tmp_path):
    monkeypatch.setattr(cf_ai, "ready", lambda: True)
    monkeypatch.setattr(fv, "resolve_trend", lambda it, log=None: None)
    with pytest.raises(RuntimeError, match="not rendering a wrong video"):
        fv.produce(str(tmp_path), viral={"top": [{"title": "Some AI Short Film", "views_per_hour": 10}]})


# --- religious block (Bret 2026-11-17) + honest skip labels
KAABA = {"title": "Kaaba", "description": "Building at the center of Masjid al-Haram", "lead": "The Kaaba is a stone building.",
         "text": "", "commons_category": "", "url": ""}
KAABA_CATS = ["Arabic architecture", "Buildings and structures in Mecca", "Hajj", "Islamic architecture", "Kaaba",
              "Religious buildings and structures converted into mosques"]
JAGANNATH = {"title": "Jagannath", "description": "Regional form of the Hindu deity Mahavishnu", "lead": "", "text": "",
             "commons_category": "", "url": ""}
JAGANNATH_CATS = ["Deities of Jagannath", "Forms of Krishna", "Forms of Vishnu", "Hindu gods", "Regional Hindu gods"]


def test_religious_category_check_blocks_kaaba_and_jagannath():
    assert fv.religious_subject(KAABA, KAABA_CATS)
    assert fv.religious_subject(dict(KAABA, description="Cube building"), KAABA_CATS).startswith("category")
    assert fv.religious_subject(JAGANNATH, JAGANNATH_CATS)
    assert fv.religious_subject(dict(JAGANNATH, description="Regional form"), JAGANNATH_CATS).startswith("category")


@pytest.mark.parametrize("subj,cats", [
    ({"title": "Voyager 1", "description": "NASA space probe launched in 1977"}, ["NASA space probes", "Missions to Saturn"]),
    ({"title": "Greenhouse", "description": "Building for growing plants"}, ["Agricultural buildings", "Greenhouses"]),
    ({"title": "Titanic", "description": "British passenger liner that sank in 1912"}, ["Ships sunk by icebergs"]),
    ({"title": "Holy Roman Empire", "description": "European political entity (800/962-1806)"},
     ["Christendom", "Former Christian states", "History of Catholicism in Europe", "Western Christianity"]),
    ({"title": "Christmas Island", "description": "External territory of Australia"}, ["Islands of Australia"]),
    ({"title": "Great Pyramid of Giza", "description": "Largest pyramid in the Giza Necropolis, Egypt"},
     ["Seven Wonders of the Ancient World"]),
])
def test_religious_check_leaves_secular_subjects(subj, cats):
    assert fv.religious_subject(subj, cats) is None


def test_resolve_rejects_kaaba_as_religious_before_verify(monkeypatch):
    answers = iter([{"real_world": True, "subject": "Kaaba", "wikipedia_title": "Kaaba", "confidence": 1.0}])
    monkeypatch.setattr(cf_ai, "chat_json", lambda *a, **k: next(answers))
    monkeypatch.setattr(fv.fm, "_wiki_page", lambda *a, **k: dict(KAABA))
    monkeypatch.setattr(fv.fm, "subject_ok", lambda s: (True, ""))
    monkeypatch.setattr(fv, "_wiki_categories", lambda *a, **k: list(KAABA_CATS))
    log = []
    assert fv.resolve_trend({"title": "Snowfall at Holy Kaaba | Beautiful Islamic AI Video"}, log=log) is None
    assert log[-1].startswith("subject skip (religious:")


def test_resolve_rejects_when_verify_model_says_religious(monkeypatch):
    page = {"title": "Some Shrine", "description": "Building", "lead": "", "text": "", "commons_category": "", "url": ""}
    answers = iter([{"real_world": True, "subject": "x", "wikipedia_title": "Some Shrine", "confidence": 1.0},
                    {"same_subject": True, "confidence": 1.0, "religious": True, "why": "a place of worship"}])
    monkeypatch.setattr(cf_ai, "chat_json", lambda *a, **k: next(answers))
    monkeypatch.setattr(cf_ai, "embed", lambda texts: [[1.0, 0.0], [1.0, 0.0]])
    monkeypatch.setattr(fv.fm, "_wiki_page", lambda *a, **k: dict(page))
    monkeypatch.setattr(fv.fm, "subject_ok", lambda s: (True, ""))
    log = []
    assert fv.resolve_trend({"title": "Some Shrine AI film"}, log=log) is None
    assert "religious: verify model" in log[-1]


@pytest.mark.parametrize("title,label", [
    ("Kling 4.0 on Artlist Tutorial - Create Cinematic AI Videos with Audio", "tutorial/how-to"),
    ("browns raiders NFL week 12", "sport/wrestling"),
    ("LIVE now: AI art stream", "livestream"),
    ("Minecraft but AI", "video game"),
])
def test_trend_skip_reason_names_the_real_reason(title, label):
    assert fv.trend_skip_reason(title).startswith(label)


def test_ai_short_films_are_not_prefiltered():
    assert fv.trend_skip_reason("She Asked How Much Dinner Cost... | AI Short Film") is None
    assert fv.trend_skip_reason("TITANIC | AI Short Film made with Veo 3") is None


def test_meta_line_match_accepts_category_clip_with_title_overlap():
    subj = {"title": "Apollo 11", "commons_category": "Apollo 11", "description": "first crewed moon landing"}
    cand = {"title": "Apollo 11 launch from Kennedy Space Center", "description": "Saturn V lifts off",
            "provider": "wikimedia_commons", "id": "File:A.ogv", "curated": True, "kind": "video"}
    line = {"show": "Apollo 11 lifts off from Kennedy Space Center."}
    got = fv.meta_line_match(cand, subj, line)
    assert got["ok"] is True
    assert any("kennedy" in x or "apollo" in x or "lifts" in x for x in got["overlap"])


def test_meta_line_match_rejects_unrelated_or_uncurated_clip():
    subj = {"title": "Apollo 11", "commons_category": "Apollo 11", "description": ""}
    unrelated = {"title": "Tekken 8 tournament finals", "description": "a talk show about a fighting game",
                 "provider": "wikimedia_commons", "id": "File:T.webm", "curated": False, "kind": "video"}
    line = {"show": "Apollo 11 lifts off from Kennedy Space Center."}
    assert fv.meta_line_match(unrelated, subj, line)["ok"] is False
    # curated but the title does not overlap the line
    weak = {"title": "Apollo 11 patch artwork", "description": "mission insignia",
            "provider": "wikimedia_commons", "id": "File:P.svg", "curated": True, "kind": "image"}
    assert fv.meta_line_match(weak, subj, line)["ok"] is False


def test_library_fallback_skips_religious_subjects(monkeypatch):
    log = []
    temple = {"title": "Kaaba", "description": "Building at the center of Masjid al-Haram",
              "lead": "The Kaaba is a stone building.", "text": "", "commons_category": "Kaaba",
              "url": "https://en.wikipedia.org/wiki/Kaaba"}
    monkeypatch.setattr(fv.fm, "wiki_subject", lambda *a, **k: temple)
    monkeypatch.setattr(fv.fm, "_wiki_page", lambda *a, **k: temple)
    monkeypatch.setattr(fv.fm, "subject_ok", lambda subj: (True, ""))
    monkeypatch.setattr(fv, "_wiki_categories", lambda *a, **k: KAABA_CATS)
    monkeypatch.setattr("footage_topics.TOPICS", [{"key": "kaaba", "topic": "Kaaba"}], raising=False)
    import footage_topics
    monkeypatch.setattr(footage_topics, "TOPICS", [{"key": "kaaba", "topic": "Kaaba"}])
    out = fv.library_fallback_subjects(set(), "", log)
    assert out == []
    assert any("religious" in line for line in log)


def test_write_script_from_footage_requires_guaranteed_lines():
    subj = {"title": "Apollo 11", "commons_category": "Apollo 11",
            "text": "Apollo 11 was the American spaceflight that first landed humans on the Moon. "
                    "The Apollo 11 launch took place at Kennedy Space Center. "
                    "Neil Armstrong stepped onto the lunar surface. "
                    "The Saturn V rocket carried the crew into Earth orbit. "
                    "A camera recorded the Apollo 11 landing on the Moon. "
                    "Mission control in Houston watched the Apollo 11 landing. "
                    "The crew splashed down in the Pacific Ocean after Apollo 11. "
                    "Apollo 11 returned the first Moon rocks to Earth. "
                    "The command module Columbia carried the Apollo 11 crew home."}
    pool = [
        {"title": "Apollo 11 launch at Kennedy Space Center", "description": "Saturn V liftoff",
         "provider": "wikimedia_commons", "id": "File:L.ogv", "curated": True, "kind": "video"},
        {"title": "Apollo 11 landing on the Moon", "description": "lunar module descent",
         "provider": "wikimedia_commons", "id": "File:M.ogv", "curated": True, "kind": "video"},
        {"title": "Unrelated cooking video", "description": "pasta",
         "provider": "wikimedia_commons", "id": "File:Z.ogv", "curated": False, "kind": "video"},
    ]
    log = []
    script = fv.write_script_from_footage(subj, pool, log)
    # Only two clips can be guaranteed, FALLBACK_MIN_LINES is 5, so the script must be refused.
    assert script is None
    assert any(
        "guaranteed lines" in line or "no Wikipedia sentence describes" in line
        or "no image-backed" in line or "skipping slot" in line
        for line in log
    )


def test_place_by_meta_downloads_still_before_return(monkeypatch, tmp_path):
    cand = {"title": "Apollo 11 launch photo", "description": "", "provider": "wikimedia_commons",
            "id": "File:Apollo11.jpg", "curated": True, "kind": "image"}
    class _R:
        content = b"fakepng"
    monkeypatch.setattr(fv.fm, "_get", lambda *a, **k: _R())
    monkeypatch.setattr(fv, "_resolve_url", lambda c, get=None: None)
    got = fv.place_by_meta(cand, 3.0, {}, str(tmp_path))
    assert got is not None
    assert cand["_img"] == b"fakepng"
    assert got["vision"]["method"] == "metadata"


NORMANDY = {
    "title": "Normandy landings",
    "commons_category": "Normandy landings",
    "description": "Allied invasion of France, 6 June 1944",
    "lead": "The Normandy landings were the landing operations on Tuesday, 6 June 1944 of the Allied invasion of Normandy in Operation Overlord during World War II.",
    "url": "https://en.wikipedia.org/wiki/Normandy_landings",
}


def _still(title, description="", ident="File:X.jpg"):
    return {"title": title, "description": description, "provider": "wikimedia_commons",
            "id": ident, "curated": True, "kind": "image"}


def test_meta_line_match_rejects_normandy_radar_barges_fdr_fleet_and_may_infantry():
    """Regression: the first 429 fallback used subject-category stills that did not depict the line."""
    radar = {"show": "Many of the German radar stations on the French coast were destroyed in preparation for the landings."}
    barges = _still(
        "From Coast Guard landing barges hitting the French Coast with the first waves of invaders, "
        "American fighting men wade ashore under heavy machine gun fire from Nazi beach nests. (51210534892).jpg",
        ident="File:barges.jpg",
    )
    assert fv.meta_line_match(barges, NORMANDY, radar)["ok"] is False

    fdr = {"show": "US president Franklin D Roosevelt placed Major General Dwight D Eisenhower in command of Allied forces."}
    fleet = _still("D-day - Allied Forces during the Invasion of Normandy 6 June 1944 A23843.jpg",
                   ident="File:fleet.jpg")
    assert fv.meta_line_match(fleet, NORMANDY, fdr)["ok"] is False

    may_plan = {"show": "The Allies planned to launch the invasion on 1 May 1944."}
    infantry = _still("D-day - British Forces during the Invasion of Normandy 6 June 1944 B5058.jpg",
                      ident="File:inf.jpg")
    assert fv.meta_line_match(infantry, NORMANDY, may_plan)["ok"] is False


def test_meta_line_match_accepts_line_specific_normandy_stills():
    radar = {"show": "Many of the German radar stations on the French coast were destroyed in preparation for the landings."}
    radar_still = _still(
        "Destroyed German radar station on the French coast before the Normandy landings.jpg",
        "A German radar station wrecked in preparation for the landings",
        "File:radar.jpg",
    )
    got = fv.meta_line_match(radar_still, NORMANDY, radar)
    assert got["ok"] is True
    assert "radar" in got["overlap"]

    fdr = {"show": "US president Franklin D Roosevelt placed Major General Dwight D Eisenhower in command of Allied forces."}
    fdr_still = _still(
        "President Franklin D. Roosevelt and General Eisenhower.jpg",
        "US president Franklin D Roosevelt with Dwight D Eisenhower, appointed to command Allied forces",
        "File:fdr.jpg",
    )
    got = fv.meta_line_match(fdr_still, NORMANDY, fdr)
    assert got["ok"] is True
    assert any("roosevelt" in x for x in got["overlap"])


def test_write_script_from_footage_picks_sentences_that_fit_the_images():
    subj = dict(NORMANDY)
    subj["text"] = (
        "The Normandy landings began on 6 June 1944. "
        "Many of the German radar stations on the French coast were destroyed in preparation for the landings. "
        "US president Franklin D Roosevelt placed Major General Dwight D Eisenhower in command of Allied forces. "
        "Allied landing craft approached the Normandy beaches at dawn. "
        "British infantry advanced through a Normandy village on D-Day. "
        "Glider borne troops passed over Royal Navy ships on their way to the invasion beaches of Normandy. "
        "The Allies planned to launch the invasion on 1 May 1944. "
        "General Dwight D Eisenhower was appointed commander of the Supreme Headquarters Allied Expeditionary Force."
    )
    pool = [
        _still("Destroyed German radar station on the French coast before the Normandy landings.jpg",
               "A German radar station wrecked in preparation for the landings", "File:radar.jpg"),
        _still("President Franklin D. Roosevelt and General Eisenhower at the Normandy landings conference.jpg",
               "US president Franklin D Roosevelt with Dwight D Eisenhower, appointed to command Allied forces",
               "File:fdr.jpg"),
        {"title": "Allied landing craft approach the Normandy beaches.ogv",
         "description": "Landing craft approaching the Normandy beaches at dawn",
         "provider": "wikimedia_commons", "id": "File:craft.ogv", "curated": True, "kind": "video"},
        _still("British infantry in a Normandy village on D-Day.jpg",
               "British infantry advanced through a Normandy village on D-Day", "File:infantry.jpg"),
        _still("Glider borne troops passing over Royal Navy ships towards the Normandy landings.jpg",
               "Glider borne troops passed over Royal Navy ships on their way to the invasion beaches of Normandy",
               "File:gliders.jpg"),
        _still("General Eisenhower Letter to Soldiers of the Allied Expeditionary Force.png",
               "General Dwight D Eisenhower appointed commander of the Supreme Headquarters Allied Expeditionary Force",
               "File:letter.png"),
    ]
    log = []
    script = fv.write_script_from_footage(subj, pool, log)
    assert script is not None, log
    shows = " ".join(ln["show"].lower() for ln in script["lines"])
    assert "radar" in shows
    assert "roosevelt" in shows
    # The May-plan sentence must not be attached to the infantry village still.
    for ln in script["lines"]:
        pick = (ln.get("picks") or [{}])[0]
        if "1 may" in ln["show"].lower() or "1 may" in ln["say"].lower():
            assert "infantry" not in (pick.get("title") or "").lower()
        if "radar" in ln["show"].lower():
            assert "radar" in (pick.get("title") or "").lower() + (pick.get("description") or "").lower()


def test_write_script_prefers_video_when_a_still_also_matches():
    subj = {"title": "Apollo 11", "commons_category": "Apollo 11",
            "text": "Apollo 11 lifts off from Kennedy Space Center. "
                    "Neil Armstrong stepped onto the lunar surface. "
                    "A camera recorded the Apollo 11 landing on the Moon. "
                    "The crew splashed down in the Pacific Ocean after Apollo 11. "
                    "The command module Columbia carried the Apollo 11 crew home. "
                    "Mission control in Houston watched the Apollo 11 landing."}
    # Six guaranteed line/clip pairs; launch has both a still and a video.
    pool = [
        {"title": "Apollo 11 launch from Kennedy Space Center still", "description": "Saturn V lifts off",
         "provider": "wikimedia_commons", "id": "File:launch.jpg", "curated": True, "kind": "image"},
        {"title": "Apollo 11 launch from Kennedy Space Center video", "description": "Saturn V lifts off",
         "provider": "wikimedia_commons", "id": "File:launch.ogv", "curated": True, "kind": "video"},
        {"title": "Neil Armstrong steps onto the lunar surface", "description": "Armstrong on the Moon",
         "provider": "wikimedia_commons", "id": "File:arm.ogv", "curated": True, "kind": "video"},
        {"title": "Apollo 11 landing on the Moon", "description": "camera recorded the landing",
         "provider": "wikimedia_commons", "id": "File:land.ogv", "curated": True, "kind": "video"},
        {"title": "Apollo 11 crew splashdown in the Pacific Ocean", "description": "the crew splashed down",
         "provider": "wikimedia_commons", "id": "File:splash.ogv", "curated": True, "kind": "video"},
        {"title": "Command module Columbia after Apollo 11", "description": "Columbia carried the crew home",
         "provider": "wikimedia_commons", "id": "File:columbia.ogv", "curated": True, "kind": "video"},
        {"title": "Mission control in Houston during the Apollo 11 landing", "description": "Houston mission control",
         "provider": "wikimedia_commons", "id": "File:houston.ogv", "curated": True, "kind": "video"},
    ]
    log = []
    script = fv.write_script_from_footage(subj, pool, log)
    assert script is not None, log
    launch_lines = [ln for ln in script["lines"] if "kennedy" in ln["show"].lower() or "launch" in ln["show"].lower()]
    assert launch_lines
    pick = (launch_lines[0].get("picks") or [{}])[0]
    assert pick.get("kind") == "video"
    assert pick.get("id") == "File:launch.ogv"


def test_run_fallback_skips_slot_when_no_topic_has_enough_lines(monkeypatch):
    monkeypatch.setattr(fv, "cached_fallback_subjects", lambda *a, **k: [])
    monkeypatch.setattr(fv, "library_fallback_subjects", lambda *a, **k: [
        (NORMANDY, {"title": "Normandy landings", "views_per_hour": 0}, "v2-normandy-landings")
    ])
    monkeypatch.setattr(fv, "build_fallback_topic", lambda *a, **k: None)
    log = []
    assert fv.run_fallback(set(), "", "/tmp", log) is None
    assert any("ai fallback (cf 429)" == line for line in log)
    assert any("skipping slot" in line for line in log)


GREENHOUSE = {
    "title": "Greenhouse",
    "commons_category": "Greenhouses",
    "description": "Building for growing plants",
    "lead": "A greenhouse is a structure with walls and roof made of transparent material in which plants requiring regulated climatic conditions are grown.",
    "url": "https://en.wikipedia.org/wiki/Greenhouse",
}


def test_meta_line_match_rejects_greenhouse_false_positives_from_first_tight_run():
    """Regression: the first tightened dry-run still accepted stills on weak words or place last-names."""
    flower = _still("PepperFlower4K.jpg", "a pepper flower, either indoors or out", "File:PepperFlower4K.jpg")
    heated = {"show": "However, both heated and unheated structures can generally class as greenhouses. may reference either a hothouse or a glasshouse."}
    assert fv.meta_line_match(flower, GREENHOUSE, heated)["ok"] is False

    plan = _still(
        "Decorations for parks and gardens - designs for gates, garden seats, alcoves, temples, baths, "
        "entrance gates, lodges, facades, prospect towers, cattle sheds, ruins, bridges, greenhouses, &c. &c. also (14770045621).jpg",
        ident="File:decorations.jpg",
    )
    belfast = {"show": "One of the earliest examples of a palm house is in the Belfast Botanic Gardens."}
    assert fv.meta_line_match(plan, GREENHOUSE, belfast)["ok"] is False

    woodward = _still("The Conservatory at Woodward's Garden (NYPL b11707333-G89F411 007F).tiff",
                      ident="File:woodward.tiff")
    chelsea = {"show": "The first 'stove' greenhouse in the UK was completed at Chelsea Physic Garden by 1681."}
    assert fv.meta_line_match(woodward, GREENHOUSE, chelsea)["ok"] is False

    bau = _still("BAU greenhouse.jpg", "Bangladesh Agricultural University greenhouse", "File:BAU greenhouse.jpg")
    before = {"show": "Before the development of greenhouses, agricultural practices were constrained to weather conditions."}
    assert fv.meta_line_match(bau, GREENHOUSE, before)["ok"] is False


def test_meta_line_match_accepts_true_greenhouse_line_specific_stills():
    glass = _still("Glasshouse 1-2.jpg", "a glasshouse with potted plants", "File:Glasshouse 1-2.jpg")
    terms = {"show": "The terms greenhouse, glasshouse, and hothouse often refer interchangeably to buildings used for cultivating plants."}
    got = fv.meta_line_match(glass, GREENHOUSE, terms)
    assert got["ok"] is True
    assert "glasshouse" in got["overlap"]

    equip = _still(
        'Photograph with caption "Humidifying and cooling equipment for a propagating case in the greenhouses used for plant... - NARA - 282464.jpg',
        ident="File:equip.jpg",
    )
    line = {"show": "Such glass greenhouses may feature extensive equipment."}
    got = fv.meta_line_match(equip, GREENHOUSE, line)
    assert got["ok"] is True
    assert "equipment" in got["overlap"]

    belfast_photo = _still(
        "Palm house in the Belfast Botanic Gardens.jpg",
        "The palm house at Belfast Botanic Gardens",
        "File:belfast.jpg",
    )
    belfast = {"show": "One of the earliest examples of a palm house is in the Belfast Botanic Gardens."}
    got = fv.meta_line_match(belfast_photo, GREENHOUSE, belfast)
    assert got["ok"] is True


def test_meta_line_match_rejects_normandy_polarity_and_nationality_mismatches():
    """A British gun crew is not German tank losses; US troops on a transport did not 'lack transport'."""
    german_losses = {"show": "All told, the German forces stationed in France were deprived of 45,827 troops and 363 tanks, assault guns, and self-propelled anti-tank guns."}
    british_guns = _still("The British Army in Normandy 1944 B5020.jpg",
                          "A British anti-tank gun in a field in Normandy",
                          "File:B5020.jpg")
    assert fv.meta_line_match(british_guns, NORMANDY, german_losses)["ok"] is False

    lacked = {"show": "They were provided mainly with unreliable captured equipment and lacked motorised transport."}
    us_transport = _still(
        "H Hour has arrived. American soldiers, hardened and ready, lounge under full fighting equipment on the deck of a Coast Guard assault transport in the fateful trip across the English Channel. (49380246091).jpg",
        ident="File:transport.jpg",
    )
    assert fv.meta_line_match(us_transport, NORMANDY, lacked)["ok"] is False

    mulberry = {"show": "The most serious drawback of the Normandy coast would be overcome through the development of artificial Mulberry harbours."}
    harbor = _still("D-Day Harbor (15103484811).jpg", "Remains of an artificial Mulberry harbour caisson", "File:harbor.jpg")
    got = fv.meta_line_match(harbor, NORMANDY, mulberry)
    assert got["ok"] is True
    assert "mulberry" in " ".join(got["overlap"]) or "artificial" in got["overlap"]


def test_person_names_extract_bare_surname_but_not_many_german():
    names = fv._person_names("Eisenhower had tentatively selected 5 June as the date for the assault.")
    assert any(n.lower() == "eisenhower" for n in names)
    assert fv._person_names("Many German units were under strength.") == []
    assert fv._person_names("The two generals insisted that the scale of the initial assault should be increased.") == []
    # Full names still work, including FDR + Eisenhower
    both = fv._person_names("US president Franklin D Roosevelt placed Major General Dwight D Eisenhower in command.")
    assert any("roosevelt" in n.lower() for n in both)
    assert any("eisenhower" in n.lower() for n in both)


def test_dates_extract_day_month_but_are_never_the_accept_basis():
    dates = fv._dates("Eisenhower had tentatively selected 5 June as the date for the assault.")
    assert any(d[0] == "d" and d[2] == 6 and d[3] == 5 for d in dates)
    # Conflicting calendar days still reject (May 1 vs June 6; 5 June vs 6 June).
    assert fv._dates_conflict([("d", 1944, 5, 1)], "British Forces during the Invasion of Normandy 6 June 1944")
    assert fv._dates_conflict([("d", 0, 6, 5)], "Invasion of Normandy 6 June 1944")
    assert not fv._dates_conflict([("d", 0, 6, 5)], "American soldiers on a Coast Guard assault transport")
    # A line whose only distinctive token is a date must not be accepted on the date alone.
    date_only = {"show": "The landings were scheduled for 5 June."}
    transport = _still(
        "H Hour has arrived. American soldiers, hardened and ready, lounge under full fighting equipment "
        "on the deck of a Coast Guard assault transport in the fateful trip across the English Channel. (49380246091).jpg",
        ident="File:transport.jpg",
    )
    assert fv.meta_line_match(transport, NORMANDY, date_only)["ok"] is False


def test_v3_normandy_failures_and_near_miss_are_rejected():
    """Regression: v3 dry-run job 8af4f105 still accepted three mismatches and a near miss."""
    generals = {"show": "The two generals insisted that the scale of the initial assault should be increased from three to five divisions."}
    road = _still("The Allied Campaign in North-West Europe 6 June 1944 - 7 May 1945 B5221.jpg",
                  ident="File:B5221.jpg")
    assert fv.meta_line_match(road, NORMANDY, generals)["ok"] is False

    ike_date = {"show": "Eisenhower had tentatively selected 5 June as the date for the assault."}
    us_transport = _still(
        "H Hour has arrived. American soldiers, hardened and ready, lounge under full fighting equipment "
        "on the deck of a Coast Guard assault transport in the fateful trip across the English Channel. (49380246091).jpg",
        ident="File:transport.jpg",
    )
    terms = fv.line_specific_terms(ike_date, NORMANDY)
    assert any("eisenhower" in n.lower() for n in terms["people"])
    assert fv.meta_line_match(us_transport, NORMANDY, ike_date)["ok"] is False

    german_str = {"show": "Many German units were under strength."}
    tank = _still("D-day - British Forces during the Invasion of Normandy 6 June 1944 B5089.jpg",
                  ident="File:B5089.jpg")
    assert fv.line_specific_terms(german_str, NORMANDY)["people"] == []
    assert fv.meta_line_match(tank, NORMANDY, german_str)["ok"] is False

    winds = {"show": "Strong winds blew the landing craft east of their intended positions."}
    navy = _still(
        "The Royal Navy during the Second World War- Operation Overlord (the Normandy Landings), June 1944 A24101.jpg",
        ident="File:A24101.jpg",
    )
    assert fv.meta_line_match(navy, NORMANDY, winds)["ok"] is False


def test_write_script_image_first_does_not_reuse_v3_mismatches():
    """Start from the images: each file keeps a sentence that describes its own metadata, or is dropped."""
    subj = dict(NORMANDY)
    subj["text"] = (
        "The Normandy landings were the Allied invasion of France. "
        "The two generals insisted that the scale of the initial assault should be increased from three to five divisions. "
        "The most serious drawback of the Normandy coast would be overcome through the development of artificial Mulberry harbours. "
        "Eisenhower had tentatively selected 5 June as the date for the assault. "
        "American soldiers crossed the English Channel on a Coast Guard assault transport. "
        "General Eisenhower was appointed commander of the Supreme Headquarters Allied Expeditionary Force. "
        "Strong winds blew the landing craft east of their intended positions. "
        "Many German units were under strength. "
        "British tanks came ashore with the first waves on D-Day. "
        "The landing was divided into sectors shown on the official D-Day sectors chart."
    )
    pool = [
        _still("D-Day Sectors Chart NARA 84786164.jpg",
               "Chart of the D-Day sectors", "File:sectors.jpg"),
        _still("The Allied Campaign in North-West Europe 6 June 1944 - 7 May 1945 B5221.jpg",
               ident="File:B5221.jpg"),
        _still("D-Day Harbor (15103484811).jpg",
               "Remains of an artificial Mulberry harbour caisson", "File:harbor.jpg"),
        _still(
            "H Hour has arrived. American soldiers, hardened and ready, lounge under full fighting equipment "
            "on the deck of a Coast Guard assault transport in the fateful trip across the English Channel. (49380246091).jpg",
            ident="File:transport.jpg",
        ),
        _still("General Eisenhower Letter to Soldiers of the Allied Expeditionary Force.png",
               "General Dwight D Eisenhower appointed commander of the Supreme Headquarters Allied Expeditionary Force",
               "File:letter.png"),
        _still("The Royal Navy during the Second World War- Operation Overlord (the Normandy Landings), June 1944 A24101.jpg",
               ident="File:A24101.jpg"),
        _still("D-day - British Forces during the Invasion of Normandy 6 June 1944 B5089.jpg",
               "A British tank coming ashore with the first waves on D-Day", "File:B5089.jpg"),
    ]
    log = []
    script = fv.write_script_from_footage(subj, pool, log)
    assert script is not None, log
    by_id = {}
    for ln in script["lines"]:
        pick = (ln.get("picks") or [{}])[0]
        by_id[pick.get("id")] = ln["show"].lower()
    shows = " ".join(by_id.values())
    # Known-good pairings
    assert "File:harbor.jpg" in by_id
    assert "mulberry" in by_id["File:harbor.jpg"]
    assert "File:letter.png" in by_id
    assert "eisenhower" in by_id["File:letter.png"]
    assert "File:transport.jpg" in by_id
    assert "channel" in by_id["File:transport.jpg"] or "transport" in by_id["File:transport.jpg"]
    assert "eisenhower" not in by_id["File:transport.jpg"]
    assert "5 june" not in by_id["File:transport.jpg"]
    # v3 failures must not be reassigned to those stills
    if "File:B5221.jpg" in by_id:
        assert "generals" not in by_id["File:B5221.jpg"] and "divisions" not in by_id["File:B5221.jpg"]
    if "File:B5089.jpg" in by_id:
        assert "under strength" not in by_id["File:B5089.jpg"]
        assert "german" not in by_id["File:B5089.jpg"]
    if "File:A24101.jpg" in by_id:
        assert "winds" not in by_id["File:A24101.jpg"]
    # And the bad sentences should not appear at all on any still
    assert "two generals" not in shows
    assert "tentatively selected" not in shows
    assert "under strength" not in shows
    assert "strong winds" not in shows


def test_statistic_lines_are_never_matched_to_photos():
    assert fv.is_statistic_line("German casualties on D-Day have been estimated at 4,000 to 9,000 men.")
    assert fv.is_statistic_line("All told, the German forces stationed in France were deprived of 45,827 troops and 363 tanks.")
    assert not fv.is_statistic_line("The target 80-kilometre stretch of the Normandy coast was divided into five sectors.")
    assert not fv.is_statistic_line("General Eisenhower was appointed commander of the Supreme Headquarters Allied Expeditionary Force.")
    prisoners = _still(
        "D-day - British Forces during the Invasion of Normandy 6 June 1944 B5089.jpg",
        "A group of German prisoners standing in the water next to a disabled Sherman Crab flail tank "
        "watch as a jeep is towed from the sea, Queen beach, Sword area, 6 June 1944.",
        "File:B5089.jpg",
    )
    casualties = {"show": "German casualties on D-Day have been estimated at 4,000 to 9,000 men."}
    assert fv.meta_line_match(prisoners, NORMANDY, casualties)["ok"] is False
    assert fv.meta_line_match(prisoners, NORMANDY, casualties)["reason"].startswith("statistic")


def test_v4_dummies_winds_and_airborne_compound_are_rejected():
    """v4 dry-run f6c59af3: distinctive head noun/event must appear in the image description."""
    dummies = {"show": "These dummies led the Germans to believe that an additional airborne landing had occurred."}
    benouville = _still(
        "D-day - British Forces during the Invasion of Normandy 6 June 1944 B5058.jpg",
        "Commandos of No. 4 Commando, 1st Special Service Brigade, and troops of 6th Airborne Division "
        "in Bénouville after the link-up between the two forces, 6 June 1944.",
        "File:B5058.jpg",
    )
    got = fv.meta_line_match(benouville, NORMANDY, dummies)
    assert got["ok"] is False
    assert any(h.lower() == "dummies" for h in fv.required_heads(dummies, NORMANDY))
    # A nationality or 'airborne landing' compound alone must never carry the line.
    assert "dummies" not in (benouville["title"] + " " + benouville["description"]).lower()

    winds = {"show": "Strong winds blew the landing craft east of their intended positions."}
    lcts = _still(
        "D-day - British Forces during the Invasion of Normandy 6 June 1944 B5170.jpg",
        "LSTs (Landing Ship Tank) and LCTs (Landing Craft Tank) off the Normandy coast, "
        "as seen from LST 406, 6 June 1944. Visible are LST 228 and LCT 3627.",
        "File:B5170.jpg",
    )
    got = fv.meta_line_match(lcts, NORMANDY, winds)
    assert got["ok"] is False
    heads = [h.lower() for h in fv.line_head_terms(winds, NORMANDY)]
    assert any("wind" in h or h == "blew" or h == "winds" for h in heads)


def test_v4_true_matches_and_document_letterbox_still_hold():
    letter = _still(
        "General Eisenhower Letter to Soldiers of the Allied Expeditionary Force.png",
        "General Dwight D Eisenhower appointed commander of the Supreme Headquarters Allied Expeditionary Force",
        "File:letter.png",
    )
    ike = {"show": "General Dwight D Eisenhower was appointed commander of the Supreme Headquarters Allied Expeditionary Force."}
    got = fv.meta_line_match(letter, NORMANDY, ike)
    assert got["ok"] is True
    assert fv.still_is_document(letter) is True

    harbor = _still("D-Day Harbor (15103484811).jpg",
                    "Remains of an artificial Mulberry harbour caisson", "File:harbor.jpg")
    mulberry = {"show": "The most serious drawback of the Normandy coast would be overcome through the development of artificial Mulberry harbours."}
    assert fv.meta_line_match(harbor, NORMANDY, mulberry)["ok"] is True
    assert fv.still_is_document(harbor) is False

    chart = _still("D-Day Sectors Chart NARA 84786164.jpg", "Chart of the D-Day sectors", "File:sectors.jpg")
    sectors = {"show": "The target 80-kilometre stretch of the Normandy coast was divided into five sectors: Utah, Omaha, Gold, Juno, and Sword."}
    got = fv.meta_line_match(chart, NORMANDY, sectors)
    assert got["ok"] is True
    assert fv.still_is_document(chart) is True

    # Maps and documents are letterboxed (fit/contain), not cover-cropped.
    import footage_mode as fm
    contain = fm._shot_filter("image", 1080, 1920, 5.0, 0, 0.5, "contain")
    cover = fm._shot_filter("image", 1080, 1920, 5.0, 0, 0.5, "cover")
    assert "force_original_aspect_ratio=decrease" in contain and "pad=" in contain
    assert "zoompan" in cover and "pad=" not in cover


ARTEMIS = {
    "title": "Artemis 1",
    "commons_category": "Artemis 1",
    "description": "uncrewed Moon mission",
}


def test_v5_weak_heads_and_place_only_without_event_are_rejected():
    """v5 Artemis holes: weak nouns never count; a place-only match without the event never counts."""
    michoud_line = {"show": "Construction began mid-November 2014 at the Michoud Assembly Facility."}
    assert "Michoud Assembly Facility" in fv.required_heads(michoud_line, ARTEMIS)
    # Same place, later engine install, even with the year stripped so the date gate cannot carry the reject.
    engine = _still(
        "RS-25 engine install at Michoud.jpg",
        "An RS-25 engine is installed at the Michoud Assembly Facility.",
        "File:engine.jpg",
    )
    got = fv.meta_line_match(engine, ARTEMIS, michoud_line)
    assert got["ok"] is False
    assert "place-only" in got["reason"] or "event" in got["reason"] or "date" in got["reason"]

    engine_dated = _still(
        "RS-25 engine install at Michoud.jpg",
        "An RS-25 engine is installed at the Michoud Assembly Facility, 2019-10-28.",
        "File:engine-dated.jpg",
    )
    assert fv.meta_line_match(engine_dated, ARTEMIS, michoud_line)["ok"] is False

    started = _still(
        "Michoud construction 2014.jpg",
        "Construction of the core stage began in mid-November 2014 at the Michoud Assembly Facility.",
        "File:started.jpg",
    )
    assert fv.meta_line_match(started, ARTEMIS, michoud_line)["ok"] is True

    stage = {"show": "The stage adapter was stacked on the Core Stage on June 22."}
    weak = {h.lower() for h in fv.required_heads(stage, ARTEMIS) + fv.line_head_terms(stage, ARTEMIS)}
    assert "stage" not in weak and "stages" not in weak
    sep = _still(
        "Orion ICPS separation.jpg",
        "Orion and the interim cryogenic propulsion stage separate in space on November 16.",
        "File:sep.jpg",
    )
    assert fv.meta_line_match(sep, ARTEMIS, stage)["ok"] is False

    crew = {"show": "A shorter crewed flight would have carried two astronauts."}
    crew_heads = [h.lower() for h in fv.required_heads(crew, ARTEMIS)]
    assert "astronauts" not in crew_heads
    assert "flight" not in crew_heads
    assert "crew" not in crew_heads and "crewed" not in crew_heads
    graphic = _still("lunar gravity assist.jpg", "A graphic of a lunar gravity-assist trajectory.", "File:g.jpg")
    assert fv.meta_line_match(graphic, ARTEMIS, crew)["ok"] is False


def test_clip_confirm_placement_fails_closed_and_scores_the_chosen_segment(monkeypatch):
    """Decode or score failure rejects the line; video frames come from the chosen segment, not a skip."""
    log = []
    line = {"show": "Construction began at the Michoud Assembly Facility.", "_other_lines": ["other line"],
            "_subj": ARTEMIS}

    # No source at all: reject, never skip.
    assert fv._clip_confirm_placement({"cand": {}, "start": 0.0}, line, log, need=6.0) is False
    assert any("rejecting line" in row and "skipped" not in row for row in log)

    class _Mod:
        @staticmethod
        def available():
            return True

        @staticmethod
        def image_from_bytes(data):
            return None

        @staticmethod
        def frames_from_segment(path, start, duration, n=3):
            seen["args"] = (path, start, duration, n)
            return [None, object(), object()]

        @staticmethod
        def confirm_line(*a, **k):
            raise AssertionError("must not score a missing frame")

    seen = {}
    monkeypatch.setitem(__import__("sys").modules, "footage_clip", _Mod)
    log = []
    placed = {"cand": {"_src": "/tmp/path.mp4", "title": "Path to the Pad"}, "start": 297.0}
    assert fv._clip_confirm_placement(placed, line, log, need=6.0) is False
    assert seen["args"][0] == "/tmp/path.mp4"
    assert seen["args"][1] == 297.0
    assert seen["args"][2] == 6.0
    assert any("rejecting line" in row for row in log)
    assert not any("skipped" in row for row in log)

    class _OkThenFail:
        @staticmethod
        def available():
            return True

        @staticmethod
        def frames_from_segment(path, start, duration, n=3):
            return [object(), object(), object()]

        n = {"i": 0}

        @staticmethod
        def confirm_line(*a, **k):
            _OkThenFail.n["i"] += 1
            if _OkThenFail.n["i"] < 3:
                return {"ok": True, "reason": "clip confirms line", "line": 0.3, "desc": 0.28,
                        "best_other": 0.2, "ms": 1, "rss_mb": 100}
            return {"ok": False, "reason": "line is not a clear winner", "line": 0.21, "desc": 0.28,
                    "best_other": 0.25, "ms": 1, "rss_mb": 100}

    monkeypatch.setitem(__import__("sys").modules, "footage_clip", _OkThenFail)
    log = []
    assert fv._clip_confirm_placement(placed, line, log, need=6.0) is False
    assert any("rejecting line" in row for row in log)


def test_run_fallback_skip_log_names_the_five_line_floor(monkeypatch):
    monkeypatch.setattr(fv, "cached_fallback_subjects", lambda *a, **k: [])
    monkeypatch.setattr(fv, "library_fallback_subjects", lambda *a, **k: [
        (NORMANDY, {"title": "Normandy landings", "views_per_hour": 0}, "v2-normandy-landings")
    ])
    monkeypatch.setattr(fv, "build_fallback_topic", lambda *a, **k: None)
    log = []
    assert fv.run_fallback(set(), "", "/tmp", log) is None
    assert any("fewer than 5 lines survived with guaranteed footage; skipping slot" in line for line in log)
