# ── Playwright MCP Server ────────────────────────────────────────────────
# ARM64 (Graviton) — consistent with agent container, cheaper on Fargate
#
# Supports two modes via BROWSER_MODE env var:
#   headed    → Chromium + Xvfb (virtual display) + noVNC (web viewer on :6080)
#   headless  → Chromium headless only
#
# Ports:
#   3000 → Playwright MCP server (SSE transport)
#   6080 → noVNC web viewer (headed mode only)

FROM public.ecr.aws/docker/library/node:20-slim

# ── System dependencies ──────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Chromium browser + dependencies
    chromium \
    chromium-sandbox \
    # Virtual display (headed mode)
    xvfb \
    # VNC server (headed mode)
    x11vnc \
    # noVNC web viewer dependencies
    novnc \
    websockify \
    # Playwright browser dependencies
    libglib2.0-0 \
    libnss3 \
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
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    fonts-liberation \
    # Process management
    procps \
    && rm -rf /var/lib/apt/lists/*

# ── Install @playwright/mcp globally ────────────────────────────────────
RUN npm install -g @playwright/mcp@latest

# ── Tell Playwright to use the system Chromium ───────────────────────────
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium

# ── Copy entrypoint ──────────────────────────────────────────────────────
WORKDIR /app
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# ── Runtime env defaults ─────────────────────────────────────────────────
# Override BROWSER_MODE at ECS task level: headed | headless
ENV BROWSER_MODE=headless
ENV MCP_PORT=3000
ENV NOVNC_PORT=6080
ENV DISPLAY_NUM=99

# ── Ports ────────────────────────────────────────────────────────────────
# 3000 — Playwright MCP SSE endpoint (agent connects here)
# 6080 — noVNC web viewer (DEV headed mode — QA watches browser live)
EXPOSE 3000 6080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD node -e "require('http').get('http://localhost:3000/health', r => process.exit(r.statusCode===200?0:1)).on('error',()=>process.exit(1))"

ENTRYPOINT ["/app/entrypoint.sh"]
