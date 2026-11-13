#!/usr/bin/env python3
"""Dry run of the X viral scan (x_viral_scan.py): top 10 viral AI videos on X in the last 24 h, recreate scores,
the pick, and (with --render) the $0 recreation rendered locally into an approval package.
Never publishes, never touches the post queue, no paid API.

    python3 scripts/x_scan_dry_run.py --snapshot x_scan_snapshot.json [--render] [--out /workspace/marketing-approvals]

Without --snapshot it uses the app's own source (X_BEARER_TOKEN / X_SCAN_SNAPSHOT); with neither it reports the skip.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("VIDEO_ENGINE", "footage")

import brand_gate  # noqa: E402
import footage_mode as fm  # noqa: E402
import x_viral_scan as xs  # noqa: E402


def review_text(pkg: dict) -> str:
    v = pkg["viral_source"]
    lines = [f"Slot: {pkg['slot']}   Status: {pkg['status']}", f"Title: {pkg['title_line']}",
             f"Source X post: {v.get('url')} by @{v.get('author')} at {v.get('published_at')} "
             f"({v.get('age_hours_at_pick')} h old at scan): {v.get('views')} views, {v.get('likes')} likes, "
             f"{v.get('video_duration_s')} s video", f"  genre evidence: {v.get('genre_evidence')}",
             f"Recreation: {pkg['recreation']['method']}", "", "Top 10 (views, recreate score):"]
    for t in pkg["x_scan"]["top"]:
        lines.append(f"  #{t['rank']} @{t['author']} {t['views']:,} views {t['likes']:,} likes {t['video_duration_s']}s "
                     f"score={t['recreate_score']} {'RECREATABLE' if t['recreatable'] else 'blocked'} "
                     f"{t['blocks'] or ''} {t['url']}")
    lines += ["", "Script:"] + [f"  {i + 1}. {s}" for i, s in enumerate(pkg["script"])]
    lines += ["", "Per-shot checks:"]
    for c in pkg.get("clip_checks") or []:
        lines.append(f"  shot {c.get('shot')} [{c.get('start_s')}-{c.get('end_s')}s] {'OK ' if c.get('ok') else 'BAD'} "
                     f"{c.get('clip')} :: {c.get('reason')} :: narration {c.get('narration')}")
    lines += ["", "Captions:", json.dumps(pkg.get("platform_captions"), indent=1, ensure_ascii=False), "", pkg["note"]]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot")
    ap.add_argument("--render", action="store_true")
    ap.add_argument("--out", default="/workspace/marketing-approvals")
    ap.add_argument("--now", help="ISO time to evaluate the 24 h window at (default: now)")
    a = ap.parse_args()
    now = datetime.fromisoformat(a.now) if a.now else datetime.now(timezone.utc)
    print(f"== X scan dry run at {now.isoformat()} (x-scan {xs.VERSION}, gate {brand_gate.GATE_VERSION}); NOTHING is published")
    try:
        src = xs.SnapshotSource(a.snapshot) if a.snapshot else None
        scan = xs.run_scan(source=src, now=now)
    except xs.XScanUnavailable as exc:
        print("== RESULT: SKIP.", exc)
        return 0
    print(f"== source: {json.dumps(scan['source'])}; {scan['posts_seen']} posts seen")
    for t in scan["top"]:
        print(f"#{t['rank']:>2} @{t['author']:<16} {t['views']:>9,} views {t['likes']:>7,} likes {t['video_duration_s']}s "
              f"{t['age_hours']}h old | score {t['recreate_score']} ({'recreatable' if t['recreatable'] else 'NOT recreatable'}) "
              f"{t['points']} {t['blocks']} | {t['url']}")
    for line in scan["log"]:
        print("   ", line[:220])
    if not scan["pick"]:
        print("== RESULT: SKIP. Nothing in the top 10 is recreatable at $0.")
        return 0
    print(f"== PICK: {scan['pick']['url']} (recipe {scan['pick']['recipe']})")
    if not a.render:
        return 0
    import growth_v2
    slot = now.strftime("xscan-dryrun-%Y%m%d-%H%M")
    d = os.path.join(a.out, slot)
    os.makedirs(d, exist_ok=True)
    v, concept, info = growth_v2.produce_x_recreate({}, {}, scan=scan, out_dir=d, name="video")
    gate = brand_gate.publish_gate(concept, info, now=now, min_shots=fm.min_shots())
    info["brand_gate"] = gate
    caps = growth_v2.build_captions(concept)
    pkg = {"slot": slot, "status": "DRY_RUN AWAITING_APPROVAL (local only; not queued, not published)",
           "topic": concept["topic"], "title_line": concept.get("title_line"), "on_screen_hook": concept.get("on_screen_hook"),
           "viral_source": gate["viral_item"], "brand": gate["brand"], "x_scan": info["x_scan"], "recreation": info["recreation"],
           "script": [x["say"] for x in concept["vo"]], "captions": info.get("captions"), "platform_captions": caps,
           "clip_checks": gate["clips"], "footage_sources": info.get("footage_sources"), "duration_s": info.get("duration_s"),
           "video": v, "video_16x9": info.get("landscape_path"),
           "note": "Held for Bret's approval. Nothing posts unless he explicitly approves; this dry run is never queued."}
    with open(os.path.join(d, "approval.json"), "w") as fh:
        json.dump(pkg, fh, indent=1, default=str)
    with open(os.path.join(d, "review.txt"), "w") as fh:
        fh.write(review_text(pkg))
    print("== rendered:", v, info.get("landscape_path"), "| package:", d)
    return 0


if __name__ == "__main__":
    sys.exit(main())
