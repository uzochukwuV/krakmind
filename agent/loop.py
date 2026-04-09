"""
Trading Loop
Orchestrates: signal engine → AI brain → position manager → Kraken CLI
Two async loops run concurrently:
  1. main_loop: AI analysis on each cycle, enters new trades
  2. position_monitor: checks stops/targets every 30s, asks AI to review
"""

import asyncio
import time
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import print as rprint

from config import config
from kraken_wrappers.cli_wrapper import KrakenCLI
from kraken_wrappers.rest_client import KrakenRESTClient
from data.cmc_client import CMCClient
from agent.ai_brain import AIBrain
from agent.signals import SignalEngine
from agent.position_manager import PositionManager
from agent.prism_loop import PrismSignalEngine, prism_store
from agent.rebalancer import CapitalRebalancer
from api import shared_state
from utils.logger import get_logger

logger = get_logger("loop")
console = Console()


class TradingLoop:
    def __init__(self):
        logger.info("Initialising ArbMind components (Perp, Options, Meme Spot)...")
        self.cli = KrakenCLI(paper_mode=config.paper_mode)
        self.rest = KrakenRESTClient()
        self.cmc = CMCClient()
        self.signals = SignalEngine()
        self.positions = PositionManager(self.cli)
        self.brain = AIBrain(self.signals, self.cmc, self.positions)
        self.prism = PrismSignalEngine(position_manager=self.positions)
        
        self.rebalancer = CapitalRebalancer(self.positions)

        self._cycle = 0
        self._last_decision_time = {
            "PERP": 0,
            "OPTIONS": 0,
            "MEME": 0
        }
        self._min_decision_gap = 60  # 1 min minimum between full AI analyses per loop

        logger.info(f"Mode: {'PAPER' if config.paper_mode else '⚠️  LIVE'}")
        logger.info(f"Capital: ${config.paper_capital:.2f}")
        logger.info(f"Loop interval: {config.loop_interval}s")
        logger.info("Prism Signal Engine: enabled (polling every 5m)")

        # Seed shared state with static config
        shared_state.update("agent", {
            "mode": "PAPER" if config.paper_mode else "LIVE",
            "status": "initialising",
            "tradeable_alts": config.tradeable_alts,
            "loop_interval": config.loop_interval,
        })
        shared_state.update("capital", {"total": config.paper_capital})

    # ── Main entry ──────────────────────────────────────────────

    async def run(self):
        """Start all async loops concurrently."""
        logger.info("Starting 3 independent trading loops (Perp, Options, Meme) + position monitor...")
        await asyncio.gather(
            self._trading_cycle_loop("PERP", config.perp_alts),
            self._trading_cycle_loop("OPTIONS", config.options_alts),
            self._trading_cycle_loop("MEME", config.spot_memecoins),
            self._position_monitor(),
            self.prism.run()
        )

    # ── Loop 1, 2, 3: AI decision loops ──────────────────────────

    async def _trading_cycle_loop(self, loop_name: str, symbols: list):
        """
        Generic cycle: every LOOP_INTERVAL seconds, run a pre-check for a specific set of symbols.
        If conditions look interesting, invoke the full AI analysis for that loop.
        """
        while True:
            # We only increment global cycle on one of the loops to avoid triple counting, or just use separate counters
            if loop_name == "PERP":
                self._cycle += 1
            
            cycle_start = time.time()

            try:
                await self._main_cycle(loop_name, symbols)
            except Exception as e:
                safe_e = str(e).replace("[", "(").replace("]", ")")
                logger.error(f"[{loop_name}] Loop error: {safe_e}", exc_info=True)

            elapsed = time.time() - cycle_start
            sleep_time = max(0, config.loop_interval - elapsed)
            await asyncio.sleep(sleep_time)

    def _push_state(self, summary: dict, is_volatile: bool, vol_triggers: list, canary: dict = None):
        """Push live trading state to the shared API state store."""
        import time as _time
        shared_state.update("agent", {
            "status": "running",
            "cycle": self._cycle,
            "last_update": _time.time(),
        })
        shared_state.update("capital", {
            "total":         summary["capital"],
            "deployed":      summary["capital"] - summary.get("available_capital", summary["capital"]),
            "available":     summary.get("available_capital", summary["capital"]),
            "today_pnl":     summary["today_pnl"],
            "today_pnl_pct": summary["today_pnl"] / config.paper_capital * 100,
            "all_time_pnl":  summary.get("total_pnl", 0.0),
        })
        shared_state.update("stats", {
            "total_trades": summary["wins"] + summary["losses"],
            "wins":         summary["wins"],
            "losses":       summary["losses"],
            "win_rate":     summary["win_rate_pct"],
        })
        # Positions
        open_pos = self.positions.get_open_positions()
        shared_state._state["positions"] = open_pos
        # Trade history
        shared_state._state["trade_history"] = list(self.positions._state.get("closed_trades", []))

        # Signal state
        sig_update = {
            "is_volatile": is_volatile,
            "vol_triggers": vol_triggers,
        }
        if canary:
            sig_update["canary"] = canary
            try:
                regime = self.signals.classify_regime()
                sig_update["regime"] = regime if isinstance(regime, dict) else {"regime": str(regime)}
            except Exception:
                pass
        shared_state.update("signals", sig_update)

        # Prism state
        if prism_store.is_fresh:
            snap = prism_store.get_snapshot() or {}
            fg  = snap.get("fear_greed", {})
            glb = snap.get("global", {})
            shared_state.update("prism", {
                "fear_greed":        fg.get("value"),
                "fear_greed_label":  fg.get("label"),
                "btc_dominance":     glb.get("btc_dominance"),
                "market_change_24h": glb.get("market_cap_change_24h_pct"),
                "gainers":           snap.get("gainers", []),
                "losers":            snap.get("losers", []),
                "trending":          snap.get("trending", []),
                "signals":           prism_store.get_signals(),
                "last_updated":      prism_store.last_updated,
                "is_fresh":          True,
            })

    async def _main_cycle(self, loop_name: str, symbols: list):
        """One iteration of the main loop for a specific strategy/asset class."""
        # Check rebalancer health occasionally (only on PERP to avoid redundant checks)
        if loop_name == "PERP" and self._cycle % 60 == 0:
            await self.rebalancer.check_balances()

        # Check market volatility specifically for this loop's symbols
        is_volatile, vol_triggers = self.signals.check_market_volatility(symbols=symbols)
        summary = self.positions.get_account_summary()

        if loop_name == "PERP":
            self._print_status_bar(is_volatile, vol_triggers, summary)

        # Respect minimum gap between full AI analyses for this specific loop
        time_since_last = time.time() - self._last_decision_time[loop_name]
        if time_since_last < self._min_decision_gap and not is_volatile:
            return

        # If we have 10 open positions already, skip analysis
        if summary["open_positions"] >= 10:
            return

        # Quick canary pre-check
        canary = self.signals.get_canary_signal()
        dip_triggered = canary["dip_triggered"]

        # Update shared API state every cycle (only PERP does it to avoid race conditions)
        if loop_name == "PERP":
            self._push_state(summary, is_volatile, vol_triggers, canary)

        # Prism strong signal overrides time-window gate
        prism_signals = prism_store.get_signals() if prism_store.is_fresh else []
        strong_prism = [s for s in prism_signals if s["signal_score"] >= 3]
        prism_trigger = bool(strong_prism)

        if not dip_triggered and not is_volatile and not prism_trigger:
            pass # Force AI trigger in Hackathon Mode

        # Log what triggered analysis
        triggers = []
        if is_volatile:     triggers.append(f"volatility({','.join(vol_triggers)})")
        if dip_triggered:   triggers.append("canary_dip")
        if prism_trigger:   triggers.append(f"prism_strong({len(strong_prism)} signals)")
        logger.info(f"[{loop_name}] Analysis triggered by: {', '.join(triggers)}")

        # ── Full AI analysis ──────────────────────────────────────
        logger.info(f"[{loop_name}] Invoking AI analysis...")
        self._last_decision_time[loop_name] = time.time()

        decision = await asyncio.get_event_loop().run_in_executor(
            None, self.brain.analyze_and_decide, loop_name, symbols
        )

        self._print_decision(loop_name, decision)

        # Persist AI decision to shared state for dashboard
        shared_state._state["last_ai_decision"] = {
            "loop":           loop_name,
            "decision":       decision.decision,
            "confidence":     decision.confidence,
            "market_context": decision.market_context,
            "reasoning":      decision.reasoning,
            "trades":         decision.trades,
            "decided_at":     time.time(),
        }

        if decision.decision == "ENTER" and decision.confidence >= 0.3:
            await self._execute_trades(decision, loop_name)
        elif decision.decision == "SKIP":
            logger.info(f"[{loop_name}] AI SKIP: {decision.reasoning[:80]}")
        elif decision.decision == "CLOSE":
            await self._close_all_positions(f"AI requested close ({loop_name})")

    # ── Loop 4: Position monitor ─────────────────────────────────

    async def _position_monitor(self):
        """
        Runs every POSITION_CHECK_INTERVAL seconds.
        Enforces stops, targets, time-stops, and calls AI for ambiguous positions.
        """
        while True:
            await asyncio.sleep(config.position_check_interval)
            try:
                await self._check_positions()
            except Exception as e:
                logger.error(f"Position monitor error: {e}", exc_info=True)

    async def _check_positions(self):
        """Check all positions for stop/target/time-stop triggers."""
        open_positions = self.positions.get_open_positions()
        if not open_positions:
            return

        to_close = self.positions.check_stops_and_targets()
        for close_info in to_close:
            pos_id = close_info["id"]
            reason = close_info["reason"]
            logger.info(f"Auto-closing {pos_id}: {reason}")
            self.positions.close_position(pos_id, reason=reason)

        # For positions not auto-closed, ask AI every 30min
        still_open = self.positions.get_open_positions()
        for pos in still_open:
            import datetime
            opened = datetime.datetime.fromisoformat(pos["opened_at"])
            age_min = (datetime.datetime.utcnow() - opened).total_seconds() / 60
            if int(age_min) % 30 == 0 and int(age_min) > 0:
                logger.info(f"AI position review: {pos['symbol']} ({age_min:.0f}m old)")
                pos_decision = await asyncio.get_event_loop().run_in_executor(
                    None, self.brain.analyze_position_for_close, pos
                )
                logger.info(
                    f"AI position decision: {pos_decision.action} — {pos_decision.reason[:60]}"
                )
                if pos_decision.action == "CLOSE":
                    self.positions.close_position(pos["id"], reason="ai_review")

    # ── Trade execution ──────────────────────────────────────────

    async def _execute_trades(self, decision, loop_name: str):
        """Execute trades from AI decision using Kelly criterion sizing."""
        kelly_size = self.positions.kelly_position_size(confidence=decision.confidence)
        logger.info(
            f"[{loop_name}] Kelly sizing: confidence={decision.confidence:.0%} → "
            f"size={kelly_size:.2%} of capital per trade"
        )

        for trade in decision.trades:
            symbol = trade.get("symbol")
            if not symbol:
                continue

            # Validate symbol is in our allowed list
            if symbol not in config.tradeable_alts:
                logger.warning(f"[{loop_name}] AI suggested {symbol} — not in tradeable list, skipping")
                continue

            # Check signal quality
            signal_quality = trade.get("signal_quality", 0)
            if signal_quality == 0 and decision.confidence < 0.3:
                logger.info(
                    f"[{loop_name}] Skipping {symbol}: signal_quality=0 & confidence={decision.confidence:.0%} "
                )
                continue

            opened = self.positions.open_position(
                symbol=symbol,
                direction=trade.get("direction", "long"),
                size_pct=kelly_size,
                entry_type=trade.get("entry_type", "market"),
                stop_loss_pct=max(trade.get("stop_loss_pct", 0.05), 0.05), # Wider stops for momentum
                take_profit_pct=trade.get("take_profit_pct", 0.15),
                thesis=trade.get("thesis", ""),
            )

            if opened:
                logger.info(
                    f"[{loop_name}] Position opened: {symbol} | kelly={kelly_size:.2%} | "
                    f"thesis: {trade.get('thesis', '')[:60]}"
                )
            await asyncio.sleep(1)

    async def _close_all_positions(self, reason: str):
        """Close all open positions."""
        for pos in self.positions.get_open_positions():
            self.positions.close_position(pos["id"], reason=reason)
            await asyncio.sleep(0.5)

    # ── Display helpers ──────────────────────────────────────────

    def _print_status_bar(self, is_volatile: bool, vol_triggers: list, summary: dict):
        mode_tag = "[green]PAPER[/green]" if config.paper_mode else "[red]LIVE[/red]"
        window_tag = f"[yellow]VOLATILE: {','.join(vol_triggers)}[/yellow]" if is_volatile else "[dim]quiet[/dim]"
        pnl_color = "green" if summary["today_pnl"] >= 0 else "red"
        loss_limit_tag = (
            "[bold red] ⛔ DAILY LOSS LIMIT[/bold red]"
            if summary.get("daily_loss_limit_hit") else ""
        )
        console.print(
            f"[dim]Cycle {self._cycle}[/dim] | {mode_tag} | {window_tag} | "
            f"Capital: [bold]${summary['capital']:.2f}[/bold] | "
            f"Today P&L: [{pnl_color}]{summary['today_pnl']:+.2f}[/{pnl_color}] | "
            f"Positions: {summary['open_positions']}/10 | "
            f"Win rate: {summary['win_rate_pct']:.0f}% ({summary['wins']}W/{summary['losses']}L)"
            f"{loss_limit_tag}"
        )

    def _print_decision(self, loop_name: str, decision):
        """Pretty-print AI decision to terminal."""
        color = {
            "ENTER": "bold green",
            "SKIP": "dim",
            "HOLD": "yellow",
            "CLOSE": "red",
        }.get(decision.decision, "white")

        panel_content = (
            f"Decision: [{color}]{decision.decision}[/{color}]\n"
            f"Confidence: {decision.confidence:.0%}\n"
            f"Context: {decision.market_context}\n"
            f"Reasoning: {decision.reasoning}\n"
        )

        if decision.trades:
            panel_content += "\nTrades:\n"
            for t in decision.trades:
                panel_content += (
                    f"  • {t.get('symbol')} {t.get('direction', 'long').upper()} "
                    f"| stop={t.get('stop_loss_pct', 0):.0%} "
                    f"| target={t.get('take_profit_pct', 0):.0%}\n"
                    f"    thesis: {t.get('thesis', '')[:70]}\n"
                )

        console.print(Panel(panel_content, title=f"[bold]AI Analysis [{loop_name}][/bold]", border_style="blue"))

        if decision.trades:
            table = Table(title=f"Proposed Trades ({loop_name})", show_header=True)
            table.add_column("Symbol")
            table.add_column("Dir")
            table.add_column("Size%")
            table.add_column("Stop")
            table.add_column("Target")
            table.add_column("Thesis")
            for t in decision.trades:
                table.add_row(
                    t.get("symbol", ""),
                    t.get("direction", "long"),
                    f"{t.get('size_pct_capital', 0):.0%}",
                    f"{t.get('stop_loss_pct', 0):.0%}",
                    f"{t.get('take_profit_pct', 0):.0%}",
                    t.get("thesis", "")[:50],
                )
            console.print(table)
