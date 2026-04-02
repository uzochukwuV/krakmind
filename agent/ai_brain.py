"""
AI Brain — OpenRouter LLM (free tier models)
Drop-in replacement for the Anthropic brain.
Uses OpenAI-compatible API via OpenRouter, with manual tool-call loop
since LangChain's tool binding may not work uniformly across all free models.
"""

import json
import re
import os
from typing import Optional
from openai import OpenAI
from utils.logger import get_logger
from config import config

logger = get_logger("ai_brain")

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "google/gemma-3n-e4b-it:free"

# Fallback models tried in order if the primary is rate-limited
FALLBACK_MODELS = [
    "google/gemma-3n-e4b-it:free",
    "nvidia/nemotron-nano-9b-v2:free",
    "openai/gpt-oss-20b:free",
    "qwen/qwen3.6-plus:free",
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


# ── Tool definitions ──────────────────────────────────────────────────────────

def make_tools(signal_engine, cmc_client, position_manager):
    """
    Returns (tool_schemas, tool_executor_map).
    tool_schemas  : list[dict] — OpenAI function-calling format
    tool_executor : dict[name → callable]
    """

    def get_market_snapshot() -> str:
        """Full market snapshot: signals, regime, CMC top-20, fear/greed."""
        try:
            signals = signal_engine.get_full_signal_snapshot()
            cmc = cmc_client.get_market_snapshot()
            return json.dumps({
                "signals": signals,
                "cmc": {
                    "fear_and_greed": cmc.get("fear_and_greed"),
                    "global": cmc.get("global"),
                    "top5_coins": cmc.get("top20", [])[:5],
                    "coins_dipping_1h_count": len(cmc.get("coins_dipping_1h", [])),
                    "coins_dipping_1h": cmc.get("coins_dipping_1h", [])[:8],
                    "coins_rallying_1h": cmc.get("coins_rallying_1h", [])[:4],
                },
            }, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)})

    def get_correlation_candidates() -> str:
        """Altcoins with high BTC correlation that are dipping — best trade candidates."""
        try:
            return json.dumps(signal_engine.get_high_correlation_alts(), indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)})

    def get_open_positions() -> str:
        """All open paper positions with entry price, P&L, time held."""
        try:
            return json.dumps(position_manager.get_open_positions(), indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)})

    def get_account_summary() -> str:
        """Paper account balance, total P&L, win rate, exposure."""
        try:
            return json.dumps(position_manager.get_account_summary(), indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)})

    def get_cmc_coin_detail(symbol: str) -> str:
        """Detailed CMC data for a specific coin symbol (e.g. 'SOL')."""
        try:
            coins = cmc_client.get_top20_listings()
            coin = next((c for c in coins if c["symbol"].upper() == symbol.upper()), None)
            if not coin:
                return json.dumps({"error": f"Coin {symbol} not found in top-20"})
            return json.dumps(coin, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)})

    def get_regime_analysis() -> str:
        """Regime classification: BULL/BEAR/SIDEWAYS with BTC SMA20/200 data."""
        try:
            return json.dumps(signal_engine.classify_regime(), indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)})

    schemas = [
        {
            "type": "function",
            "function": {
                "name": "get_market_snapshot",
                "description": "Get full market snapshot: BTC/ETH canary signals, regime, time window, CMC top-20, fear/greed. Call this FIRST.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_correlation_candidates",
                "description": "Get altcoins with high correlation to BTC that are currently dipping. Best mean-reversion candidates.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_open_positions",
                "description": "Get all open paper trading positions with P&L.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_account_summary",
                "description": "Get paper account balance, P&L, win rate, exposure.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_cmc_coin_detail",
                "description": "Detailed CMC data for a specific coin by symbol, e.g. 'SOL', 'BNB', 'XRP'.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "Coin symbol, e.g. 'SOL'"}
                    },
                    "required": ["symbol"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_regime_analysis",
                "description": "Detailed regime classification with BTC SMA data.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
    ]

    executor = {
        "get_market_snapshot": get_market_snapshot,
        "get_correlation_candidates": get_correlation_candidates,
        "get_open_positions": get_open_positions,
        "get_account_summary": get_account_summary,
        "get_cmc_coin_detail": get_cmc_coin_detail,
        "get_regime_analysis": get_regime_analysis,
    }

    return schemas, executor


# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are ArbMind, an autonomous crypto trading agent specialising in
mean-reversion day trades on Kraken Futures (paper mode).

Your edge:
- BTC and ETH are canary assets. When they dip -1.5% / -1.2% in 1h, correlated 
  top-20 alts tend to follow, then recover within the same session.
- Two high-probability windows per day (UTC+1): 6AM (Asian open) and 4PM (NY-London handoff).
- In bear regimes, skip all longs. In bull/sideways, look for the dip entry.

Your decision process (ALWAYS follow this order):
1. Call get_market_snapshot first — never reason without data.
2. Check regime: if BEAR, output SKIP immediately.
3. Check time window: if not active, output SKIP with countdown.
4. Check canary: BTC and ETH must both be dipping (threshold in data).
5. Call get_correlation_candidates — only trade alts with corr >= 0.6 to BTC.
6. Check CMC: confirm fear/greed context, volume, any unusual activity.
7. Check open positions: respect max 30% capital exposure rule.
8. For each candidate alt: call get_cmc_coin_detail to validate.
9. Output your decision as structured JSON.

Output format (ALWAYS end with this JSON block, no matter what):
```json
{
  "decision": "ENTER" | "SKIP" | "HOLD" | "CLOSE",
  "confidence": 0.0-1.0,
  "reasoning": "2-3 sentence summary of why",
  "trades": [
    {
      "symbol": "PF_SOLUSD",
      "direction": "long",
      "size_pct_capital": 0.05,
      "entry_type": "market",
      "stop_loss_pct": 0.02,
      "take_profit_pct": 0.04,
      "thesis": "SOL corr=0.82 to BTC, dipped -2.1% in 1h, recovery likely in dip window"
    }
  ],
  "market_context": "brief 1-line market condition summary",
  "next_check_minutes": 15
}
```

Rules:
- Never trade in BEAR regime.
- Never exceed 5% capital per single trade.
- Never open more than 3 positions simultaneously.
- Confidence < 0.6 → SKIP, no matter how tempting.
- Always use stop_loss_pct >= 0.02 (2% hard stop).
- In paper mode, be bold enough to test the strategy properly.
"""


# ── AI Brain class ────────────────────────────────────────────────────────────

class AIBrain:
    def __init__(self, signal_engine, cmc_client, position_manager):
        self.client = _get_client()
        self.model = os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL)
        self.tool_schemas, self.tool_executor = make_tools(
            signal_engine, cmc_client, position_manager
        )
        logger.info(f"AI Brain initialised: {self.model} via OpenRouter")

    def _call_tool(self, name: str, args: dict) -> str:
        fn = self.tool_executor.get(name)
        if not fn:
            return json.dumps({"error": f"Unknown tool: {name}"})
        try:
            return fn(**args) if args else fn()
        except Exception as e:
            return json.dumps({"error": str(e)})

    def _run_agentic_loop(self, user_message: str, max_rounds: int = 6) -> str:
        """
        OpenAI-compatible tool-use loop.
        Model calls tools as needed, we execute them, then get the final answer.
        """
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]

        for round_num in range(max_rounds):
            response = None
            last_err = None
            models_to_try = [self.model] + [m for m in FALLBACK_MODELS if m != self.model]
            for m in models_to_try:
                try:
                    response = self.client.chat.completions.create(
                        model=m,
                        messages=messages,
                        tools=self.tool_schemas,
                        tool_choice="auto",
                        max_tokens=2048,
                        temperature=0.1,
                    )
                    if m != self.model:
                        logger.info(f"Switched to fallback model: {m}")
                    break
                except Exception as e:
                    last_err = e
                    logger.warning(f"Model {m} failed: {str(e)[:80]} — trying next")
            if response is None:
                logger.error(f"All models failed: {last_err}")
                return json.dumps({
                    "decision": "SKIP",
                    "confidence": 0.0,
                    "reasoning": f"All OpenRouter models failed: {last_err}",
                    "trades": [],
                    "market_context": "api error",
                    "next_check_minutes": 15,
                })

            choice = response.choices[0]
            msg = choice.message

            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in (msg.tool_calls or [])
                ] or None,
            })

            tool_calls = msg.tool_calls or []
            if not tool_calls:
                return msg.content or ""

            for tc in tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = self._call_tool(tc.function.name, args)
                logger.debug(f"Tool {tc.function.name} → {result[:120]}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })

            logger.debug(f"AI tool round {round_num + 1}: {[tc.function.name for tc in tool_calls]}")

        # Fallback: ask for final answer without tools
        messages.append({"role": "user", "content": "You have enough data. Give your final decision JSON now."})
        try:
            final = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=1024,
                temperature=0.1,
            )
            return final.choices[0].message.content or ""
        except Exception as e:
            logger.error(f"Final answer API error: {e}")
            return '{"decision": "SKIP", "confidence": 0.0, "reasoning": "fallback", "trades": [], "market_context": "", "next_check_minutes": 15}'

    def analyze_and_decide(self) -> "TradeDecision":
        """Main entry: returns a TradeDecision with full AI reasoning."""
        prompt = (
            "Analyze current market conditions and decide whether to enter any trades. "
            "Start by calling get_market_snapshot, then follow the decision process in your instructions."
        )

        logger.info(f"AI analysis started ({self.model})...")
        raw = self._run_agentic_loop(prompt)
        logger.info(f"AI raw output length: {len(raw)} chars")

        decision = TradeDecision.parse(raw)
        logger.info(
            f"AI decision: {decision.decision} | "
            f"confidence={decision.confidence:.2f} | "
            f"trades={len(decision.trades)}"
        )
        return decision

    def analyze_position_for_close(self, position: dict) -> "PositionDecision":
        """Ask AI whether to close a specific open position."""
        prompt = (
            f"Review this open position and decide whether to close it, "
            f"hold it, or update the stop loss.\n\n"
            f"Position:\n{json.dumps(position, indent=2)}\n\n"
            f"First call get_market_snapshot to check current conditions. "
            f"Then decide: CLOSE, HOLD, or UPDATE_STOP.\n\n"
            f'Output JSON: {{"action": "CLOSE"|"HOLD"|"UPDATE_STOP", '
            f'"reason": "...", "new_stop_pct": 0.02}}'
        )
        raw = self._run_agentic_loop(prompt, max_rounds=4)
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
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if not match:
            match = re.search(r"(\{[^{}]*\"decision\".*?\})", raw, re.DOTALL)
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
                logger.error(f"Failed to parse AI decision JSON: {e}\nRaw: {raw[:300]}")
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
