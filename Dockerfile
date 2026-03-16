# ── DexPay Telegram Bot ─────────────────────────────────────────────────
# Playwright needs Chromium system libs; use the official Playwright image
# or a slim Python base with manual lib installation.
FROM python:3.11-slim

# System dependencies required by Playwright's Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Chromium runtime libs
    libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libasound2 libpango-1.0-0 \
    libpangocairo-1.0-0 libatspi2.0-0 libx11-6 libx11-xcb1 \
    libxcb1 libxext6 \
    # Utilities
    wget curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium browser
RUN playwright install chromium

# Copy application source
COPY . .

# The bot expects these directories to exist
RUN mkdir -p browser_profile temp_profiles screenshots user_uploads assets

CMD ["python", "main.py"]
