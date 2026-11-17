"""Swappable LLM for Content Team: xAI Grok, Gemini, stub, or local fallback.

Never invent a paid provider. Failures return a structured error and the
caller may use a template fallback. CONTENT_TEAM_LLM=auto|xai|gemini|stub
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

logger = logging.getLogger("content_team.llm")

STUB_MARKER = "CONTENT_TEAM_STUB"


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def truthy(name: str) -> bool:
    return _env(name).lower() in {"1", "true", "yes", "on"}


def xai_key() -> str:
    return _env("XAI_API_KEY") or _env("GROK_API_KEY")


def gemini_key() -> str:
    return _env("GEMINI_API_KEY") or _env("GOOGLE_API_KEY")


def configured_provider() -> str:
    forced = _env("CONTENT_TEAM_LLM").lower()
    if forced in {"stub", "xai", "gemini", "fallback", "auto"}:
        return forced
    if xai_key():
        return "xai"
    if gemini_key():
        return "gemini"
    return "fallback"


def provider_status() -> dict:
    choice = configured_provider()
    return {
        "configured": choice,
        "xai_key_present": bool(xai_key()),
        "gemini_key_present": bool(gemini_key()),
        "model_xai": _env("XAI_MODEL") or "grok-4",
        "model_gemini": _env("CONTENT_TEAM_GEMINI_MODEL") or "gemini-2.5-flash",
    }


def complete_json(prompt: str, *, fallback: Any, system: str = "", role: str = "") -> dict:
    """Ask the LLM for JSON. Always returns {ok, provider, llm_status, data, error, raw}."""
    choice = configured_provider()
    order: list[str] = []
    if choice == "stub":
        order = ["stub"]
    elif choice == "fallback":
        order = []
    elif choice == "xai":
        order = ["xai"]
    elif choice == "gemini":
        order = ["gemini"]
    else:  # auto
        if xai_key():
            order.append("xai")
        if gemini_key():
            order.append("gemini")

    last_error = None
    for provider in order:
        try:
            if provider == "stub":
                raw = _stub_complete(prompt, fallback, role=role)
                data = _parse_json(raw, fallback)
                return _result(True, "stub", "stub", data, None, raw)
            if provider == "xai":
                raw = _xai_complete(prompt, system)
                data = _parse_json(raw, None)
                if data is None:
                    raise ValueError("xAI returned non-JSON")
                return _result(True, "xai", "real", data, None, raw)
            if provider == "gemini":
                raw = _gemini_complete(prompt, system)
                data = _parse_json(raw, None)
                if data is None:
                    raise ValueError("Gemini returned non-JSON")
                return _result(True, "gemini", "real", data, None, raw)
        except Exception as exc:  # noqa: BLE001 — caller needs a graceful fallback
            last_error = f"{provider}: {exc}"
            logger.warning("content_team LLM %s failed: %s", provider, exc)

    return _result(False, "fallback", "fallback", fallback, last_error or "no LLM provider configured", "")


def _result(ok: bool, provider: str, llm_status: str, data: Any, error: str | None, raw: str) -> dict:
    return {
        "ok": ok,
        "provider": provider,
        "llm_status": llm_status,
        "data": data,
        "error": error,
        "raw": raw[:4000] if raw else "",
    }


def _parse_json(text: str, fallback: Any) -> Any:
    if not text:
        return fallback
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass
        start = text.find("[")
        end = text.rfind("]")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass
    return fallback


def _stub_complete(prompt: str, fallback: Any, role: str = "") -> str:
    """Deterministic JSON used in tests. Shape follows role or a prompt hint."""
    lower = prompt.lower()
    if not role:
        if "30 content ideas" in lower or "x researcher" in lower:
            role = "researcher"
        elif "competitor" in lower and "digest" in lower:
            role = "competitors"
        elif "bookmark" in lower:
            role = "bookmarks"
        elif "quote-tweet" in lower or "quote tweet" in lower or "qt scout" in lower:
            role = "qt_scout"
        elif "titles and packaging" in lower or "sparring" in lower:
            role = "sparring"
        elif "x writer" in lower:
            role = "writer"
        elif "image prompt" in lower or "banner" in lower:
            role = "visuals"
        elif "fact check" in lower or "claims" in lower:
            role = "factcheck"
        elif "linkedin" in lower and "newsletter" in lower:
            role = "repurposer"
        elif "reply draft" in lower or "reply scout" in lower:
            role = "reply_scout"
        elif "sponsor" in lower:
            role = "sponsors"
        elif "collab" in lower:
            role = "collabs"
        elif "end-of-day" in lower or "chief of staff" in lower:
            role = "chief"
        else:
            role = "generic"

    if role == "researcher":
        ideas = [{"title": f"Stub idea {i}", "hook": f"Hook {i}", "angle": "cinematic craft", "why": STUB_MARKER, "source_id": ""} for i in range(1, 31)]
        return json.dumps({"ideas": ideas})
    if role == "competitors":
        return json.dumps({"summary": "Stub competitor digest. " + STUB_MARKER, "highlights": [{"author": "stub", "takeaway": "They posted a cinematic breakdown.", "url": ""}]})
    if role == "bookmarks":
        return json.dumps({"summary": "Stub 7 PM bookmark summary. " + STUB_MARKER, "themes": ["craft", "hooks"], "keep": []})
    if role == "qt_scout":
        cands = [{"source_id": "", "author": "stub", "quote": f"Stub QT {i}. {STUB_MARKER}", "why": "adds craft context"} for i in range(1, 4)]
        return json.dumps({"candidates": cands})
    if role == "sparring":
        return json.dumps({"titles": [f"Stub title {i}" for i in range(1, 6)], "packaging": {"hook": "Stub hook", "visual": "macro close-up", "cta": "watch the process"}, "marker": STUB_MARKER})
    if role == "writer":
        return json.dumps({"post": "Stub X post in 6Frame voice. " + STUB_MARKER, "thread": ["Stub tweet 1", "Stub tweet 2"]})
    if role == "visuals":
        return json.dumps({"prompts": [{"title": "key art", "prompt": "Cinematic still, anamorphic, stub " + STUB_MARKER}], "negative": "no watermarks, no logos"})
    if role == "factcheck":
        return json.dumps({"claims": [{"claim": "stub claim", "verdict": "unverified", "sources": [], "notes": STUB_MARKER}]})
    if role == "repurposer":
        return json.dumps({"linkedin": "Stub LinkedIn version. " + STUB_MARKER, "newsletter": "Stub newsletter blurb. " + STUB_MARKER})
    if role == "reply_scout":
        return json.dumps({"replies": [{"source_id": "", "author": "stub", "reply": "Stub draft reply. " + STUB_MARKER}]})
    if role == "sponsors":
        return json.dumps({"leads": [{"name": "Stub brand", "why": STUB_MARKER, "url": ""}]})
    if role == "collabs":
        return json.dumps({"leads": [{"name": "Stub creator", "why": STUB_MARKER, "url": ""}]})
    if role == "chief":
        return json.dumps({"summary": "Stub end-of-day rollup. " + STUB_MARKER, "highlights": [], "needs_review": 0})
    return json.dumps(fallback if fallback is not None else {"ok": True, "marker": STUB_MARKER})


def _xai_complete(prompt: str, system: str) -> str:
    key = xai_key()
    if not key:
        raise RuntimeError("XAI_API_KEY / GROK_API_KEY is not set")
    import requests

    model = _env("XAI_MODEL") or "grok-4"
    url = _env("XAI_API_BASE") or "https://api.x.ai/v1/chat/completions"
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt + "\n\nReturn only valid JSON."})
    res = requests.post(
        url,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "messages": messages, "temperature": 0.4},
        timeout=60,
    )
    if res.status_code >= 400:
        raise RuntimeError(f"xAI HTTP {res.status_code}: {res.text[:300]}")
    data = res.json()
    return ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""


def _gemini_complete(prompt: str, system: str) -> str:
    key = gemini_key()
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    from google import genai
    from google.genai import types

    model = _env("CONTENT_TEAM_GEMINI_MODEL") or "gemini-2.5-flash"
    client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=60000))
    contents = prompt + "\n\nReturn only valid JSON."
    if system:
        contents = system + "\n\n" + contents
    response = client.models.generate_content(model=model, contents=contents)
    return getattr(response, "text", None) or ""
