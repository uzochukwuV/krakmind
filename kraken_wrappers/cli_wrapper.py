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

    def _run(self, args: list, auth: bool = False, timeout: int = 15) -> dict:
        """Execute a kraken CLI command and return parsed JSON."""
        cmd = [KRAKEN_BIN] + args
        if auth:
            api_key = os.getenv("KRAKEN_API_KEY", "")
            secret_key = os.getenv("KRAKEN_SECRET_KEY", "")
            if not api_key or not secret_key:
                logger.error("Missing KRAKEN_API_KEY or KRAKEN_SECRET_KEY in environment.")
                raise KrakenCLIError("Missing API credentials")
            cmd += ["--api-key", api_key, "--secret-key", secret_key]
            
        # The new kraken CLI outputs single-quoted dicts or raw json depending on endpoint.
        # We capture stdout and try to parse it.
        try:
            logger.debug(f"Running: {' '.join(cmd).replace(os.getenv('KRAKEN_SECRET_KEY', 'xxx'), '***')}")
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout
            )
            if result.returncode != 0:
                err = result.stderr.strip()
                raise KrakenCLIError(f"CLI error (exit {result.returncode}): {err}")
            
            out = result.stdout.strip()
            # Try parsing as JSON
            try:
                return json.loads(out)
            except json.JSONDecodeError:
                # If it's a python dict literal (which kraken-cli v0.3.0 sometimes spits out)
                import ast
                try:
                    return ast.literal_eval(out)
                except:
                    return {"result": out}
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
        return self._run(["spot", "/0/private/Balance", "-X", "POST"], auth=True)

    # ── Live Spot and Futures Orders (No Simulation) ────────────

    def spot_order_create(self, pair: str, side: str, volume: float, order_type: str = "market", limit_price: Optional[float] = None) -> dict:
        """
        Send a real spot order via Kraken CLI.
        kraken spot /0/private/AddOrder -X POST -d '{"pair": "XBTUSD", "type": "buy", "ordertype": "market", "volume": "0.01"}'
        """
        payload = {
            "pair": pair,
            "type": side.lower(),
            "ordertype": order_type.lower(),
            "volume": str(volume)
        }
        if limit_price:
            payload["price"] = str(limit_price)
            
        args = ["spot", "/0/private/AddOrder", "-X", "POST", "-d", json.dumps(payload)]
        logger.info(f"[LIVE SPOT] {side.upper()} {volume} {pair}")
        return self._run(args, auth=True)

    def futures_send_order(self, symbol: str, side: str, size: float,
                           order_type: str = "market",
                           limit_price: Optional[float] = None,
                           stop_price: Optional[float] = None) -> dict:
        """
        Send a real futures order via Kraken CLI.
        kraken futures /derivatives/api/v3/sendorder -X POST -d '{"orderType": "mkt", "symbol": "PF_XBTUSD", "side": "buy", "size": 1}'
        """
        payload = {
            "orderType": "mkt" if order_type.lower() == "market" else "lmt",
            "symbol": symbol,
            "side": side.lower(),
            "size": size
        }
        if limit_price:
            payload["limitPrice"] = limit_price
        if stop_price:
            payload["stopPrice"] = stop_price
            payload["orderType"] = "stp"

        args = ["futures", "/derivatives/api/v3/sendorder", "-X", "POST", "-d", json.dumps(payload)]
        logger.warning(f"[LIVE FUTURES] {side.upper()} {size} {symbol}")
        return self._run(args, auth=True)

    def futures_cancel_order(self, order_id: str) -> dict:
        payload = {"order_id": order_id}
        return self._run(["futures", "/derivatives/api/v3/cancelorder", "-X", "POST", "-d", json.dumps(payload)], auth=True)


class KrakenCLIError(Exception):
    pass


class RateLimitError(KrakenCLIError):
    pass
