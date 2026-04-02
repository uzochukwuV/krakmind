# ArbMind — AI Trading Agent

An AI-first autonomous paper trading agent for Kraken Futures using LangChain + Claude Sonnet. Trades top-20 cryptocurrencies via a mean-reversion strategy triggered by BTC/ETH canary dips.

## Architecture

- **Language**: Python 3.12
- **Package manager**: pip (`requirements.txt`)
- **AI Engine**: LangChain + Claude 3.5 Sonnet (Anthropic)
- **Exchange**: Kraken Futures (paper mode by default)

## Package Structure

The codebase uses a multi-package layout (all files are in the root, but Python packages are organized into subdirectories):

```
main.py              # Entry point
config.py            # Central config via .env / env vars
agent/
  loop.py            # Dual async loops (AI decision + position monitor)
  ai_brain.py        # LangChain + Claude LLM integration
  signals.py         # Signal engine: canary dips, regime, correlation
  position_manager.py # Paper position tracking, P&L
kraken_wrappers/
  cli_wrapper.py     # Wrapper for `kraken` CLI binary
  rest_client.py     # python-kraken-sdk REST fallback
data/
  cmc_client.py      # CoinMarketCap API client
  journal.py         # Trade logging and performance summary
  journal/           # JSON/CSV trade logs
  paper_positions.json  # Live paper position state
utils/
  logger.py          # Rich-powered logging
logs/                # Application log files
```

**Note**: The project wrapper packages use `kraken_wrappers/` (not `kraken/`) to avoid shadowing the `python-kraken-sdk`'s `kraken` namespace.

## Required Environment Variables

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Claude AI brain |
| `CMC_API_KEY` | Yes | CoinMarketCap data |
| `KRAKEN_API_KEY` | Optional | Authenticated Kraken endpoints |
| `KRAKEN_API_SECRET` | Optional | Authenticated Kraken endpoints |
| `KRAKEN_FUTURES_API_KEY` | Optional | Live futures (not needed for paper mode) |
| `KRAKEN_FUTURES_API_SECRET` | Optional | Live futures (not needed for paper mode) |

## Key Config Defaults (.env overrides)

- `PAPER_MODE=true` — always trade in paper mode unless explicitly disabled
- `PAPER_CAPITAL=10000` — starting virtual capital
- `LOOP_INTERVAL_SECONDS=60` — main AI analysis loop
- `POSITION_CHECK_INTERVAL=30` — position monitor interval
- `STOP_LOSS_PCT=0.02` — 2% stop loss
- `TAKE_PROFIT_PCT=0.04` — 4% take profit

## Workflow

- **Start application**: `python3 main.py` (console output)

## External Binary

The `kraken` CLI binary (Rust, from krakenfx/kraken-cli) is installed at version 3.2.7 and is found on PATH. The agent falls back to REST SDK if unavailable.
