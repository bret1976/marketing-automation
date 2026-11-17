"""Swappable LLM for Content Team.

Default path is Cloudflare Workers AI (free, 10k Neurons/day). Groq and
OpenRouter :free are optional fallbacks only when their keys are set.
Gemini and xAI are never auto-selected. 429 / quota errors, and a reached
daily cap, fall back to the labeled placeholder and never to a paid route.

CONTENT_TEAM_LLM=auto|openai|groq|cloudflare|openrouter|ollama|xai|gemini|stub|fallback
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger("content_team.llm")


class QuotaError(RuntimeError):
    """Raised on 429 / payment / neuron quota errors, or when the daily cap is hit.

    Callers must fall back to the placeholder and must not try a paid provider.
    """

STUB_MARKER = "CONTENT_TEAM_STUB"
# 60 short-to-medium calls of the default Cloudflare model stay under the
# Workers Free 10,000 Neurons/day hard stop, even at max_tokens=3072.
DEFAULT_DAILY_CAP = 60
# Auto never includes gemini (credits are paid/depleted) or xai (paid).
AUTO_PROVIDERS = ("cloudflare", "groq", "openrouter")
# Neurons per 1M tokens for the default Cloudflare model.
# Source: Workers AI pricing, 16 Nov 2026.
CF_DEFAULT_MODEL = "@cf/zai-org/glm-4.7-flash"
CF_NEURONS_PER_M_INPUT = 5500
CF_NEURONS_PER_M_OUTPUT = 36400
WORKERS_AI_FREE_NEURONS_PER_DAY = 10_000
# Enough for a 30-idea researcher JSON blob; still safe at DEFAULT_DAILY_CAP.
DEFAULT_MAX_TOKENS = 3072

KNOWN_PROVIDERS = (
    "openai",
    "groq",
    "cloudflare",
    "openrouter",
    "ollama",
    "xai",
    "gemini",
    "stub",
    "fallback",
    "auto",
)

DEFAULT_MODELS = {
    "groq": "openai/gpt-oss-20b",
    "cloudflare": CF_DEFAULT_MODEL,
    "openrouter": "openrouter/free",
    "openai": "local",
    "ollama": "llama3.2",
    "xai": "grok-4",
    "gemini": "gemini-2.5-flash",
}

DEFAULT_BASES = {
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "ollama": "http://127.0.0.1:11434/v1",
    "xai": "https://api.x.ai/v1",
}


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def truthy(name: str) -> bool:
    return _env(name).lower() in {"1", "true", "yes", "on"}


def xai_key() -> str:
    return _env("XAI_API_KEY") or _env("GROK_API_KEY")


def gemini_key() -> str:
    return _env("GEMINI_API_KEY") or _env("GOOGLE_API_KEY")


def groq_key() -> str:
    return _env("GROQ_API_KEY")


def openrouter_key() -> str:
    return _env("OPENROUTER_API_KEY")


def _account_from_base_url() -> str:
    base = openai_base_url()
    match = re.search(r"/accounts/([^/]+)/ai", base)
    return match.group(1) if match else ""


def cloudflare_account() -> str:
    return _env("CLOUDFLARE_ACCOUNT_ID") or _env("CF_ACCOUNT_ID") or _account_from_base_url()


def cloudflare_token() -> str:
    return (
        _env("CLOUDFLARE_API_TOKEN")
        or _env("CF_API_TOKEN")
        or _env("CLOUDFLARE_API_KEY")
        or (openai_api_key() if cloudflare_account() or "cloudflare.com" in openai_base_url() else "")
    )


def openai_base_url() -> str:
    return _env("CONTENT_TEAM_LLM_BASE_URL").rstrip("/")


def openai_api_key() -> str:
    return _env("CONTENT_TEAM_LLM_API_KEY")


def daily_cap() -> int:
    raw = _env("CONTENT_TEAM_LLM_DAILY_CAP")
    if raw == "":
        return DEFAULT_DAILY_CAP
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_DAILY_CAP


def configured_provider() -> str:
    forced = _env("CONTENT_TEAM_LLM").lower()
    if forced in KNOWN_PROVIDERS:
        return forced
    return "auto"


def _provider_ready(name: str) -> bool:
    if name == "openai":
        return bool(openai_base_url())
    if name == "groq":
        return bool(groq_key() or (openai_api_key() and "groq.com" in openai_base_url()))
    if name == "cloudflare":
        token = cloudflare_token()
        if cloudflare_account() and token:
            return True
        # CONTENT_TEAM_LLM_BASE_URL + CONTENT_TEAM_LLM_API_KEY can fully override.
        return bool(openai_base_url() and (token or openai_api_key()) and (
            "cloudflare.com" in openai_base_url() or configured_provider() == "cloudflare"
        ))
    if name == "openrouter":
        return bool(openrouter_key())
    if name == "ollama":
        return bool(openai_base_url() or _env("OLLAMA_HOST") or configured_provider() == "ollama")
    if name == "xai":
        return bool(xai_key())
    if name == "gemini":
        return bool(gemini_key())
    if name == "stub":
        return True
    return False


def provider_order() -> list[str]:
    """Ordered real providers to try. Gemini is never added in auto."""
    choice = configured_provider()
    if choice == "stub":
        return ["stub"]
    if choice == "fallback":
        return []
    if choice == "auto":
        raw = _env("CONTENT_TEAM_LLM_PROVIDERS")
        names = [p.strip().lower() for p in raw.split(",") if p.strip()] if raw else list(AUTO_PROVIDERS)
        # Safety: never silently add gemini to auto even if listed... actually if they
        # explicitly set CONTENT_TEAM_LLM_PROVIDERS=gemini we should honor it.
        # Default AUTO_PROVIDERS has no gemini. If they use default auto, skip gemini/xai
        # unless listed in CONTENT_TEAM_LLM_PROVIDERS.
        if not raw:
            names = [n for n in names if n not in {"gemini", "xai"}]
        return [n for n in names if n in KNOWN_PROVIDERS and n not in {"auto", "fallback"} and _provider_ready(n)]
    if choice in KNOWN_PROVIDERS:
        return [choice]
    return []


def provider_status() -> dict:
    choice = configured_provider()
    order = provider_order()
    usage = current_usage()
    return {
        "configured": choice,
        "active": order[0] if order else ("stub" if choice == "stub" else "fallback"),
        "order": order,
        "gemini_in_auto": False,
        "groq_key_present": bool(groq_key()),
        "cloudflare_ready": bool(cloudflare_account() and cloudflare_token()),
        "openrouter_key_present": bool(openrouter_key()),
        "openai_base_url_set": bool(openai_base_url()),
        "xai_key_present": bool(xai_key()),
        "gemini_key_present": bool(gemini_key()),
        "model": _model_for(order[0] if order else choice),
        "daily_cap": daily_cap(),
        "calls_today": usage.get("count", 0),
        "usage_date": usage.get("date"),
    }


def complete_json(prompt: str, *, fallback: Any, system: str = "", role: str = "") -> dict:
    """Ask the LLM for JSON. Always returns {ok, provider, llm_status, data, error, raw}."""
    order = provider_order()
    last_error = None
    for provider in order:
        try:
            if provider == "stub":
                raw = _stub_complete(prompt, fallback, role=role)
                data = _parse_json(raw, fallback)
                return _result(True, "stub", "stub", data, None, raw)
            _assert_under_daily_cap()
            raw = _complete_with(provider, prompt, system)
            data = _parse_json(raw, None)
            if data is None:
                raise ValueError(f"{provider} returned non-JSON")
            record_call(provider)
            return _result(True, provider, "real", data, None, raw)
        except QuotaError as exc:
            last_error = f"{provider}: {exc}"
            logger.warning("content_team LLM %s quota/rate-limit: %s", provider, exc)
            # Do not try another provider (including any paid route). Use the placeholder.
            break
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


def _utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _usage_path():
    from content_team import store

    return store.state_root() / "llm_usage.json"


def current_usage() -> dict:
    from content_team import store

    data = store.read_json(_usage_path(), {"date": _utc_date(), "count": 0, "by_provider": {}})
    if data.get("date") != _utc_date():
        return {"date": _utc_date(), "count": 0, "by_provider": {}}
    return data


def record_call(provider: str) -> dict:
    from content_team import store

    data = current_usage()
    data["date"] = _utc_date()
    data["count"] = int(data.get("count") or 0) + 1
    by_provider = dict(data.get("by_provider") or {})
    by_provider[provider] = int(by_provider.get(provider) or 0) + 1
    data["by_provider"] = by_provider
    store.write_json(_usage_path(), data)
    return data


def _assert_under_daily_cap() -> None:
    cap = daily_cap()
    used = int(current_usage().get("count") or 0)
    if used >= cap:
        raise QuotaError(f"daily LLM cap reached ({used}/{cap})")


def _model_for(provider: str) -> str:
    override = _env("CONTENT_TEAM_LLM_MODEL")
    if override:
        return override
    if provider == "gemini":
        return _env("CONTENT_TEAM_GEMINI_MODEL") or DEFAULT_MODELS["gemini"]
    if provider == "xai":
        return _env("XAI_MODEL") or DEFAULT_MODELS["xai"]
    return DEFAULT_MODELS.get(provider, "local")


def _complete_with(provider: str, prompt: str, system: str) -> str:
    if provider in {"openai", "groq", "cloudflare", "openrouter", "ollama", "xai"}:
        return _openai_complete(provider, prompt, system)
    if provider == "gemini":
        return _gemini_complete(prompt, system)
    raise RuntimeError(f"unknown provider {provider}")


def _chat_url(base: str) -> str:
    base = (base or "").rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def _base_and_key(provider: str) -> tuple[str, str]:
    if provider == "openai":
        base = openai_base_url()
        if not base:
            raise RuntimeError("CONTENT_TEAM_LLM_BASE_URL is not set")
        return base, openai_api_key()
    if provider == "groq":
        base = openai_base_url() if "groq.com" in openai_base_url() else DEFAULT_BASES["groq"]
        key = groq_key() or openai_api_key()
        if not key:
            raise RuntimeError("GROQ_API_KEY is not set")
        return base, key
    if provider == "cloudflare":
        account = cloudflare_account()
        token = cloudflare_token() or openai_api_key()
        override = openai_base_url()
        if override:
            base = override
        elif account:
            base = f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/v1"
        else:
            raise RuntimeError("CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN are not set")
        if not token:
            raise RuntimeError("CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN are not set")
        return base, token
    if provider == "openrouter":
        key = openrouter_key() or openai_api_key()
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY is not set")
        base = openai_base_url() if "openrouter.ai" in openai_base_url() else DEFAULT_BASES["openrouter"]
        return base, key
    if provider == "ollama":
        host = _env("OLLAMA_HOST").rstrip("/")
        if openai_base_url():
            base = openai_base_url()
        elif host:
            base = host if host.endswith("/v1") else host + "/v1"
        else:
            base = DEFAULT_BASES["ollama"]
        return base, openai_api_key() or "ollama"
    if provider == "xai":
        key = xai_key()
        if not key:
            raise RuntimeError("XAI_API_KEY / GROK_API_KEY is not set")
        return _env("XAI_API_BASE") or DEFAULT_BASES["xai"], key
    raise RuntimeError(f"no OpenAI-compatible config for {provider}")


def _is_quota_status(status: int, body: str) -> bool:
    """True for rate-limit, credit, or Workers AI neuron-quota failures."""
    if status in {402, 429}:
        return True
    lower = (body or "").lower()
    needles = (
        "quota",
        "rate limit",
        "rate_limit",
        "too many requests",
        "neuron",
        "out of capacity",
        "exceeded your",
        "insufficient credits",
        "credit",
        "payment required",
        "upgrade to workers paid",
        "error 5035",
        "3040",
    )
    return any(n in lower for n in needles)


def _openai_complete(provider: str, prompt: str, system: str) -> str:
    import requests

    base, key = _base_and_key(provider)
    model = _model_for(provider)
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt + "\n\nReturn only valid JSON."})
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if provider == "openrouter":
        headers["HTTP-Referer"] = _env("PUBLIC_BASE_URL") or "https://6framestudio.com"
        headers["X-Title"] = "6Frame Content Team"
    # Never let OpenRouter fall through to a paid model.
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.4,
        "max_tokens": DEFAULT_MAX_TOKENS,
    }
    timeout = 90 if provider in {"openai", "ollama"} else 60
    url = _chat_url(base)
    host = urlparse(url).hostname or ""
    logger.info("content_team LLM calling %s at %s model=%s", provider, host, model)
    res = requests.post(url, headers=headers, json=payload, timeout=timeout)
    if res.status_code >= 400:
        snippet = res.text[:300]
        if _is_quota_status(res.status_code, snippet):
            raise QuotaError(f"{provider} HTTP {res.status_code}: {snippet}")
        raise RuntimeError(f"{provider} HTTP {res.status_code}: {snippet}")
    data = res.json()
    # Cloudflare and OpenAI both use choices[0].message.content
    content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    return content or ""


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


def _gemini_complete(prompt: str, system: str) -> str:
    """Explicit opt-in only (CONTENT_TEAM_LLM=gemini). Not used in auto."""
    key = gemini_key()
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    from google import genai
    from google.genai import types

    model = _model_for("gemini")
    client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=60000))
    contents = prompt + "\n\nReturn only valid JSON."
    if system:
        contents = system + "\n\n" + contents
    response = client.models.generate_content(model=model, contents=contents)
    return getattr(response, "text", None) or ""
