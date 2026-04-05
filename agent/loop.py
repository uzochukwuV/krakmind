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
from agent.arb_loop import ArbLoop
from agent.arb_detector import ArbDetector
from agent.arb_executor import ArbExecutor
from agent.rebalancer import CapitalRebalancer
from agent.funding_harvester import FundingRateHarvester
from data.dex_price_client import DexPriceClient
from api import shared_state
from utils.logger import get_logger

logger = get_logger("loop")
console = Console()


class TradingLoop:
    def __init__(self):
        logger.info("Initialising ArbMind components...")
        self.cli = KrakenCLI(paper_mode=config.paper_mode)
        self.rest = KrakenRESTClient()
        self.cmc = CMCClient()
        self.signals = SignalEngine()
        self.positions = PositionManager(self.cli)
        self.brain = AIBrain(self.signals, self.cmc, self.positions)
        self.prism = PrismSignalEngine(position_manager=self.positions)
        
        self.dex_prices = DexPriceClient()
        self.arb_detector = ArbDetector(self.dex_prices, self.rest, self.positions)
        self.arb_executor = ArbExecutor(self.cli, self.positions)
        self.arb_loop = ArbLoop(self.arb_detector, self.arb_executor, self.positions)
        self.rebalancer = CapitalRebalancer(self.positions)
        self.funding_harvester = FundingRateHarvester(self.rest, self.positions)

        self._cycle = 0
        self._last_decision_time = 0
        self._min_decision_gap = 300  # 5 min minimum between full AI analyses

        logger.info(f"Mode: {'PAPER' if config.paper_mode else '⚠️  LIVE'}")
        logger.info(f"Capital: ${config.paper_capital:.2f}")
        logger.info(f"Loop interval: {config.loop_interval}s")
        logger.info(f"Tradeable alts: {', '.join(config.tradeable_alts)}")
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
        logger.info("Starting trading loops (main + position monitor + Prism signals + Arb)...")
        await asyncio.gather(
            self._main_loop(),
            self._position_monitor(),
            self.prism.run(),
            self.arb_loop.run(),
            self.funding_harvester.run(),
        )

    # ── Loop 1: AI decision loop ─────────────────────────────────

    async def _main_loop(self):
        """
        Main cycle: every LOOP_INTERVAL seconds, run a pre-check.
        If conditions look interesting, invoke the full AI analysis.
        """
        while True:
            self._cycle += 1
            cycle_start = time.time()

            try:
                await self._main_cycle()
            except Exception as e:
                logger.error(f"Main loop error (cycle {self._cycle}): {e}", exc_info=True)

            elapsed = time.time() - cycle_start
            sleep_time = max(0, config.loop_interval - elapsed)
            logger.debug(f"Cycle {self._cycle} took {elapsed:.1f}s. Sleeping {sleep_time:.0f}s.")
            await asyncio.sleep(sleep_time)

    def _push_state(self, summary: dict, in_window: bool, window_label: str, canary: dict = None):
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
        # Trade history (read directly from the persisted state)
        shared_state._state["trade_history"] = list(self.positions._state.get("closed_trades", []))

        # Signal state
        sig_update = {
            "in_window":              in_window,
            "window_label":           window_label,
            "minutes_to_next_window": self.signals.minutes_to_next_window(),
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

    async def _main_cycle(self):
        """One iteration of the main loop."""
        # Check rebalancer health occasionally
        if self._cycle % 60 == 0:
            await self.rebalancer.check_balances()

        in_window, window_label = self.signals.is_dip_window()
        summary = self.positions.get_account_summary()

        self._print_status_bar(in_window, window_label, summary)

        # Respect minimum gap between full AI analyses
        time_since_last = time.time() - self._last_decision_time
        if time_since_last < self._min_decision_gap and not in_window:
            logger.debug(f"Not in window & recent analysis {time_since_last:.0f}s ago — skipping")
            return

        # If we have 3 open positions already, skip analysis
        if summary["open_positions"] >= 3:
            logger.info(f"Max positions open ({summary['open_positions']}/3) — skipping analysis")
            return

        # Quick canary pre-check
        canary = self.signals.get_canary_signal()
        dip_triggered = canary["dip_triggered"]

        # Update shared API state every cycle
        self._push_state(summary, in_window, window_label, canary)

        # Prism strong signal overrides time-window gate
        prism_signals = prism_store.get_signals() if prism_store.is_fresh else []
        strong_prism = [s for s in prism_signals if s["signal_score"] >= 3]
        prism_trigger = bool(strong_prism)

        if not dip_triggered and not in_window and not prism_trigger:
            next_window = self.signals.minutes_to_next_window()
            btc_chg = canary['btc']['change_1h_pct']
            eth_chg = canary['eth']['change_1h_pct']
            btc_str = f"{btc_chg:.2f}%" if btc_chg is not None else "N/A"
            eth_str = f"{eth_chg:.2f}%" if eth_chg is not None else "N/A"
            prism_str = f"Prism={len(prism_signals)} signals" if prism_signals else "Prism=no signals"
            logger.info(
                f"Canary: BTC={btc_str} | ETH={eth_str} | "
                f"Next window in {next_window}m | {prism_str}"
            )
            return

        # Log what triggered analysis
        triggers = []
        if in_window:       triggers.append(f"time_window({window_label})")
        if dip_triggered:   triggers.append("canary_dip")
        if prism_trigger:   triggers.append(f"prism_strong({len(strong_prism)} signals)")
        logger.info(f"Analysis triggered by: {', '.join(triggers)}")

        # ── Full AI analysis ──────────────────────────────────────
        logger.info("Invoking AI analysis...")
        self._last_decision_time = time.time()

        decision = await asyncio.get_event_loop().run_in_executor(
            None, self.brain.analyze_and_decide
        )

        self._print_decision(decision)

        # Persist AI decision to shared state for dashboard
        shared_state._state["last_ai_decision"] = {
            "decision":       decision.decision,
            "confidence":     decision.confidence,
            "market_context": decision.market_context,
            "reasoning":      decision.reasoning,
            "trades":         decision.trades,
            "decided_at":     time.time(),
        }

        if decision.decision == "ENTER" and decision.confidence >= 0.6:
            await self._execute_trades(decision)
        elif decision.decision == "SKIP":
            logger.info(f"AI SKIP: {decision.reasoning[:80]}")
        elif decision.decision == "CLOSE":
            await self._close_all_positions("AI requested close")

    # ── Loop 2: Position monitor ─────────────────────────────────

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
            # Check at 30-min intervals after opening
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

    async def _execute_trades(self, decision):
        """Execute trades from AI decision using Kelly criterion sizing."""
        # Kelly size is computed once per decision batch (same confidence for all trades)
        kelly_size = self.positions.kelly_position_size(confidence=decision.confidence)
        logger.info(
            f"Kelly sizing: confidence={decision.confidence:.0%} → "
            f"size={kelly_size:.2%} of capital per trade"
        )

        for trade in decision.trades:
            symbol = trade.get("symbol")
            if not symbol:
                continue

            # Validate symbol is in our allowed list
            if symbol not in config.tradeable_alts:
                logger.warning(f"AI suggested {symbol} — not in tradeable list, skipping")
                continue

            # Check signal quality — require RSI + volume confirmation on weak signals
            signal_quality = trade.get("signal_quality", 0)
            if signal_quality == 0 and decision.confidence < 0.75:
                logger.info(
                    f"Skipping {symbol}: signal_quality=0 & confidence={decision.confidence:.0%} "
                    f"(need quality≥1 or confidence≥75%)"
                )
                continue

            opened = self.positions.open_position(
                symbol=symbol,
                direction=trade.get("direction", "long"),
                size_pct=kelly_size,
                entry_type=trade.get("entry_type", "market"),
                stop_loss_pct=max(trade.get("stop_loss_pct", 0.02), 0.02),
                take_profit_pct=trade.get("take_profit_pct", 0.04),
                thesis=trade.get("thesis", ""),
            )

            if opened:
                logger.info(
                    f"Position opened: {symbol} | kelly={kelly_size:.2%} | "
                    f"thesis: {trade.get('thesis', '')[:60]}"
                )
            await asyncio.sleep(1)  # Small gap between orders

    async def _close_all_positions(self, reason: str):
        """Close all open positions."""
        for pos in self.positions.get_open_positions():
            self.positions.close_position(pos["id"], reason=reason)
            await asyncio.sleep(0.5)

    # ── Display helpers ──────────────────────────────────────────

    def _print_status_bar(self, in_window: bool, window_label: str, summary: dict):
        mode_tag = "[green]PAPER[/green]" if config.paper_mode else "[red]LIVE[/red]"
        window_tag = f"[yellow]WINDOW: {window_label}[/yellow]" if in_window else "[dim]no window[/dim]"
        pnl_color = "green" if summary["today_pnl"] >= 0 else "red"
        loss_limit_tag = (
            "[bold red] ⛔ DAILY LOSS LIMIT[/bold red]"
            if summary.get("daily_loss_limit_hit") else ""
        )
        console.print(
            f"[dim]Cycle {self._cycle}[/dim] | {mode_tag} | {window_tag} | "
            f"Capital: [bold]${summary['capital']:.2f}[/bold] | "
            f"Today P&L: [{pnl_color}]{summary['today_pnl']:+.2f}[/{pnl_color}] | "
            f"Positions: {summary['open_positions']}/3 | "
            f"Win rate: {summary['win_rate_pct']:.0f}% ({summary['wins']}W/{summary['losses']}L)"
            f"{loss_limit_tag}"
        )

    def _print_decision(self, decision):
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

        console.print(Panel(panel_content, title="[bold]AI Analysis[/bold]", border_style="blue"))

        if decision.trades:
            table = Table(title="Proposed Trades", show_header=True)
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
