"""
ArbMind — AI-first autonomous trading agent
Kraken Futures paper trading | LangChain + Claude AI brain
"""

import asyncio
import signal
import sys
from agent.loop import TradingLoop
from api.server import start_server
from utils.logger import get_logger

logger = get_logger("main")

API_PORT = 8000


def handle_exit(sig, frame):
    logger.info("Shutdown signal received. Stopping agent cleanly...")
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    logger.info("=" * 60)
    logger.info("  ArbMind Trading Agent — PAPER MODE")
    logger.info("  AI-first | Kraken Futures | Top-20 CMC")
    logger.info("=" * 60)

    # Start the dashboard API server in a background thread
    start_server(host="0.0.0.0", port=API_PORT)
    logger.info(f"Dashboard API running on port {API_PORT}")

    loop = TradingLoop()
    asyncio.run(loop.run())
