# ArbMind — AI-First Autonomous Trading Agent

**Kraken Futures paper trading | LangChain + Claude | Top-20 CMC | Mean-reversion day trades**

> ⚠️ **Paper mode by default.** No real money is used until you explicitly set `PAPER_MODE=false`.

---

## Strategy

ArbMind exploits a well-documented intraday pattern:

- **BTC and ETH are canary assets.** When both dip ≥1.5% / 1.2% within 60 minutes, correlated top-20 alts follow — and tend to recover within the same session.
- **Two high-probability windows per day (WAT / UTC+1):**
  - `05:30–06:30` — Asian session open
  - `15:00–16:30` — NY/London handoff
- **Regime gate:** Bear markets skip all longs.
- **Correlation filter:** Only alts with rolling 12h correlation ≥ 0.6 to BTC qualify.

---

## Architecture

```
main.py
  └── TradingLoop (agent/loop.py)
        ├── Loop 1: AI Decision (every 60s)
        │     ├── Quick pre-check (time window + canary) — no AI tokens burned
        │     └── Full AI analysis when conditions interesting
        │           ├── SignalEngine → regime, correlation, canary
        │           ├── CMCClient → top-20 data, fear/greed
        │           └── AIBrain (Claude Sonnet) → TradeDecision
        │                 └── LangChain tools: get_market_snapshot,
        │                     get_correlation_candidates, get_open_positions,
        │                     get_account_summary, get_cmc_coin_detail
        │
        └── Loop 2: Position Monitor (every 30s)
              ├── Enforces stop loss (−2% default)
              ├── Enforces take profit (+4% default)
              ├── Time-stop (4 hours max hold)
              └── AI position review every 30 min
```

---

## Project Structure

```
arbmind/
├── main.py                   # Entry point
├── config.py                 # Typed config from .env
├── requirements.txt
├── setup.py                  # One-time setup validator
├── .env.example              # Copy to .env and fill in keys
│
├── agent/
│   ├── loop.py               # Main + position monitor loops
│   ├── ai_brain.py           # LangChain + Claude decision engine
│   ├── signals.py            # Regime, correlation, canary signals
│   └── position_manager.py   # Paper position tracking + P&L
│
├── kraken/
│   ├── cli_wrapper.py        # Official krakenfx/kraken-cli subprocess wrapper
│   └── rest_client.py        # python-kraken-sdk REST fallback
│
├── data/
│   ├── cmc_client.py         # CoinMarketCap API client
│   ├── journal.py            # Trade log + performance summary
│   └── paper_positions.json  # Live state (auto-created)
│
└── utils/
    └── logger.py             # Rich-powered logger
```

---

## Installation

### 1. Clone and set up Python env

```bash
git clone <your-repo>
cd arbmind
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
```

### 2. Run the setup validator

```bash
python setup.py
```

This installs all pip deps, checks your `.env`, tests API connections, and creates data directories.

### 3. Install the Kraken CLI binary

Download from: https://github.com/krakenfx/kraken-cli/releases

```bash
# Linux/Mac example
chmod +x kraken
sudo mv kraken /usr/local/bin/

# Configure it
kraken setup
```

### 4. Configure API keys

```bash
cp .env.example .env
# Edit .env with your keys
```

You need:
| Key | Where to get it |
|-----|----------------|
| `ANTHROPIC_API_KEY` | https://console.anthropic.com |
| `CMC_API_KEY` | https://coinmarketcap.com/api/ (free tier works) |
| `KRAKEN_API_KEY` | kraken.com → Security → API |
| `KRAKEN_FUTURES_API_KEY` | futures.kraken.com → Settings → Create Key |

---

## Running

```bash
# Start the agent (paper mode)
python main.py

# View performance summary
python data/journal.py

# Reset paper positions
python -c "from kraken.cli_wrapper import KrakenCLI; KrakenCLI().paper_reset()"
```

---

## Risk Config (`.env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `PAPER_MODE` | `true` | Use paper trading (no real money) |
| `MAX_POSITION_PCT` | `0.05` | Max 5% of capital per trade |
| `STOP_LOSS_PCT` | `0.02` | 2% hard stop |
| `TAKE_PROFIT_PCT` | `0.04` | 4% target |
| `BTC_DIP_THRESHOLD` | `-1.5` | BTC 1h dip to trigger scan |
| `ETH_DIP_THRESHOLD` | `-1.2` | ETH confirmation dip |
| `MIN_CORRELATION` | `0.6` | Min rolling corr to BTC |
| `PAPER_CAPITAL` | `10000.0` | Starting paper balance |
| `LOOP_INTERVAL_SECONDS` | `60` | Main loop frequency |

---

## AI Decision Flow

Every trade goes through Claude. The AI:

1. Calls `get_market_snapshot` → BTC/ETH canary + regime + CMC fear/greed
2. Checks regime — **BEAR = immediate SKIP**
3. Checks time window — not active = SKIP
4. Calls `get_correlation_candidates` → alts with corr ≥ 0.6 to BTC
5. Calls `get_cmc_coin_detail` for each candidate
6. Calls `get_open_positions` + `get_account_summary` → exposure check
7. Outputs structured JSON: `ENTER | SKIP | HOLD | CLOSE`

**Confidence < 0.6 → always SKIP**, no matter how good the setup looks.

---

## Promoting to Live Trading

When you're satisfied with 2+ weeks of paper results:

1. Set `PAPER_MODE=false` in `.env`
2. Add your Futures API keys with minimum permissions (Trade + Query)
3. Reduce `MAX_POSITION_PCT` to 0.01–0.02 for the first week live
4. **Never disable stop losses**

---

## Dependencies

```
python-kraken-sdk    Official Kraken REST/WS SDK
langchain            LangChain orchestration
langchain-anthropic  Claude Sonnet integration
anthropic            Anthropic SDK
requests             CoinMarketCap API
pandas               Correlation matrix, OHLC analysis
numpy                Numerical ops
python-dotenv        Config from .env
rich                 Terminal output
aiohttp              Async HTTP
pytz                 Timezone handling (WAT/UTC+1)
```

Plus the **krakenfx/kraken-cli** binary (Rust, installed separately).

---

## Disclaimer

This is a research/educational project. Crypto trading involves substantial risk of loss.
Past paper performance does not guarantee future live results. Use at your own risk.
