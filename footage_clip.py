"""Local CLIP (ONNX, free) for the no-Cloudflare footage fallback.

A quantized OpenAI CLIP ViT-B-32 (~150 MB) is baked into the image under /opt/clip,
like Kokoro, and is never stored on the data volume. Used only to confirm that a
frame actually looks like its voiceover line after metadata has already accepted
the pairing. If the model is missing or a frame cannot be scored, the caller must reject the line.
"""
from __future__ import annotations

import logging
import os
import time
from typing import List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger("footage_clip")

DEFAULT_MODEL = os.environ.get("FOOTAGE_CLIP_ONNX", "/opt/clip/model_quantized.onnx")
DEFAULT_TOKENIZER = os.environ.get("FOOTAGE_CLIP_TOKENIZER", "/opt/clip/tokenizer.json")
MIN_SCORE = float(os.environ.get("FOOTAGE_CLIP_MIN_SCORE", "0.20"))
DESC_SLACK = float(os.environ.get("FOOTAGE_CLIP_DESC_SLACK", "0.02"))
WIN_MARGIN = float(os.environ.get("FOOTAGE_CLIP_WIN_MARGIN", "0.03"))
ENTITY_DESC_SLACK = float(os.environ.get("FOOTAGE_CLIP_ENTITY_DESC_SLACK", "0.045"))

_SESSION = None
_TOKENIZER = None
_LOAD_ERROR = None
_LOAD_MS = None
_LAST_RSS_MB = None


def _rss_mb() -> Optional[float]:
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        return None
    return None


def available() -> bool:
    if _LOAD_ERROR:
        return False
    if _SESSION is not None:
        return True
    return os.path.isfile(DEFAULT_MODEL) and os.path.isfile(DEFAULT_TOKENIZER)


def _load():
    global _SESSION, _TOKENIZER, _LOAD_ERROR, _LOAD_MS, _LAST_RSS_MB
    if _SESSION is not None or _LOAD_ERROR:
        return _SESSION
    if not available():
        _LOAD_ERROR = "clip model or tokenizer not baked into the image"
        return None
    t0 = time.time()
    try:
        import onnxruntime as ort
        from tokenizers import Tokenizer
        _TOKENIZER = Tokenizer.from_file(DEFAULT_TOKENIZER)
        _SESSION = ort.InferenceSession(DEFAULT_MODEL, providers=["CPUExecutionProvider"])
        _LOAD_MS = (time.time() - t0) * 1000.0
        _LAST_RSS_MB = _rss_mb()
        logger.info("clip loaded in %.0fms rss=%.0fMB", _LOAD_MS, _LAST_RSS_MB or 0)
        return _SESSION
    except Exception as exc:  # noqa: BLE001
        _LOAD_ERROR = str(exc)[:240]
        logger.warning("clip unavailable: %s", _LOAD_ERROR)
        return None


def _tokenize(texts: Sequence[str]) -> Tuple["np.ndarray", "np.ndarray"]:
    _load()
    ids_out, masks = [], []
    for raw in texts:
        text = (raw or "").strip().lower()
        if text and not text.startswith("a photo"):
            text = "a photo of " + text
        enc = _TOKENIZER.encode(text)
        ids = list(enc.ids)
        if not ids or ids[0] != 49406:
            ids = [49406] + ids
        if ids[-1] != 49407:
            ids = ids + [49407]
        ids = ids[:77]
        mask = [1] * len(ids) + [0] * (77 - len(ids))
        ids = ids + [0] * (77 - len(ids))
        ids_out.append(ids)
        masks.append(mask)
    return np.array(ids_out, dtype=np.int64), np.array(masks, dtype=np.int64)


def _preprocess(image) -> "np.ndarray":
    from PIL import Image
    if hasattr(image, "convert"):
        im = image.convert("RGB")
    else:
        im = Image.open(image).convert("RGB")
    width, height = im.size
    if width < height:
        new_w, new_h = 224, int(round(height * 224 / width))
    else:
        new_h, new_w = 224, int(round(width * 224 / height))
    im = im.resize((new_w, new_h), Image.BICUBIC)
    left, top = (new_w - 224) // 2, (new_h - 224) // 2
    im = im.crop((left, top, left + 224, top + 224))
    arr = np.asarray(im).astype(np.float32) / 255.0
    mean = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
    std = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
    arr = (arr - mean) / std
    return np.transpose(arr, (2, 0, 1))[None, ...]


def _embed_image(image) -> Optional["np.ndarray"]:
    if _load() is None:
        return None
    pix = _preprocess(image)
    ids, mask = _tokenize(["a photo"])
    vec = _SESSION.run(["image_embeds"], {"input_ids": ids, "pixel_values": pix, "attention_mask": mask})[0][0]
    norm = np.linalg.norm(vec) or 1.0
    return vec / norm


def _embed_texts(texts: Sequence[str]) -> Optional["np.ndarray"]:
    if _load() is None:
        return None
    from PIL import Image
    dummy = Image.new("RGB", (224, 224), (0, 0, 0))
    pix = _preprocess(dummy)
    ids, mask = _tokenize(texts)
    vec = _SESSION.run(["text_embeds"], {"input_ids": ids, "pixel_values": pix, "attention_mask": mask})[0]
    norms = np.linalg.norm(vec, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vec / norms


def score_image_texts(image, texts: Sequence[str]) -> Optional[List[float]]:
    """Cosine similarities between one image and each text. None if CLIP is unavailable."""
    img = _embed_image(image)
    txt = _embed_texts(list(texts))
    if img is None or txt is None:
        return None
    global _LAST_RSS_MB
    _LAST_RSS_MB = _rss_mb()
    return [float(row @ img) for row in txt]


def confirm_line(image, line: str, image_description: str = "", other_lines: Optional[Sequence[str]] = None,
                 exact_entity: bool = False) -> dict:
    """Accept a line only when it is a clear CLIP winner for this exact frame.

    Fail closed: if scoring is unavailable, the line is rejected (ok=False), never skipped.
    The assigned line must beat the next-best script line by WIN_MARGIN (0.03) and must
    not trail the file's own description by more than DESC_SLACK (0.02). An exact-entity
    match does not waive the winner rule unless this line is also ranked first
    (line > best other); a second-place or tied line is always rejected.
    """
    t0 = time.time()
    desc = (image_description or "").strip()
    others = []
    seen = {(line or "").strip().lower()}
    for raw in other_lines or []:
        text = (raw or "").strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            others.append(text)
    texts = [line]
    has_desc = bool(desc)
    if has_desc:
        texts.append(desc)
    texts.extend(others)
    scores = score_image_texts(image, texts)
    ms = (time.time() - t0) * 1000.0
    if scores is None:
        return {"ok": False, "reason": "clip unavailable: " + (_LOAD_ERROR or "not loaded"),
                "line": None, "desc": None, "best_other": None, "ms": ms, "rss_mb": _LAST_RSS_MB}
    line_score = scores[0]
    idx = 1
    desc_score = None
    if has_desc:
        desc_score = scores[idx]
        idx += 1
    other_scores = scores[idx:]
    best_other = max(other_scores) if other_scores else None
    ok = line_score >= MIN_SCORE
    reason = "clip confirms line"
    if not ok:
        reason = f"line score {line_score:.3f} below {MIN_SCORE:.2f}"
    elif desc_score is not None and line_score < desc_score - DESC_SLACK:
        ok = False
        reason = (f"line {line_score:.3f} trails image description {desc_score:.3f} "
                  f"by more than {DESC_SLACK:.3f}")
    elif best_other is not None and line_score < best_other + WIN_MARGIN:
        # Exact-entity may keep a first-place line that misses the margin, never a loser or tie.
        if exact_entity and line_score > best_other:
            reason = (f"clip ranks line first {line_score:.3f} vs best other {best_other:.3f} "
                      f"(exact-entity, margin below {WIN_MARGIN:.2f})")
        else:
            ok = False
            reason = (f"line {line_score:.3f} is not a clear winner over other lines "
                      f"(best other {best_other:.3f}, need +{WIN_MARGIN:.2f})")
    return {"ok": ok, "reason": reason, "line": round(line_score, 3),
            "desc": None if desc_score is None else round(desc_score, 3),
            "best_other": None if best_other is None else round(best_other, 3),
            "ms": round(ms, 1), "rss_mb": _LAST_RSS_MB}


def image_from_bytes(data: bytes):
    from io import BytesIO
    from PIL import Image
    return Image.open(BytesIO(data)).convert("RGB")


def frames_from_segment(path: str, start: float, duration: float, n: int = 3) -> List:
    """Sample 2–3 frames from the exact segment that will be rendered. None entries mean a decode miss."""
    duration = max(0.0, float(duration or 0.0))
    start = max(0.0, float(start or 0.0))
    count = 3 if n >= 3 and duration >= 1.5 else 2
    fracs = (0.2, 0.5, 0.8) if count == 3 else (0.3, 0.7)
    return [frame_from_video(path, start + duration * f) for f in fracs[:count]]


def frame_from_video(path: str, start: float = 0.0):
    """Grab one frame for CLIP. Returns a PIL image or None."""
    import subprocess
    from io import BytesIO
    from PIL import Image
    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, float(start)):.3f}", "-i", path,
             "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=False,
        )
        if proc.returncode != 0 or not proc.stdout:
            return None
        return Image.open(BytesIO(proc.stdout)).convert("RGB")
    except Exception:  # noqa: BLE001
        return None


def self_check() -> str:
    """Build-time / boot check. Raises on a broken bake."""
    from PIL import Image
    if _load() is None:
        raise RuntimeError("clip self-check failed: " + (_LOAD_ERROR or "model missing"))
    img = Image.new("RGB", (224, 224), (30, 80, 40))
    scores = score_image_texts(img, ["a green field", "a typographic letter"])
    if not scores or not all(np.isfinite(s) for s in scores):
        raise RuntimeError("clip self-check produced no finite scores")
    return f"CLIP_BUILD_OK scores={scores[0]:.3f},{scores[1]:.3f} load_ms={_LOAD_MS:.0f} rss_mb={_LAST_RSS_MB or 0:.0f}"


if __name__ == "__main__":
    print(self_check())
