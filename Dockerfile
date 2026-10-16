# Deal Room Agent: Node API + Python pipeline + headless Chromium. No paid services.
FROM mcr.microsoft.com/playwright/python:v1.56.0-noble

# Node 22 for the API/CLI (no npm packages needed)
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV HOST=0.0.0.0 PORT=8790 DATA_DIR=/data PYTHON=python3
RUN useradd -m agent && mkdir -p /data && chown agent /data
USER agent
VOLUME ["/data"]
EXPOSE 8790
HEALTHCHECK --interval=30s --timeout=5s CMD curl -fs http://127.0.0.1:8790/health || exit 1
CMD ["node", "src/server.js"]
