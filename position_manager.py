"""
Position Manager
Tracks open paper positions, computes P&L, enforces stops/targets.
Persists state to a JSON file so the agent survives restarts.
"""

import json
import os
import time
import datetime
from typing import Optional
from utils.logger import get_logger
from config import config
from kraken.rest_client import KrakenRESTClient

logger = get_logger("positions")

STATE_FILE = "data/paper_positions.json"


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
        return {
            "capital": config.paper_capital,
            "deployed": 0.0,
            "positions": {},
            "closed_trades": [],
            "stats": {
                "total_trades": 0,
                "wins": 0,
                "losses": 0,
                "total_pnl": 0.0,
                "today_trades": 0,
                "today_pnl": 0.0,
                "last_reset_date": str(datetime.date.today()),
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
            self._state["stats"]["last_reset_date"] = today
            self._save_state()

    # ── Position ops ────────────────────────────────────────────

    def can_open_position(self, size_pct: float) -> tuple[bool, str]:
        """Check if a new position is allowed under risk rules."""
        positions = self._state["positions"]
        capital = self._state["capital"]
        deployed = self._state["deployed"]

        if len(positions) >= 3:
            return False, f"Max 3 simultaneous positions reached ({len(positions)} open)"

        required = capital * size_pct
        if deployed + required > capital * 0.30:
            return False, f"Max 30% capital deployment reached (deployed={deployed:.2f}, capital={capital:.2f})"

        return True, "ok"

    def open_position(self, symbol: str, direction: str, size_pct: float,
                      entry_type: str = "market",
                      stop_loss_pct: float = 0.02,
                      take_profit_pct: float = 0.04,
                      thesis: str = "") -> Optional[dict]:
        """
        Open a paper position. Returns position dict or None if rejected.
        """
        self._reset_daily_stats_if_needed()
        allowed, reason = self.can_open_position(size_pct)
        if not allowed:
            logger.warning(f"Position rejected for {symbol}: {reason}")
            return None

        # Get current price via REST (for precise entry)
        spot_pair = self._get_spot_pair(symbol)
        entry_price = self._get_current_price(spot_pair)
        if not entry_price:
            logger.error(f"Cannot get price for {symbol} / {spot_pair}")
            return None

        capital = self._state["capital"]
        position_value = capital * size_pct
        quantity = position_value / entry_price

        # Execute paper order via CLI
        try:
            if direction == "long":
                order_result = self.cli.paper_buy(
                    symbol=spot_pair.replace("XXBT", "BTC").replace("X", "").replace("Z", ""),
                    size=round(quantity, 4),
                    order_type=entry_type,
                )
            else:
                order_result = self.cli.paper_sell(
                    symbol=spot_pair.replace("XXBT", "BTC").replace("X", "").replace("Z", ""),
                    size=round(quantity, 4),
                    order_type=entry_type,
                )
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

        # Get exit price
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

        # Update stats
        self._state["capital"] += pnl
        self._state["deployed"] = max(0, self._state["deployed"] - pos["position_value"])
        self._state["stats"]["total_pnl"] += pnl
        self._state["stats"]["today_pnl"] += pnl
        if pnl > 0:
            self._state["stats"]["wins"] += 1
        else:
            self._state["stats"]["losses"] += 1

        self._state["closed_trades"].append(closed)
        del self._state["positions"][position_id]
        self._save_state()

        emoji = "✅" if pnl > 0 else "❌"
        logger.info(
            f"{emoji} [PAPER CLOSE] {pos['symbol']} | "
            f"pnl={pnl:+.2f} ({pnl_pct:+.2f}%) | "
            f"reason={reason} | duration={closed['duration_minutes']}m"
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
        Returns list of positions that should be closed and why.
        """
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

        return {
            "capital": round(self._state["capital"], 2),
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
            "paper_mode": config.paper_mode,
        }

    # ── Helpers ──────────────────────────────────────────────────

    # Futures → Spot pair mapping
    FUTURES_TO_SPOT = {
        "PF_SOLUSD":   "SOLUSD",
        "PF_XRPUSD":   "XXRPZUSD",
        "PF_ADAUSD":   "ADAUSD",
        "PF_AVAXUSD":  "AVAXUSD",
        "PF_DOTUSD":   "DOTUSD",
        "PF_LTCUSD":   "XLTCZUSD",
        "PF_LINKUSD":  "LINKUSD",
        "PF_UNIUSD":   "UNIUSD",
        "PF_MATICUSD": "MATICUSD",
        "PF_XBTUSD":   "XXBTZUSD",
        "PF_ETHUSD":   "XETHZUSD",
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
