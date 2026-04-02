"""
Kraken CLI wrapper
Wraps the official krakenfx/kraken-cli binary (Rust, installed separately).
Falls back to python-kraken-sdk REST calls for data endpoints.

Install CLI:  https://github.com/krakenfx/kraken-cli/releases
Install SDK:  pip install python-kraken-sdk
"""

import subprocess
import json
import asyncio
import shutil
import os
from typing import Optional
from utils.logger import get_logger

logger = get_logger("kraken_cli")

KRAKEN_BIN_CANDIDATES = [
    "/home/runner/.cargo/bin/kraken",
    "/usr/local/bin/kraken",
]


def _find_kraken_binary() -> str:
    """Return the path to the official krakenfx/kraken-cli binary."""
    for path in KRAKEN_BIN_CANDIDATES:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    # fallback: search PATH but skip the python-kraken-sdk shim
    found = shutil.which("kraken")
    if found and ".pythonlibs" not in found:
        return found
    return "kraken"


KRAKEN_BIN = _find_kraken_binary()


class KrakenCLI:
    """
    Thin wrapper around the `kraken` CLI binary.
    Every method returns parsed JSON or raises on error.
    """

    def __init__(self, paper_mode: bool = True):
        self.paper_mode = paper_mode
        self._verify_cli()

    def _verify_cli(self):
        """Check kraken binary is available."""
        try:
            result = subprocess.run(
                [KRAKEN_BIN, "--version"],
                capture_output=True, text=True, timeout=5
            )
            logger.info(f"Kraken CLI found: {result.stdout.strip()} ({KRAKEN_BIN})")
        except FileNotFoundError:
            logger.warning(
                "kraken CLI binary not found.\n"
                "Install from: https://github.com/krakenfx/kraken-cli/releases\n"
                "Falling back to REST SDK for all calls."
            )

    def _run(self, args: list, timeout: int = 15) -> dict:
        """Execute a kraken CLI command and return parsed JSON."""
        cmd = [KRAKEN_BIN] + args + ["-o", "json"]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout
            )
            if result.returncode != 0:
                err = result.stderr.strip()
                # Parse enriched rate-limit errors from CLI
                try:
                    err_data = json.loads(err)
                    if err_data.get("retryable"):
                        logger.warning(f"Rate limited. Suggestion: {err_data.get('suggestion')}")
                        raise RateLimitError(err_data.get("suggestion", ""))
                except (json.JSONDecodeError, TypeError):
                    pass
                raise KrakenCLIError(f"CLI error (exit {result.returncode}): {err}")
            return json.loads(result.stdout)
        except subprocess.TimeoutExpired:
            raise KrakenCLIError(f"CLI timeout after {timeout}s: {' '.join(cmd)}")

    # ── Public market data (no auth needed) ────────────────────

    def get_ticker(self, pair: str) -> dict:
        """Get current ticker for a spot pair. e.g. BTCUSD"""
        return self._run(["ticker", pair])

    def get_ohlc(self, pair: str, interval: int = 15) -> list:
        """
        Get OHLC candles for a spot pair.
        interval: minutes (1, 5, 15, 30, 60, 240, 1440, 10080, 21600)
        Returns list of [time, open, high, low, close, vwap, volume, count]
        """
        data = self._run(["ohlc", pair, "--interval", str(interval)])
        result = data.get("result", {})
        # Key is the pair name, with a 'last' timestamp key also present
        for key, val in result.items():
            if key != "last" and isinstance(val, list):
                return val
        return []

    def get_orderbook(self, pair: str, count: int = 10) -> dict:
        """Get order book depth."""
        return self._run(["orderbook", pair, "--count", str(count)])

    def get_trades(self, pair: str, count: int = 20) -> dict:
        """Get recent trades."""
        return self._run(["trades", pair, "--count", str(count)])

    def get_futures_ticker(self, symbol: str) -> dict:
        """
        Get futures ticker. e.g. PF_XBTUSD
        Returns: last, bid, ask, volume, fundingRate, openInterest etc.
        """
        return self._run(["futures", "ticker", symbol])

    def get_futures_instruments(self) -> dict:
        """List all available futures instruments."""
        return self._run(["futures", "instruments"])

    # ── Authenticated account data ──────────────────────────────

    def get_balance(self) -> dict:
        return self._run(["balance"])

    def get_open_orders(self) -> dict:
        return self._run(["open-orders"])

    def get_futures_open_positions(self) -> dict:
        return self._run(["futures", "open-positions"])

    def get_futures_account(self) -> dict:
        return self._run(["futures", "account"])

    # ── Paper trading ───────────────────────────────────────────

    def paper_buy(self, symbol: str, size: float, order_type: str = "market",
                  limit_price: Optional[float] = None) -> dict:
        """
        Place a paper buy order: kraken paper buy <PAIR> <VOLUME> [--type market|limit] [--price N]
        """
        args = ["paper", "buy", symbol, str(size), "--type", order_type]
        if order_type == "limit" and limit_price:
            args += ["--price", str(limit_price)]
        logger.info(f"[PAPER] BUY {size} {symbol} @ {'market' if not limit_price else limit_price}")
        return self._run(args)

    def paper_sell(self, symbol: str, size: float, order_type: str = "market",
                   limit_price: Optional[float] = None) -> dict:
        """
        Place a paper sell order: kraken paper sell <PAIR> <VOLUME> [--type market|limit] [--price N]
        """
        args = ["paper", "sell", symbol, str(size), "--type", order_type]
        if order_type == "limit" and limit_price:
            args += ["--price", str(limit_price)]
        logger.info(f"[PAPER] SELL {size} {symbol} @ {'market' if not limit_price else limit_price}")
        return self._run(args)

    def paper_reset(self) -> dict:
        """Reset paper trading state."""
        return self._run(["paper", "reset", "--yes"])

    def paper_positions(self) -> dict:
        """Get current paper positions."""
        return self._run(["paper", "positions"])

    def paper_balance(self) -> dict:
        """Get paper account balance."""
        return self._run(["paper", "balance"])

    # ── Live futures (only used when paper_mode=False) ──────────

    def futures_send_order(self, symbol: str, side: str, size: float,
                           order_type: str = "market",
                           limit_price: Optional[float] = None,
                           stop_price: Optional[float] = None) -> dict:
        """
        Send a live futures order.
        WARNING: Uses real money. Only called when PAPER_MODE=false.
        side: 'buy' | 'sell'
        """
        if self.paper_mode:
            logger.warning("futures_send_order called in paper mode — routing to paper_buy/sell")
            if side == "buy":
                return self.paper_buy(symbol, size, order_type, limit_price)
            return self.paper_sell(symbol, size, order_type, limit_price)

        args = [
            "futures", "sendorder",
            "--symbol", symbol,
            "--side", side,
            "--size", str(size),
            "--orderType", order_type,
        ]
        if limit_price:
            args += ["--limitPrice", str(limit_price)]
        if stop_price:
            args += ["--stopPrice", str(stop_price)]
        logger.warning(f"[LIVE] {side.upper()} {size} {symbol}")
        return self._run(args)

    def futures_cancel_order(self, order_id: str) -> dict:
        if self.paper_mode:
            return self._run(["paper", "cancel", order_id])
        return self._run(["futures", "cancelorder", "--orderId", order_id])


class KrakenCLIError(Exception):
    pass


class RateLimitError(KrakenCLIError):
    pass
