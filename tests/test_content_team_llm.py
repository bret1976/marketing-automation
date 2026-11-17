import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

os.environ.setdefault("DISABLE_BACKGROUND_SCHEDULER", "true")
os.environ.setdefault("SPEND_GUARD", "0")

from content_team import llm


class _FakeOpenAIHandler(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, fmt, *args):  # noqa: A003
        return

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        _FakeOpenAIHandler.calls.append({"path": self.path, "body": body.decode(), "auth": self.headers.get("Authorization")})
        if self.path.endswith("/chat/completions"):
            payload = {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": json.dumps({"post": "A real-shaped draft from the fake server.", "thread": ["one"]}),
                    }
                }]
            }
            raw = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        self.send_response(404)
        self.end_headers()


def _serve():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenAIHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_auto_ignores_gemini_and_xai_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_TEAM_STATE_DIR", str(tmp_path / "ct"))
    monkeypatch.delenv("CONTENT_TEAM_LLM", raising=False)
    monkeypatch.delenv("CONTENT_TEAM_LLM_PROVIDERS", raising=False)
    monkeypatch.delenv("CONTENT_TEAM_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    monkeypatch.delenv("CF_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("CF_API_TOKEN", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "should-not-be-used")
    monkeypatch.setenv("XAI_API_KEY", "should-not-be-used")
    monkeypatch.setenv("GROK_API_KEY", "should-not-be-used")
    assert llm.configured_provider() == "auto"
    assert "gemini" not in llm.provider_order()
    assert "xai" not in llm.provider_order()
    status = llm.provider_status()
    assert status["gemini_in_auto"] is False
    assert status["active"] == "fallback"
    result = llm.complete_json("Write 30 content ideas", fallback={"ideas": []}, role="researcher")
    assert result["llm_status"] == "fallback"
    assert result["provider"] == "fallback"


def test_openai_compatible_client_is_real(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_TEAM_STATE_DIR", str(tmp_path / "ct"))
    server = _serve()
    host, port = server.server_address
    _FakeOpenAIHandler.calls.clear()
    monkeypatch.setenv("CONTENT_TEAM_LLM", "openai")
    monkeypatch.setenv("CONTENT_TEAM_LLM_BASE_URL", f"http://127.0.0.1:{port}/v1")
    monkeypatch.setenv("CONTENT_TEAM_LLM_API_KEY", "test-key")
    monkeypatch.setenv("CONTENT_TEAM_LLM_MODEL", "test-model")
    monkeypatch.setenv("CONTENT_TEAM_LLM_DAILY_CAP", "10")
    result = llm.complete_json("Write an X post about a ship in a bottle.", fallback={"post": "nope"}, role="writer")
    server.shutdown()
    assert result["ok"] is True
    assert result["provider"] == "openai"
    assert result["llm_status"] == "real"
    assert result["data"]["post"].startswith("A real-shaped draft")
    assert _FakeOpenAIHandler.calls, "server was not called"
    assert _FakeOpenAIHandler.calls[0]["auth"] == "Bearer test-key"
    assert llm.current_usage()["count"] == 1


def test_daily_cap_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_TEAM_STATE_DIR", str(tmp_path / "ct"))
    server = _serve()
    host, port = server.server_address
    monkeypatch.setenv("CONTENT_TEAM_LLM", "openai")
    monkeypatch.setenv("CONTENT_TEAM_LLM_BASE_URL", f"http://127.0.0.1:{port}/v1")
    monkeypatch.setenv("CONTENT_TEAM_LLM_MODEL", "test-model")
    monkeypatch.setenv("CONTENT_TEAM_LLM_DAILY_CAP", "1")
    first = llm.complete_json("one", fallback={"n": 0}, role="writer")
    second = llm.complete_json("two", fallback={"n": 2, "capped": True}, role="writer")
    server.shutdown()
    assert first["llm_status"] == "real"
    assert second["llm_status"] == "fallback"
    assert "daily LLM cap" in (second["error"] or "")
    assert second["data"]["capped"] is True


def test_failed_openai_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_TEAM_STATE_DIR", str(tmp_path / "ct"))
    monkeypatch.setenv("CONTENT_TEAM_LLM", "openai")
    monkeypatch.setenv("CONTENT_TEAM_LLM_BASE_URL", "http://127.0.0.1:1/v1")
    result = llm.complete_json("hello", fallback={"ok": False}, role="writer")
    assert result["ok"] is False
    assert result["llm_status"] == "fallback"
    assert result["data"] == {"ok": False}


def _clear_provider_env(monkeypatch):
    for name in (
        "CONTENT_TEAM_LLM",
        "CONTENT_TEAM_LLM_PROVIDERS",
        "CONTENT_TEAM_LLM_BASE_URL",
        "CONTENT_TEAM_LLM_API_KEY",
        "CONTENT_TEAM_LLM_MODEL",
        "GROQ_API_KEY",
        "OPENROUTER_API_KEY",
        "CLOUDFLARE_ACCOUNT_ID",
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_API_KEY",
        "CF_ACCOUNT_ID",
        "CF_API_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)


def test_auto_uses_cloudflare_as_primary(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_TEAM_STATE_DIR", str(tmp_path / "ct"))
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct-primary")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cf-token")
    monkeypatch.setenv("GROQ_API_KEY", "groq-optional")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-optional")
    monkeypatch.setenv("GEMINI_API_KEY", "should-not-be-used")
    monkeypatch.setenv("XAI_API_KEY", "should-not-be-used")
    assert llm.AUTO_PROVIDERS[0] == "cloudflare"
    assert llm.DEFAULT_MODELS["cloudflare"] == "@cf/zai-org/glm-4.7-flash"
    assert llm.provider_order() == ["cloudflare", "groq", "openrouter"]
    status = llm.provider_status()
    assert status["active"] == "cloudflare"
    assert status["model"] == "@cf/zai-org/glm-4.7-flash"
    assert status["gemini_in_auto"] is False
    base, key = llm._base_and_key("cloudflare")
    assert base == "https://api.cloudflare.com/client/v4/accounts/acct-primary/ai/v1"
    assert key == "cf-token"


def test_auto_skips_optional_fallbacks_without_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_TEAM_STATE_DIR", str(tmp_path / "ct"))
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct-only")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cf-token")
    assert llm.provider_order() == ["cloudflare"]


def test_content_team_llm_overrides_cloudflare(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_TEAM_STATE_DIR", str(tmp_path / "ct"))
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv(
        "CONTENT_TEAM_LLM_BASE_URL",
        "https://api.cloudflare.com/client/v4/accounts/override-acct/ai/v1",
    )
    monkeypatch.setenv("CONTENT_TEAM_LLM_API_KEY", "override-token")
    monkeypatch.setenv("CONTENT_TEAM_LLM_MODEL", "@cf/meta/llama-3.2-3b-instruct")
    assert llm.cloudflare_account() == "override-acct"
    assert llm._provider_ready("cloudflare")
    assert llm.provider_order() == ["cloudflare"]
    base, key = llm._base_and_key("cloudflare")
    assert base.endswith("/accounts/override-acct/ai/v1")
    assert key == "override-token"
    assert llm._model_for("cloudflare") == "@cf/meta/llama-3.2-3b-instruct"


def test_cloudflare_429_falls_back_to_placeholder_not_groq(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_TEAM_STATE_DIR", str(tmp_path / "ct"))
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cf-token")
    monkeypatch.setenv("GROQ_API_KEY", "groq-should-not-be-used")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-should-not-be-used")
    called = []

    def fake_complete(provider, prompt, system):
        called.append(provider)
        if provider == "cloudflare":
            raise llm.QuotaError("cloudflare HTTP 429: rate limited")
        raise AssertionError(f"{provider} must not be called after a Cloudflare quota error")

    monkeypatch.setattr(llm, "_openai_complete", fake_complete)
    result = llm.complete_json("Write an X post.", fallback={"post": "placeholder"}, role="writer")
    assert called == ["cloudflare"]
    assert result["ok"] is False
    assert result["provider"] == "fallback"
    assert result["llm_status"] == "fallback"
    assert "429" in (result["error"] or "")
    assert result["data"] == {"post": "placeholder"}


def test_daily_cap_does_not_try_next_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENT_TEAM_STATE_DIR", str(tmp_path / "ct"))
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cf-token")
    monkeypatch.setenv("GROQ_API_KEY", "groq-should-not-be-used")
    monkeypatch.setenv("CONTENT_TEAM_LLM_DAILY_CAP", "0")
    called = []
    monkeypatch.setattr(llm, "_openai_complete", lambda *a, **k: called.append("hit") or "x")
    result = llm.complete_json("hello", fallback={"capped": True}, role="writer")
    assert called == []
    assert result["llm_status"] == "fallback"
    assert "daily LLM cap" in (result["error"] or "")
    assert result["data"]["capped"] is True


def test_daily_cap_fits_free_neuron_allowance():
    worst_in = 4000
    worst_out = llm.DEFAULT_MAX_TOKENS
    worst = (
        worst_in * llm.CF_NEURONS_PER_M_INPUT
        + worst_out * llm.CF_NEURONS_PER_M_OUTPUT
    ) / 1_000_000
    assert llm.DEFAULT_DAILY_CAP * worst < llm.WORKERS_AI_FREE_NEURONS_PER_DAY


def test_quota_status_helper():
    assert llm._is_quota_status(429, "rate limited")
    assert llm._is_quota_status(402, "payment required")
    assert llm._is_quota_status(403, "upgrade to Workers Paid")
    assert llm._is_quota_status(400, "exceeded your neuron quota")
    assert not llm._is_quota_status(500, "internal error")
    assert not llm._is_quota_status(401, "unauthorized")
