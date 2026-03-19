# ── Stage 1: Build ──────────────────────────────────────────────────────
FROM public.ecr.aws/docker/library/python:3.12-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# ── Stage 2: Runtime ────────────────────────────────────────────────────
FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /root/.local /root/.local

# Copy agent source code
COPY agent/ ./agent/

# Make sure scripts in .local are usable
ENV PATH=/root/.local/bin:$PATH
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Bedrock AgentCore Runtime listens on port 8080
EXPOSE 8000

# Health check — AgentCore pings /health
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["python", "-m", "uvicorn", "agent.main:app", "--host", "0.0.0.0", "--port", "8000"]
