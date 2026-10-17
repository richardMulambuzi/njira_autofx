# Auto FX MW

Auto FX MW (Auto FX Malawi) is a full-stack, broker-connected forex trading platform. It ingests live ticks, builds multi-timeframe market structure, generates trade signals from a rule-based strategy engine, applies quality and risk gates, and executes orders through cTrader / Pepperstone. A backtesting and cost-aware measurement harness sits behind the pipeline.

A monorepo with two applications:

| Path | What it is | Stack |
|------|------------|-------|
| [`njira_fx_api/`](njira_fx_api/) | Trading backend: signal engine, risk manager, broker integration, REST API | Django 5.2 + DRF, ClickHouse, cTrader Open API, background workers |
| [`njira_fx_ui/`](njira_fx_ui/) | Web dashboard: accounts, live charts, signals, trade history | Next.js 16 + React 19 + TypeScript + Tailwind |

---

## 1. What it does

### Automated trading
Trades are generated and executed without manual intervention. The pipeline runs continuously: live ticks → candles → multi-timeframe structure → strategy drafts → quality gates → confirmed signal → risk sizing → broker order.

- Real-time order execution through the cTrader Open API (Pepperstone).
- Strategy roster is configurable, and only strategies that survive cost-aware measurement ship enabled.
- Full backtesting over historical candles using the same analysis pipeline as live trading.
- Runs 24/7 as a service; `TRADING_MODE` (demo / paper / live) makes live execution an explicit opt-in.

### Trading signals
Buy/sell drafts are produced from price structure, not from a black box: multi-timeframe support/resistance levels, engulfing and pin-bar rejections, order blocks, fair-value gaps, liquidity sweeps and market structure.

Every signal is persisted with its full reasoning: direction, entry, stop-loss, take-profit, risk/reward, confidence, strength, the evidence dict that produced it, and a human-readable explanation chain.

### Risk management
A dedicated risk manager sits between a confirmed signal and the broker:

- Position sizing from account balance and risk percentage per trade.
- Dynamic stop-loss / take-profit and a minimum risk-to-reward floor.
- Daily loss limit, daily trade count and daily volume caps.
- Maximum spread guard: trades are refused when the spread is too wide.
- Trailing stop / break-even handling on the live position.

### Market analysis & visualization
- Multi-timeframe aggregation: 1M, 5M, 15M, 30M, 1H, 4H, 1D.
- Structure engines for support/resistance, order blocks, fair-value gaps, liquidity, volume profile / POC, micro-structure and volatility regime.
- Session awareness and market-structure storylines, plus a contradiction advisory that warns when a setup fights current conditions.
- UI: live charts (lightweight-charts), account overview, signals list, trade history, settings.

### Backtesting & measurement
Two independent harnesses, because they answer different questions:

| Tool | Use it for |
|------|-----------|
| `manage.py backtest` | Full multi-strategy simulation over a CSV, using the real analysis pipeline, with the 70% win-rate gate reported per strategy. |
| `manage.py snr_measure` | **The cost-aware, walk-forward measurement tool.** Applies spread, slippage and commission and splits the run into out-of-sample folds. This is the only number that should drive a decision. |
| `manage.py snr_fast` | Fast, cost-free, in-sample. Useful for smoke tests only; it overstates expectancy. |

### Multi-broker & operations
- Broker abstraction layer with a Pepperstone/cTrader provider and a mock provider for development.
- API key protected tick/trade ingest endpoints for external bridges.
- JWT authentication, Swagger/OpenAPI docs, Pusher real-time updates to the UI.
- Email notifications, Docker Compose deployment, structured health and metrics endpoints.

### AI-assisted optimization
A `trading_optimizer` app uses Gemini to help analyse and tune strategy parameters against measured results.

---

## 2. Measured performance (honest numbers)

The repository's own walk-forward, cost-inclusive results are published rather than hidden. Expectancy is in R (risk multiples) per trade, net of a 1.4-pip round trip on EURUSD.

| Instrument | Period | Trades | Win rate | Expectancy |
|------------|--------|--------|----------|------------|
| EURUSD | 2021 | 761 | 58.6% | +0.228R |
| EURUSD | 2022 | 691 | 60.5% | +0.293R |
| EURUSD | 2023 | 613 | 60.8% | +0.291R |
| EURUSD | 2024 | 685 | 43.4% | **−0.151R** |
| EURUSD | Jun 2025 | 60 | 56.7% | +0.185R |
| XAUUSD | 2023 | — | — | +0.128R |
| XAUUSD | 2024 | — | — | +0.290R |
| XAUUSD | Jun 2025 | — | — | +0.380R |

XAUUSD 2024 walk-forward passed 4 of 4 folds positive, with a train/test expectancy ratio of 0.59.

**Known limits, stated plainly:** 2024 was an adverse year for EURUSD and no tested filter isolates it; the results are still in-sample in the sense that instrument and setup were chosen after seeing outcomes; roughly 56% of trading days produce no signal at all (structural: the enabled analyzer requires two joined 4H levels and only the two most recent are eligible). Silver (XAGUSD) is unprofitable at any win rate because the round-trip cost is 1.45–2.35R against 1R of risk, so the system does not trade it.

Full detail: [`njira_fx_api/README.MD`](njira_fx_api/README.MD).

---

## 3. Architecture

```
        ┌──────────────┐        ┌──────────────────────────────────────────┐
        │  Broker      │  ticks │  njira_fx_api (Django)                   │
        │  cTrader /   ├───────►│  ticks engine → candle registry          │
        │  Pepperstone │        │      → multi-timeframe aggregation       │
        └──────┬───────┘        │      → structure / liquidity / FVG /     │
               │  orders        │        order-block / volume-profile      │
               ◄────────────────┤      → strategy roster → quality gates   │
                                │      → signal engine → risk manager      │
   ┌────────────┐               │      → execution engine                  │
   │ ClickHouse │◄──────────────┤                                          │
   │ ticks      │   persistence │  REST API + Swagger + JWT                │
   │ candles    │               └───────────────┬──────────────────────────┘
   │ signals    │                               │ Pusher / REST
   └────────────┘               ┌───────────────▼──────────────────────────┐
                                │  njira_fx_ui (Next.js)                   │
                                │  dashboard · live charts · signals ·     │
                                │  trade history · settings                │
                                └──────────────────────────────────────────┘
```

The backtest engine replays base candles through the *same* pipeline the live tick engine uses, so a backtest result and a live result are produced by the same code path.

---

## 4. Tech stack

**Backend**: Python / Django 5.2, Django REST Framework, drf-yasg (OpenAPI), SimpleJWT, ClickHouse (tick, candle, signal and trade storage), cTrader Open API, pandas / numpy, backtrader, Gunicorn + WhiteNoise, Docker.

**Frontend**: Next.js 16, React 19, TypeScript, Tailwind CSS, Zustand, TanStack Query, lightweight-charts v5, Recharts, Framer Motion, Pusher, Firebase, Vitest, Storybook (pnpm).

---

## 5. Quickstart

```bash
# API
cd njira_fx_api
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in broker + ClickHouse credentials
python manage.py migrate
python manage.py runall --port 9090

# UI
cd ../njira_fx_ui
pnpm install
pnpm dev
```

Or bring both up with Docker:

```bash
docker compose up -d --build     # API :9090, UI :3000
```

Configuration lives in `njira_fx_api/.env`: Django secrets, database and ClickHouse connection, broker credentials, `TRADING_PROVIDER` / `TRADING_MODE`, risk limits, spread and position limits, timeframe map, JWT and mail keys. See the environment variable tables in [`njira_fx_api/README.MD`](njira_fx_api/README.MD).

---

## 6. Repository layout

```
.
├── njira_fx_api/          Django backend
│   ├── forex/             core app: analysis engines, broker, backtest, risk manager
│   │   ├── analysis/      structure, liquidity, order blocks, FVG, volume profile, signal engine
│   │   ├── backtest/      engine + MT4/MT5/HistData CSV parsing
│   │   ├── broker/        broker abstraction and providers
│   │   ├── pepperstone/   cTrader client, query worker, ticks engine
│   │   └── management/    CLI: runall, backtest, snr_measure, snr_fast, snr_temporal, …
│   ├── trade/ signals/ auth_app/ notifications/ bot_settings/ trading_optimizer/
│   └── docs/              generated API documentation
├── njira_fx_ui/           Next.js dashboard
│   └── src/               app routes, components, charts, chat/notifications
├── docs/                  architecture notes
├── docker-compose.yml
└── deploy.sh
```

---

## 7. Documentation

- Backend documentation and measured strategy results: [`njira_fx_api/README.MD`](njira_fx_api/README.MD)
- Frontend documentation: [`njira_fx_ui/README.md`](njira_fx_ui/README.md)
- Architecture audit: [`docs/architecture-audit.md`](docs/architecture-audit.md)
- Live OpenAPI schema: `/swagger` on a running API instance

---

## 8. Disclaimer

This software places real orders against a live brokerage account when configured to do so. Nothing in this repository is financial advice. Trading foreign exchange on margin carries a high level of risk. Measured historical results do not guarantee future results.

---

## 9. License

MIT © 2026 Richard Mlambuzi. See [LICENSE](LICENSE).
