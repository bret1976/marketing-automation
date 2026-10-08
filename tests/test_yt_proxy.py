import importlib.machinery
import importlib.util
import os
import subprocess

import pytest

import yt_proxy

SHIM = os.path.join(os.path.dirname(os.path.dirname(__file__)), "bin", "ytdlp-shim", "yt-dlp")


def load_shim():
    loader = importlib.machinery.SourceFileLoader("ytdlp_shim", SHIM)
    spec = importlib.util.spec_from_loader("ytdlp_shim", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def proxy_env(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("YT_DOWNLOAD_PROXY_TOKEN", "test-token")
    monkeypatch.setenv("YT_DOWNLOAD_PROXY_URL", "https://env.example.com")
    return tmp_path


def test_hosts():
    assert yt_proxy.is_youtube("https://www.youtube.com/watch?v=abc")
    assert yt_proxy.is_youtube("https://youtu.be/abc")
    assert not yt_proxy.is_youtube("https://www.reddit.com/r/x/comments/1/y/")
    assert yt_proxy.mini_supports("https://v.redd.it/abc123")
    assert yt_proxy.mini_supports("https://www.reddit.com/r/x/comments/1/y/")
    assert not yt_proxy.mini_supports("https://example.com/clip.mp4")


def test_register_prefers_registered_url(proxy_env):
    assert yt_proxy.urls() == ["https://env.example.com"]
    yt_proxy.register("https://fresh.trycloudflare.com/")
    assert yt_proxy.urls() == ["https://fresh.trycloudflare.com", "https://env.example.com"]
    assert yt_proxy.configured()
    with pytest.raises(ValueError):
        yt_proxy.register("http://insecure.example.com")
    assert yt_proxy.token_ok("test-token") and not yt_proxy.token_ok("nope")
    assert "test-token" not in str(yt_proxy.summary())


def test_parse_section():
    shim = load_shim()
    assert shim.parse_section("*0-60") == (0.0, 60.0)
    assert shim.parse_section("*12.5-40") == (12.5, 27.5)
    assert shim.parse_section(None) == (0.0, 60.0)


def _fake_pull(calls, ok=True):
    def pull(url, dest, start=0.0, duration=60.0, timeout=0):
        calls.append(url)
        if not ok:
            raise yt_proxy.ProxyError("down")
        with open(dest, "wb") as fh:
            fh.write(b"x" * 5000)
        return True
    return pull


def test_youtube_goes_to_mini_first(proxy_env, monkeypatch):
    shim = load_shim()
    calls = []
    monkeypatch.setattr(yt_proxy, "pull", _fake_pull(calls))
    monkeypatch.setattr(shim, "passthrough", lambda argv: pytest.fail("should not hit local yt-dlp"))
    out = proxy_env / "original_abc.%(ext)s"
    url = "https://www.youtube.com/watch?v=vS0AvT0PU6I"
    rc = shim.main(["-f", "b", "--download-sections", "*0-60", "-o", str(out), url])
    assert rc == 0 and calls == [url]
    assert (proxy_env / "original_abc.mp4").stat().st_size == 5000
    assert yt_proxy.recent_pulls(1)[0]["via"] == "mini_proxy"


def test_reddit_tries_local_then_mini(proxy_env, monkeypatch):
    shim = load_shim()
    calls = []
    monkeypatch.setattr(yt_proxy, "pull", _fake_pull(calls))
    monkeypatch.setattr(shim, "mini_failed_recently", lambda url: False)
    monkeypatch.setattr(
        shim.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "ERROR: HTTP Error 403: Blocked"),
    )
    out = proxy_env / "original_r.%(ext)s"
    url = "https://www.reddit.com/r/nextfuckinglevel/comments/1w0m69c/negotiating_with_gravity/"
    assert shim.main(["-o", str(out), url]) == 0
    assert calls == [url]


def test_probe_and_unconfigured_pass_through(proxy_env, monkeypatch):
    shim = load_shim()
    seen = []
    monkeypatch.setattr(shim, "passthrough", lambda argv: (seen.append(argv), (_ for _ in ()).throw(SystemExit(0))))
    with pytest.raises(SystemExit):
        shim.main(["--simulate", "https://www.youtube.com/watch?v=x"])
    monkeypatch.delenv("YT_DOWNLOAD_PROXY_TOKEN")
    with pytest.raises(SystemExit):
        shim.main(["-o", "/tmp/x.%(ext)s", "https://www.youtube.com/watch?v=x"])
    assert len(seen) == 2
