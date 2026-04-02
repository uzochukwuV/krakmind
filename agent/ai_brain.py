"""
AI Brain — OpenRouter (free models, no tool calling required)
Strategy: gather ALL market data upfront, inject into one rich prompt,
let the LLM reason and output a structured TradeDecision JSON.
This approach works with any free model, no function-calling support needed.
"""

import json
import re
import os
import time
from typing import Optional
from openai import OpenAI
from utils.logger import get_logger
from config import config

logger = get_logger("ai_brain")

OPENROUTER_BASE = "https://openrouter.ai/api/v1"

# Models tried in priority order — all free, no tool-use required
FREE_MODELS = [
    "qwen/qwen3.6-plus:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "openai/gpt-oss-20b:free",
    "qwen/qwen3-next-80b-a3b-instruct:free",
]


def _get_client() -> OpenAI:
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    return OpenAI(
        base_url=OPENROUTER_BASE,
        api_key=api_key,
        default_headers={
            "HTTP-Referer": "https://arbmind.replit.app",
            "X-Title": "ArbMind Trading Agent",
        },
    )


# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are ArbMind, an autonomous crypto trading agent specialising in
mean-reversion day trades on Kraken Futures (paper mode).

Your edge:
- BTC and ETH are canary assets. When they dip -1.5% / -1.2% in 1h, correlated
  top-20 alts tend to follow, then recover within the same session.
- Two high-probability windows per day (UTC+1): 06:00-06:30 and 15:00-16:30.
- In BEAR regimes, skip all longs. In BULL/SIDEWAYS, look for the dip entry.

Decision rules (follow strictly):
1. If regime = BEAR → output SKIP.
2. If not in time window AND no dip triggered → output SKIP.
3. If BTC and ETH are NOT both dipping below thresholds → output SKIP.
4. If 3 positions already open → output HOLD.
5. Only trade alts with correlation >= 0.6 to BTC.
6. Never exceed 5% capital per trade. Max 3 concurrent positions.
7. Confidence < 0.6 → SKIP.
8. Always set stop_loss_pct >= 0.02.
9. SIGNAL QUALITY FILTERS (prefer alts with all three):
   a. RSI < 35 on the alt (oversold = mean-reversion likely). Prefer this strongly.
   b. Volume spike on BTC (volume_ratio > 1.5) confirms the move is real.
   c. Alt dipping > 0.3% in 1h alongside BTC/ETH.
   If daily_loss_limit_hit = true → output SKIP immediately (capital protection day).
10. signal_quality score 2–3 = strong setup. Score 0–1 = weak, require confidence >= 0.75.

You will receive a full market snapshot. Analyse it and output EXACTLY this JSON (no other text after it):

```json
{
  "decision": "ENTER",
  "confidence": 0.75,
  "reasoning": "2-3 sentence explanation referencing RSI, volume, correlation",
  "trades": [
    {
      "symbol": "PF_SOLUSD",
      "direction": "long",
      "size_pct_capital": 0.05,
      "entry_type": "market",
      "stop_loss_pct": 0.02,
      "take_profit_pct": 0.04,
      "thesis": "SOL corr=0.82, RSI=28 (oversold), vol_spike=true, dipped -2.1%"
    }
  ],
  "market_context": "one-line summary",
  "next_check_minutes": 15
}
```

decision must be one of: ENTER, SKIP, HOLD, CLOSE
If decision is SKIP/HOLD/CLOSE, set trades to [].
"""


# ── Context builder ───────────────────────────────────────────────────────────

class ContextBuilder:
    """Gathers all live market data and formats it into a prompt context block."""

    def __init__(self, signal_engine, cmc_client, position_manager):
        self.signals = signal_engine
        self.cmc = cmc_client
        self.positions = position_manager

    def build(self) -> str:
        sections = []

        # 1. Time window & canary
        try:
            in_window, window_label = self.signals.is_dip_window()
            next_window = self.signals.minutes_to_next_window()
            canary = self.signals.get_canary_signal()
            btc = canary['btc']
            eth = canary['eth']
            btc_rsi = btc.get('rsi', {})
            eth_rsi = eth.get('rsi', {})
            btc_vol = btc.get('volume', {})
            eth_vol = eth.get('volume', {})
            sections.append(f"""## Time & Canary Signals
- In trading window: {in_window} ({window_label if in_window else f'next in {next_window}m'})
- BTC 1h change: {self._fmt(btc['change_1h_pct'])}% (threshold: {btc['threshold']}%)
- BTC 15m change: {self._fmt(btc['change_15m_pct'])}%
- BTC price: ${self._fmt(btc['price'], 0)}
- BTC RSI(14): {self._fmt(btc_rsi.get('rsi'))} [{btc_rsi.get('signal', 'unknown')}]
- BTC volume_ratio (1h vs 20-avg): {self._fmt(btc_vol.get('volume_ratio'))} [spike={btc_vol.get('spike')}]
- ETH 1h change: {self._fmt(eth['change_1h_pct'])}% (threshold: {eth['threshold']}%)
- ETH 15m change: {self._fmt(eth['change_15m_pct'])}%
- ETH price: ${self._fmt(eth['price'], 0)}
- ETH RSI(14): {self._fmt(eth_rsi.get('rsi'))} [{eth_rsi.get('signal', 'unknown')}]
- ETH volume_ratio (1h vs 20-avg): {self._fmt(eth_vol.get('volume_ratio'))} [spike={eth_vol.get('spike')}]
- Dip triggered: {canary['dip_triggered']}
- Strong signal (dip+RSI+vol): {canary.get('strong_signal', False)}""")
        except Exception as e:
            sections.append(f"## Time & Canary Signals\nError: {e}")

        # 2. Regime
        try:
            regime = self.signals.classify_regime()
            sections.append(f"""## Market Regime
- Regime: {regime.get('regime')}
- BTC price: ${self._fmt(regime.get('btc_price', 0), 0)}
- SMA20 slope (5d): {self._fmt(regime.get('sma20_slope_5d'))}%
- Above SMA20: {regime.get('above_sma20')}
- Above SMA200: {regime.get('above_sma200')}
- Reason: {regime.get('reason', '')}""")
        except Exception as e:
            sections.append(f"## Market Regime\nError: {e}")

        # 3. Correlation candidates
        try:
            candidates = self.signals.get_high_correlation_alts()
            if candidates:
                rows = "\n".join(
                    f"  - {c['symbol']}: corr={self._fmt(c.get('correlation'))}, "
                    f"1h={self._fmt(c.get('change_1h_pct'))}%, price=${self._fmt(c.get('current_price', 0), 2)}, "
                    f"dipping={c.get('dipping', False)}"
                    for c in candidates[:8]
                )
            else:
                rows = "  None found"
            sections.append(f"## High-Correlation Alt Candidates\n{rows}")
        except Exception as e:
            sections.append(f"## High-Correlation Alt Candidates\nError: {e}")

        # 4. CMC global
        try:
            snap = self.cmc.get_market_snapshot()
            g = snap.get("global", {})
            fg = snap.get("fear_and_greed", {})
            sections.append(f"""## CMC Global Market
- Total market cap: ${g.get('total_market_cap', 0)/1e9:.1f}B
- 24h volume: ${g.get('total_volume_24h', 0)/1e9:.1f}B
- BTC dominance: {self._fmt(g.get('btc_dominance'))}%
- Market cap change 24h: {self._fmt(g.get('market_cap_change_24h'))}%
- Fear & Greed: {fg.get('score', 'N/A')} ({fg.get('label', 'N/A')})""")

            # Top 10 coins
            top10 = snap.get("top20", [])[:10]
            rows = "\n".join(
                f"  {c['rank']}. {c['symbol']}: ${self._fmt(c['price'], 2)} | "
                f"1h={self._fmt(c['percent_change_1h'])}% | 24h={self._fmt(c['percent_change_24h'])}%"
                for c in top10
            )
            sections.append(f"## Top 10 Coins (CMC)\n{rows}")

            # Dipping coins
            dipping = snap.get("coins_dipping_1h", [])
            if dipping:
                d_rows = ", ".join(f"{c['symbol']}({self._fmt(c['percent_change_1h'])}%)" for c in dipping[:6])
                sections.append(f"## Coins Dipping >0.5% in 1h\n  {d_rows}")
        except Exception as e:
            sections.append(f"## CMC Data\nError: {e}")

        # 5. Open positions & account
        try:
            summary = self.positions.get_account_summary()
            sections.append(f"""## Account Summary
- Capital: ${summary['capital']:.2f}
- Today P&L: ${summary['today_pnl']:+.2f}
- Open positions: {summary['open_positions']}/3
- Win rate: {summary['win_rate_pct']:.0f}%
- Total trades: {summary['total_trades']}""")

            open_pos = self.positions.get_open_positions()
            if open_pos:
                pos_rows = "\n".join(
                    f"  - {p['symbol']}: {p['direction']} | entry=${p.get('entry_price', 0):.2f} | "
                    f"pnl=${p.get('unrealised_pnl', 0):+.2f} | age={p.get('age_minutes', 0):.0f}m"
                    for p in open_pos
                )
                sections.append(f"## Open Positions\n{pos_rows}")
            else:
                sections.append("## Open Positions\n  None")
        except Exception as e:
            sections.append(f"## Account\nError: {e}")

        return "\n\n".join(sections)

    def _fmt(self, val, decimals=2):
        if val is None:
            return "N/A"
        try:
            return f"{val:.{decimals}f}"
        except Exception:
            return str(val)


# ── AI Brain class ────────────────────────────────────────────────────────────

class AIBrain:
    def __init__(self, signal_engine, cmc_client, position_manager):
        self.client = _get_client()
        self.context_builder = ContextBuilder(signal_engine, cmc_client, position_manager)
        self.model = os.getenv("OPENROUTER_MODEL", FREE_MODELS[0])
        logger.info(f"AI Brain initialised: {self.model} via OpenRouter")

    def _chat(self, messages: list, max_tokens: int = 1024) -> Optional[str]:
        """Call OpenRouter, trying each free model until one succeeds."""
        models_to_try = [self.model] + [m for m in FREE_MODELS if m != self.model]
        for model in models_to_try:
            try:
                resp = self.client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=0.15,
                )
                if resp.choices and resp.choices[0].message.content:
                    if model != self.model:
                        logger.info(f"Using fallback model: {model}")
                    return resp.choices[0].message.content
                logger.warning(f"Model {model} returned empty response — trying next")
            except Exception as e:
                logger.warning(f"Model {model} failed: {str(e)[:100]} — trying next")
                time.sleep(1)
        logger.error("All OpenRouter models failed")
        return None

    def analyze_and_decide(self) -> "TradeDecision":
        """Main entry: build full context, ask AI, return TradeDecision."""
        logger.info(f"AI analysis started ({self.model})...")

        context = self.context_builder.build()
        prompt = f"""Here is the current market snapshot for ArbMind paper trading analysis:

{context}

Based on this data, analyse the market and produce your trading decision.
Follow your decision rules strictly. Output only the JSON block."""

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]

        raw = self._chat(messages, max_tokens=1024)
        if not raw:
            raw = '{"decision":"SKIP","confidence":0,"reasoning":"API unavailable","trades":[],"market_context":"","next_check_minutes":15}'

        logger.info(f"AI raw output ({len(raw)} chars)")
        decision = TradeDecision.parse(raw)
        logger.info(
            f"AI decision: {decision.decision} | "
            f"confidence={decision.confidence:.2f} | "
            f"trades={len(decision.trades)}"
        )
        return decision

    def analyze_position_for_close(self, position: dict) -> "PositionDecision":
        """Ask AI whether to close a specific open position."""
        context = self.context_builder.build()
        prompt = f"""Current market context:
{context}

Open position to review:
{json.dumps(position, indent=2)}

Should this position be CLOSED, HELD, or have its stop updated (UPDATE_STOP)?
Consider current market regime, P&L, time held, and risk.

Output ONLY this JSON:
{{"action": "CLOSE", "reason": "explanation", "new_stop_pct": 0.02}}"""

        messages = [
            {"role": "system", "content": "You are ArbMind, a crypto risk manager. Be concise and output only JSON."},
            {"role": "user", "content": prompt},
        ]
        raw = self._chat(messages, max_tokens=256) or '{"action":"HOLD","reason":"api error","new_stop_pct":0.02}'
        return PositionDecision.parse(raw)


# ── Result dataclasses ────────────────────────────────────────────────────────

class TradeDecision:
    def __init__(self, decision, confidence, reasoning, trades,
                 market_context, next_check_minutes, raw):
        self.decision = decision
        self.confidence = confidence
        self.reasoning = reasoning
        self.trades = trades
        self.market_context = market_context
        self.next_check_minutes = next_check_minutes
        self.raw = raw

    @classmethod
    def parse(cls, raw: str) -> "TradeDecision":
        # Try markdown code block first
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if not match:
            # Try to find JSON object containing "decision"
            match = re.search(r"(\{[^{}]*\"decision\"[^{}]*\})", raw, re.DOTALL)
        if not match:
            # Broader search for any JSON object
            match = re.search(r"(\{.*\"decision\".*\})", raw, re.DOTALL)

        if match:
            try:
                data = json.loads(match.group(1))
                return cls(
                    decision=data.get("decision", "SKIP"),
                    confidence=float(data.get("confidence", 0.0)),
                    reasoning=data.get("reasoning", ""),
                    trades=data.get("trades", []),
                    market_context=data.get("market_context", ""),
                    next_check_minutes=int(data.get("next_check_minutes", 15)),
                    raw=raw,
                )
            except (json.JSONDecodeError, ValueError) as e:
                logger.error(f"JSON parse failed: {e}\nRaw snippet: {raw[:400]}")

        return cls(
            decision="SKIP",
            confidence=0.0,
            reasoning="JSON parse failed — defaulting to SKIP",
            trades=[],
            market_context="parse error",
            next_check_minutes=15,
            raw=raw,
        )


class PositionDecision:
    def __init__(self, action, reason, new_stop_pct, raw):
        self.action = action
        self.reason = reason
        self.new_stop_pct = new_stop_pct
        self.raw = raw

    @classmethod
    def parse(cls, raw: str) -> "PositionDecision":
        match = re.search(r"\{.*?\}", raw, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
                return cls(
                    action=data.get("action", "HOLD"),
                    reason=data.get("reason", ""),
                    new_stop_pct=float(data.get("new_stop_pct", 0.02)),
                    raw=raw,
                )
            except Exception:
                pass
        return cls(action="HOLD", reason="parse error", new_stop_pct=0.02, raw=raw)
