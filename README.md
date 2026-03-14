# Bobrito — Automated Trading Bot for Binance Spot BTC/USDT

**Version 1.1** · Python 3.11+ · FastAPI · SQLite · Docker

> A production-grade automated trading platform with strict risk management,
> paper/testnet/live mode switching, full observability, and a REST API for control.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Quick Start (Paper Trading)](#quick-start-paper-trading)
3. [Configuration](#configuration)
4. [Trading Strategy](#trading-strategy)
5. [Risk Management](#risk-management)
6. [Execution Modes](#execution-modes)
7. [API Reference](#api-reference)
8. [Docker Deployment](#docker-deployment)
9. [Running Tests](#running-tests)
10. [Project Structure](#project-structure)
11. [Safety Notes](#safety-notes)

---

## Architecture Overview

```
Market Data Feed (WebSocket)
        │
        ▼
  Candle Buffers (1m / 5m)
        │
        ▼
  Strategy Engine          ← TrendPullbackStrategy
        │  signal
        ▼
  Risk Manager             ← validates entry, sizes position
        │  decision
        ▼
  Broker Interface         ← PaperBroker | BinanceBroker
        │  OrderResult
        ▼
  Portfolio Manager        ← tracks PnL, equity snapshots
        │
        ▼
  Persistence (SQLite)     ← signals, orders, positions, events
        │
        ▼
  REST API + Prometheus    ← monitoring & control
```

**Modules:**

| Layer | Package |
|-------|---------|
| Config | `bobrito.config` |
| Market Data | `bobrito.market_data` |
| Strategy | `bobrito.strategy` |
| Risk | `bobrito.risk` |
| Execution | `bobrito.execution` |
| Portfolio | `bobrito.portfolio` |
| Persistence | `bobrito.persistence` |
| API | `bobrito.api` |
| Monitoring | `bobrito.monitoring` |
| Bot Engine | `bobrito.engine` |

---

## Quick Start (Paper Trading)

### 1. Prerequisites

- Python 3.11+
- pip

### 2. Install

```bash
# Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate        # Linux/macOS
.venv\Scripts\activate           # Windows

# Install with dev dependencies
pip install -e ".[dev]"
```

### 3. Configure

```bash
cp .env.example .env
# Edit .env — defaults work for paper trading without any API keys
```

### 4. Run

```bash
python -m bobrito.main
# or
uvicorn bobrito.main:app --host 0.0.0.0 --port 8080
```

The bot will:
- Connect to Binance public WebSocket streams (no API key needed for paper mode)
- Start buffering 1m and 5m candles
- Generate signals and simulate trades once indicators warm up (~30 candles)
- Expose the REST API at `http://localhost:8080`

### 5. Check status

```bash
# Health check (no auth required)
curl http://localhost:8080/health

# Bot status (requires API token)
curl -H "Authorization: Bearer your_api_secret_key" \
     http://localhost:8080/status
```

---

## Configuration

All configuration is via environment variables. Copy `.env.example` to `.env`.

| Variable | Default | Description |
|----------|---------|-------------|
| `BOT_MODE` | `paper` | `paper` / `testnet` / `live` |
| `PAPER_INITIAL_USDT` | `200` | Starting balance for paper trading |
| `RISK_PER_TRADE_PCT` | `0.75` | Risk % of capital per trade |
| `MAX_DAILY_LOSS_PCT` | `3.0` | Daily loss circuit breaker |
| `MAX_CONSECUTIVE_LOSSES` | `3` | Consecutive loss limit |
| `COOLDOWN_MINUTES_AFTER_LOSSES` | `60` | Pause after max consecutive losses |
| `MAX_TRADES_PER_DAY` | `10` | Maximum trades per calendar day |
| `EMA_FAST` | `9` | Fast EMA period |
| `EMA_SLOW` | `21` | Slow EMA period |
| `ATR_PERIOD` | `14` | ATR lookback period |
| `VOLUME_MULTIPLIER` | `1.5` | Volume confirmation multiplier |
| `API_SECRET_KEY` | `change_me…` | Bearer token for API auth |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/bobrito.db` | Database connection string |

---

## Trading Strategy

**Type:** Long-only intraday (USDT → BTC → USDT)

**Timeframes:** 5m (trend), 1m (entry)

### Entry conditions (all must be true)

1. **Uptrend on 5m** — fast EMA > slow EMA with ≥0.05% separation
2. **Pullback on 1m** — price recently touched near the slow EMA
3. **Momentum resumption** — close crosses back above fast EMA
4. **Volume confirmation** — current volume > SMA(volume) × multiplier
5. **Risk approved** — all risk rules pass

### Stop & Target

- Stop: `entry_price − 1.5 × ATR`
- Target: `entry_price + 3.0 × ATR`
- Risk/Reward ≥ 2:1

### Exit conditions

| Trigger | Action |
|---------|--------|
| Stop price hit | Market SELL |
| Target price hit | Market SELL |
| Close < fast EMA (1m) | Momentum failure SELL |
| Emergency stop | Immediate market SELL |

### Regime filter

No trades are taken when the market is detected as sideways (fast EMA ≈ slow EMA on 5m).

---

## Risk Management

Risk management **overrides** strategy — signals cannot bypass it.

### Rules enforced before every entry

1. No existing open position (one position at a time)
2. Daily loss limit not reached
3. Consecutive loss limit not reached
4. Cooldown period (after max consecutive losses) expired
5. Max daily trades not reached
6. Minimum free balance maintained
7. Safe mode not active

### Position sizing

```
risk_amount = capital × risk_per_trade_pct / 100
quantity    = risk_amount / stop_distance
```

Rounded to exchange `stepSize`, capped by available balance.

### Safe mode

Activated automatically on critical errors. Blocks all new entries. Cleared manually via `POST /bot/resume` after investigation.

---

## Execution Modes

| Mode | Description |
|------|-------------|
| `paper` | In-memory simulation with real market data. No API keys needed. |
| `testnet` | Orders sent to Binance Spot Testnet. Requires testnet API keys. |
| `live` | Real orders. Requires `LIVE_TRADING_ENABLED=true` + live API keys. |

**Live trading safety gate:** You must explicitly set `LIVE_TRADING_ENABLED=true` in `.env`. The bot will refuse to start in live mode without this flag.

---

## API Reference

All endpoints except `/health` require `Authorization: Bearer <API_SECRET_KEY>`.

### System

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness probe |
| GET | `/status` | Full runtime status |

### Bot Control

| Method | Path | Description |
|--------|------|-------------|
| POST | `/bot/start` | Start the bot |
| POST | `/bot/stop` | Graceful stop |
| POST | `/bot/pause` | Pause new entries |
| POST | `/bot/resume` | Resume entries |
| POST | `/bot/emergency-stop` | Immediate stop + close position |

### Trading Data

| Method | Path | Description |
|--------|------|-------------|
| GET | `/trading/balances` | Current balances |
| GET | `/trading/positions` | Open positions |
| GET | `/trading/trades?limit=50` | Recent closed trades |
| GET | `/trading/metrics` | Portfolio + risk metrics |

Interactive docs available at `http://localhost:8080/docs`.

---

## Docker Deployment

```bash
# Copy and configure environment
cp .env.example .env

# Start all services (bot + prometheus + grafana)
docker-compose up -d

# View logs
docker-compose logs -f bobrito

# Stop
docker-compose down
```

Services:

| Service | Port | Description |
|---------|------|-------------|
| bobrito | 8080 | REST API |
| bobrito | 9090 | Prometheus metrics |
| prometheus | 9191 | Prometheus UI |
| grafana | 3000 | Dashboard (admin/admin) |

---

## Running Tests

```bash
# All tests
pytest

# Unit tests only
pytest tests/unit -v

# Integration tests only
pytest tests/integration -v

# With coverage
pytest --cov=bobrito --cov-report=term-missing
```

---

## Project Structure

```
bobrito/
├── src/bobrito/
│   ├── config/           # Settings (pydantic-settings)
│   ├── market_data/      # WebSocket feed, candle buffer, data models
│   ├── strategy/         # Indicators + TrendPullback strategy
│   ├── risk/             # Risk manager (position sizing, rules)
│   ├── execution/        # PaperBroker, BinanceBroker
│   ├── portfolio/        # Portfolio manager (PnL, equity tracking)
│   ├── persistence/      # SQLAlchemy models + DB manager
│   ├── api/              # FastAPI app + routes
│   ├── monitoring/       # Loguru logging + Prometheus metrics
│   └── engine/           # Bot orchestrator
├── tests/
│   ├── unit/             # Pure function tests
│   └── integration/      # Pipeline tests
├── alembic/              # Database migrations
├── docker/               # Prometheus config
├── .env.example
├── pyproject.toml
├── Dockerfile
└── docker-compose.yml
```

---

## Safety Notes

1. **Never commit `.env`** — it contains API secrets. Only `.env.example` is tracked.
2. **Start with paper mode** — validate strategy behaviour before using real capital.
3. **Test on testnet** — verify order execution before switching to live.
4. **Set `LIVE_TRADING_ENABLED=true` deliberately** — this flag prevents accidental live activation.
5. **Initial capital is 200 USDT** — the bot will never risk more than configured in `RISK_PER_TRADE_PCT`.
6. **Review daily loss limits** — `MAX_DAILY_LOSS_PCT=3%` means a maximum drawdown of 6 USDT per day on 200 USDT capital.
7. **Futures and leverage are disabled** — v1 is spot-only with no margin.

---

## License

Internal use only. Not for redistribution.
