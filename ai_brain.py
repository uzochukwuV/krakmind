"""
AI Brain — LangChain + Claude Sonnet
This is the core decision-maker. Every trade goes through here.
The AI has access to tools that let it check signals, CMC data,
and reason about whether to enter, hold, or skip.

Architecture: Claude gets the full market context and must call
tools to gather what it needs, then output a structured TradeDecision.
"""

import json
from typing import Optional
from langchain_anthropic import ChatAnthropic
from langchain.tools import tool
from langchain_core.messages import HumanMessage, SystemMessage
from utils.logger import get_logger
from config import config

logger = get_logger("ai_brain")


# ── Tool definitions (LangChain @tool) ──────────────────────────────────────
# These are injected into Claude so it can pull live data mid-reasoning.

def make_tools(signal_engine, cmc_client, position_manager):
    """
    Factory: creates bound LangChain tools with access to live data sources.
    Called once at agent init time.
    """

    @tool
    def get_market_snapshot() -> str:
        """
        Get the full market snapshot: BTC/ETH canary signals, regime classification,
        time window status, and top-20 CMC data with fear/greed proxy.
        Call this first before any analysis.
        """
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

    @tool
    def get_correlation_candidates() -> str:
        """
        Get the list of altcoins with high correlation to BTC that are currently
        dipping. These are the best candidates for mean-reversion long trades.
        Returns correlation coefficient, 1h price change, and current price for each.
        """
        try:
            candidates = signal_engine.get_high_correlation_alts()
            return json.dumps(candidates, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)})

    @tool
    def get_open_positions() -> str:
        """
        Get all currently open paper trading positions, including entry price,
        current P&L, and time held.
        """
        try:
            positions = position_manager.get_open_positions()
            return json.dumps(positions, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)})

    @tool
    def get_account_summary() -> str:
        """
        Get paper account balance, total P&L, number of trades today,
        win rate, and current exposure percentage.
        """
        try:
            summary = position_manager.get_account_summary()
            return json.dumps(summary, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)})

    @tool
    def get_cmc_coin_detail(symbol: str) -> str:
        """
        Get detailed CMC data for a specific coin by symbol (e.g. 'SOL', 'BNB', 'XRP').
        Returns price, 1h/24h/7d changes, volume, and market cap.
        Use this to validate a specific alt before trading it.
        """
        try:
            coins = cmc_client.get_top20_listings()
            coin = next((c for c in coins if c["symbol"].upper() == symbol.upper()), None)
            if not coin:
                return json.dumps({"error": f"Coin {symbol} not found in top-20"})
            return json.dumps(coin, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)})

    @tool
    def get_regime_analysis() -> str:
        """
        Get detailed regime classification: BULL/BEAR/SIDEWAYS with BTC SMA20/200,
        slope, and whether trading is enabled.
        """
        try:
            regime = signal_engine.classify_regime()
            return json.dumps(regime, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)})

    return [
        get_market_snapshot,
        get_correlation_candidates,
        get_open_positions,
        get_account_summary,
        get_cmc_coin_detail,
        get_regime_analysis,
    ]


# ── System prompt ────────────────────────────────────────────────────────────

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


# ── AI Brain class ───────────────────────────────────────────────────────────

class AIBrain:
    def __init__(self, signal_engine, cmc_client, position_manager):
        self.llm = ChatAnthropic(
            model="claude-sonnet-4-6",
            api_key=config.anthropic_api_key,
            max_tokens=2048,
            temperature=0.1,  # Low temp for consistent decisions
        )
        self.tools = make_tools(signal_engine, cmc_client, position_manager)
        self.llm_with_tools = self.llm.bind_tools(self.tools)
        self._tool_map = {t.name: t for t in self.tools}

    def _execute_tool(self, tool_call: dict) -> str:
        """Execute a single tool call and return string result."""
        name = tool_call.get("name")
        args = tool_call.get("args", {})
        tool_fn = self._tool_map.get(name)
        if not tool_fn:
            return json.dumps({"error": f"Unknown tool: {name}"})
        try:
            result = tool_fn.invoke(args)
            return result if isinstance(result, str) else json.dumps(result)
        except Exception as e:
            return json.dumps({"error": str(e)})

    def _run_agentic_loop(self, user_message: str, max_rounds: int = 6) -> str:
        """
        Run LangChain agentic tool-use loop.
        Claude can call tools multiple times before giving its final answer.
        """
        from langchain_core.messages import AIMessage, ToolMessage

        messages = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=user_message),
        ]

        for round_num in range(max_rounds):
            response = self.llm_with_tools.invoke(messages)
            messages.append(response)

            tool_calls = getattr(response, "tool_calls", [])
            if not tool_calls:
                # No more tool calls — final answer
                return response.content

            # Execute all tool calls in this round
            for tc in tool_calls:
                result = self._execute_tool(tc)
                messages.append(
                    ToolMessage(content=result, tool_call_id=tc["id"])
                )
            logger.debug(f"AI tool round {round_num + 1}: called {[tc['name'] for tc in tool_calls]}")

        # Fallback: ask for final decision without more tools
        messages.append(HumanMessage(
            content="You have enough data. Give your final decision JSON now."
        ))
        final = self.llm.invoke(messages)
        return final.content

    def analyze_and_decide(self) -> "TradeDecision":
        """
        Main entry point. Returns a TradeDecision with full AI reasoning.
        The AI will call tools as needed, then output structured JSON.
        """
        prompt = (
            "Analyze current market conditions and decide whether to enter any trades. "
            "Start by calling get_market_snapshot, then follow the decision process in your instructions."
        )

        logger.info("AI analysis started...")
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
        """
        Ask the AI whether to close a specific open position.
        Called by the position monitor loop.
        """
        prompt = (
            f"Review this open position and decide whether to close it, "
            f"hold it, or update the stop loss.\n\n"
            f"Position:\n{json.dumps(position, indent=2)}\n\n"
            f"First call get_market_snapshot to check current conditions. "
            f"Then decide: CLOSE, HOLD, or UPDATE_STOP.\n\n"
            f"Output JSON:\n"
            f'{{"action": "CLOSE"|"HOLD"|"UPDATE_STOP", '
            f'"reason": "...", '
            f'"new_stop_pct": 0.02}}'
        )

        raw = self._run_agentic_loop(prompt, max_rounds=4)
        return PositionDecision.parse(raw)


# ── Result dataclasses ───────────────────────────────────────────────────────

class TradeDecision:
    def __init__(self, decision, confidence, reasoning, trades,
                 market_context, next_check_minutes, raw):
        self.decision = decision
        self.confidence = confidence
        self.reasoning = reasoning
        self.trades = trades  # list of dicts
        self.market_context = market_context
        self.next_check_minutes = next_check_minutes
        self.raw = raw

    @classmethod
    def parse(cls, raw: str) -> "TradeDecision":
        """Extract JSON from AI response (handles markdown code blocks)."""
        import re
        # Find JSON block in response
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if not match:
            # Try bare JSON
            match = re.search(r"(\{[^{}]*\"decision\"[^{}]*\})", raw, re.DOTALL)

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
                logger.error(f"Failed to parse AI decision JSON: {e}")

        # Fallback safe decision
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
        import re
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
