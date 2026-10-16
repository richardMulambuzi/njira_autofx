# Auto FX MW — Backend

The trading backend of **Auto FX MW (Auto FX Malawi)**: a Django service that ingests live market data, analyses multi-timeframe price structure, produces trade signals, applies risk and quality gates, and executes orders through a broker.

- Django 5.2 + Django REST Framework + drf-yasg
- ClickHouse for ticks, candles, signals and trade history
- cTrader Open API (Pepperstone) for market data and execution, plus a mock provider for development
- Docker for deployment

Frontend lives in [`../njira_fx_ui`](../njira_fx_ui). Platform overview: [root README](../README.md).

---

## Contents

1. [What it does](#1-what-it-does)
2. [Trading pipeline](#2-trading-pipeline)
3. [Architecture and apps](#3-architecture-and-apps)
4. [Quickstart](#4-quickstart)
5. [Configuration](#5-configuration)
6. [Management commands](#6-management-commands)
7. [Backtesting and measurement](#7-backtesting-and-measurement)
8. [Strategy roster and measured results](#8-strategy-roster-and-measured-results)
9. [Risk management](#9-risk-management)
10. [API](#10-api)
11. [Testing](#11-testing)
12. [Deployment](#12-deployment)
13. [Known limits](#13-known-limits)
14. [License](#14-license)

---

## 1. What it does

**Automated trading** is the core capability. Trades are generated, sized and executed without manual intervention:

- Executes buy and sell orders in real time against a live brokerage account.
- Strategy roster is configurable, and only analyzers that pass cost-aware measurement are enabled by default.
- Strategies can be backtested against historical data before going live.
- Runs continuously as a background service.
- Risk parameters — stop-loss, take-profit, position size — are applied automatically between signal and order.

Supporting capabilities:

| Capability | Detail |
|-----------|--------|
| Trading signals | Real-time signal drafts from multi-timeframe structure, with a persisted evidence trail and human-readable explanation per signal. |
| Market analysis | Structure, support/resistance, order blocks, fair-value gaps, liquidity, volume profile / POC, micro-structure, volatility regime, sessions and storylines. |
| Risk management | Balance-based position sizing, dynamic SL/TP, minimum risk-to-reward floor, daily loss/trade/volume caps, spread guard, trailing stop and break-even. |
| Broker integration | Broker abstraction layer with a Pepperstone/cTrader Open API provider and a mock provider. Market, limit and stop orders. |
| Multi-timeframe | 1M, 5M, 15M, 30M, 1H, 4H, 1D aggregated from the base feed. |
| Storage | ClickHouse tables for ticks, candles, signals and trade history. |
| Realtime | Pusher channels push signal and trade updates to the dashboard. |
| AI-assisted tuning | A `trading_optimizer` app uses Gemini to help analyse and tune strategy parameters against measured results. |
| Notifications | Transactional email for signals and account events. |

---

## 2. Trading pipeline

Each closed base candle (default M1) is pushed through the same chain, whether it arrives live or from a backtest replay:

```
broker feed
   │  ticks
   ▼
ticks engine ──► candle registry ──► multi-timeframe aggregation (1M…1D)
                                        │
                                        ▼
                          intelligence engines
                          structure · liquidity · order blocks · FVG
                          volume profile / POC · micro-structure · volatility
                                        │
                                        ▼
                          market context (MTF bias, sessions, storyline,
                                          contradiction advisory)
                                        │
                                        ▼
                          strategy roster ──► draft signals
                                        │
                                        ▼
                          quality & confluence gates   ← most drafts die here
                                        │
                                        ▼
                              confirmed signal  (direction, entry, SL, TP,
                                                 confidence, strength, evidence)
                                        │
                                        ▼
                                 risk manager  (sizing, caps, spread guard)
                                        │
                                        ▼
                                execution engine ──► broker order
```

Every stage can emit funnel counters, so it is possible to see exactly how many candidates an analyzer produced and how many survived the gates.

The backtest engine (`forex/backtest/engine.py`) replays this identical chain over historical candles — the live and simulated code paths are the same.

---

## 3. Architecture and apps

Django apps:

| App | Responsibility |
|-----|----------------|
| `forex` | Core: analysis engines, signal engine, broker layer, backtesting, risk manager, market API, ticks API, realtime, ClickHouse access, walk-forward harness. |
| `trade` | Trade records, order execution endpoints, trade history. |
| `signals` | Persisted signals and their lifecycle. |
| `auth_app` | Accounts, JWT authentication, permissions. |
| `notifications` | Email and in-app notification delivery. |
| `bot_settings` | Per-account strategy and bot configuration. |
| `trading_optimizer` | Gemini-assisted parameter analysis and tuning. |
| `source` | Sphinx documentation sources. |
| `tests` | Cross-app integration tests. |

Inside `forex/`:

| Module | Responsibility |
|--------|----------------|
| `analysis/` | 39 modules: `signal_engine.py`, `snr_breakout.py`, `marriage.py`, `engulfing.py`, `market_structure.py`, `liquidity.py`, `order_blocks.py`, `fvg.py`, `volume_profile*.py`, `confluence_gate.py`, `quality_gate.py`, `context.py`, `sessions.py`, `storyline.py`, `signal_lifecycle.py`, `volatility.py`, `microstructure.py`, `poc.py`, and the candle/cache plumbing. |
| `backtest/` | `engine.py` (multi-strategy simulator), `parser.py` (CSV parsing for MT4/MT5 exports and HistData), `views.py`. |
| `broker/` | `base.py`, `factory.py`, `providers/`, `config.py`, health checks. |
| `pepperstone/` | cTrader client, `query_worker.py` (paged history worker), `ticks_engine.py` (live feed + candle closes), `execution_engine.py`. |
| `sim/` | Cost models and reward/risk shaping used by measurement and simulation. |
| `management/commands/` | CLI surface — see [Management commands](#6-management-commands). |
| `walkforward.py` | Cost-aware replay with walk-forward folds. |
| `risk_manager.py` | Position sizing, caps and spread guard. |
| `mtf_strategy.py`, `realtime.py`, `metrics.py`, `health.py`, `streams`, `urls.py`, `swagger.py`. |

---

## 4. Quickstart

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # then fill it in
python manage.py migrate
python manage.py runall --port 9090
```

`runall` starts the tick engine, the aggregation workers and the HTTP server together. For a lighter loop see `start_services`.

Useful first checks:

```bash
python manage.py check
python manage.py test
curl http://localhost:9090/api/health/
```

---

## 5. Configuration

All configuration is environment based (`.env`). Nothing is committed — `.env` is gitignored.

**Core**

| Variable | Purpose |
|----------|---------|
| `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS` | Django and CORS configuration |
| `PRIVATE_KEY_CONTENT` | JWT signing key |
| `TRADING_PROVIDER` | `pepperstone` or `mock` (default `mock`) |
| `TRADING_MODE` | `demo`, `paper` or `live`. `live` must be opted into explicitly. |
| `TRADING_SYMBOLS`, `TRADING_ASSETS` | Instruments traded |
| `TIMEFRAMES` | Timeframe map, e.g. `{"Day":86400,"4H":14400,"1H":3600,"30M":1800,"15M":900,"5M":300,"1M":60}` |

**Broker**

`PEPPERSTONE_CLIENT_ID`, `PEPPERSTONE_CLIENT_SECRET`, `PEPPERSTONE_ACCESS_TOKEN`, `PEPPERSTONE_REFRESH_TOKEN`, `PEPPERSTONE_ACCOUNT_ID`.

**Storage**

`CLICKHOUSE_HOST`, `CLICKHOUSE_PORT`, `CLICKHOUSE_USER`, `CLICKHOUSE_PASSWORD`, `CLICKHOUSE_DATABASE` plus table names for ticks, candles, trade history, signals and FVG.

**Risk and trade limits**

| Variable | Purpose |
|----------|---------|
| `RISK_STARTING_BALANCE`, `RISK_ACCOUNT_ID` | Risk baseline |
| `RISK_PERCENTAGE`, `RISK_MAX_DAILY_LOSS_PCT` | Risk per trade and the daily loss cap |
| `RISK_MAX_SPREAD_PIPS` | Spread guard |
| `REWARD_TO_RISK_RATIO`, `MIN_RISK_REWARD_RATIO`, `MIN_TP_PIPS`, `MAX_TP_PIPS` | Target and stop shaping |
| `TRADE_MAX_VOLUME`, `TRADE_DAILY_LIMIT`, `TRADE_DAILY_VOLUME_LIMIT` | Volume and frequency caps |
| `BUFFER_PIPS`, `TOLERANCE`, `MARGIN_THRESHOLD`, `PIP_VALUE` | Execution calibration |

**Strategy roster**

| Variable | Purpose |
|----------|---------|
| `SNR_MALAYSIAN_ANALYZERS` | Comma-separated analyzers to enable, e.g. `"marriage,snr"`. Invalid names are ignored. Default is `marriage` only. |

**Integration**

`TICKS_INGEST_API_KEY` (API key for the tick and trade ingest endpoints), `PUSHER_*` (realtime), `MAIL_*` and `RESEND_API_KEY` (email), `GEMINI_API_KEY` (trading optimizer), `DEV_URL`, `PROD_URL`, `PROD_DB_URL`.

---

## 6. Management commands

```bash
# services
python manage.py runall --port 9090        # everything: ticks, aggregation, HTTP
python manage.py start_services            # service group without the HTTP server
python manage.py ticks_engine              # live tick ingest + candle closes
python manage.py mtf_aggregator            # multi-timeframe aggregation
python manage.py backfill_history          # historical backfill

# simulation and measurement
python manage.py backtest --csv <file> --symbol <SYM> [--timeframe 60] [--bars N] \
                          [--strategies A,B] [--balance N] [--risk N] \
                          [--funnel-only] [--fast]
python manage.py snr_measure --csv <file> --symbol <SYM> [--analyzer marriage] \
                             [--folds 4] [--spread S] [--slippage S] [--commission N]
python manage.py snr_temporal --csv <file> --symbol <SYM>
python manage.py snr_fast --csv <file> --symbol <SYM>
python manage.py simulate
```

| Command | Notes |
|---------|-------|
| `backtest` | Full multi-strategy simulation using the real analysis pipeline. `--funnel-only` replays signals and prints the funnel without simulating trades. `--fast` skips the intelligence engines (SNR only). |
| `snr_measure` | Cost-aware, walk-forward measurement. Applies spread, slippage and commission and reports per-fold out-of-sample results plus a train/test drift ratio. |
| `snr_temporal` | Session/temporal distribution of signals. |
| `snr_fast` | Cost-free and in-sample. Smoke tests only — never a tuning signal. |

CSV input accepts MT4/MT5 exports (`YYYY.MM.DD,HH:MM,open,high,low,close,volume`) and HistData semicolon format, and fails fast on implausible magnitudes (e.g. a gold series with EURUSD-sized prices).

---

## 7. Backtesting and measurement

There are deliberately three tools, because they answer different questions.

| Tool | Costs modelled | Out-of-sample | Use for |
|------|----------------|---------------|---------|
| `snr_fast` | ✗ | ✗ | smoke tests |
| `backtest` | ✗ | ✗ | full-pipeline behaviour and the win-rate gate per strategy |
| `snr_measure` | ✓ spread + slippage + commission | ✓ walk-forward folds | **any decision that changes the strategy** |

Costs are applied in R (risk-multiple) space, which avoids the contract-size errors that make naive "dollars per pip" estimates wrong by orders of magnitude:

```
cost_in_R = (2 × spread + 2 × slippage) / |entry − stop|
pnl_usd   = (move_in_R − cost_in_R) × balance × risk_fraction
```

Exits are deliberately conservative: when a single bar spans both the stop and the target, the **stop** is taken, because OHLC data cannot say which printed first. Entry fills are located by matching the recorded entry price to the candle that produced it, falling back to the confirmation bar.

Example:

```bash
python manage.py snr_measure --csv EURUSD_2024.csv --symbol EURUSD \
    --analyzer marriage --folds 4 --balance 100000 \
    --spread 0.00012 --slippage 0.00002
```

Default cost parameters are instrument-specific and expressed in price units:

| Instrument | Spread | Slippage |
|------------|--------|----------|
| EURUSD | 0.00012 (1.2 pips) | 0.00002 |
| XAUUSD | 0.30 / oz | 0.05 |

The report ends with a cost-realism diagnostic — the stop-width distribution and the round-trip cost expressed in R at the median stop — and flags `COSTS EXCEED 1R` when a market cannot be traded profitably at any win rate.

---

## 8. Strategy roster and measured results

The Malaysian SNR family contains six analyzers: `snr`, `engulfing`, `marriage`, `flip`, `qml` and `confluence`.

**Only `marriage` ships enabled.** The others remain in the codebase and unit-tested but are disabled by default, because cost-aware walk-forward measurement showed negative expectancy for them. They can be re-enabled with the `SNR_MALAYSIAN_ANALYZERS` environment variable. A draft without an `evidence["analyzer"]` tag is dropped rather than guessed.

Measured expectancy, EURUSD, net of a 1.4-pip round trip, in R per trade:

| Period | Trades | Win rate | Expectancy |
|--------|--------|----------|------------|
| 2021 | 761 | 58.6% | +0.228R |
| 2022 | 691 | 60.5% | +0.293R |
| 2023 | 613 | 60.8% | +0.291R |
| 2024 | 685 | 43.4% | **−0.151R** |
| Jun 2025 | 60 | 56.7% | +0.185R |

XAUUSD at a $0.30/oz spread: 2023 **+0.128R**, 2024 **+0.290R**, Jun 2025 **+0.380R**. XAUUSD 2024 passed walk-forward 4 of 4 folds positive with a train/test expectancy ratio of 0.59.

**XAGUSD is not tradable with this system.** The round-trip cost is 1.45–2.35R against 1R of risk, so it is unprofitable at any win rate; breakeven would need a spread around 0.008 (0.8 cents/oz), far tighter than retail silver.

---

## 9. Risk management

`forex/risk_manager.py` sits between a confirmed signal and the broker:

- Position size derived from account balance and the risk percentage per trade.
- Stop-loss and take-profit applied to every order; a minimum risk-to-reward floor rejects setups with no room.
- Daily loss cap, daily trade count cap and daily volume cap.
- Spread guard — orders are refused when the current spread exceeds the configured maximum.
- Break-even and trailing-stop management on an open position.
- Account state, margin threshold and volume limits validated before submission.

---

## 10. API

- JWT-authenticated REST API (SimpleJWT) with per-account permissions.
- OpenAPI schema and interactive docs served by drf-yasg at `/swagger`.
- Tick and trade ingest endpoints secured with an `X-API-Key` header (`TICKS_INGEST_API_KEY`) for external bridges.
- Health and metrics endpoints for monitoring.
- Pusher channels for realtime signal and trade updates to the dashboard.

---

## 11. Testing

The suite is run with Django's test runner:

```bash
python manage.py test
```

Coverage includes the signal engine and analyzers, the Malaysian SNR family, marriage, the backtest engine and MTF flow, walk-forward behaviour, win-rate gates, the strategy registry, quality gates, signal de-duplication, authentication and authorisation, the market API and the architecture boundary rules.

---

## 12. Deployment

```bash
docker compose up -d --build
```

Or from the repository root:

```bash
./deploy.sh          # down --remove-orphans, build --no-cache, up -d
```

The API container runs `python manage.py runall --port 9090` behind Gunicorn with WhiteNoise serving static files.

---

## 13. Known limits

These are published deliberately, because a measured number that hides its own weaknesses is worse than no number.

- Results are still **in-sample** in the sense that instrument and setup were chosen after seeing outcomes.
- **2024 was an adverse year for EURUSD** (−0.151R over 685 trades) and no tested filter isolates that regime.
- Roughly **56% of trading days produce no signal at all**. This is structural: `marriage` requires two 4H SNR levels to be joined, and only the two most recent levels are eligible.
- The core `snr` analyzer fires only **2–7 times a year**, which is too few trades to measure meaningfully.
- **Silver is not tradable** with this system at retail spreads (see above).
- `backtest` does not model costs, so its P&L will always look better than `snr_measure`. Only `snr_measure` should be trusted for a decision.

---

## 14. License

MIT © 2026 Richard Mlambuzi — see the root [LICENSE](../LICENSE).

This software places real orders when configured for live mode. Nothing here is financial advice.
