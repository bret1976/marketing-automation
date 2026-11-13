# X viral scan (Bret, 2026-11-13, t752u)

> "I want the viral scan to now do the search like it just did on X within the last 24 hours and then take the top 10
> viral videos and decide which would be the easiest to recreate and then that's the video i want you to recreate.
> Only the videos, do not include the prompt-sharing ones."

Autopilot in footage mode (`VIDEO_ENGINE=footage`) now uses `x_viral_scan.py` instead of the YouTube/open finder
(`VIRAL_SCAN=youtube` is the only way back to the old scan).

1. **Scan**: X posts with a native video, created in the last 24 h, in the AI-video / AI-film genre. Genre
   evidence is an X News story about an AI video that clusters the post, or AI-video wording in the post.
   Dropped (and logged): prompt-sharing posts, prompt guides, tutorials, tool reviews / ads, photos / text posts.
2. **Rank**: by views (`impression_count`); the same clip re-shared by other accounts counts once; top 10 kept with
   url, author, created_at, views, likes, video duration.
3. **Score** (logged per item): duration (<=15 s 3, <=35 s 2, <=60 s 1), single scene 2, few characters 2,
   a $0 recipe exists 3. Real identifiable people / celebrity likeness, copyrighted show / film / game / brand IP
   and artists' music videos are hard-blocked (score 0). Text-only checks: Bret's approval is the real check.
4. **Pick**: highest score among recreatable items (ties: more views). None -> slot skipped with the reason.
5. **Recreate**: `x_recreate_recipes.py` turns the pick into an original short made by the free footage pipeline
   (public-domain / CC footage and photos, local Kokoro voice, ffmpeg). It is NOT AI-generated: the app has no free AI
   video generator (Gemini Veo/Omni, FAL, Runway are paid and blocked by the $0 rule).
6. **Hold**: every video is `AWAITING_APPROVAL`; the package (`GET /api/approvals`) adds `x_scan` (top 10 with scores,
   rubric, pick) and `recreation`. Only `POST /api/approval-queue/{id}/approve` publishes.

## Reading X

* `X_BEARER_TOKEN` (X API v2 app token) -> live scan (news search + post lookup + recent search). **Not set on the live
  app** and UNVERIFIED end-to-end.
* Otherwise a fresh snapshot of the same X API v2 responses: `X_SCAN_SNAPSHOT=<path>` or
  `POST /api/x-scan/snapshot` (needs `X_SCAN_INGEST_TOKEN` set and sent as `X-Scan-Token`). Older than
  `X_SNAPSHOT_MAX_AGE_HOURS` (6) -> refused.
* Neither -> the slot is skipped: "X scan unavailable: the app has no X API credential ...".

`GET /api/x-scan/preview` runs the scan read-only. `scripts/x_scan_dry_run.py --snapshot FILE [--render]` does the
same locally and renders the approval package into `/workspace/marketing-approvals/<slot>/`.
