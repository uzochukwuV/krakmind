"""
Prism Signal Engine — independent async loop
Runs in parallel with the main trading loop.

Every PRISM_POLL_INTERVAL seconds it:
  1. Fetches a full signal snapshot from PrismAPI
  2. Writes it to PrismSignalStore (shared in-memory store)
  3. Emits trade opportunities if a high-conviction signal is found

The main trading loop reads from PrismSignalStore when building AI context.
Neither loop blocks the other.

Signal classification:
- STRONG_BUY:   F&G < 30 (fear) + BTC sentiment bullish + dipping (mean-reversion)
- BUY:          F&G < 45 + one positive signal
- NEUTRAL:      No clear signal
- SELL_SIGNAL:  F&G > 75 (extreme greed) + funding rate high positive (overheated)
"""

import asyncio
import time
import datetime
from typing import Optional
from rich.console import Console
from rich.table import Table

from data.prism_client import PrismClient, PRISM_TO_KRAKEN
from utils.logger import get_logger

logger = get_logger("prism_loop")
console = Console()

PRISM_POLL_INTERVAL = 300   # 5 minutes — respects rate limits, data updates every 5m anyway
FAST_POLL_INTERVAL  = 60    # 1 minute when in a trading window or signal is hot


class PrismSignalStore:
    """
    Thread-safe in-memory store for latest Prism signal data.
    Written by PrismSignalEngine, read by AIBrain context builder.
    Single shared instance (module-level singleton).
    """

    def __init__(self):
        self._snapshot: dict = {}
        self._last_update: float = 0.0
        self._poll_count: int = 0
        self._signals: list[dict] = []   # latest classified signals

    def update(self, snapshot: dict, signals: list[dict]):
        self._snapshot  = snapshot
        self._last_update = time.time()
        self._poll_count += 1
        self._signals = signals

    @property
    def is_fresh(self) -> bool:
        """Returns True if data was updated in the last 10 minutes."""
        return (time.time() - self._last_update) < 600

    @property
    def age_seconds(self) -> float:
        return time.time() - self._last_update if self._last_update else float("inf")

    def get_snapshot(self) -> dict:
        return self._snapshot

    def get_signals(self) -> list[dict]:
        return self._signals

    @property
    def last_updated(self) -> float:
        return self._last_update

    def get_summary_text(self) -> str:
        """Short text summary for AI context injection."""
        if not self._snapshot:
            return "Prism: No data yet"
        fg = self._snapshot.get("fear_greed", {})
        g  = self._snapshot.get("global", {})
        age_min = self.age_seconds / 60
        return (
            f"F&G={fg.get('value','N/A')} ({fg.get('label','')}) | "
            f"BTC dom={g.get('btc_dominance', 0):.1f}% | "
            f"Mkt cap Δ24h={g.get('market_cap_change_24h_pct', 0):+.2f}% | "
            f"Age={age_min:.0f}m"
        )


# Module-level singleton — shared across loops
prism_store = PrismSignalStore()


class PrismSignalEngine:
    """
    Independent async signal engine powered by PrismAPI.
    Runs as a third coroutine alongside the main and position-monitor loops.
    """

    def __init__(self, position_manager=None):
        self.client = PrismClient()
        self.positions = position_manager
        self._cycle = 0
        logger.info("Prism Signal Engine initialised")

    async def run(self):
        """Main loop — runs forever."""
        logger.info("Prism Signal Engine starting...")

        # First poll immediately on start
        await self._poll()

        while True:
            interval = FAST_POLL_INTERVAL if self._is_hot_period() else PRISM_POLL_INTERVAL
            await asyncio.sleep(interval)
            await self._poll()

    async def _poll(self):
        """Fetch snapshot, classify signals, store results."""
        self._cycle += 1
        try:
            # Run blocking HTTP calls in executor
            snapshot = await asyncio.get_event_loop().run_in_executor(
                None, self.client.get_signal_snapshot
            )
            signals = self._classify_signals(snapshot)
            prism_store.update(snapshot, signals)

            self._print_prism_status(snapshot, signals)

        except Exception as e:
            logger.error(f"Prism poll #{self._cycle} failed: {e}", exc_info=True)

    def _is_hot_period(self) -> bool:
        """Use fast polling when there are active signals or open positions."""
        has_signals = bool(prism_store.get_signals())
        has_positions = bool(
            self.positions and self.positions.get_open_positions()
        ) if self.positions else False
        return has_signals or has_positions

    # ── Signal classification ─────────────────────────────────────

    def _classify_signals(self, snap: dict) -> list[dict]:
        """
        Convert raw Prism snapshot into a list of actionable trade signals.
        Each signal targets a Kraken Futures symbol.
        """
        signals = []
        if not snap:
            return signals

        fg_value  = snap.get("fear_greed", {}).get("value", 50) or 50
        fg_label  = snap.get("fear_greed", {}).get("label", "Neutral")
        btc_dom   = snap.get("global", {}).get("btc_dominance", 50) or 50
        mkt_chg   = snap.get("global", {}).get("market_cap_change_24h_pct", 0) or 0
        alt_sigs  = snap.get("alt_signals", {})

        # Map losers to a quick lookup
        losers_syms = {c["symbol"] for c in snap.get("losers", [])}
        trending_syms = {t["symbol"] for t in snap.get("trending", [])}

        for kraken_sym, alt in alt_sigs.items():
            prism_sym   = alt.get("prism_symbol", "")
            change_24h  = alt.get("change_24h_pct") or 0
            sentiment   = alt.get("sentiment_label", "")
            sent_score  = alt.get("sentiment_score") or 50
            momentum    = alt.get("price_momentum") or 0

            signal_score  = 0
            signal_tags   = []
            direction     = "long"

            # ── FEAR zone = mean-reversion buy opportunity ──────
            if fg_value <= 25:
                signal_score += 2
                signal_tags.append(f"extreme_fear(F&G={fg_value})")
            elif fg_value <= 40:
                signal_score += 1
                signal_tags.append(f"fear(F&G={fg_value})")

            # ── 24h dip = potential oversold entry ──────────────
            if change_24h <= -5.0:
                signal_score += 2
                signal_tags.append(f"large_dip_24h({change_24h:.1f}%)")
            elif change_24h <= -2.0:
                signal_score += 1
                signal_tags.append(f"dip_24h({change_24h:.1f}%)")

            # ── Sentiment confirmation ──────────────────────────
            if sentiment == "bullish" and sent_score >= 60:
                signal_score += 1
                signal_tags.append(f"sentiment_bullish({sent_score:.0f})")

            # ── Market context ──────────────────────────────────
            if mkt_chg > 1.0:
                signal_score += 1
                signal_tags.append(f"market_rising(+{mkt_chg:.1f}%)")

            # ── Trending = attention / momentum ─────────────────
            if prism_sym in trending_syms:
                signal_score += 1
                signal_tags.append("trending")

            # ── Alt is among top losers (oversold candidate) ────
            if prism_sym in losers_syms and fg_value < 50:
                signal_score += 1
                signal_tags.append("top_loser_in_fear")

            # ── Greed zone = avoid longs / look for shorts ──────
            if fg_value >= 80:
                signal_score = max(0, signal_score - 2)
                signal_tags.append(f"extreme_greed_CAUTION(F&G={fg_value})")
                direction = "skip"  # overheated market — skip entries

            # Only emit signal if score is meaningful
            if signal_score >= 2 and direction != "skip" and kraken_sym.startswith("PF_"):
                signals.append({
                    "kraken_symbol": kraken_sym,
                    "prism_symbol":  prism_sym,
                    "direction":     direction,
                    "signal_score":  signal_score,
                    "tags":          signal_tags,
                    "change_24h_pct": change_24h,
                    "sentiment_label": sentiment,
                    "sentiment_score": sent_score,
                    "fear_greed":    fg_value,
                    "timestamp":     time.time(),
                    "classification": (
                        "STRONG_BUY" if signal_score >= 4 else
                        "BUY"        if signal_score >= 2 else
                        "NEUTRAL"
                    ),
                })

        # Sort by score descending
        signals.sort(key=lambda x: x["signal_score"], reverse=True)
        return signals

    # ── Display ───────────────────────────────────────────────────

    def _print_prism_status(self, snap: dict, signals: list[dict]):
        fg = snap.get("fear_greed", {})
        g  = snap.get("global", {})
        fg_val = fg.get("value", "N/A")
        fg_lbl = fg.get("label", "")

        fg_color = (
            "red"    if isinstance(fg_val, int) and fg_val <= 25 else
            "yellow" if isinstance(fg_val, int) and fg_val <= 45 else
            "green"  if isinstance(fg_val, int) and fg_val >= 75 else
            "white"
        )

        console.print(
            f"[dim][PRISM #{self._cycle}][/dim] "
            f"F&G: [{fg_color}]{fg_val} {fg_lbl}[/{fg_color}] | "
            f"BTC dom: {g.get('btc_dominance', 0):.1f}% | "
            f"Mkt Δ24h: {g.get('market_cap_change_24h_pct', 0):+.2f}% | "
            f"Signals: [bold]{len(signals)}[/bold]"
        )

        if signals:
            table = Table(title="Prism Signals", show_header=True, min_width=80)
            table.add_column("Symbol", style="bold")
            table.add_column("Score")
            table.add_column("Class")
            table.add_column("24h Δ")
            table.add_column("Sentiment")
            table.add_column("Tags")
            for s in signals[:6]:
                cls_color = "green" if s["classification"] == "STRONG_BUY" else "yellow"
                table.add_row(
                    s["kraken_symbol"],
                    str(s["signal_score"]),
                    f"[{cls_color}]{s['classification']}[/{cls_color}]",
                    f"{s['change_24h_pct']:+.1f}%",
                    f"{s.get('sentiment_label','')[:8]} ({s.get('sentiment_score',0):.0f})",
                    ", ".join(s["tags"][:3]),
                )
            console.print(table)
