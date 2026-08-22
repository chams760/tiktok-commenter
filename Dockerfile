FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    curl \
    unzip \
    gnupg \
    ca-certificates \
    fonts-noto-cjk \
    fonts-freefont-ttf \
    fonts-unifont \
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
    libgtk-3-0 \
    libgdk-pixbuf2.0-0 \
    libxcursor1 \
    libx11-xcb1 \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*

# Install Chrome + matching chromedriver via Chrome for Testing (stable channel)
RUN CHROME_URL="https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb" \
    && wget -q -O /tmp/chrome.deb "$CHROME_URL" \
    && apt-get update \
    && apt-get install -y --no-install-recommends /tmp/chrome.deb \
    && rm /tmp/chrome.deb \
    && rm -rf /var/lib/apt/lists/* \
    && CHROME_VERSION=$(google-chrome-stable --version | grep -oP '\d+\.\d+\.\d+\.\d+') \
    && CHROME_MAJOR=$(echo "$CHROME_VERSION" | cut -d. -f1) \
    && echo "Chrome version: $CHROME_VERSION (major: $CHROME_MAJOR)" \
    && DRIVER_URL="https://storage.googleapis.com/chrome-for-testing-public/${CHROME_VERSION}/linux64/chromedriver-linux64.zip" \
    && echo "Downloading chromedriver from: $DRIVER_URL" \
    && wget -q -O /tmp/chromedriver.zip "$DRIVER_URL" \
    && unzip -o /tmp/chromedriver.zip -d /tmp/ \
    && mv /tmp/chromedriver-linux64/chromedriver /usr/local/bin/chromedriver \
    && chmod +x /usr/local/bin/chromedriver \
    && rm -rf /tmp/chromedriver* \
    && chromedriver --version

# Patch chromedriver: remove cdc_ detection variable (same as undetected-chromedriver)
RUN python3 -c "\
import re, sys; \
f=open('/usr/local/bin/chromedriver','rb'); d=f.read(); f.close(); \
p=re.sub(rb'cdc_[a-zA-Z0-9]{22}_', lambda m: b'aaa_' + b'a'*(len(m.group())-4), d); \
changed=d!=p; \
f=open('/usr/local/bin/chromedriver','wb'); f.write(p); f.close(); \
print(f'Chromedriver patched: {changed}'); \
"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir setuptools && pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p logs screenshots sessions /dev/shm && chmod 1777 /dev/shm

EXPOSE 8080

CMD ["python", "bot.py"]
