# Drop-in static/ package for marketing-automation

This folder is a normal, unbundled set of static files — no base64 packing, no client-side unpacking step. It's meant to **replace the contents of `static/` in your `marketing-automation` repo**.

## Files
- `index.html` — the frontend (served at `/` by `main.py`'s `read_root()`)
- `styles.css` — global stylesheet entry point (imports `tokens/*.css`)
- `tokens/` — color, typography, spacing/radius/motion CSS custom properties
- `_ds_bundle.js` — compiled component bundle (Button, GlassCard, Tabs, Input, Select, Switch, Checkbox, Badge, TrendCard, DropZone, ProgressStepper, Spinner, etc. — everything `index.html`'s `window.TrendPilotAIDesignSystem_1ab598` namespace uses)

React, ReactDOM, and Babel are loaded from unpkg CDN in `index.html` — no separate files needed for those, and no build step required (Babel transpiles the inline JSX in the browser at load time).

## Install

1. In your `marketing-automation` repo, delete (or back up) the existing `static/` folder contents.
2. Copy everything in this folder into `static/` (so you end up with `static/index.html`, `static/styles.css`, `static/tokens/`, `static/_ds_bundle.js`).
3. Commit and push. Railway will auto-redeploy.
4. Visit your Railway URL — `main.py` already mounts `/static` and serves `static/index.html` at `/`, so this is a drop-in replacement, same origin as the API, no CORS config needed.

## What this frontend does

Every screen calls the real API — no mock data. See `ui_kits/live-integration/README.md` in the design system project for the full endpoint map (which screen calls which `POST`/`GET` route, and the exact response shape each expects). The short version:
- Upload & Analyze → `/api/upload`, `/api/analyze`, `/api/status/{job_id}`
- Viral Trends → `/api/viral-search`, `/api/viral-status/{job_id}`
- Repurposer → `/api/repurpose-video-link`, `/api/load-original-video`, `/api/video-status/{job_id}`
- Cockpit → `/api/settings`, `/api/scheduled-queue`, `/api/trigger-autopilot`
- Publish → `/api/publish/twitter`, `/api/publish/linkedin`

## Still true from before

This backend is single-tenant (one shared settings file, no user accounts). Fine to run solo; needs a real auth + database layer before you can sell it to multiple customers.
