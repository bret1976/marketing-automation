FROM python:3.11-slim

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
RUN npm install hyperframes@0.7.40

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy rest of the application code
COPY . .

# Expose FastAPI default port
EXPOSE 8000

# Start Uvicorn server
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
