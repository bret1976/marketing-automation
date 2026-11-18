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
    assert any("guaranteed lines" in line for line in log)


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
            "text": "The Apollo 11 launch took place at Kennedy Space Center. "
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
