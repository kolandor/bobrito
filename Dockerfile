# ── Stage 1: build dependencies ──────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install --upgrade pip \
    && pip install --prefix=/install . \
    && pip install --prefix=/install ".[dev]"


# ── Stage 2: runtime image ────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

LABEL maintainer="bobrito-bot"
LABEL description="Bobrito Trading Bot — Binance Spot BTC/USDT"

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy source
COPY src/ ./src/
COPY pyproject.toml ./

# Install the package in editable mode for the runtime
RUN pip install --no-deps -e .

# Create directories that must exist at runtime
RUN mkdir -p /app/data /app/logs

# Non-root user for security
RUN groupadd -r bobrito && useradd -r -g bobrito bobrito
RUN chown -R bobrito:bobrito /app
USER bobrito

# Prometheus metrics port + API port
EXPOSE 8080 9090

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" \
    || exit 1

CMD ["python", "-m", "bobrito.main"]
