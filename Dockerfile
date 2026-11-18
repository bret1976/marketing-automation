FROM python:3.11.18-slim

# Install system dependencies (ffmpeg/ffprobe, Chromium for HyperFrames renders, fonts)
RUN apt-get update && apt-get install -y \
    ca-certificates \
    ffmpeg \
    curl \
    chromium \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libatspi2.0-0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    dbus \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js 22 from NodeSource (HyperFrames requires Node >= 22)
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs

RUN printf '%s\n' '#!/bin/sh' 'exec /usr/bin/chromium --no-sandbox --disable-setuid-sandbox --disable-dev-shm-usage --disable-gpu "$@"' > /usr/local/bin/chromium-no-sandbox \
    && chmod +x /usr/local/bin/chromium-no-sandbox

ENV PUPPETEER_EXECUTABLE_PATH=/usr/local/bin/chromium-no-sandbox
ENV HYPERFRAMES_BROWSER_PATH=/usr/local/bin/chromium-no-sandbox

# Install yt-dlp binary to /usr/local/bin/yt-dlp
RUN curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp \
    && chmod a+rx /usr/local/bin/yt-dlp

WORKDIR /app

# Preinstall HyperFrames locally so npx resolves the package runtime from /app/node_modules.
# package.json pins hyperframes 0.7.40 and overrides its vulnerable transitive deps
# (sharp, adm-zip, puppeteer-core, @puppeteer/browsers); npm ci installs the exact lockfile.
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund

# Copy requirements and install dependencies
COPY requirements.txt requirements-nodeps.txt ./
RUN pip install --no-cache-dir --upgrade pip==26.2.1 setuptools==83.0.0 && pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir --no-deps -r requirements-nodeps.txt

# Footage mode (VIDEO_ENGINE=footage): bake the free Apache-2.0 Kokoro TTS model into the image so the
# voiceover never depends on a runtime download, then prove synthesis works at build time.
# footage v2 (2026-11-17) voices with the full-precision model (KOKORO_MODEL_FP32): the int8 model + blended voice
# scored 3.99 UTMOS vs 4.49 for fp32 am_fenrir. Lives in the image, not on the nearly full data volume.
ENV KOKORO_MODEL=/opt/kokoro/kokoro-v1.0.int8.onnx \
    KOKORO_MODEL_FP32=/opt/kokoro/kokoro-v1.0.onnx \
    KOKORO_VOICES=/opt/kokoro/voices-v1.0.bin
RUN mkdir -p /opt/kokoro \
    && curl -fsSL -o /opt/kokoro/kokoro-v1.0.int8.onnx https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx \
    && curl -fsSL -o /opt/kokoro/kokoro-v1.0.onnx https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx \
    && curl -fsSL -o /opt/kokoro/voices-v1.0.bin https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin \
    && python -c "import os; from kokoro_onnx import Kokoro; k = Kokoro(os.environ['KOKORO_MODEL_FP32'], os.environ['KOKORO_VOICES']); a, sr = k.create('Footage mode voice check.', voice='am_fenrir', speed=1.0, lang='en-us'); assert sr == 24000 and len(a) > 12000, len(a); print('KOKORO_BUILD_OK', len(a) / sr, 's')"


# Footage fallback (no Cloudflare): bake a quantized CLIP ViT-B-32 (~150 MB) into the image, like Kokoro,
# never onto the data volume. Used to confirm a raw still or video frame matches its voiceover line.
ENV FOOTAGE_CLIP_ONNX=/opt/clip/model_quantized.onnx \
    FOOTAGE_CLIP_TOKENIZER=/opt/clip/tokenizer.json
RUN mkdir -p /opt/clip \
    && curl -fsSL -o /opt/clip/model_quantized.onnx https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/onnx/model_quantized.onnx \
    && curl -fsSL -o /opt/clip/tokenizer.json https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/tokenizer.json \
    && python -c "from pathlib import Path; p=Path('/opt/clip/model_quantized.onnx'); assert p.stat().st_size > 100_000_000, p.stat().st_size; t=Path('/opt/clip/tokenizer.json'); assert t.stat().st_size > 1000, t.stat().st_size; print('CLIP_FILES_OK', p.stat().st_size, t.stat().st_size)"

# Copy rest of the application code
COPY . .
RUN chmod a+rx /app/bin/ytdlp-shim/yt-dlp
RUN python -c "import footage_clip; print(footage_clip.self_check())"

# Expose FastAPI default port
EXPOSE 8000

# Start Uvicorn server
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
