# ArbMind — AI-First Autonomous Trading Agent (Hackathon Edition)

**Kraken Futures & Spot | Groq API (openai/gpt-oss-120b) | High-Beta Momentum Trading**

> ⚠️ **Paper mode by default.** No real money is used until you explicitly set `PAPER_MODE=false`.

---

## Strategy (Hackathon Pivot)

ArbMind has been aggressively restructured to maximize PnL in a short-term Hackathon environment:

- **3 Independent Trading Loops:**
  - **[PERP] Loop:** Trades Major/Stable coins via Kraken Perpetual Futures.
  - **[OPTIONS] Loop:** Evaluates Options placeholders tracking directional volatility.
  - **[MEME] Loop:** Hunts purely for high-volatility Spot Memecoins (PEPE, WIF, BONK, DOGE, SHIB).
- **Momentum Breakouts over Mean-Reversion:** Instead of waiting for market crashes, the AI triggers on massive volume spikes and RSI breakouts (> 65).
- **Loosened Risk Parameters:** Daily loss limits removed, wider stop-losses (10-15%), and higher take-profit targets (20-40%) to handle memecoin volatility and capture explosive runs.

---

## Architecture

```
main.py
  ├── Dashboard API (FastAPI + React/Vite on port 8000)
  └── TradingLoop (agent/loop.py)
        ├── Loop 1: PERP Futures (Majors)
        ├── Loop 2: OPTIONS (Directional Volatility)
        ├── Loop 3: MEME Spot (High-Beta Hunting)
        │
        ├── Position Monitor (every 30s)
        │     ├── Enforces stop loss (dynamic per loop)
        │     ├── Enforces take profit
        │     └── AI position review every 30 min
        │
        └── Prism Signal Engine (every 5m)
              └── Real-time Fear/Greed and Market Sentiment
```

### The AI Brain
The bot utilizes the **Groq API** for ultra-fast, zero-latency inference using `openai/gpt-oss-120b`. 
Instead of burning tokens on tool-calling, it uses a massive Context Builder to inject all OHLC, RSI, Volume, and Prism sentiment data into a single prompt, forcing the LLM to output a structured JSON `TradeDecision`.

---

## Installation

### 1. Clone and set up Python env

```bash
git clone <your-repo>
cd arbmind
python -m venv .venv
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Install the Kraken CLI binary

Download from: https://github.com/krakenfx/kraken-cli/releases

```bash
# Linux/Mac example
chmod +x kraken
sudo mv kraken /usr/local/bin/
```

### 4. Configure API keys

Create a `.env` file in the root directory:

```env
GROQ_API_KEY=gsk_your_groq_key_here
CMC_API_KEY=your_cmc_key_here  # Optional: falls back to mock data if missing
PAPER_MODE=true
```

---

## Running

```bash
# Start the agent (Background Loops + React Dashboard)
python main.py
```

Open `http://localhost:8000` in your browser to view the real-time cyberpunk control center!

---

## Disclaimer

This is a research/educational project optimized for paper-trading hackathons. Crypto trading involves substantial risk of loss. Past paper performance does not guarantee future live results. Use at your own risk.
