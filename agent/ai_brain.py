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
from agent.prism_loop import prism_store
from api import shared_state

logger = get_logger("ai_brain")

OPENROUTER_BASE = "https://openrouter.ai/api/v1"

# Models tried in priority order — all free, no tool-use required
FREE_MODELS = [
    "deepseek/deepseek-r1:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemini-2.0-flash-lite-preview-02-05:free",
    "google/gemma-2-9b-it:free"
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

You now have THREE signal sources — use all of them together:
1. Canary dip signals (BTC/ETH 1h change + RSI + volume)
2. Time window gate (06:00–06:30 WAT and 15:00–16:30 WAT)
3. Prism Intelligence (real Fear & Greed, social sentiment, funding rates, trending)

Your core edge:
- In FEAR zones (F&G < 40), dipping alts are prime mean-reversion candidates.
- Prism signals with score ≥ 3 are strong enough to trigger entries outside time windows.
- BTC/ETH funding rates signal leverage sentiment — neutral/negative = safer for longs.
- Social sentiment "bullish" + oversold RSI = high-conviction setup.
- Arb signal validation: if an arb opportunity exists for your trade candidate with
  net_gap_pct > 0.3% in the same direction, add 0.1 to confidence score.
- If arb gap direction CONTRADICTS your mean-reversion trade, reduce confidence by 0.15.

Decision rules:
1. If regime = BEAR AND no Prism strong signal → SKIP.
2. If daily_loss_limit_hit = true → SKIP (capital protection, non-negotiable).
3. If 3 positions already open → HOLD.
4. ENTER conditions (need ≥ 2 of these):
   a. In time window (06:00-06:30 or 15:00-16:30 WAT)
   b. BTC/ETH canary dip triggered (both below threshold)
   c. Prism signal score ≥ 3 for the target alt
   d. Fear & Greed ≤ 40 (fear = mean-reversion opportunity)
   e. Alt RSI < 35 (oversold on technical)
5. For stop_loss_pct, adapt to the coin's volatility. Typically use 2-4%.
   For take_profit_pct, typically use 3-8% depending on RSI momentum.
   DO NOT use static numbers. Adjust SL/TP based on the specific asset's liquidity and fear/greed.
6. Confidence < 0.6 → SKIP. Low quality signals (score=0) need confidence ≥ 0.75.
7. Only trade symbols from: PF_SOLUSD, PF_XRPUSD, PF_ADAUSD, PF_AVAXUSD,
   PF_DOTUSD, PF_LINKUSD, PF_LTCUSD, PF_UNIUSD, PF_MATICUSD, PF_BNBUSD.

IMPORTANT: You have Prism real-time intelligence. Use it. A Prism STRONG_BUY signal
with F&G < 40 and RSI < 35 is a high-conviction setup even without a canary dip.

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
      "entry_type": "market",
      "stop_loss_pct": 0.02,
      "take_profit_pct": 0.04,
      "signal_quality": 2,
      "thesis": "SOL corr=0.82, RSI=28 (oversold), vol_spike=true, dipped -2.1%"
    }
  ],
  "market_context": "one-line summary",
  "next_check_minutes": 15
}
```

signal_quality is an integer 0-3: count of these that are true:
  1. alt RSI < 35 (oversold)
  2. BTC volume spike (volume_ratio > 1.5)
  3. alt dipping > 0.3% in 1h

Note: size_pct_capital is NOT required in your output — position sizing is handled by Kelly criterion automatically.
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
- SMA20 slope (5d): {self._fmt(regime.get('sma20_slope_5d_pct'))}%
- Above SMA20: {regime.get('above_sma20')}
- Above SMA200: {regime.get('above_sma200')}
- Reason: {regime.get('reason', regime.get('regime', ''))}""")
        except Exception as e:
            sections.append(f"## Market Regime\nError: {e}")

        # 3. Correlation candidates
        try:
            candidates = self.signals.get_high_correlation_alts()
            if candidates:
                rows = "\n".join(
                    f"  - {c['symbol']}: corr={self._fmt(c.get('correlation'))}, "
                    f"1h={self._fmt(c.get('change_1h_pct'))}%, RSI={self._fmt(c.get('rsi'))}, "
                    f"oversold={c.get('rsi_oversold')}, vol_ratio={self._fmt(c.get('volume_ratio'))}, "
                    f"vol_spike={c.get('volume_spike')}, dipping={c.get('dipping', False)}, "
                    f"signal_quality={c.get('signal_quality', 0)}/3"
                    for c in candidates[:8]
                )
            else:
                rows = "  None found (no alts with correlation >= threshold)"
            sections.append(f"## High-Correlation Alt Candidates (sorted by signal quality)\n{rows}")
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

        # 5. Prism signal intelligence
        try:
            if prism_store.is_fresh:
                snap = prism_store.get_snapshot()
                signals = prism_store.get_signals()
                fg = snap.get("fear_greed", {})
                g  = snap.get("global", {})

                sections.append(f"""## Prism Market Intelligence (age={prism_store.age_seconds/60:.0f}m)
- Fear & Greed (REAL index): {fg.get('value', 'N/A')} — {fg.get('label', '')}
- BTC dominance: {g.get('btc_dominance', 0):.1f}%
- Total market cap: ${(g.get('total_market_cap_usd') or 0)/1e9:.0f}B
- Market cap Δ24h: {g.get('market_cap_change_24h_pct', 0):+.2f}%
- 24h volume: ${(g.get('total_volume_24h_usd') or 0)/1e9:.0f}B""")

                if signals:
                    sig_rows = "\n".join(
                        f"  {s['kraken_symbol']}: score={s['signal_score']} "
                        f"[{s['classification']}] | 24h={s['change_24h_pct']:+.1f}% | "
                        f"sentiment={s.get('sentiment_label','')} | tags={', '.join(s['tags'][:3])}"
                        for s in signals[:6]
                    )
                    sections.append(f"## Prism Trade Signals (ranked by score)\n{sig_rows}")
                else:
                    sections.append("## Prism Trade Signals\n  None meeting threshold right now")

                # Trending
                trending = snap.get("trending", [])
                if trending:
                    t_row = ", ".join(
                        f"{t['symbol']}({t.get('change_24h', 0):+.1f}%)" for t in trending[:6]
                    )
                    sections.append(f"## Prism Trending Tokens\n  {t_row}")

                # Top losers (mean-reversion candidates)
                losers = snap.get("losers", [])
                if losers:
                    l_row = ", ".join(
                        f"{c['symbol']}({c.get('change_24h', 0):+.1f}%)" for c in losers[:5]
                    )
                    sections.append(f"## Prism Top Losers 24h (mean-reversion watch)\n  {l_row}")

                # Per-alt Prism signals mapped to Kraken symbols
                alt_sigs = snap.get("alt_signals", {})
                if alt_sigs:
                    a_rows = "\n".join(
                        f"  {k}: price=${v.get('price_usd') or 0:.3f} | "
                        f"24h={v.get('change_24h_pct') or 0:+.1f}% | "
                        f"sentiment={v.get('sentiment_label', 'N/A')} ({v.get('sentiment_score') or 0:.0f})"
                        for k, v in alt_sigs.items()
                        if k.startswith("PF_")
                    )
                    sections.append(f"## Prism Per-Alt Signals\n{a_rows}")

                # BTC/ETH funding rates (leverage sentiment)
                funding = snap.get("funding", {})
                for sym in ["BTC", "ETH"]:
                    fd = funding.get(sym, {})
                    if fd.get("interpretation"):
                        sections.append(f"## Prism {sym} Funding Rate\n  {fd['interpretation']}")
            else:
                sections.append(f"## Prism Market Intelligence\n  Stale or unavailable (age={prism_store.age_seconds/60:.0f}m)")
        except Exception as e:
            sections.append(f"## Prism\nError: {e}")

        # Arb signals section
        # NOTE: Arbitrage is now mostly auto-executed by arb_executor, but we still feed it to AI 
        # so it knows if there's massive order book imbalance.
        try:
            arb_alerts = shared_state.get_section("arb_alerts")
            if isinstance(arb_alerts, list):
                recent = [a for a in arb_alerts if time.time() - a.get("detected_at", 0) < 300]
                if recent:
                    rows = "\n".join(
                        f"  {a['symbol']}: gap={a['net_gap_pct']:+.2f}% | dir={a['direction']} | "
                        f"liquidity=${a['dex_liquidity_usd']/1e6:.1f}M"
                        for a in recent[:5]
                    )
                    sections.append(f"## Live Arb Imbalances (last 5min)\n{rows}")
                else:
                    sections.append("## Live Arb Imbalances\n  None detected in last 5min")
            else:
                sections.append("## Live Arb Imbalances\n  None detected in last 5min")
        except Exception as e:
            sections.append(f"## Arb Imbalances\nError: {e}")

        # 6. Open positions & account
        try:
            summary = self.positions.get_account_summary()
            daily_loss_hit = summary.get('daily_loss_limit_hit', False)
            daily_limit_pct = summary.get('daily_loss_limit_pct', 3)
            kelly_size = self.positions.kelly_position_size(confidence=0.75)  # preview at 75% conf
            sections.append(f"""## Account Summary
- Capital: ${summary['capital']:.2f} | Peak: ${summary['peak_capital']:.2f}
- Wallets: Kraken Spot: ${summary.get('wallets', {}).get('kraken_spot', 0):.2f} | Kraken Futures: ${summary.get('wallets', {}).get('kraken_futures', 0):.2f} | Base Web3: ${summary.get('wallets', {}).get('base_web3', 0):.2f}
- Today P&L: ${summary['today_pnl']:+.2f} ({summary.get('today_loss_pct', 0):+.2f}%)
- DAILY LOSS LIMIT HIT: {daily_loss_hit} (limit={daily_limit_pct:.0f}% of day-start capital)
- Open positions: {summary['open_positions']}/3 | Deployed: ${summary['deployed']:.2f}
- Win rate: {summary['win_rate_pct']:.0f}% ({summary['wins']}W / {summary['losses']}L)
- Total trades: {summary['total_trades']} | Kelly size @ 75% conf: {kelly_size:.2%}
- Trailing stop activations: {summary.get('trailing_stop_activations', 0)}""")

            open_pos = self.positions.get_open_positions()
            if open_pos:
                pos_rows = "\n".join(
                    f"  - {p['symbol']}: {p['direction']} | entry=${p.get('entry_price', 0):.2f} | "
                    f"pnl=${p.get('unrealised_pnl', 0):+.2f} | age={p.get('age_minutes', 0):.0f}m | "
                    f"protocol={p.get('protocol', 'kraken_futures')}"
                    for p in open_pos
                )
                sections.append(f"## Open Positions\n{pos_rows}")
            else:
                sections.append("## Open Positions\n  None")
        except Exception as e:
            sections.append(f"## Account\nError: {e}")

        # 7. Add protocol allocation logic
        sections.append(
            "## Capital Allocation Rules\n"
            "  You must ensure trades are directed to the correct protocol.\n"
            "  If kraken_spot is low (<20%), avoid large new positional trades.\n"
            "  Consider returning funds from base_web3 or kraken_futures to kraken_spot if needed."
        )

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
        api_key = os.getenv("OPENROUTER_API_KEY", "")
        if not api_key:
            logger.error("OPENROUTER_API_KEY not set — AI brain disabled")
            return None

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
                err_str = str(e)
                # 401 means bad key — no point trying other models
                if "401" in err_str or "User not found" in err_str or "AuthenticationError" in err_str:
                    logger.error(
                        "OpenRouter API key invalid or expired (401). "
                        "Update OPENROUTER_API_KEY at openrouter.ai/keys — AI decisions skipped."
                    )
                    return None
                logger.warning(f"Model {model} failed: {err_str[:100]} — trying next")
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
