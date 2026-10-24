"""Hook/commentary/footer text is burned with drawtext textfile=. ffmpeg runs that text through
its %{...} expansion by default, so a '%' (e.g. a "99% of heat blocked" hook, and hooks must carry a
number) makes ffmpeg 7.1 log "Stray %" and draw nothing while still exiting 0. expansion=none fixes it."""
import os
import shutil
import subprocess
import tempfile

import pytest

os.environ.setdefault("DISABLE_BACKGROUND_SCHEDULER", "true")
import growth_v2 as g
import stitched_shorts as ss


def _capture_render(monkeypatch, hook, commentary=None, footer=""):
    captured = {}

    class _Res:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(cmd, *a, **k):
        captured["cmd"] = cmd
        return _Res()

    monkeypatch.setattr(g, "_font", lambda: "/fake/font.ttf")
    monkeypatch.setattr(g, "probe_video", lambda p: {"width": 1080, "height": 1920, "duration": 8.0})
    monkeypatch.setattr(g.subprocess, "run", fake_run)
    g.render_vertical("in.mp4", "out.mp4", hook, commentary, footer, 15.0, False)
    return captured["cmd"][captured["cmd"].index("-filter_complex") + 1]


def test_render_vertical_every_drawtext_is_literal(monkeypatch):
    filt = _capture_render(monkeypatch, "99% of heat blocked", ["Line one 50%", "Line two"], "credit @x")
    parts = [p for p in filt.split(",") if p.startswith("drawtext=")]
    assert len(parts) == 4  # hook + 2 commentary + footer
    for p in parts:
        assert ":expansion=none:" in p


def test_stitched_drawtext_is_literal(tmp_path):
    tf = tmp_path / "hook.txt"
    tf.write_text("99% HOTTER\n1,200\\u00b0C")
    chain = ss._drawtext(str(tf), "/fake/font.ttf", 96, "h*0.16", 0.0, 2.0)
    parts = chain.split(",drawtext=")
    assert len(parts) == 2
    assert chain.count(":expansion=none:") == 2
    assert (tmp_path / "hook.txt.0").read_text() == "99% HOTTER"


def _ffmpeg_with_drawtext():
    ff = shutil.which("ffmpeg")
    if not ff:
        return None
    out = subprocess.run([ff, "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    return ff if " drawtext " in out and g._font() else None


@pytest.mark.skipif(_ffmpeg_with_drawtext() is None, reason="ffmpeg with drawtext + font not available")
def test_real_ffmpeg_draws_percent_text():
    ff = _ffmpeg_with_drawtext()
    font = g._font()
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "src.mp4")
        subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=360x640:d=1",
                        "-t", "1", src], check=True)
        tf = os.path.join(d, "t.txt")
        with open(tf, "w") as fh:
            fh.write("99% HOT")

        def frame(vf):
            png = os.path.join(d, "f.png")
            subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-vf", vf, "-frames:v", "1",
                            "-update", "1", png], check=True)
            return open(png, "rb").read()

        plain = frame("null")
        literal = frame(f"drawtext=fontfile='{font}':textfile='{tf}':expansion=none:fontcolor=white:fontsize=60:x=10:y=100")
        assert literal != plain  # the '%' text is actually drawn
