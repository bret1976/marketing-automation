#!/usr/bin/env bash
set -euo pipefail

PORT="${PORT:-8001}"

exec railway run env \
  AUTONOMOUS_POSTING=false \
  MOCK_MODE=true \
  REQUIRE_AUTOPILOT_APPROVAL=true \
  DISABLE_BACKGROUND_SCHEDULER=true \
  AUTONOMOUS_VIDEO_ENGINE=google_veo_lite \
  AUTONOMOUS_VIDEO_DURATION=8 \
  PUBLIC_BASE_URL="http://127.0.0.1:${PORT}" \
  .venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port "${PORT}" --reload
