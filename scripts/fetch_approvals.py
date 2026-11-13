#!/usr/bin/env python3
"""Download every Autopilot video waiting for Bret's approval (read-only; never approves or posts).

    python3 scripts/fetch_approvals.py [--base https://marketing-automation-production-dbd5.up.railway.app]
                                       [--out /workspace/marketing-approvals]

Writes <out>/<slot>/video.mp4 (+ video_16x9.mp4), approval.json and review.txt (topic, viral source with
time and engagement, script, captions, per-shot checks, approve / reject calls).
"""
import argparse
import json
import os
import re
import sys

import requests


def review_text(pkg: dict) -> str:
    v = pkg.get("viral_source") or {}
    lines = [f"Slot: {pkg.get('slot')}   Post: {pkg.get('post_id')}   Status: {pkg.get('status')}",
             f"Topic: {pkg.get('topic')}   Title: {pkg.get('title_line')}",
             f"Viral source: {v.get('title')} | {v.get('url')}",
             f"  uploaded {v.get('published_at')} ({v.get('age_hours_at_pick')} h old when picked), "
             f"{v.get('views')} views, {v.get('views_per_hour')} views/h, channel {v.get('author')}",
             "", "Script:"] + [f"  {i + 1}. {s}" for i, s in enumerate(pkg.get("script") or [])]
    lines += ["", "Per-shot checks:"]
    for c in pkg.get("clip_checks") or []:
        lines.append(f"  shot {c.get('shot')} [{c.get('start_s')}-{c.get('end_s')}s] {'OK ' if c.get('ok') else 'BAD'} "
                     f"{c.get('clip')} :: {c.get('reason')} :: narration {c.get('narration')}")
    lines += ["", f"Caption: {pkg.get('caption')}", "", f"Approve: {pkg.get('approve')}", f"Reject:  {pkg.get('reject')}",
              pkg.get("note") or ""]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://marketing-automation-production-dbd5.up.railway.app")
    ap.add_argument("--out", default="/workspace/marketing-approvals")
    a = ap.parse_args()
    pkgs = requests.get(f"{a.base}/api/approvals", params={"status": "AWAITING_APPROVAL"}, timeout=60).json()
    for pkg in pkgs:
        d = os.path.join(a.out, re.sub(r"[^A-Za-z0-9_.-]", "_", pkg.get("slot") or pkg.get("post_id") or "manual"))
        os.makedirs(d, exist_ok=True)
        for key, name in (("video_url", "video.mp4"), ("video_16x9_url", "video_16x9.mp4")):
            if pkg.get(key):
                with requests.get(pkg[key], stream=True, timeout=300) as r:
                    r.raise_for_status()
                    with open(os.path.join(d, name), "wb") as fh:
                        for chunk in r.iter_content(1 << 20):
                            fh.write(chunk)
        with open(os.path.join(d, "approval.json"), "w") as fh:
            json.dump(pkg, fh, indent=1)
        with open(os.path.join(d, "review.txt"), "w") as fh:
            fh.write(review_text(pkg))
        print(d)
    print(f"{len(pkgs)} package(s) awaiting approval", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
