# 6Frame Studio - Multi-Agent Social Marketing Hub

This is an in-house, standalone multi-agent orchestration tool and dashboard designed specifically for **6Frame Studio** to automate social media campaigns for visual assets. It allows uploading engineered video clips, extracting visual context via a multimodal Gemini 2.5 Pro agent, and generating optimized social copies (for LinkedIn, X/Twitter, and Instagram) with specialized Gemini 2.5 Flash agents.

## Features
- **Multimodal Video Analysis**: Ingests heavy MP4 files directly and uses Gemini 2.5 Pro to reason about frames, pacing, aesthetics, and themes.
- **Specialized Copywriting Agents**: Runs platform-specific prompts tailored to the B2B landscape of LinkedIn, viral hooks for X/Twitter, and aesthetic styles for Instagram.
- **Glassmorphic Local Dashboard**: Built with a sleek dark-themed, premium interface for dragging-and-dropping videos, watching live orchestration steps, reviewing staged content, and modifying copy before publishing.
- **Direct Platform API Integration**: Publishes staged posts to X (single tweets or threads) using `tweepy` and LinkedIn using `requests` REST API.
- **Mock Publish Sandbox Mode**: Allows sandbox execution and copy generation without needing live Twitter or LinkedIn API keys.

---

## Setup & Running Locally

Since the python dependencies have already been compiled and installed in your project's local virtual environment, you can run the server directly.

### 1. Set Workspace Folder
We recommend setting this directory as your active workspace folder in the Antigravity IDE:
`/Users/majid/.gemini/antigravity/scratch/marketing-automation`

### 2. Start the Server
Run the FastAPI development server from your terminal inside the project directory:
```bash
.venv/bin/python3.11 -m uvicorn main:app --reload --port 8000
```

### 3. Open the Dashboard
Navigate to your web browser:
[http://127.0.0.1:8000](http://127.0.0.1:8000)

---

## How to Use the Pipeline

1. **Configure API Keys**:
   - Click the **Settings** gear icon in the top right.
   - Enter your **Gemini API Key** (from Google AI Studio).
   - Enter your brand voice instructions (e.g. telling the agents to focus on Runway, Sora, Kling, or custom artistic style guides).
   - Enter Twitter and LinkedIn developer keys if you want to publish live. Leave **Enable Mock Publish Mode** checked to run in copy-paste mode without keys.
   - Click **Save Changes**.

2. **Run the Agents**:
   - Drag and drop your cinematic `.mp4` video into the drop zone.
   - Input your landing page or website URL (default: `https://6framestudio.com/`).
   - Click **Generate Social Campaigns**.
   - Watch the live orchestration status:
     - *Step 1*: Visual Asset uploading to the Gemini File API.
     - *Step 2*: Context Agent (Gemini 2.5 Pro) conducting deep multimodal analysis.
     - *Step 3*: Copywriting Agents (Gemini 2.5 Flash) writing copy in parallel.
     - *Step 4*: Social posts staged in the review dashboard.

3. **Review & Publish**:
   - Navigate through the LinkedIn, Twitter, and Instagram tabs to read the staged posts.
   - Edit the copy directly in the textboxes. The Twitter Thread view features active character counters for each tweet (highlighting red if you exceed X's 280-character limit).
   - Click **Publish to LinkedIn** or **Publish Thread to X** to execute the posting, or copy them to your clipboard.

## Autopilot Watch (autopilot-watch-v1, backend-only)

Read-only dead-man's switch for the scheduled Autopilot slots (`AUTONOMOUS_HOURS`, UTC).
It compares expected slots against the slot ledger the live overlay writes
(`STATE_DIR/autonomous_slot.json`) and reports `ok` / `late` (1 missed) / `down` (2+ missed in a row),
plus classified upstream issues (Gemini prepaid credits empty, YouTube bot wall, TikTok discovery empty,
Facebook page_id missing, ...) from a passive log tap and the scheduled-posts log.

- `GET /health` → `packs.autopilot_watch` + compact `autopilot_watch` block
- `GET /api/autopilot-watch/summary` → full slot table, issues with hints, next slot (UTC + PT)
- Env: `AUTOPILOT_WATCH=0` (kill switch), `AUTOPILOT_WATCH_GRACE_MIN` (default 75), `AUTOPILOT_WATCH_LOOKBACK_HOURS` (default 72)
- Never posts, schedules, retries, or calls outside services.

Pattern credit (no code copied): healthchecks/healthchecks (BSD-3-Clause) OK/LATE/DOWN + grace;
jjdoor/deadcron (MIT) period + grace overdue and consecutive misses.

## Spend Guard (spend-guard-v1, backend-only)

The hub runs without a login wall, so the routes that spend money or post publicly are capped
("denial-of-wallet" backstop). Each guarded route class gets a per-client limit per hour and a
global per-UTC-day ceiling (persisted in `STATE_DIR/spend_guard_ledger.json`). Requests are
counted when they start and refunded if the route answers with an error, so only accepted work
uses budget. Over the limit → HTTP 429 + `Retry-After`; the route never runs (no provider call,
render, or post). Scheduled Autopilot slots run in-process and are never counted or blocked.

| Class | Routes | Per client / hour | Global / UTC day |
|-------|--------|-------------------|------------------|
| `video_gen` | generate-video, generate-video-variants, growth-os/multiscene-video, apply-template | 6 | 40 |
| `ai_call` | analyze, viral-search, virality-score, repurpose-video-link, load-original-video, growth-os campaign-plan/live-refresh/report, analytics/refresh, engagement-queue/refresh | 40 | 400 |
| `autopilot` | trigger-autopilot | 4 | 16 |
| `publish` | publish/twitter, publish/linkedin, postproxy/test-publish, approval-queue/{id}/approve, engagement-queue/{id}/send | 30 | 150 |
| `upload` | upload | 30 | 200 |

- `GET /health` → `packs.spend_guard` + compact usage; `GET /api/spend-guard/summary` → caps, usage, recent blocks (IPs masked)
- Env: `SPEND_GUARD=0` (kill switch), `SPEND_GUARD_WINDOW_SEC` (3600), `SPEND_GUARD_<CLASS>_PER_CLIENT`,
  `SPEND_GUARD_<CLASS>_DAILY` (0 = no cap), optional `SPEND_GUARD_BYPASS_TOKEN` (header `X-Spend-Guard-Bypass`)
- Client key: `X-Real-IP` → first `X-Forwarded-For` hop → socket peer. Headers can be forged, so the
  global daily ceiling is the hard backstop.

Pattern credit (no code copied): laurentS/slowapi (MIT) per-route limits + 429/Retry-After;
m24927605/agentic-spendguard (Apache-2.0) reserve-before-call / refund-after;
jonahberg/the-blue-board PR #235 window + daily cap with 0 = kill switch; OWASP LLM10 Unbounded Consumption.

## X safe mode (x-safe-v1, backend-only)

`x_policy.py` is the single gate for every X publish (PostProxy and direct tweepy). Added 2026-10-16
after @6framestudios was spam-filtered. Other networks are unchanged.

- No automated X replies/comments: mention-reply drafting for X is off and the engagement-queue
  X send returns 403. `X_REPLIES_ENABLED=1` re-enables.
- No X threads (self-reply chains); one post per publish.
- At most 2 native-video X posts per rolling 24h, at least 4h apart (`X_MAX_POSTS_PER_DAY` /
  `X_MIN_GAP_HOURS` can only tighten). X is queued as its own post at a varied minute (7-53 min jitter),
  never in the same second as the slot. A held X post is rescheduled, not failed.
- X caption: spectacle hook on line 1, <= 280 chars, 0-1 hashtag, no links, video required.
- Never posts the same caption or the same video to X twice (`STATE_DIR/x_post_ledger.json`,
  seeded from earlier X posts on first boot), so delete-and-repost and retry double-posts are refused.
- `/health` -> `x_policy` shows the live settings and next allowed X time; boot logs `X_POLICY ...`.
- Kill switch: `X_SAFE_MODE=0` restores the old X behaviour.
