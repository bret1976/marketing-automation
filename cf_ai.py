"""Free Cloudflare Workers AI helpers for footage mode (text JSON, embeddings, vision).

Same account/token env as the Content Team writer (CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN).
Free plan only: every call is counted against a per-process budget, and a 429/quota answer raises
CFQuota (callers then skip the step; nothing ever falls through to a paid route, Gemini or xAI).
"""
import base64
import json
import logging
import os
import re
import threading
import time
from typing import Any, List, Optional

import requests

logger = logging.getLogger("cf_ai")

TEXT_MODEL = os.environ.get("FOOTAGE_TEXT_MODEL", "@cf/zai-org/glm-4.7-flash")
TEXT_FALLBACK = os.environ.get("FOOTAGE_TEXT_FALLBACK", "@cf/mistralai/mistral-small-3.1-24b-instruct")
VISION_MODEL = os.environ.get("FOOTAGE_VISION_MODEL", "@cf/meta/llama-4-scout-17b-16e-instruct")
VISION_FALLBACK = os.environ.get("FOOTAGE_VISION_FALLBACK", "@cf/mistralai/mistral-small-3.1-24b-instruct")
EMBED_MODEL = "@cf/baai/bge-base-en-v1.5"

_LOCK = threading.Lock()
USAGE = {"text": 0, "vision": 0, "embed": 0, "neurons": 0.0}
MAX_NEURONS = float(os.environ.get("FOOTAGE_MAX_NEURONS", "4500"))  # per run; the free plan has 10k/day for everything
MAX_CALLS = {"text": int(os.environ.get("FOOTAGE_MAX_TEXT_CALLS", "40")),
             "vision": int(os.environ.get("FOOTAGE_MAX_VISION_CALLS", "80")),
             "embed": int(os.environ.get("FOOTAGE_MAX_EMBED_CALLS", "60"))}


class CFQuota(RuntimeError):
    pass


def _creds():
    try:
        from content_team import llm
        return llm.cloudflare_account(), llm.cloudflare_token()
    except Exception:  # noqa: BLE001
        return (os.environ.get("CLOUDFLARE_ACCOUNT_ID") or "").strip(), (os.environ.get("CLOUDFLARE_API_TOKEN") or "").strip()


def ready() -> bool:
    a, t = _creds()
    return bool(a and t)


def _count(kind: str) -> None:
    with _LOCK:
        if USAGE["neurons"] >= MAX_NEURONS:
            raise CFQuota(f"footage neuron budget ({MAX_NEURONS:.0f}) used up for this run")
        if USAGE[kind] >= MAX_CALLS[kind]:
            raise CFQuota(f"footage {kind} call budget ({MAX_CALLS[kind]}) used up for this run")
        USAGE[kind] += 1


def reset_usage() -> None:
    with _LOCK:
        for k in USAGE:
            USAGE[k] = 0.0 if k == "neurons" else 0


def run(model: str, payload: dict, timeout: int = 90) -> dict:
    account, token = _creds()
    if not (account and token):
        raise RuntimeError("Cloudflare Workers AI is not configured")
    url = f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{model}"
    res = requests.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload, timeout=timeout)
    if res.status_code == 429 or "neuron" in res.text[:400].lower() and res.status_code >= 400:
        raise CFQuota(f"{model} HTTP {res.status_code}: {res.text[:200]}")
    if res.status_code >= 400:
        raise RuntimeError(f"{model} HTTP {res.status_code}: {res.text[:200]}")
    result = res.json().get("result") or {}
    try:
        n = float(((result.get("usage") or {}).get("neurons")) or 0)
    except (TypeError, ValueError, AttributeError):
        n = 0.0
    with _LOCK:
        USAGE["neurons"] = round(USAGE["neurons"] + n, 2)
    return result


def _content(result: dict) -> str:
    if isinstance(result.get("response"), str):
        return result["response"]
    msg = ((result.get("choices") or [{}])[0].get("message") or {})
    content = msg.get("content") or ""
    if isinstance(content, list):
        content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return content or ""


def parse_json(text: str) -> Any:
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = text.find(opener), text.rfind(closer)
        if i != -1 and j > i:
            try:
                return json.loads(text[i:j + 1])
            except ValueError:
                continue
    return None


def chat_json(prompt: str, system: str = "", max_tokens: int = 2500, timeout: int = 120) -> Optional[Any]:
    """JSON from the free text model (GLM 4.7 Flash), then the free fallback model. None if both fail."""
    msgs = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": prompt + "\n\nReturn only valid JSON, no commentary."}]
    for model in (TEXT_MODEL, TEXT_FALLBACK):
        if not model:
            continue
        try:
            _count("text")
            t0 = time.time()
            out = _content(run(model, {"messages": msgs, "max_tokens": max_tokens, "temperature": 0.2}, timeout=timeout))
            data = parse_json(out)
            logger.info("cf_ai text %s %.1fs json=%s", model, time.time() - t0, data is not None)
            if data is not None:
                return data
        except CFQuota:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("cf_ai text %s failed: %s", model, str(exc)[:200])
    return None


def embed(texts: List[str]) -> List[List[float]]:
    out: List[List[float]] = []
    for i in range(0, len(texts), 90):
        _count("embed")
        res = run(EMBED_MODEL, {"text": [t[:2000] or "-" for t in texts[i:i + 90]]}, timeout=60)
        out += res.get("data") or []
    return out


def cosine(a: List[float], b: List[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    da = sum(x * x for x in a) ** 0.5
    db = sum(y * y for y in b) ** 0.5
    return float(num / (da * db)) if da and db else 0.0


def vision_json(jpeg: bytes, prompt: str, max_tokens: int = 300) -> Optional[dict]:
    """Ask a free vision model about one JPEG; returns parsed JSON (or None)."""
    url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
    for model in (VISION_MODEL, VISION_FALLBACK):
        if not model:
            continue
        try:
            _count("vision")
            out = _content(run(model, {"messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt + "\nAnswer with JSON only."},
                {"type": "image_url", "image_url": {"url": url}}]}], "max_tokens": max_tokens, "temperature": 0.0}, timeout=90))
            data = parse_json(out)
            if isinstance(data, dict):
                data["_model"] = model
                return data
        except CFQuota:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("cf_ai vision %s failed: %s", model, str(exc)[:200])
    return None
