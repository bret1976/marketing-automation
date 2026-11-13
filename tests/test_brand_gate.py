"""Regression tests for the Nov 12-13, 2026 off-brand posts, built from the live post records:
  * 2026-11-13 7:31 AM PT "The Slap: Real Footage" (post a0756925): viral item
    "Wife slap husband #drama #shorts #aivideo #reels" -> Wikipedia "The Slap (novel)" -> Slovene waterfall
    ("slap" = waterfall), chalk and slap photos.
  * 2026-11-12 7:12 PM PT "Mood" (post 08bf8146): viral item "Weather Mood" -> "Mood (psychology)" ->
    VOA South Sudan interview, Boer-war river film, "Mood of San Francisco".
Plus the mandatory human approval hold (Bret 2026-11-13: "send me the video so I can approve of it before you post it").
These use only public functions so they fail on the pre-fix code by assertion, not by import."""
import asyncio
import json
import os
import types
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("DISABLE_BACKGROUND_SCHEDULER", "true")
import footage_mode as fm  # noqa: E402
import growth_v2 as g  # noqa: E402


class _Resp:
    def __init__(self, data):
        self._d = data
        self.status_code = 200

    def json(self):
        return self._d


def _v(title, views, hours_old=21.1, url="https://www.youtube.com/watch?v=iwE6A0c6WOM"):
    pub = (datetime.now(timezone.utc) - timedelta(hours=hours_old)).isoformat()
    return {"platform": "YouTube", "title": title, "url": url, "views": views, "published_at": pub,
            "views_per_hour": round(views / hours_old, 1), "author": "some channel"}


SLAP_ITEM = _v("Wife slap husband #drama #shorts #aivideo #reels", 67552)  # real item, 3199.4 views/h
MOOD_ITEM = _v("Weather Mood \U0001F327\ufe0f", 283957, hours_old=10.1, url="https://www.youtube.com/watch?v=izjVb1YBnQ0")
SLAP_PAGE = ("2008 novel by Christos Tsiolkas",
             "The Slap is a 2008 novel by Australian author Christos Tsiolkas. The novel is set in suburban Melbourne. "
             "At a barbecue, a man slaps a child who is not his own. The story follows eight people who witness the slap. "
             "Each chapter is told from the point of view of a different guest. The book won the Commonwealth Writers' Prize "
             "in 2009. It was adapted into an Australian television series in 2011. A second adaptation followed in the "
             "United States in 2015. Critics praised its portrait of class and family in modern Australia. "
             "The novel was longlisted for the Man Booker Prize in 2010.")
MOOD_PAGE = ("", "In psychology, a mood is an affective state. In contrast to emotions or feelings, moods are less specific, "
             "less intense and less likely to be provoked by a particular stimulus or event. Moods are typically described "
             "as having either a positive or negative valence. Mood also differs from temperament or personality traits "
             "which are even longer-lasting. Nevertheless, personality traits such as optimism and neuroticism predispose "
             "certain types of moods. Long term disturbances of mood such as clinical depression and bipolar disorder are "
             "considered mood disorders. Mood is an internal, subjective state but it often can be inferred from posture "
             "and other behaviors. Researchers have long studied how mood shapes memory and judgement. A happy mood "
             "tends to make people recall happier events. A sad mood tends to make people recall sadder events. Sleep, "
             "exercise, weather and diet can all change mood over hours or days. Some cultures describe mood with colour "
             "words. Mood rings were sold in the nineteen seventies as a novelty.")


def _wiki(pages):
    def get(url, params=None, **k):
        if "wikidata" in url:
            return _Resp({"claims": {}})
        if params.get("list") == "search":
            q = params["srsearch"].lower()
            return _Resp({"query": {"search": [{"title": t} for t in pages
                                               if any(w in q for w in t.lower().replace("(", " ").replace(")", " ").split() if len(w) > 3)]}})
        t = params["titles"]
        return _Resp({"query": {"pages": {"1": {"title": t, "description": pages[t][0], "extract": pages[t][1]}}}})
    return get


# --------------------------------------------------------------------------- topic: genre + 24 h + subject
def test_slap_hashtag_only_ai_tag_is_not_in_genre():
    log = []
    picks = fm.viral_topics([SLAP_ITEM], get=_wiki({"The Slap (novel)": SLAP_PAGE}), log=log)
    assert picks == [], f"'The Slap' must never be picked from a skit that only carries #aivideo: {picks}"
    assert any("only in hashtags" in l for l in log), log


def test_slap_novel_is_rejected_even_from_a_real_ai_film_title():
    log = []
    picks = fm.viral_topics([_v("The Slap | AI short film made with Veo 3", 50000)],
                            get=_wiki({"The Slap (novel)": SLAP_PAGE}), log=log)
    assert picks == [], picks
    assert any("novel" in l or "homonym" in l for l in log), log


def test_mood_weather_upload_is_rejected():
    log = []
    picks = fm.viral_topics([MOOD_ITEM], get=_wiki({"Mood (psychology)": MOOD_PAGE}), log=log)
    assert picks == [] and any("off-genre" in l for l in log), log


def test_item_older_than_24h_or_without_upload_time_is_rejected():
    import brand_gate
    ok, why, _ = brand_gate.check_viral_item(_v("Titanic | AI short film made with Veo 3", 9e5, hours_old=30))
    assert not ok and "24h" in why
    item = _v("Titanic | AI short film made with Veo 3", 9e5)
    item.pop("published_at")
    assert not brand_gate.check_viral_item(item)[0]
    ok, why, prov = brand_gate.check_viral_item(_v("Titanic | AI short film made with Veo 3", 9e5, hours_old=3))
    assert ok and prov["published_at"] and prov["views"] and prov["url"] and prov["age_hours_at_pick"] == pytest.approx(3, abs=0.1)


def test_good_item_keeps_source_time_and_engagement():
    pages = {"Hindenburg disaster": ("1937 airship accident in the United States",
                                     "The Hindenburg disaster was an airship accident that occurred on May 6, 1937, in "
                                     "Manchester Township, New Jersey. The German passenger airship LZ 129 Hindenburg caught "
                                     "fire and was destroyed during its attempt to dock with its mooring mast at Naval Air "
                                     "Station Lakehurst. There were 97 people on board. There were 36 fatalities. The disaster "
                                     "was the subject of newsreel coverage and photographs. The event shattered public "
                                     "confidence in the giant airship and marked the end of the airship era.")}
    item = _v("Hindenburg disaster | AI short film made with Veo 3", 120000, hours_old=5)
    picks = fm.viral_topics([item], get=_wiki(pages))
    assert [p["topic"]["key"] for p in picks] in (["hindenburg-1937"], ["viral-hindenburg-disaster"])
    m = picks[0]["matched"][0]
    assert m["url"] == item["url"] and m["published_at"] == item["published_at"] and m["views"] == 120000


# --------------------------------------------------------------------------- footage: per-clip match
SLAP_CLIPS = ["Slap City - Open alpha is OUT!.webm", "Slap Savica 2021.webm", "Slap Virje and Boka, Bovec, Slovenia.webm",
              "Slap bass on a 6 string.ogv", "Slap Beri,Poljubinj, Tolmin, Slovenia.ogv"]
SLAP_STILLS = ["Slap-chalk-fortemplate.jpg", "Eurydice Ruhl Shimer College Eurydice slaps Loud Stone.jpg", "Slap on his face.jpg"]
MOOD_CLIPS = [("Troops Passing Over Mooder River (John Bennett-Stanford, 1899).webm", ""),
              ("John Tanza talks about the mood in South Sudan after the arrival of Riek Machar.webm", "VOA"),
              ("Mood of San Francisco", ""), ("PRIMADONNA SWIM Serie Ocean Mood bei CarlMarie.webm", "")]


def _search(videos, images):
    def commons(q, media="video", get=None, limit=15):
        rows = videos if media == "video" else images
        return [{"provider": "wikimedia_commons", "kind": media, "id": t, "title": t, "description": "", "author": a,
                 "height": 1080, "license": "CC BY 3.0", "url": f"https://upload.wikimedia.org/{i}"}
                for i, (t, a) in enumerate(rows)]
    return commons


def _patch_sources(monkeypatch, videos, images=()):
    monkeypatch.setattr(fm, "search_commons", _search(videos, images))
    for f in ("search_nasa", "search_archive", "search_pexels", "search_pixabay"):
        monkeypatch.setattr(fm, f, lambda *a, **k: [])


def test_slap_footage_does_not_match_the_narration(monkeypatch):
    topic = fm.topic_from_subject({"title": "The Slap (novel)", "description": SLAP_PAGE[0], "lead": SLAP_PAGE[1],
                                   "text": SLAP_PAGE[1], "url": "https://en.wikipedia.org/wiki/The_Slap_(novel)"})
    assert topic
    _patch_sources(monkeypatch, [(t, "JohannPiber") for t in SLAP_CLIPS], [(t, "Geoff Stearns") for t in SLAP_STILLS])
    kept, _ = fm.gather_candidates(topic)
    assert kept == [], [c["title"] for c in kept]


def test_mood_footage_does_not_match_the_narration(monkeypatch):
    topic = fm.topic_from_subject({"title": "Mood (psychology)", "description": "", "lead": MOOD_PAGE[1],
                                   "text": MOOD_PAGE[1], "url": "https://en.wikipedia.org/wiki/Mood_(psychology)"})
    assert topic
    _patch_sources(monkeypatch, MOOD_CLIPS)
    kept, _ = fm.gather_candidates(topic)
    assert kept == [], [c["title"] for c in kept]


def test_on_subject_clip_passes_and_records_evidence(monkeypatch):
    import brand_gate
    subj = {"title": "Hindenburg disaster", "description": "1937 airship accident",
            "lead": "The Hindenburg disaster was an airship accident in May 1937. It happened at Lakehurst, New "
                    "Jersey. The German passenger airship caught fire while docking. Thirty-six people died. Newsreel cameras "
                    "filmed the fire. It marked the end of the airship era. The ship had crossed the Atlantic from Frankfurt. "
                    "Storms delayed the landing by hours. Hydrogen fed the fire. Investigators never settled the cause. "
                    "Herbert Morrison recorded his famous radio report on the landing field that evening. The tail struck the "
                    "ground first and the burning hull followed within seconds. Ground crew ran to pull survivors from the wreck. "
                    "Sixty-two of the ninety-seven people on board survived the fire.",
            "url": "https://en.wikipedia.org/wiki/Hindenburg_disaster"}
    subj["text"] = subj["lead"]
    topic = fm.topic_from_subject(subj)
    ok = brand_gate.check_clip({"title": "Hindenburg burning at Lakehurst 1937.webm", "description": "", "author": "US Navy"}, topic)
    assert ok["ok"] and "lakehurst" in ok["evidence"], ok
    bad = brand_gate.check_clip({"title": "Hindenburg crash - BBC News report.webm", "description": "", "author": "BBC"}, topic)
    assert not bad["ok"] and "news" in bad["reason"]


# --------------------------------------------------------------------------- every publish path: gate + approval hold
def _fake_main(tmp_path, published):
    posts = []

    async def publish(post, settings, **k):
        published.append(post["id"])
        return {"successes": ["tiktok"], "errors": []}
    return types.SimpleNamespace(
        STATE_DIR=str(tmp_path), GENERATED_DIR=str(tmp_path),
        load_settings=lambda: {"public_base_url": "https://app.example", "require_autopilot_approval": False},
        load_scheduled_posts=lambda: [dict(p) for p in posts],
        save_scheduled_posts=lambda ps: (posts.clear(), posts.extend(ps)),
        publish_post_to_platforms=publish, posts=posts)


def _good_info():
    import brand_gate
    ok, _, prov = brand_gate.check_viral_item(_v("Hindenburg disaster | AI short film made with Veo 3", 9e4, hours_old=4))
    assert ok
    clips = [{"ok": True, "clip": f"Hindenburg {i}.webm", "reason": "named", "shot": i, "narration": ["x"]} for i in range(4)]
    return {"engine": "footage", "media_source": "archival_footage", "captions": [{"text": "x", "start": 0, "end": 2}],
            "brand_gate": {"brand": dict(brand_gate.PINNED_BRAND), "viral_item": prov, "clips": clips}}


def _concept(topic="Hindenburg disaster"):
    return {"topic": topic, "topic_key": topic.lower(), "title_line": f"{topic}: Real Footage", "on_screen_hook": topic.upper(),
            "hook": f"{topic}.", "lines": ["Real archival footage."], "hashtags": ["#History"],
            "vo": [{"say": "May sixth, nineteen thirty-seven.", "show": "May 6, 1937."}]}


def _run(monkeypatch, tmp_path, info, concept=None, slot="2026-11-14-0"):
    published = []
    fake = _fake_main(tmp_path, published)
    monkeypatch.setattr(g, "_main", lambda: fake)
    monkeypatch.setenv("GROWTH_PLATFORMS", "tiktok,instagram,linkedin")
    video = tmp_path / "g2_footage_x.mp4"
    video.write_bytes(b"\0")

    async def produce(settings, state):
        return str(video), dict(concept or _concept()), info
    monkeypatch.setattr(g, "produce", produce)
    return published, fake, (lambda: asyncio.run(g.run_slot(slot, fake.load_settings())))


def test_slap_post_record_is_never_published(monkeypatch, tmp_path):
    """The exact media_info shape the live app stored for 'The Slap' (no gate record) must not publish."""
    info = {"engine": "footage", "media_source": "archival_footage", "footage_mode": "2026-11-12.6",
            "viral_match": {"score": 3199.4, "reason": "viral_subject",
                            "matched": [{"title": SLAP_ITEM["title"], "url": SLAP_ITEM["url"], "views": 67552}]}}
    published, fake, run = _run(monkeypatch, tmp_path, info, _concept("The Slap"))
    assert run() is False
    assert published == []
    slot = g.load_state()["slots"]["2026-11-14-0"]
    assert slot["status"] == "skipped" and "brand gate" in slot["skip_reason"]
    assert g.due_slot(datetime(2026, 11, 14, 0, 5), {"autonomous_hours": "15,0"}, g.load_state()) is None


def test_clip_mismatch_at_publish_time_skips(monkeypatch, tmp_path):
    info = _good_info()
    info["brand_gate"]["clips"][2] = {"ok": False, "clip": "Slap Virje and Boka, Bovec, Slovenia.webm",
                                      "reason": "shares no narration term"}
    published, fake, run = _run(monkeypatch, tmp_path, info)
    assert run() is False and published == []
    assert g.load_state()["slots"]["2026-11-14-0"]["status"] == "skipped"


def test_good_video_is_held_for_approval_never_published(monkeypatch, tmp_path):
    published, fake, run = _run(monkeypatch, tmp_path, _good_info())
    assert run() is True  # the slot is done: a video is waiting for Bret
    assert published == [], "nothing may publish without Bret's explicit approval"
    held = [p for p in fake.posts if p["status"] == "AWAITING_APPROVAL"]
    assert len(held) == 1
    assert g.load_state()["slots"]["2026-11-14-0"]["status"] == "awaiting_approval"
    pkg = json.load(open(tmp_path / "approvals" / "2026-11-14-0" / "approval.json"))
    assert pkg["post_id"] == held[0]["id"] and pkg["topic"] == "Hindenburg disaster"
    assert pkg["viral_source"]["published_at"] and pkg["viral_source"]["views"] and pkg["viral_source"]["url"]
    assert pkg["script"] and pkg["captions"] and len(pkg["clip_checks"]) == 4
    assert pkg["video_url"] == "https://app.example/static/assets/generated/g2_footage_x.mp4"
    assert pkg["approve"].endswith(f"/api/approval-queue/{held[0]['id']}/approve")
    assert g.list_approval_packages()[0]["post_id"] == held[0]["id"]


def test_approval_cannot_be_switched_off(monkeypatch):
    monkeypatch.setenv("REQUIRE_AUTOPILOT_APPROVAL", "false")
    assert g.approval_required({"require_autopilot_approval": False}) is True


def test_manual_trigger_and_dry_run_paths_hit_the_same_gate(monkeypatch, tmp_path):
    published, fake, _ = _run(monkeypatch, tmp_path, {"engine": "gemini_omni", "media_source": "ai_generated"})
    import brand_gate
    with pytest.raises(brand_gate.BrandGateSkip):
        asyncio.run(g.run_autopilot(fake.load_settings()))  # /api/trigger-autopilot + legacy hour trigger path
    with pytest.raises(brand_gate.BrandGateSkip):
        asyncio.run(g.run_autopilot(fake.load_settings(), dry_run=True))
    assert published == []


def test_held_posts_are_never_auto_published_by_the_queue_loop():
    """main.py's queue loop only publishes status PENDING; a held post stays AWAITING_APPROVAL until /approve."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")).read()
    loop = src[src.index("for post in posts:\n                if post[\"status\"] == \"PENDING\""):][:600]
    assert "AWAITING_APPROVAL" not in loop
    assert "require_approval = True" in src
