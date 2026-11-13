#!/usr/bin/env python3
"""Dry run of what the next Autopilot slot would pick RIGHT NOW. Never publishes, never touches the post queue.
Free sources only (yt-dlp YouTube search, Wikipedia, Wikimedia Commons, Internet Archive, NASA); no paid API.

    VIDEO_ENGINE=footage VIRAL_SOURCE=open python3 scripts/dry_run_next_slot.py [--render] [--out DIR] [--recent 'a|b']

Prints the brand check, every viral item considered (and why it was skipped), each candidate topic with its source
upload (time + engagement) and the per-clip match result for every candidate clip. With --render it also renders
the video locally (Kokoro TTS + ffmpeg, $0) and writes an approval package to --out/<dryrun-stamp>/.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("VIDEO_ENGINE", "footage")
os.environ.setdefault("VIRAL_SOURCE", "open")

import brand_gate  # noqa: E402
import footage_mode as fm  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--render", action="store_true")
    ap.add_argument("--out", default="/workspace/marketing-approvals")
    ap.add_argument("--recent", default="", help="'|'-separated recent topics to avoid (from /api/growth-v2/status)")
    a = ap.parse_args()
    now = datetime.now(timezone.utc)
    print(f"== dry run at {now.isoformat()} (footage {fm.VERSION}, gate {brand_gate.GATE_VERSION}); NOTHING is published")
    print("== brand:", json.dumps(brand_gate.brand_profile()))
    viral = fm.todays_viral()
    items = viral.get("top") or []
    print(f"== viral scan: {len(items)} uploads from the last {viral.get('window_hours')}h; errors={(viral.get('errors') or [])[:3]}")
    recent = [t for t in a.recent.split("|") if t]
    log = []
    order = fm.viral_topics(items, recent_topics=recent, limit=int(os.environ.get("FOOTAGE_TOPIC_ATTEMPTS", 4)), log=log, now=now)
    for line in log:
        print("   ", line)
    if not order:
        print("== RESULT: SKIP. No AI-filmmaking upload from the last 24h names a filmable subject. The slot would be "
              "skipped with that reason; nothing would be rendered or posted.")
        return 0
    chosen = None
    for pick in order:
        t = pick["topic"]
        m = pick["matched"][0]
        print(f"\n== candidate topic: {t['topic']} ({t['key']}, {pick['reason']})")
        print(f"   source: {m.get('platform')} {m.get('url')} | '{m.get('title')}' | uploaded {m.get('published_at')} "
              f"({m.get('age_hours_at_pick')}h before the run) | {m.get('views')} views, {m.get('views_per_hour')} views/h | {m.get('author')}")
        print("   narration:", [v["show"] for v in t["vo"]][:12])
        seen = []
        orig = brand_gate.check_clip

        def spy(c, topic, pinned_titles=()):
            r = orig(c, topic, pinned_titles)
            seen.append(r)
            return r
        brand_gate.check_clip = spy
        try:
            kept, errs = fm.gather_candidates(t)
        finally:
            brand_gate.check_clip = orig
        for r in seen:
            print(f"   clip {'PASS' if r['ok'] else 'FAIL'} | {r['clip'][:70]} | {r['reason']} | {r.get('evidence')}")
        print(f"   -> {len(kept)} clips pass (need >= {fm.min_shots()} usable shots); search errors: {errs[:3]}")
        if len(kept) >= fm.min_shots() and chosen is None:
            chosen = pick
    if not chosen:
        print("\n== RESULT: SKIP. No candidate topic has enough clips that match its narration; nothing would be posted.")
        return 0
    print(f"\n== RESULT: would build '{chosen['topic']['topic']}' (still subject to the shot-quality probe and the final gate)")
    if not a.render:
        return 0
    stamp = now.strftime("dryrun-%Y%m%d-%H%M")
    d = os.path.join(a.out, stamp)
    os.makedirs(d, exist_ok=True)
    v, l, concept, info = fm.produce_footage(d, viral=viral, recent_topics=recent, name="video")
    gate = brand_gate.publish_gate(concept, info, min_shots=fm.min_shots())
    pkg = {"slot": stamp, "status": "DRY_RUN (not queued, not published)", "topic": concept["topic"],
           "title_line": concept.get("title_line"), "viral_source": gate["viral_item"], "brand": gate["brand"],
           "script": [x["say"] for x in concept["vo"]], "captions": info.get("captions"), "clip_checks": gate["clips"],
           "footage_sources": info.get("footage_sources"), "duration_s": info.get("duration_s"),
           "video": v, "video_16x9": l}
    with open(os.path.join(d, "approval.json"), "w") as fh:
        json.dump(pkg, fh, indent=1, default=str)
    print("== rendered:", v, l, "| package:", os.path.join(d, "approval.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
