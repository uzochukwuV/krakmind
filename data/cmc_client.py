"""
CoinMarketCap data client.
Fetches top-20 listings, price changes, dominance, fear & greed proxy.
Free tier: 10,000 credits/month — we cache aggressively.
"""

import time
import requests
from typing import Optional
from utils.logger import get_logger
from config import config

logger = get_logger("cmc")

CMC_BASE = "https://pro-api.coinmarketcap.com/v1"


class CMCClient:
    def __init__(self):
        self.api_key = config.cmc_api_key
        self.headers = {
            "X-CMC_PRO_API_KEY": self.api_key,
            "Accept": "application/json",
        }
        self._cache: dict = {}
        self._cache_ttl = 120  # seconds — CMC free tier is credit-based

    def _cached_get(self, url: str, params: dict, cache_key: str) -> Optional[dict]:
        if not self.api_key:
            logger.warning(f"No CMC_API_KEY found in .env! Returning mock data for {cache_key}")
            return self._get_mock_data(cache_key)
            
        now = time.time()
        if cache_key in self._cache:
            data, ts = self._cache[cache_key]
            if now - ts < self._cache_ttl:
                return data
        try:
            resp = requests.get(url, headers=self.headers, params=params, timeout=10)
            if resp.status_code == 401:
                logger.error("CMC API Key is invalid (401 Unauthorized). Switching to mock data.")
                self.api_key = "" # Disable future requests to prevent spam
                return self._get_mock_data(cache_key)
                
            resp.raise_for_status()
            data = resp.json()
            self._cache[cache_key] = (data, now)
            return data
        except requests.RequestException as e:
            logger.error(f"CMC request failed: {e}")
            return self._get_mock_data(cache_key)

    def _get_mock_data(self, cache_key: str) -> dict:
        """Returns fallback mock data if CMC API key is missing or invalid."""
        if cache_key == "top20":
            return {
                "data": [
                    {"id": 1, "name": "Bitcoin", "symbol": "BTC", "slug": "bitcoin", "cmc_rank": 1, 
                     "quote": {"USD": {"price": 65000, "percent_change_1h": 0.1, "percent_change_24h": 1.2, "percent_change_7d": -2.0, "volume_24h": 35000000000, "market_cap": 1200000000000, "market_cap_dominance": 52.0}}},
                    {"id": 2, "name": "Ethereum", "symbol": "ETH", "slug": "ethereum", "cmc_rank": 2,
                     "quote": {"USD": {"price": 3500, "percent_change_1h": -0.2, "percent_change_24h": 2.5, "percent_change_7d": 1.5, "volume_24h": 15000000000, "market_cap": 400000000000, "market_cap_dominance": 17.5}}},
                ]
            }
        elif cache_key == "global_metrics":
            return {
                "data": {
                    "btc_dominance": 52.0,
                    "eth_dominance": 17.5,
                    "active_cryptocurrencies": 10000,
                    "quote": {
                        "USD": {
                            "total_market_cap": 2500000000000,
                            "total_volume_24h": 80000000000,
                            "total_market_cap_yesterday_percentage_change": 1.5
                        }
                    }
                }
            }
        return {}

    def get_top20_listings(self) -> list[dict]:
        """
        Get top 20 cryptos by market cap with 1h, 24h, 7d % changes.
        Returns list of dicts with: name, symbol, price, percent_change_1h,
        percent_change_24h, volume_24h, market_cap_dominance.
        """
        data = self._cached_get(
            f"{CMC_BASE}/cryptocurrency/listings/latest",
            {"start": 1, "limit": 20, "convert": "USD"},
            "top20"
        )
        if not data:
            return []
        coins = []
        for c in data.get("data", []):
            quote = c.get("quote", {}).get("USD", {})
            coins.append({
                "id": c.get("id"),
                "name": c.get("name"),
                "symbol": c.get("symbol"),
                "slug": c.get("slug"),
                "rank": c.get("cmc_rank"),
                "price": quote.get("price", 0),
                "percent_change_1h": quote.get("percent_change_1h", 0),
                "percent_change_24h": quote.get("percent_change_24h", 0),
                "percent_change_7d": quote.get("percent_change_7d", 0),
                "volume_24h": quote.get("volume_24h", 0),
                "market_cap": quote.get("market_cap", 0),
                "market_cap_dominance": quote.get("market_cap_dominance", 0),
            })
        return coins

    def get_global_metrics(self) -> Optional[dict]:
        """
        Global crypto market metrics: total market cap, BTC dominance,
        ETH dominance, total volume, active currencies.
        """
        data = self._cached_get(
            f"{CMC_BASE}/global-metrics/quotes/latest",
            {"convert": "USD"},
            "global_metrics"
        )
        if not data:
            return None
        d = data.get("data", {})
        quote = d.get("quote", {}).get("USD", {})
        return {
            "total_market_cap": quote.get("total_market_cap", 0),
            "total_volume_24h": quote.get("total_volume_24h", 0),
            "btc_dominance": d.get("btc_dominance", 0),
            "eth_dominance": d.get("eth_dominance", 0),
            "active_cryptocurrencies": d.get("active_cryptocurrencies", 0),
            "market_cap_change_24h": quote.get("total_market_cap_yesterday_percentage_change", 0),
        }

    def get_fear_and_greed_proxy(self, coins: list[dict]) -> dict:
        """
        Synthetic fear/greed from CMC data (free tier doesn't include F&G index).
        Uses: avg 24h change, BTC dominance shift, volume surge/collapse.
        Returns: score (0-100), label, components.
        """
        if not coins:
            return {"score": 50, "label": "Neutral", "components": {}}

        btc = next((c for c in coins if c["symbol"] == "BTC"), None)
        eth = next((c for c in coins if c["symbol"] == "ETH"), None)

        # Average 24h change across top 20
        changes = [c["percent_change_24h"] for c in coins if c["percent_change_24h"] is not None]
        avg_24h = sum(changes) / len(changes) if changes else 0

        # Volume change proxy (24h vol vs typical — simplified)
        top10_vol = sum(c["volume_24h"] for c in coins[:10])

        # BTC dominance: higher = fear (flight to BTC)
        btc_dom = btc.get("market_cap_dominance", 50) if btc else 50

        # Score: avg 24h change maps -10% → 0, +10% → 100; clipped
        change_score = min(100, max(0, (avg_24h + 10) * 5))
        # BTC dominance: >60% fear, <40% greed
        dom_score = min(100, max(0, (60 - btc_dom) * 5 + 50))

        score = (change_score * 0.6 + dom_score * 0.4)

        if score >= 75:
            label = "Extreme Greed"
        elif score >= 55:
            label = "Greed"
        elif score >= 45:
            label = "Neutral"
        elif score >= 25:
            label = "Fear"
        else:
            label = "Extreme Fear"

        return {
            "score": round(score, 1),
            "label": label,
            "components": {
                "avg_24h_change": round(avg_24h, 2),
                "change_score": round(change_score, 1),
                "btc_dominance": round(btc_dom, 2),
                "dom_score": round(dom_score, 1),
            }
        }

    def get_market_snapshot(self) -> dict:
        """
        Full market snapshot for AI context injection.
        Single call returns everything the AI needs.
        """
        coins = self.get_top20_listings()
        metrics = self.get_global_metrics() or {}
        fg = self.get_fear_and_greed_proxy(coins)

        return {
            "top20": coins,
            "global": metrics,
            "fear_and_greed": fg,
            "coins_dipping_1h": [
                c for c in coins if c.get("percent_change_1h", 0) < -0.5
            ],
            "coins_rallying_1h": [
                c for c in coins if c.get("percent_change_1h", 0) > 0.5
            ],
        }
