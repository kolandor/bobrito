# Bobrito — Automated Trading Bot for Binance Spot BTC/USDT

**Version 1.3** · Python 3.11+ · FastAPI · SQLite · Docker · Web UI

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
8. [Web UI](#web-ui)
9. [Docker Deployment](#docker-deployment) — [local](#option-a--docker-compose-local--development) · [production server](#option-b--production-server-deployment)
10. [Running Tests](#running-tests)
11. [Project Structure](#project-structure)
12. [Safety Notes](#safety-notes)

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
| `MAX_TRADES_PER_DAY` | `10` | Maximum trades per calendar day (UTC) |
| `EMA_FAST` | `9` | Fast EMA period |
| `EMA_SLOW` | `21` | Slow EMA period |
| `ATR_PERIOD` | `14` | ATR lookback period |
| `VOLUME_MULTIPLIER` | `1.5` | Volume confirmation multiplier |
| `API_SECRET_KEY` | `change_me…` | Bearer token for API auth |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/bobrito.db` | Database connection string |

See [Configuration Changes in v1.3](#configuration-changes-in-v13) for additional v1.3 parameters.

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

- Stop: `entry_price − STOP_ATR_MULTIPLIER × ATR` (default 1.5)
- Target: `entry_price + TARGET_ATR_MULTIPLIER × ATR` (default 3.0)
- Risk/Reward ≥ 2:1

### Exit conditions

| Trigger | Action |
|---------|--------|
| Stop price hit | Market SELL |
| Target price hit | Market SELL |
| Momentum failure (v1.3) | See [Momentum Failure Exit](#momentum-failure-exit-v13) below |
| Emergency stop | Immediate market SELL |

### Regime filter

No trades are taken when the market is detected as sideways (fast EMA ≈ slow EMA on 5m).

---

## Configuration Changes in v1.3

v1.3 introduces ~25 new configurable parameters, all with sensible defaults. Existing `.env` files continue to work without modification.

| Category | Variables |
|----------|-----------|
| Exchange filters | `ALLOW_FILTER_FALLBACK`, `FALLBACK_STEP_SIZE`, `FALLBACK_MIN_QTY`, `FALLBACK_MIN_NOTIONAL`, `FALLBACK_TICK_SIZE` |
| Strategy | `EMA_MIN_SEPARATION_PCT`, `PULLBACK_LOOKBACK_BARS`, `PULLBACK_NEAR_SLOW_EMA_PCT`, `VOLUME_SMA_PERIOD`, `STOP_ATR_MULTIPLIER`, `TARGET_ATR_MULTIPLIER`, `MIN_1M_WARMUP_CANDLES`, `MIN_5M_WARMUP_CANDLES`, `SWING_LOW_LOOKBACK` |
| Fee filter | `MIN_EXPECTED_EDGE_ENABLED`, `ESTIMATED_ROUNDTRIP_FEE_BPS`, `ESTIMATED_ROUNDTRIP_SLIPPAGE_BPS`, `MIN_EXPECTED_NET_EDGE_BPS`, `MIN_TARGET_DISTANCE_BPS` |
| Momentum failure | `MOMENTUM_FAILURE_CONFIRM_BARS`, `MOMENTUM_FAILURE_MIN_HOLD_BARS`, `MOMENTUM_FAILURE_EXIT_EMA` |

---

## Fee-Aware Entry Filter (v1.3)

Before validating an entry, the bot applies a **fee-aware filter** that rejects trades when:

- **Target distance** — The distance from entry to target (in bps) is below `MIN_TARGET_DISTANCE_BPS` (default 45).
- **Expected net edge** — After subtracting estimated roundtrip fees and slippage (`ESTIMATED_ROUNDTRIP_FEE_BPS` + `ESTIMATED_ROUNDTRIP_SLIPPAGE_BPS`), the expected net edge is below `MIN_EXPECTED_NET_EDGE_BPS` (default 15).

Rejections are persisted as `RiskEvent` with types `MIN_TARGET_DISTANCE` or `MIN_EXPECTED_EDGE`, logged at INFO with computed bps values (distance, net edge, cost) and thresholds, and counted in Prometheus `risk_events_total`. Set `MIN_EXPECTED_EDGE_ENABLED=false` to disable.

---

## Momentum Failure Exit (v1.3)

The momentum-failure exit now requires **two** conditions before triggering:

1. **Min hold bars** — Position must be held for at least `MOMENTUM_FAILURE_MIN_HOLD_BARS` (default 2) 1m candles.
2. **Confirm bars** — Close must be below the selected exit EMA for `MOMENTUM_FAILURE_CONFIRM_BARS` (default 2) consecutive bars.

The exit EMA is configurable: `MOMENTUM_FAILURE_EXIT_EMA=fast` or `slow`. This reduces false exits from single-bar noise.

---

## UTC Daily Reset Behavior (v1.3)

All daily counters (trades, PnL, midnight reset) use **UTC** via `datetime.utcnow().date()`. The trading day is aligned to UTC midnight, not local time. Limit overrides revert to ENV defaults at midnight UTC.

---

## Exchange Filter Loading (v1.3)

Symbol filters (`stepSize`, `minQty`, `minNotional`, `tickSize`) are now fetched from Binance `exchangeInfo` and used for order quantity/price quantization.

- **Live / Testnet:** If filters cannot be loaded, the bot raises `RuntimeError` and refuses to start (unless `ALLOW_FILTER_FALLBACK=true`).
- **Paper:** If filters fail, the bot activates safe mode, uses fallback values for quantization, and **blocks all new entries** until safe mode is cleared.
- **Fallback:** When `ALLOW_FILTER_FALLBACK=true`, fallback values from `.env` are used. **Not recommended for live trading.**

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
8. Fee-aware filter passed (see [Fee-Aware Entry Filter](#fee-aware-entry-filter-v13))

### Position sizing

```
risk_amount = capital × risk_per_trade_pct / 100
quantity    = risk_amount / stop_distance
```

Rounded to exchange `stepSize` ( Decimal-safe quantization), capped by available balance.

### Safe mode

Activated automatically on critical errors (including exchange filter unavailability in paper mode). **Blocks all new entries.** Cleared manually via `POST /bot/resume` after investigation.

---

## Execution Modes

| Mode | Description |
|------|-------------|
| `paper` | In-memory simulation with real market data. No API keys needed. If exchange filters cannot be loaded, safe mode activates and **no new entries are opened**. |
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

### Option A — docker-compose (local / development)

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

### Option B — Production Server Deployment

This section covers deploying the bot on a remote Linux server using the Docker Hub image published by CI.

#### What gets stored where

The container writes to two internal paths. Mount them to your host so data survives restarts and updates:

| Inside container | Host path | Contents |
|---|---|---|
| `/app/data/bobrito.db` | `~/bobrito/data/` | SQLite database — all trades, signals, positions |
| `/app/logs/bobrito.log` | `~/bobrito/logs/` | Application log file |

#### Step 1 — Connect to your server

```bash
ssh user@your-server-ip
```

#### Step 2 — Create the working directory

```bash
mkdir -p ~/bobrito/data
mkdir -p ~/bobrito/logs
cd ~/bobrito
```

#### Step 3 — Create the `.env` file on the server

```bash
nano ~/bobrito/.env
```

Paste the contents of your local `.env` file. Before saving, generate a real `API_SECRET_KEY`:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Replace the `change_me_to_a_random_secret_at_least_32_chars` placeholder with the output, then save (`Ctrl+O` → `Enter` → `Ctrl+X`).

Restrict file permissions so only your user can read it:

```bash
chmod 600 ~/bobrito/.env
```

#### Step 4 — Install Docker (if not already installed)

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker
```

#### Step 5 — Pull the image

```bash
docker pull kolandor/bobrito:latest
```

#### Step 6 — Run the container

```bash
docker run -d \
  --name bobrito \
  --env-file ~/bobrito/.env \
  -p 8080:8080 \
  -v ~/bobrito/data:/app/data \
  -v ~/bobrito/logs:/app/logs \
  --restart unless-stopped \
  kolandor/bobrito:latest
```

| Flag | Purpose |
|---|---|
| `-d` | Run in background (detached) |
| `--name bobrito` | Name the container for easy management |
| `--env-file ~/bobrito/.env` | Load all config from your file |
| `-p 8080:8080` | Expose the API and Web UI on port 8080 |
| `-v ~/bobrito/data:/app/data` | Persist the database on the host |
| `-v ~/bobrito/logs:/app/logs` | Persist logs on the host |
| `--restart unless-stopped` | Auto-restart after server reboot or crash |

#### Step 7 — Verify it's running

```bash
# Check the container is up
docker ps

# View live logs
docker logs -f bobrito

# Health check
curl http://localhost:8080/health
```

If `WEB_UI_ENABLED=true`, the dashboard is available at:
```
http://your-server-ip:8080/ui
```

#### Day-to-day management

```bash
# Stop the bot
docker stop bobrito

# Start it again
docker start bobrito

# Restart (e.g. after editing .env)
docker restart bobrito
```

#### Updating to a new version

When a new image is pushed to Docker Hub after a commit to `main`:

```bash
docker stop bobrito
docker rm bobrito
docker pull kolandor/bobrito:latest
docker run -d \
  --name bobrito \
  --env-file ~/bobrito/.env \
  -p 8080:8080 \
  -v ~/bobrito/data:/app/data \
  -v ~/bobrito/logs:/app/logs \
  --restart unless-stopped \
  kolandor/bobrito:latest
```

Your database (`~/bobrito/data/`) and logs (`~/bobrito/logs/`) are unaffected by updates — they live on the host, not inside the container.

#### Changing a config value

```bash
nano ~/bobrito/.env
# Make your change, save, then restart:
docker restart bobrito
```

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

## Web UI

Bobrito includes an **optional embedded operator dashboard** built with FastAPI + Jinja2 + HTMX + Alpine.js. It runs inside the same process as the API — no separate frontend server or build step required.

### Enabling the UI

The UI is **disabled by default**. Set `WEB_UI_ENABLED=true` in your `.env` file:

```env
WEB_UI_ENABLED=true
WEB_UI_ROUTE_PREFIX=/ui
WEB_UI_USERNAME=admin
WEB_UI_PASSWORD=change_me_strong_password
WEB_UI_SESSION_SECRET=change_me_very_long_random_secret_at_least_32_chars
```

Then start the bot normally:

```bash
cp .env.example .env
# Edit .env: set WEB_UI_ENABLED=true and the credentials above
python -m bobrito.main
# Open http://localhost:8080/ui in your browser
```

Or with Docker:

```bash
docker-compose up -d
# Open http://localhost:8080/ui
```

### UI Configuration

| Variable | Default | Description |
|---|---|---|
| `WEB_UI_ENABLED` | `false` | Enable the embedded Web UI |
| `WEB_UI_ROUTE_PREFIX` | `/ui` | URL prefix for all UI routes |
| `WEB_UI_READONLY` | `false` | Disable all control actions (monitoring only) |
| `WEB_UI_USERNAME` | `admin` | Login username |
| `WEB_UI_PASSWORD` | `change_me_strong_password` | Login password |
| `WEB_UI_SESSION_SECRET` | `change_me…` | Secret for signing session cookies (min 32 chars) |
| `WEB_UI_PAGE_REFRESH_SECONDS` | `5` | HTMX polling interval for live updates |
| `WEB_UI_ALLOW_START_STOP` | `true` | Expose start/stop/pause/resume buttons |
| `WEB_UI_ALLOW_EMERGENCY_STOP` | `true` | Expose the emergency stop button |
| `WEB_UI_CONFIRM_LIVE_ACTIONS` | `true` | Require confirmation for actions in live mode |

### What the UI Provides

The operator dashboard shows:
- **Bot Status** — current state (RUNNING / PAUSED / STOPPED / SAFE_MODE), uptime, market feed lag
- **Balances** — free USDT, free BTC, estimated equity
- **Open Position** — entry price, stop, target, unrealised PnL, lifetime
- **Performance Metrics** — daily PnL, cumulative PnL, win rate, max drawdown
- **Risk Guard** — daily trade count, consecutive losses, safe mode flag
- **Control Buttons** — Start, Pause, Resume, Stop, Emergency Stop (with confirmation)
- **Recent Events** — audit trail of system events
- **Trades History** — closed positions with PnL breakdown

### Operational Flow

| Goal | Action |
|---|---|
| Start the bot | Click **Start** on Dashboard → Bot Control |
| Pause new entries | Click **Pause** (exits still monitored) |
| Resume after pause | Click **Resume** |
| Graceful shutdown | Click **Stop** (requires confirmation) |
| Emergency halt | Click **Emergency Stop** → Confirm (always requires confirmation) |
| Monitor only | Set `WEB_UI_READONLY=true` — buttons are disabled |

### Read-Only Mode

Set `WEB_UI_READONLY=true` to disable all control actions. The dashboard remains fully functional for monitoring. A banner clearly labels the interface as **READ ONLY MODE**.

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
│   ├── engine/           # Bot orchestrator
│   └── ui/               # Optional Web UI (FastAPI + Jinja2 + HTMX)
│       ├── routes.py         # HTML pages, HTMX partials, action routes
│       ├── services.py       # UI data aggregation
│       ├── viewmodels.py     # Template-specific data structures
│       ├── auth.py           # Session-based authentication
│       ├── dependencies.py   # FastAPI DI helpers
│       ├── templates/        # Jinja2 HTML templates
│       └── static/           # CSS + JS assets
├── tests/
│   ├── unit/             # Pure function + UI auth/viewmodel tests
│   └── integration/      # Pipeline + UI route tests
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
6. **Review daily loss limits** — `MAX_DAILY_LOSS_PCT=3%` means a maximum drawdown of 6 USDT per day on 200 USDT capital. Daily counters reset at midnight UTC.
7. **Futures and leverage are disabled** — v1 is spot-only with no margin.
8. **Do not expose the Web UI publicly** without authentication. Use `WEB_UI_USERNAME` / `WEB_UI_PASSWORD` and set a strong `WEB_UI_SESSION_SECRET`.
9. **Start in paper mode** before enabling the UI in production — `BOT_MODE=paper` lets you verify the dashboard works correctly without risk.
10. **Live mode is clearly marked** in the UI with a red warning banner on every page.

---

## License

Internal use only. Not for redistribution.
