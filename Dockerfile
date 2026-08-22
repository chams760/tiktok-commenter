FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 \
    libnspr4 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libatspi2.0-0 \
    libwayland-client0 \
    libglib2.0-0 \
    fonts-noto-cjk \
    fonts-freefont-ttf \
    fonts-unifont \
    wget \
    ca-certificates \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

ENV DISPLAY=:99

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN playwright install firefox

COPY . .

RUN mkdir -p logs screenshots sessions

EXPOSE 8080

CMD ["bash", "-c", "Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp &  sleep 1 && python bot.py"]
