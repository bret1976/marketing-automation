# Content Team (draft-only)

Narrow research, create, and track roles for Bret's X content, living inside the existing 6Frame cockpit. **Nothing in this pack can publish a post or send a reply.** Approve on a draft only marks it reviewed.

This app **never calls the X API**. Trending posts, competitor posts, bookmarks, quote-tweet candidates, and reply targets are pushed in by a Grok Bot routine (Grok has free X reads) through the ingest API.

## Schedules (America/Los_Angeles)

- **7:00 AM PT:** X Researcher (30 ideas), Analytics Analyst (last 24h PostProxy metrics), Competitor Watch, QT Scout
- **7:00 PM PT:** Bookmarks Brain, Chief of Staff end-of-day rollup

You can also run any role from the Content Team tab or `POST /api/content-team/run/{role}`.

## Auth for Grok routines

When `ADMIN_PASSWORD` is set, send header `X-Content-Team-Token` with the value of `CONTENT_TEAM_TOKEN` (or `CONTENT_TEAM_INGEST_TOKEN`). A normal admin session also works.

## Ingest payload

`POST /api/content-team/ingest`

```json
{
  "kind": "trending | competitor | bookmark | qt_candidate | reply_target",
  "source": "grok_routine",
  "items": [
    {
      "id": "optional-stable-id",
      "url": "https://x.com/user/status/…",
      "author": "handle",
      "author_name": "Display Name",
      "text": "post body",
      "posted_at": "2026-11-16T12:00:00-08:00",
      "metrics": {
        "likes": 0,
        "replies": 0,
        "reposts": 0,
        "quotes": 0,
        "bookmarks": 0,
        "impressions": 0,
        "followers": 0
      },
      "notes": "optional",
      "competitor": "handle (competitor only)",
      "list_name": "creators (competitor only)",
      "bookmarked_at": "ISO-8601 (bookmark only)",
      "why_qt": "optional (qt_candidate only)",
      "account_size": "large (reply_target only)",
      "why_reply": "optional (reply_target only)"
    }
  ]
}
```

Shorthand: `POST /api/content-team/ingest/{kind}` with `{ "items": [ ... ] }`.

Article Tracker: `POST /api/content-team/ideas` with `{ "text": "...", "source": "dashboard|api", "tags": [] }`.

## Report GETs (for a Grok routine to fetch)

- `/api/content-team/researcher`
- `/api/content-team/competitors`
- `/api/content-team/bookmarks`
- `/api/content-team/qt-scout`
- `/api/content-team/analytics`
- `/api/content-team/chief`
- `/api/content-team/sparring`
- `/api/content-team/writer`
- `/api/content-team/visuals`
- `/api/content-team/factcheck`
- `/api/content-team/ideas` (master list)
- `/api/content-team/repurposer`
- `/api/content-team/replies`
- `/api/content-team/sponsors`
- `/api/content-team/collabs`
- `/api/content-team/reports` (all)
- `/api/content-team/docs/ingest` (this payload, live)

Each report includes `llm_status`: `real` (live model or, for analytics, real stored metrics), `stub` (test provider), `fallback` (template used after an LLM failure or missing key), or `failed`.

## LLM provider

`CONTENT_TEAM_LLM=auto|xai|gemini|stub`. Auto uses `XAI_API_KEY` or `GROK_API_KEY` first, then `GEMINI_API_KEY`. Credit or network failures are caught and the role still writes a labeled fallback draft.

## Voice docs

Each role has an editable voice and do's/don'ts document on the volume (`STATE_DIR/content_team/voices.json`), also editable in the Voices tab. `PUT /api/content-team/voices/{role}`.
