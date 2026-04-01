"""
Kraken REST data client using python-kraken-sdk.
Used as fallback when CLI binary is not available,
and for high-frequency OHLC polling.

pip install python-kraken-sdk
"""

import asyncio
from typing import Optional
from kraken.spot import Market
from kraken.futures import Market as FuturesMarket
from utils.logger import get_logger
from config import config

logger = get_logger("kraken_rest")


class KrakenRESTClient:
    """
    Wraps python-kraken-sdk for market data.
    Auth not required for public endpoints.
    """

    def __init__(self):
        self._spot_market = Market()
        self._futures_market = FuturesMarket()

    def get_ohlc(self, pair: str, interval: int = 15) -> list:
        """
        Get OHLC candles for a spot pair.
        Returns list of candles: [time, open, high, low, close, vwap, volume, count]
        """
        try:
            data = self._spot_market.get_ohlc(pair=pair, interval=interval)
            result = data.get("result", {})
            for key, val in result.items():
                if key != "last" and isinstance(val, list):
                    return val
        except Exception as e:
            logger.error(f"OHLC fetch failed for {pair}: {e}")
        return []

    def get_ticker(self, pair: str) -> Optional[dict]:
        """Get spot ticker info."""
        try:
            data = self._spot_market.get_ticker(pair=pair)
            result = data.get("result", {})
            return result.get(pair)
        except Exception as e:
            logger.error(f"Ticker fetch failed for {pair}: {e}")
            return None

    def get_futures_tickers(self) -> dict:
        """Get all futures tickers."""
        try:
            data = self._futures_market.get_tickers()
            tickers = {}
            for t in data.get("tickers", []):
                sym = t.get("symbol", "")
                if sym:
                    tickers[sym] = t
            return tickers
        except Exception as e:
            logger.error(f"Futures ticker fetch failed: {e}")
            return {}

    def get_futures_ticker(self, symbol: str) -> Optional[dict]:
        """Get a single futures ticker."""
        tickers = self.get_futures_tickers()
        return tickers.get(symbol)

    def get_futures_orderbook(self, symbol: str) -> Optional[dict]:
        """Get futures orderbook."""
        try:
            return self._futures_market.get_orderbook(symbol=symbol)
        except Exception as e:
            logger.error(f"Futures orderbook failed for {symbol}: {e}")
            return None
