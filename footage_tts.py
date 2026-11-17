"""Voiceover for footage mode: one natural voice, never speed-changed, words never clipped.

Bake-off 2026-11-17 (same 5-line documentary script, UTMOS22 naturalness predictor, higher = more human):
  kokoro fp32 af_heart 4.51 | kokoro fp32 am_fenrir 4.49 | kokoro fp32 am_puck 4.47 | piper lessac-high 4.47
  kokoro fp32 am_michael 4.35 | chatterbox 4.27 (2.5x slower than real time, ~6.7 GB RAM) | piper ryan-high 4.32
  OLD: kokoro INT8 am_michael+am_onyx blend at 0.95x speed 3.99  <- what Bret heard
The old voice was the quantised (int8) model, a blended voice and a slowed read; all three cost naturalness.
Now: the full-precision Kokoro model, a single voice (FOOTAGE_VOICE, default am_fenrir), speed exactly 1.0.
Each sentence is synthesised alone, its silence trimmed with a generous tail (no cut-off consonants), and
sentences are joined with one constant gap (even pacing). Apache-2.0 model, local CPU, $0.
"""
import logging
import os
import tempfile
import wave
from typing import List

import numpy as np

logger = logging.getLogger("footage_tts")
SR = 24000
DEFAULT_VOICE = "am_fenrir"
HEAD_PAD = 0.03   # keep 30 ms before the first audible sample
TAIL_PAD = 0.16   # keep 160 ms after the last audible sample (soft consonants: s, t, th)
FADE = 0.03

_K = None


def _model_paths():
    model = os.environ.get("KOKORO_MODEL_FP32") or ""
    voices = os.environ.get("KOKORO_VOICES") or ""
    if model and voices and os.path.exists(model) and os.path.exists(voices):
        return model, voices
    import footage_mode
    return footage_mode._kokoro_files(fp32=True)


def _kokoro():
    global _K
    if _K is None:
        from kokoro_onnx import Kokoro
        _K = Kokoro(*_model_paths())
    return _K


def voice() -> str:
    v = (os.environ.get("FOOTAGE_VOICE") or DEFAULT_VOICE).strip()
    return v if ("," not in v and ":" not in v) else DEFAULT_VOICE  # blends sounded synthetic; one voice only


def trim(audio: np.ndarray, sr: int = SR) -> np.ndarray:
    """Trim leading/trailing silence by a relative threshold, keeping generous pads, with tiny fades."""
    if not len(audio):
        return audio
    peak = float(np.max(np.abs(audio))) or 1.0
    thr = peak * 0.02  # about -34 dB under the peak
    idx = np.where(np.abs(audio) > thr)[0]
    if not len(idx):
        return audio
    a = audio[max(0, idx[0] - int(HEAD_PAD * sr)): min(len(audio), idx[-1] + int(TAIL_PAD * sr))].astype(np.float32).copy()
    n = int(FADE * sr)
    if len(a) > 2 * n:
        a[:n] *= np.linspace(0, 1, n, dtype=np.float32)
        a[-n:] *= np.linspace(1, 0, n, dtype=np.float32)
    return a


def synth_lines(lines: List[str], workdir: str) -> dict:
    """{'clips': [np.float32], 'durations': [s], 'sr': int, 'info': {...}}. Raises if no audio."""
    k = _kokoro()
    v = voice()
    clips, durs = [], []
    for i, text in enumerate(lines):
        audio, sr = k.create(text, voice=v, speed=1.0, lang="en-us")
        a = trim(np.asarray(audio, dtype=np.float32), sr)
        clips.append(a)
        durs.append(round(len(a) / sr, 3))
    if not clips:
        raise RuntimeError("no voiceover audio")
    return {"clips": clips, "durations": durs, "sr": SR,
            "info": {"tts": "kokoro-onnx fp32", "voice": v, "speed": 1.0, "lines_used": len(clips),
                     "seconds": round(sum(durs), 2)}}


def assemble(clips: List[np.ndarray], starts: List[float], total: float, sr: int, path: str) -> str:
    out = np.zeros(int(total * sr) + sr, dtype=np.float32)
    for c, s in zip(clips, starts):
        i = int(round(s * sr))
        out[i:i + len(c)] += c[: max(0, len(out) - i)]
    pcm = (np.clip(out[: int(total * sr)], -1.0, 1.0) * 32767).astype("<i2").tobytes()
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm)
    return path
