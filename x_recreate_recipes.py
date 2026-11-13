"""$0 recreate recipes for the X viral scan (x_viral_scan.py).

The app has NO free AI video generator (Gemini Veo / Omni, FAL, Runway are all paid and blocked by the $0 rule),
so a viral AI video can only be "recreated" with the free footage pipeline (footage_mode.py: public-domain /
CC footage and photos + local Kokoro voice + ffmpeg). A recipe says, for one kind of viral AI video, what an
original, on-brand short made from real free footage looks like: a hand-written, fact-checked script that
references the viral AI clip and footage searches whose results must be named for the subject.

A viral video with no matching recipe scores "not recreatable at $0" (it is never forced into an unrelated
topic). Topic dicts have the same shape as footage_topics.TOPICS; {views_say} / {views_show} are filled in
from the picked post.
"""
import copy
import re
from typing import List, Optional

RECIPES: List[dict] = [
    {
        "key": "x-ai-shack-vs-real-cabin",
        # one person building a shack / cabin / hut / shelter (e.g. @layoproperty 2026-11-12)
        "match": r"\b(build\w*|built|hammer\w*)\b.{0,60}\b(shack|cabin|hut|shelter|tiny house)s?\b|"
                 r"\b(shack|cabin|hut|shelter)s?\b.{0,40}\b(build\w*|built)\b",
        "topic": {
            "key": "x-ai-shack-vs-real-cabin", "topic": "Building a log cabin by hand",
            "title_line": "AI Shack vs. Real Cabin", "on_screen_hook": "AI SHACK VS. REAL CABIN",
            "hook": "{views_show} views: an AI woman builds a shack alone in a field.",
            "vo": [{"say": "{views_say} people just watched an AI video of a woman building a shack alone in a field.",
                    "show": "{views_show} views: an AI woman builds a shack alone in a field."},
                   {"say": "Viewers caught the giveaways. Her size keeps changing, and her clothes stay spotless.",
                    "show": "The giveaways: her size changes, her clothes stay spotless."},
                   {"say": "So here is what building a log cabin by hand really looked like.",
                    "show": "Here's what building a log cabin by hand really looked like."},
                   {"say": "Settlers felled the trees and hewed the logs flat with axes.",
                    "show": "Trees felled, logs hewn flat with axes."},
                   {"say": "Every log was notched at the corners, so the walls locked together.",
                    "show": "Logs notched at the corners so the walls lock together."},
                   {"say": "The gaps between the logs were packed with clay, moss or mud to keep out the wind.",
                    "show": "Gaps packed with clay, moss or mud."},
                   {"say": "It took days of heavy work, and nobody stayed clean.",
                    "show": "Days of heavy work. Nobody stayed clean."},
                   {"say": "Real photos. Real work. Which AI giveaway did you spot first?",
                    "show": "Real photos, real work. Which AI giveaway did you spot first?"}],
            "lines": ["The viral AI shack video vs. real archival photos of log cabin construction."],
            "hashtags": ["#AIVideo", "#AIFilmmaking", "#RealVsAI", "#LogCabin"], "mood": "reveal",
            "comment_question": "Which AI giveaway did you spot first?",
            # library-style topic: every file TITLE must name log-cabin CONSTRUCTION (not a finished cabin,
            # an architectural drawing sheet or a portrait taken inside one)
            "match": r"\blog cabins?\b", "related": None,
            "require": r"\blog construction\b|\bbuilding a log cabin\b|\bconstruction of\b.{0,40}\blog cabin\b|"
                       r"\blog cabin construction\b",
            "exclude": r"uncle tom|\bride\b|\btrain\b|\bdriver\b|\bair cabin\b|\bruins?\b|\bfire\b|\bmuseum\b|"
                       r"\bsheet \d|\bdrawing|\bplans?\b|\b20\d\d\b",
            "queries": {"commons": ["log cabin construction", "building a log cabin"],
                        "archive": ["building a log cabin"], "nasa": []},
            "stills": ["construction of log cabin", "building a log cabin"],
            "max_per_source": 8, "source": "x_recreate",
        },
    },
    {
        "key": "x-ai-astronaut-vs-real-spacewalk",
        # an astronaut floating / falling / drifting in space (black-hole falls, spacewalk clips)
        "match": r"\b(astronaut|spacewalk|space ?suit|cosmonaut)s?\b",
        "topic": {
            "key": "x-ai-astronaut-vs-real-spacewalk", "topic": "Real spacewalks", "title_line": "AI Astronaut vs. a Real Spacewalk",
            "on_screen_hook": "AI ASTRONAUT VS. REAL SPACEWALK", "hook": "{views_show} views: an AI astronaut adrift in space.",
            "vo": [{"say": "{views_say} people just watched an AI astronaut drifting through space.",
                    "show": "{views_show} views: an AI astronaut drifting through space."},
                   {"say": "Here is what a real spacewalk looks like.", "show": "Here's what a real spacewalk looks like."},
                   {"say": "The first one was in March nineteen sixty-five, by Alexei Leonov.",
                    "show": "The first spacewalk: Alexei Leonov, March 1965."},
                   {"say": "Today, astronauts work outside the International Space Station, clipped on with tethers.",
                    "show": "Astronauts work outside the ISS, clipped on with tethers."},
                   {"say": "The suit is a small spacecraft, with its own oxygen and cooling.",
                    "show": "The suit is a small spacecraft: its own oxygen and cooling."},
                   {"say": "A single spacewalk can last more than six hours.", "show": "One spacewalk can last 6+ hours."},
                   {"say": "Real footage, from NASA. Could you tell which one was AI?",
                    "show": "Real NASA footage. Could you tell which one was AI?"}],
            "lines": ["The viral AI astronaut vs. real NASA spacewalk footage."],
            "hashtags": ["#AIVideo", "#AIFilmmaking", "#RealVsAI", "#Spacewalk"], "mood": "cinematic",
            "comment_question": "Could you tell which one was AI?",
            "match": r"\bspacewalks?\b|\beva\b|\bextravehicular\b", "related": None,
            "require": r"\bspacewalks?\b|\beva\b|\bextravehicular\b",
            "exclude": r"\banimation\b|\billustration\b|\bconcept\b|\brender\b|\bbriefing\b|\bpress\b|\binterview\b",
            "queries": {"nasa": ["spacewalk"], "commons": ["spacewalk"], "archive": []},
            "max_per_source": 6, "source": "x_recreate",
        },
    },
]


def _views(n: int):
    """(spoken, shown) for a view count: 1763518 -> ("one point eight million", "1.8M")."""
    ones = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
    if n >= 1_000_000:
        v = round(n / 1_000_000, 1)
        whole, dec = int(v), int(round((v - int(v)) * 10))
        say = (_num(whole) + (f" point {ones[dec]}" if dec else "")) + " million"
        return say, f"{v:g}M"
    if n >= 1000:
        k = int(round(n / 1000))
        return _num(k) + " thousand", f"{k}K"
    return _num(n), str(n)


def _num(n: int) -> str:
    small = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen " \
            "seventeen eighteen nineteen".split()
    tens = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
    if n < 20:
        return small[n]
    if n < 100:
        return tens[n // 10] + ("-" + small[n % 10] if n % 10 else "")
    if n < 1000:
        return small[n // 100] + " hundred" + (" " + _num(n % 100) if n % 100 else "")
    return str(n)


def build_topic(recipe_key: str, item: dict) -> Optional[dict]:
    """The footage topic for a picked X item, with its real view count spoken / shown."""
    r = next((r for r in RECIPES if r["key"] == recipe_key), None)
    if not r:
        return None
    t = copy.deepcopy(r["topic"])
    say, show = _views(int(item.get("views") or 0))
    fill = lambda s: s.replace("{views_say}", say).replace("{views_show}", show)  # noqa: E731
    t["hook"] = fill(t["hook"])
    t["vo"] = [{"say": fill(v["say"]), "show": fill(v["show"])} for v in t["vo"]]
    t["x_source"] = item.get("url")
    return t


def matches(text: str) -> Optional[str]:
    r = next((r for r in RECIPES if re.search(r["match"], text or "", re.I)), None)
    return r["key"] if r else None
