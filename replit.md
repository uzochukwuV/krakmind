# ArbMind — AI Trading Agent

An AI-first autonomous paper trading agent for Kraken Futures. Uses OpenRouter free LLMs, live CoinMarketCap data, and the official Kraken CLI binary. Trades 20 altcoins via a mean-reversion strategy triggered by BTC/ETH canary dips and Prism market intelligence.

## Architecture

- **Language**: Python 3.12
- **AI Engine**: OpenRouter (free models — meta-llama/llama-3.3-70b-instruct:free + fallbacks)
- **Exchange**: Kraken Futures paper mode via `kraken-cli` v0.3.0 binary
- **Market data**: CoinMarketCap Pro API + Kraken REST SDK (OHLC, RSI, volume) + PrismAPI
- **Dashboard API**: FastAPI server on port 8000 (for dashboard frontend)

## Package Structure

```
main.py              # Entry point (starts API server + trading loops)
config.py            # Central config via env vars
agent/
  loop.py            # Triple async loops (AI decision + position monitor + Prism)
  ai_brain.py        # OpenRouter LLM integration + context builder
  signals.py         # Signal engine: canary dips, RSI, volume, regime, correlation
  position_manager.py # Paper position tracking, P&L, Kelly sizing, trailing stops
  prism_loop.py      # Independent Prism signal engine (polls every 5m)
api/
  server.py          # FastAPI dashboard API server (port 8000)
  shared_state.py    # Thread-safe in-memory state shared between loop and API
kraken_wrappers/
  cli_wrapper.py     # Wrapper for `kraken` CLI binary (/home/runner/.cargo/bin/kraken)
  rest_client.py     # python-kraken-sdk REST for OHLC data
data/
  cmc_client.py      # CoinMarketCap API client (top20, global metrics)
  prism_client.py    # PrismAPI client (F&G, sentiment, funding rates, trending)
  journal.py         # Trade logging and performance summary
  paper_positions.json  # Live paper position state (persisted across restarts)
utils/
  logger.py          # Rich-powered logging
```

## Required Environment Variables / Secrets

| Variable | Where | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | Replit Secret | OpenRouter AI brain (free models) |
| `CMC_API_KEY` | Env var (shared) | CoinMarketCap live market data |
| `KRAKEN_API_KEY` | Optional | Authenticated Kraken endpoints |
| `KRAKEN_FUTURES_API_KEY` | Optional | Live futures (not needed for paper mode) |

## Key Config Defaults

- `PAPER_MODE=true` — always paper trade unless explicitly disabled
- `PAPER_CAPITAL=10000` — starting virtual capital ($10,000)
- `LOOP_INTERVAL_SECONDS=60` — main AI analysis loop
- `POSITION_CHECK_INTERVAL=30` — position monitor interval
- `STOP_LOSS_PCT=0.02` — 2% base stop loss (trailing stops adjust this dynamically)
- `TAKE_PROFIT_PCT=0.04` — 4% take profit
- `DAILY_LOSS_LIMIT_PCT=0.03` — halt new trades if down >3% on the day

## Key Features Implemented

### Signal Engine (signals.py)
- BTC/ETH canary dip detection (1h and 15m timeframes)
- RSI (14-period, Wilder's smoothing) on BTC, ETH, and each alt candidate
- Volume spike detection (current vs 20-period average, ratio > 1.5 = spike)
- Signal quality score (0–3) per alt: dipping + oversold RSI + volume spike
- Correlation matrix (48-candle rolling window) for all 9 tradeable alts
- Market regime classification: BULL / BEAR / SIDEWAYS via SMA20 slope + SMA200
- Time window gating: 06:00–06:30 WAT and 15:00–16:30 WAT

### AI Brain (ai_brain.py)
- OpenRouter API with 5 free model fallbacks (no tool-calling needed)
- Full market snapshot injected into prompt: canary, regime, candidates, CMC data, account
- Signal quality + RSI + volume + daily loss limit all surfaced to AI
- 401 auth errors detected immediately (no wasted retries)

### Risk Management (position_manager.py)
- **Trailing stops**: break-even at +2%, profit-lock at +3% (stop moves to entry +1%)
- **Daily loss limit**: halt all new positions if today's P&L < -3% of day-start capital
- **Kelly criterion sizing**: `f* = (p*b - q) / b × 0.5 × confidence`
  - Uses actual win/loss history from closed trades
  - Falls back to `max_position_pct × confidence` with < 5 trades
  - Confidence 60% → ~3% of capital, 90% → ~4.5% of capital
- Max 3 concurrent positions, max 30% total capital deployed

### Execution (loop.py)
- Signal quality gate: weak signals (quality=0) require ≥75% AI confidence to trade
- Kelly size logged per trade execution
- Daily loss limit displayed prominently in status bar
- Win/loss count shown in status bar

## Workflow

- **Start application**: `python3 main.py` (console output)

## External Binary

The `kraken` CLI binary (Rust, krakenfx/kraken-cli v0.3.0) is installed at `/home/runner/.cargo/bin/kraken`. Paper trading uses `kraken paper buy/sell <PAIR> <VOLUME> --type market -o json`. No API keys needed for paper mode.
