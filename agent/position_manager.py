"""
Position Manager
Tracks open paper positions, computes P&L, enforces stops/targets.
Persists state to a JSON file so the agent survives restarts.

Improvements (v2):
- Trailing stops: move stop to break-even at +2%, lock profit at +3%
- Daily loss limit: halt new trades if down >3% on the day
- Peak capital tracking for drawdown monitoring
"""

import json
import os
import time
import datetime
from typing import Optional
from utils.logger import get_logger
from config import config
from kraken_wrappers.rest_client import KrakenRESTClient

logger = get_logger("positions")

STATE_FILE = "data/paper_positions.json"

DAILY_LOSS_LIMIT_PCT = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "0.03"))   # halt if down 3% today
TRAILING_BREAKEVEN_PCT = float(os.getenv("TRAILING_BREAKEVEN_PCT", "0.02"))  # move stop to entry at +2%
TRAILING_LOCK_PCT = float(os.getenv("TRAILING_LOCK_PCT", "0.03"))            # lock in 1% profit at +3%
TRAILING_LOCK_BUFFER = float(os.getenv("TRAILING_LOCK_BUFFER", "0.01"))      # stop placed 1% above entry when locking


class PositionManager:
    def __init__(self, kraken_cli):
        self.cli = kraken_cli
        self.rest = KrakenRESTClient()
        self._state: dict = self._load_state()

    # ── State persistence ───────────────────────────────────────

    def _load_state(self) -> dict:
        os.makedirs("data", exist_ok=True)
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE) as f:
                    return json.load(f)
            except Exception:
                pass
        capital = config.paper_capital
        return {
            "capital": capital,
            "wallets": {
                "kraken_spot": capital,    # Assuming all capital starts in Kraken Spot
                "kraken_futures": 0.0,
                "base_web3": 0.0
            },
            "deployed": 0.0,
            "peak_capital": capital,
            "positions": {},
            "closed_trades": [],
            "arb_alerts": [],
            "arb_stats": {
                "total_alerts": 0,
                "total_estimated_pnl": 0.0,
                "best_gap_pct": 0.0,
                "best_gap_symbol": "",
            },
            "funding_alerts": [],
            "stats": {
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "total_pnl": 0.0,
                "today_trades": 0,
                "today_pnl": 0.0,
                "day_start_capital": capital,
                "last_reset_date": str(datetime.date.today()),
                "trailing_stop_activations": 0,
                "daily_loss_halts": 0,
            }
        }

    def _save_state(self):
        with open(STATE_FILE, "w") as f:
            json.dump(self._state, f, indent=2, default=str)

    def _reset_daily_stats_if_needed(self):
        today = str(datetime.date.today())
        if self._state["stats"].get("last_reset_date") != today:
            self._state["stats"]["today_trades"] = 0
            self._state["stats"]["today_pnl"] = 0.0
            self._state["stats"]["day_start_capital"] = round(self._state["capital"], 2)
            self._state["stats"]["last_reset_date"] = today
            self._save_state()

    # ── Risk guards ─────────────────────────────────────────────

    def is_daily_loss_limit_hit(self) -> bool:
        """
        Returns True if today's losses exceed DAILY_LOSS_LIMIT_PCT of day-start capital.
        When True, no new positions should be opened.
        """
        self._reset_daily_stats_if_needed()
        day_start = self._state["stats"].get("day_start_capital", self._state["capital"])
        today_pnl = self._state["stats"]["today_pnl"]
        if day_start <= 0:
            return False
        loss_pct = today_pnl / day_start
        return loss_pct <= -DAILY_LOSS_LIMIT_PCT

    def can_open_position(self, size_pct: float) -> tuple[bool, str]:
        """Check if a new position is allowed under risk rules."""
        if self.is_daily_loss_limit_hit():
            self._state["stats"]["daily_loss_halts"] = self._state["stats"].get("daily_loss_halts", 0) + 1
            pnl = self._state["stats"]["today_pnl"]
            return False, f"Daily loss limit hit (today P&L: ${pnl:.2f}, limit: {DAILY_LOSS_LIMIT_PCT*100:.0f}%)"

        positions = self._state["positions"]
        capital = self._state["capital"]
        deployed = self._state["deployed"]

        if len(positions) >= 3:
            return False, f"Max 3 simultaneous positions reached ({len(positions)} open)"

        required = capital * size_pct
        if deployed + required > capital * 0.30:
            return False, f"Max 30% capital deployment reached (deployed={deployed:.2f})"

        return True, "ok"

    # ── Trailing stop logic ──────────────────────────────────────

    def update_trailing_stops(self):
        """
        Adjust stop prices as trades move in favour:
        - At +TRAILING_BREAKEVEN_PCT: move stop to entry (break-even)
        - At +TRAILING_LOCK_PCT: move stop to entry + TRAILING_LOCK_BUFFER (lock profit)
        """
        # If ATR or volatility data exists in the future, we could replace these static bands.
        # Currently defaults to 2% break-even and 3% trail logic.
        for pos_id, pos in self._state["positions"].items():
            price = self._get_current_price(pos["spot_pair"])
            if not price:
                continue

            entry = pos["entry_price"]
            direction = pos["direction"]
            current_stop = pos["stop_price"]
            trailing_stage = pos.get("trailing_stage", 0)  # 0=none, 1=breakeven, 2=locked

            if direction == "long":
                pnl_pct = (price - entry) / entry
                # Stage 2: lock in profit — stop moves to entry + buffer
                if pnl_pct >= TRAILING_LOCK_PCT and trailing_stage < 2:
                    new_stop = entry * (1 + TRAILING_LOCK_BUFFER)
                    if new_stop > current_stop:
                        self._state["positions"][pos_id]["stop_price"] = round(new_stop, 6)
                        self._state["positions"][pos_id]["trailing_stage"] = 2
                        self._state["stats"]["trailing_stop_activations"] = (
                            self._state["stats"].get("trailing_stop_activations", 0) + 1
                        )
                        logger.info(
                            f"[TRAIL LOCK] {pos['symbol']} | stop moved to ${new_stop:.4f} "
                            f"(+{TRAILING_LOCK_BUFFER*100:.0f}% above entry, pnl={pnl_pct*100:.2f}%)"
                        )
                # Stage 1: break-even — stop moves to entry
                elif pnl_pct >= TRAILING_BREAKEVEN_PCT and trailing_stage < 1:
                    new_stop = entry  # break-even
                    if new_stop > current_stop:
                        self._state["positions"][pos_id]["stop_price"] = round(new_stop, 6)
                        self._state["positions"][pos_id]["trailing_stage"] = 1
                        self._state["stats"]["trailing_stop_activations"] = (
                            self._state["stats"].get("trailing_stop_activations", 0) + 1
                        )
                        logger.info(
                            f"[TRAIL BE] {pos['symbol']} | stop moved to break-even ${new_stop:.4f} "
                            f"(pnl={pnl_pct*100:.2f}%)"
                        )
            else:  # short
                pnl_pct = (entry - price) / entry
                if pnl_pct >= TRAILING_LOCK_PCT and trailing_stage < 2:
                    new_stop = entry * (1 - TRAILING_LOCK_BUFFER)
                    if new_stop < current_stop:
                        self._state["positions"][pos_id]["stop_price"] = round(new_stop, 6)
                        self._state["positions"][pos_id]["trailing_stage"] = 2
                        logger.info(f"[TRAIL LOCK] {pos['symbol']} short | stop moved to ${new_stop:.4f}")
                elif pnl_pct >= TRAILING_BREAKEVEN_PCT and trailing_stage < 1:
                    new_stop = entry
                    if new_stop < current_stop:
                        self._state["positions"][pos_id]["stop_price"] = round(new_stop, 6)
                        self._state["positions"][pos_id]["trailing_stage"] = 1
                        logger.info(f"[TRAIL BE] {pos['symbol']} short | stop moved to break-even ${new_stop:.4f}")

        self._save_state()

    # ── Position ops ────────────────────────────────────────────

    def open_position(self, symbol: str, direction: str, size_pct: float,
                      entry_type: str = "market",
                      stop_loss_pct: float = 0.02,
                      take_profit_pct: float = 0.04,
                      thesis: str = "",
                      protocol: str = "kraken_futures") -> Optional[dict]:
        """Open a paper position. Returns position dict or None if rejected."""
        self._reset_daily_stats_if_needed()
        allowed, reason = self.can_open_position(size_pct)
        if not allowed:
            logger.warning(f"Position rejected for {symbol}: {reason}")
            return None

        capital = self._state["capital"]
        position_value = capital * size_pct

        # Track protocol allocation
        if protocol not in self._state["wallets"]:
            self._state["wallets"][protocol] = 0.0
            
        if self._state["wallets"].get("kraken_spot", 0) >= position_value:
            self._state["wallets"]["kraken_spot"] -= position_value
            self._state["wallets"][protocol] += position_value
        else:
            logger.warning(f"Insufficient funds in kraken_spot to allocate to {protocol}")
            return None

        spot_pair = self._get_spot_pair(symbol)
        entry_price = self._get_current_price(spot_pair)
        if not entry_price:
            logger.error(f"Cannot get price for {symbol} / {spot_pair}")
            # Revert transfer
            self._state["wallets"]["kraken_spot"] += position_value
            self._state["wallets"][protocol] -= position_value
            return None

        quantity = position_value / entry_price

        # Execute paper order via CLI
        cli_symbol = spot_pair.replace("XXBT", "BTC").replace("X", "").replace("Z", "")
        try:
            if direction == "long":
                order_result = self.cli.paper_buy(symbol=cli_symbol, size=round(quantity, 4), order_type=entry_type)
            else:
                order_result = self.cli.paper_sell(symbol=cli_symbol, size=round(quantity, 4), order_type=entry_type)
        except Exception as e:
            logger.warning(f"CLI paper order failed, simulating: {e}")
            order_result = {"status": "simulated"}

        position_id = f"{symbol}_{int(time.time())}"
        stop_price = entry_price * (1 - stop_loss_pct) if direction == "long" else entry_price * (1 + stop_loss_pct)
        target_price = entry_price * (1 + take_profit_pct) if direction == "long" else entry_price * (1 - take_profit_pct)

        position = {
            "id": position_id,
            "symbol": symbol,
            "spot_pair": spot_pair,
            "direction": direction,
            "entry_price": entry_price,
            "quantity": quantity,
            "position_value": position_value,
            "size_pct": size_pct,
            "stop_price": stop_price,
            "target_price": target_price,
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": take_profit_pct,
            "thesis": thesis,
            "opened_at": datetime.datetime.utcnow().isoformat(),
            "order_result": order_result,
            "unrealized_pnl": 0.0,
            "unrealized_pnl_pct": 0.0,
            "trailing_stage": 0,  # 0=none, 1=break-even, 2=profit-locked
        }

        self._state["positions"][position_id] = position
        self._state["deployed"] += position_value
        self._state["stats"]["total_trades"] += 1
        self._state["stats"]["today_trades"] += 1
        self._save_state()

        logger.info(
            f"[PAPER OPEN] {direction.upper()} {symbol} | "
            f"entry={entry_price:.4f} | stop={stop_price:.4f} | "
            f"target={target_price:.4f} | size=${position_value:.2f}"
        )
        return position

    def close_position(self, position_id: str, reason: str = "manual") -> Optional[dict]:
        """Close a position and record P&L."""
        pos = self._state["positions"].get(position_id)
        if not pos:
            logger.warning(f"Position {position_id} not found")
            return None

        exit_price = self._get_current_price(pos["spot_pair"])
        if not exit_price:
            logger.error(f"Cannot get exit price for {pos['spot_pair']}")
            return None

        direction = pos["direction"]
        entry_price = pos["entry_price"]
        quantity = pos["quantity"]

        if direction == "long":
            pnl = (exit_price - entry_price) * quantity
            pnl_pct = (exit_price - entry_price) / entry_price * 100
        else:
            pnl = (entry_price - exit_price) * quantity
            pnl_pct = (entry_price - exit_price) / entry_price * 100

        closed = {
            **pos,
            "exit_price": exit_price,
            "pnl": pnl,
            "pnl_pct": round(pnl_pct, 3),
            "closed_at": datetime.datetime.utcnow().isoformat(),
            "close_reason": reason,
            "duration_minutes": int(
                (datetime.datetime.utcnow() - datetime.datetime.fromisoformat(pos["opened_at"])).total_seconds() / 60
            ),
        }

        self._state["capital"] += pnl
        self._state["peak_capital"] = max(self._state.get("peak_capital", 0), self._state["capital"])
        self._state["deployed"] = max(0, self._state["deployed"] - pos["position_value"])
        
        # Return capital to kraken_spot (simulating settlement)
        protocol = pos.get("protocol", "kraken_futures")
        return_value = pos["position_value"] + pnl
        if protocol in self._state["wallets"]:
            self._state["wallets"][protocol] -= pos["position_value"]
            self._state["wallets"]["kraken_spot"] += return_value

        self._state["stats"]["total_pnl"] += pnl
        self._state["stats"]["today_pnl"] += pnl
        if pnl > 0:
            self._state["stats"]["wins"] += 1
        else:
            self._state["stats"]["losses"] += 1

        self._state["closed_trades"].append(closed)
        self._state["closed_trades"] = self._state["closed_trades"][-50:]
        del self._state["positions"][position_id]
        
        # Log to persistent journal
        from data.journal import TradeJournal
        journal = TradeJournal()
        journal.log_trade(closed)
        
        self._save_state()

        emoji = "✅" if pnl > 0 else "❌"
        trail_tag = f" [trail_stage={pos.get('trailing_stage',0)}]" if pos.get("trailing_stage", 0) > 0 else ""
        logger.info(
            f"{emoji} [PAPER CLOSE] {pos['symbol']} | "
            f"pnl={pnl:+.2f} ({pnl_pct:+.2f}%) | "
            f"reason={reason}{trail_tag} | duration={closed['duration_minutes']}m"
        )
        return closed

    # ── Position monitoring ──────────────────────────────────────

    def update_unrealized_pnl(self):
        """Refresh unrealized P&L on all open positions."""
        for pos_id, pos in list(self._state["positions"].items()):
            price = self._get_current_price(pos["spot_pair"])
            if not price:
                continue
            if pos["direction"] == "long":
                pnl = (price - pos["entry_price"]) * pos["quantity"]
                pnl_pct = (price - pos["entry_price"]) / pos["entry_price"] * 100
            else:
                pnl = (pos["entry_price"] - price) * pos["quantity"]
                pnl_pct = (pos["entry_price"] - price) / pos["entry_price"] * 100
            self._state["positions"][pos_id]["current_price"] = price
            self._state["positions"][pos_id]["unrealized_pnl"] = round(pnl, 4)
            self._state["positions"][pos_id]["unrealized_pnl_pct"] = round(pnl_pct, 3)
        self._save_state()

    def check_stops_and_targets(self) -> list[dict]:
        """
        Check all open positions against stop/target levels.
        Applies trailing stop updates first, then checks levels.
        Returns list of positions to close and why.
        """
        self.update_trailing_stops()

        to_close = []
        for pos_id, pos in self._state["positions"].items():
            price = pos.get("current_price") or self._get_current_price(pos["spot_pair"])
            if not price:
                continue

            direction = pos["direction"]
            stop = pos["stop_price"]
            target = pos["target_price"]

            if direction == "long":
                if price <= stop:
                    to_close.append({"id": pos_id, "reason": "stop_loss", "price": price})
                elif price >= target:
                    to_close.append({"id": pos_id, "reason": "take_profit", "price": price})
            else:
                if price >= stop:
                    to_close.append({"id": pos_id, "reason": "stop_loss", "price": price})
                elif price <= target:
                    to_close.append({"id": pos_id, "reason": "take_profit", "price": price})

            # Time stop: close after 4 hours if neither hit
            opened = datetime.datetime.fromisoformat(pos["opened_at"])
            age_hours = (datetime.datetime.utcnow() - opened).total_seconds() / 3600
            if age_hours >= 4.0 and pos_id not in [x["id"] for x in to_close]:
                to_close.append({"id": pos_id, "reason": "time_stop_4h", "price": price})

        return to_close

    # ── Kelly position sizing ────────────────────────────────────

    def kelly_position_size(self, confidence: float,
                            min_pct: float = 0.01,
                            max_pct: float = None) -> float:
        """
        Fractional Kelly criterion sizing scaled by AI confidence.

        Kelly formula: f* = (p*b - q) / b
          p = win rate,  q = 1-p,  b = avg_win / avg_loss

        We then apply a half-Kelly multiplier (conservative) and
        scale by confidence so a 60% confident call gets smaller size
        than a 90% confident call.

        Returns fraction of capital to allocate (clamped to [min_pct, max_pct]).
        Falls back to config default when insufficient trade history.
        """
        cap = max_pct or config.max_position_pct
        closed = self._state.get("closed_trades", [])

        # Need at least 5 trades to calculate meaningful Kelly
        if len(closed) < 5:
            # No history — use confidence-scaled default
            base = config.max_position_pct * confidence
            return round(max(min_pct, min(base, cap)), 4)

        wins  = [t["pnl"] for t in closed if t.get("pnl", 0) > 0]
        losses = [abs(t["pnl"]) for t in closed if t.get("pnl", 0) <= 0]

        if not wins or not losses:
            base = config.max_position_pct * confidence
            return round(max(min_pct, min(base, cap)), 4)

        p = len(wins) / len(closed)            # win probability
        q = 1 - p                               # loss probability
        avg_win  = sum(wins) / len(wins)
        avg_loss = sum(losses) / len(losses)

        if avg_loss == 0:
            return round(cap, 4)

        b = avg_win / avg_loss                 # win/loss ratio
        kelly = (p * b - q) / b               # full Kelly

        # Half-Kelly (conservative) scaled by AI confidence
        sized = kelly * 0.5 * confidence

        result = round(max(min_pct, min(sized, cap)), 4)
        logger.debug(
            f"Kelly sizing: p={p:.2f} b={b:.2f} kelly={kelly:.3f} "
            f"conf={confidence:.2f} → {result:.2%} of capital"
        )
        return result

    # ── Queries ──────────────────────────────────────────────────

    def get_open_positions(self) -> list[dict]:
        self.update_unrealized_pnl()
        return list(self._state["positions"].values())

    def get_account_summary(self) -> dict:
        self._reset_daily_stats_if_needed()
        stats = self._state["stats"]
        total = stats["total_trades"]
        wins = stats["wins"]
        win_rate = (wins / total * 100) if total > 0 else 0
        day_start = stats.get("day_start_capital", self._state["capital"])
        today_loss_pct = (stats["today_pnl"] / day_start * 100) if day_start > 0 else 0

        return {
            "capital": round(self._state["capital"], 2),
            "wallets": {k: round(v, 2) for k, v in self._state.get("wallets", {}).items()},
            "peak_capital": round(self._state.get("peak_capital", self._state["capital"]), 2),
            "deployed": round(self._state["deployed"], 2),
            "exposure_pct": round(self._state["deployed"] / self._state["capital"] * 100, 1),
            "open_positions": len(self._state["positions"]),
            "total_trades": total,
            "wins": wins,
            "losses": stats["losses"],
            "win_rate_pct": round(win_rate, 1),
            "total_pnl": round(stats["total_pnl"], 2),
            "today_trades": stats["today_trades"],
            "today_pnl": round(stats["today_pnl"], 2),
            "today_loss_pct": round(today_loss_pct, 2),
            "daily_loss_limit_hit": self.is_daily_loss_limit_hit(),
            "daily_loss_limit_pct": DAILY_LOSS_LIMIT_PCT * 100,
            "trailing_stop_activations": stats.get("trailing_stop_activations", 0),
            "paper_mode": config.paper_mode,
        }

    # ── Helpers ──────────────────────────────────────────────────

    FUTURES_TO_SPOT = {
        # Canaries
        "PF_XBTUSD":   "XXBTZUSD",
        "PF_ETHUSD":   "XETHZUSD",
        # Original alts
        "PF_SOLUSD":   "SOLUSD",
        "PF_BNBUSD":   "BNBUSD",
        "PF_XRPUSD":   "XXRPZUSD",
        "PF_ADAUSD":   "ADAUSD",
        "PF_AVAXUSD":  "AVAXUSD",
        "PF_DOTUSD":   "DOTUSD",
        "PF_MATICUSD": "MATICUSD",
        "PF_LINKUSD":  "LINKUSD",
        "PF_LTCUSD":   "XLTCZUSD",
        "PF_UNIUSD":   "UNIUSD",
        # Expanded alts
        "PF_DOGEUSD":  "XDGEZUSD",
        "PF_XLMUSD":   "XXLMZUSD",
        "PF_TONUSD":   "TONUSD",
        "PF_FLOWUSD":  "FLOWUSD",
        "PF_ASTERUSD": "ASTERUSD",
        "PF_KAVAUSD":  "KAVAUSD",
        "PF_ARCUSD":   "ARCUSD",
        "PF_GMXUSD":   "GMXUSD",
        "PF_ATOMUSD":  "ATOMUSD",
        "PF_NEARUSD":  "NEARUSD",
    }

    def _get_spot_pair(self, futures_symbol: str) -> str:
        return self.FUTURES_TO_SPOT.get(futures_symbol, futures_symbol)

    def _get_current_price(self, spot_pair: str) -> Optional[float]:
        try:
            candles = self.rest.get_ohlc(pair=spot_pair, interval=1)
            if candles:
                return float(candles[-1][4])
        except Exception as e:
            logger.debug(f"Price fetch failed for {spot_pair}: {e}")
        return None
