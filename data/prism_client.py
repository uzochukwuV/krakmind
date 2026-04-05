"""
PrismAPI Client — AI-native financial data platform
https://api.prismapi.ai/docs

Covers all crypto signal endpoints relevant to ArbMind:
- Consensus prices (multi-source aggregated)
- Global market metrics + real Fear & Greed index
- Gainers / losers / trending
- Social sentiment (per symbol)
- Funding rates (DEX perp sentiment indicator)
- Market overview

Rate: paid tier — cache aggressively, poll every 3-5 min.
"""

import time
import os
import requests
from typing import Optional
from utils.logger import get_logger

logger = get_logger("prism")

PRISM_BASE = "https://api.prismapi.ai"

# Crypto symbols we track (Kraken Futures alts + canaries)
TRACKED_SYMBOLS = ["BTC", "ETH", "SOL", "XRP", "ADA", "AVAX", "DOT", "LINK", "LTC", "UNI", "BNB", "MATIC"]

# Map Prism symbol → Kraken Futures symbol
PRISM_TO_KRAKEN = {
    "BTC":   "PF_XBTUSD",
    "ETH":   "PF_ETHUSD",
    "SOL":   "PF_SOLUSD",
    "XRP":   "PF_XRPUSD",
    "ADA":   "PF_ADAUSD",
    "AVAX":  "PF_AVAXUSD",
    "DOT":   "PF_DOTUSD",
    "LINK":  "PF_LINKUSD",
    "LTC":   "PF_LTCUSD",
    "UNI":   "PF_UNIUSD",
    "MATIC": "PF_MATICUSD",
    "BNB":   "PF_BNBUSD",
}


class PrismClient:
    """HTTP client wrapping key PrismAPI crypto signal endpoints."""

    def __init__(self, api_key: str = ""):
        self.api_key = api_key or os.getenv("PRISM_API_KEY", "")
        self.session = requests.Session()
        self.session.headers.update({
            "X-API-Key": self.api_key,
            "Accept": "application/json",
        })
        self._cache: dict = {}
        self._default_ttl = 300  # 5 min default

    _last_call_ts: float = 0.0        # class-level throttle tracker
    _min_call_gap: float = 6.5        # 6.5s between calls = ~9/min (under 10/min limit)

    def _get(self, path: str, params: dict = None, ttl: int = None) -> Optional[dict]:
        """Cached GET request with rate-limit throttle."""
        cache_key = f"{path}?{params}"
        ttl = ttl or self._default_ttl
        now = time.time()

        if cache_key in self._cache:
            data, ts = self._cache[cache_key]
            if now - ts < ttl:
                return data   # served from cache — no throttle needed

        # Throttle uncached requests to stay under 10/min
        gap = time.time() - PrismClient._last_call_ts
        if gap < PrismClient._min_call_gap:
            time.sleep(PrismClient._min_call_gap - gap)

        PrismClient._last_call_ts = time.time()
        try:
            resp = self.session.get(
                f"{PRISM_BASE}{path}",
                params=params or {},
                timeout=12,
            )
            if resp.status_code == 200:
                data = resp.json()
                self._cache[cache_key] = (data, now)
                return data
            elif resp.status_code == 429:
                logger.warning(f"Prism rate-limited [{path}] — backing off 15s")
                time.sleep(15)
            else:
                logger.warning(f"Prism {path} → {resp.status_code}: {resp.text[:120]}")
        except requests.RequestException as e:
            logger.error(f"Prism request failed [{path}]: {e}")
        return None

    # ── Consensus prices ─────────────────────────────────────────

    def get_price(self, symbol: str) -> Optional[dict]:
        """Single consensus price with confidence and 24h change."""
        return self._get(f"/crypto/price/{symbol}", ttl=60)

    def get_batch_prices(self, symbols: list = None) -> list[dict]:
        """Multi-symbol consensus prices in one request."""
        syms = symbols or TRACKED_SYMBOLS
        data = self._get(
            "/crypto/prices/batch",
            params={"symbols": ",".join(syms)},
            ttl=60,
        )
        if data:
            return data.get("prices", [])
        return []

    # ── Market overview ──────────────────────────────────────────

    def get_global_market(self) -> Optional[dict]:
        """Total market cap, BTC dominance, 24h volume."""
        return self._get("/crypto/global", ttl=300)

    def get_fear_greed(self) -> Optional[dict]:
        """Real Fear & Greed index (not a proxy). Returns: value 0-100, label."""
        return self._get("/market/fear-greed", ttl=300)

    def get_gainers(self, limit: int = 10) -> list[dict]:
        """Top 24h gainers across crypto market."""
        data = self._get("/market/crypto/gainers", ttl=300)
        if data:
            return data.get("coins", [])[:limit]
        return []

    def get_losers(self, limit: int = 10) -> list[dict]:
        """Top 24h losers across crypto market."""
        data = self._get("/market/crypto/losers", ttl=300)
        if data:
            return data.get("coins", [])[:limit]
        return []

    def get_trending(self) -> list[dict]:
        """Trending tokens by social/volume score."""
        data = self._get("/crypto/trending", ttl=300)
        if data:
            return data.get("tokens", [])[:15]
        return []

    # ── Social sentiment ─────────────────────────────────────────

    def get_sentiment(self, symbol: str) -> Optional[dict]:
        """
        Sentiment for a symbol.
        Returns: sentiment_score (0-100), sentiment_label (bullish/bearish/neutral),
                 price_momentum, developer_activity.
        """
        return self._get(f"/social/{symbol}/sentiment", ttl=600)

    def get_sentiment_batch(self, symbols: list = None) -> dict[str, dict]:
        """Fetch sentiment for multiple symbols. Returns {symbol: sentiment_dict}."""
        syms = symbols or ["BTC", "ETH", "SOL", "XRP", "ADA"]
        results = {}
        for sym in syms:
            s = self.get_sentiment(sym)
            if s and "sentiment_score" in s:
                results[sym] = s
        return results

    # ── Funding rates (DEX perp sentiment) ───────────────────────

    def get_funding_rate(self, symbol: str) -> Optional[dict]:
        """
        Funding rates across DEX perps for a symbol.
        Positive rate = longs pay shorts (bullish sentiment, potentially overheated).
        Negative rate = shorts pay longs (bearish, good for mean-reversion longs).
        Includes: interpretation, best_for_long, best_for_short.
        """
        return self._get(f"/dex/{symbol}/funding/all", ttl=300)

    def get_venue_prices(self, symbol: str) -> dict:
        """Get prices across multiple venues (CEX & DEX)."""
        data = self._get(f"/resolve/{symbol}", ttl=10)
        if not data:
            return {}
            
        venues = data.get("venues", [])
        prices = {}
        for venue in venues:
            v_type = venue.get("type")
            if v_type in ["cex_spot", "dex_perp", "dex_spot"]:
                name = venue.get("name", "").lower()
                if name:
                    # Prism API returns top level price_usd for the consensus price
                    # Using consensus as proxy if venue specific isn't available
                    prices[name] = venue.get("price_usd") or data.get("price_usd")
        return prices

    def get_dex_search(self, symbol: str, chain: str = "base") -> list[dict]:
        """Search DEX pairs for a token."""
        now = time.time()
        cache_key = f"dex_search_{symbol}_{chain}"
        if cache_key in self._cache:
            data, ts = self._cache[cache_key]
            if now - ts < 60:
                return data

        gap = time.time() - PrismClient._last_call_ts
        if gap < PrismClient._min_call_gap:
            time.sleep(PrismClient._min_call_gap - gap)
            
        PrismClient._last_call_ts = time.time()
        
        try:
            resp = self.session.post(
                f"{PRISM_BASE}/dex/search",
                json={"q": symbol, "chain": chain},
                timeout=12
            )
            if resp.status_code == 200:
                data = resp.json()
                pairs = data.get("pairs", [])
                aerodrome_pairs = [p for p in pairs if p.get("dexId") == "aerodrome"]
                self._cache[cache_key] = (aerodrome_pairs, now)
                return aerodrome_pairs
            elif resp.status_code == 429:
                time.sleep(15)
        except Exception as e:
            logger.error(f"Prism DEX search failed for {symbol}: {e}")
        return []

    def get_funding_rates_all(self, symbol: str) -> dict:
        """Get funding rates across all DEX perps."""
        data = self._get(f"/dex/{symbol}/funding/all", ttl=300)
        if not data:
            return {}
            
        return {
            "best_for_long": data.get("best_for_long"),
            "best_for_short": data.get("best_for_short"),
            "interpretation": data.get("interpretation"),
            "rates": data.get("funding_rates", {})
        }

    # ── Full signal snapshot ─────────────────────────────────────

    def get_signal_snapshot(self) -> dict:
        """
        Single call that aggregates all signal data needed by the AI brain.
        Returns a structured dict consumed by PrismSignalStore.
        """
        logger.info("Fetching Prism signal snapshot...")

        # Batch prices for all tracked symbols
        batch = {p["symbol"]: p for p in self.get_batch_prices()}

        # Market-wide data
        global_data   = self.get_global_market() or {}
        fear_greed    = self.get_fear_greed() or {}
        gainers       = self.get_gainers(8)
        losers        = self.get_losers(8)
        trending      = self.get_trending()

        # Per-symbol sentiment for main alts
        sentiment = self.get_sentiment_batch(["BTC", "ETH", "SOL", "XRP", "ADA"])

        # Funding rate for BTC (proxy for overall market leverage sentiment)
        btc_funding = self.get_funding_rate("BTC") or {}
        eth_funding = self.get_funding_rate("ETH") or {}

        # Venue prices for BTC and ETH
        btc_venues = self.get_venue_prices("BTC")
        eth_venues = self.get_venue_prices("ETH")
        venue_spreads = {
            "BTC": btc_venues,
            "ETH": eth_venues
        }

        # Build per-alt signal dicts (enriched price + sentiment + funding)
        alt_signals = {}
        for sym, kraken_sym in PRISM_TO_KRAKEN.items():
            price_data = batch.get(sym, {})
            sent_data  = sentiment.get(sym, {})
            alt_signals[kraken_sym] = {
                "prism_symbol": sym,
                "price_usd":    price_data.get("price_usd"),
                "confidence":   price_data.get("confidence"),
                "change_24h_pct": price_data.get("change_24h_pct"),
                "sentiment_score": sent_data.get("sentiment_score"),
                "sentiment_label": sent_data.get("sentiment_label"),
                "price_momentum": sent_data.get("components", {}).get("price_momentum"),
            }

        snap = {
            "timestamp": time.time(),
            "global": {
                "total_market_cap_usd": global_data.get("total_market_cap_usd"),
                "btc_dominance": global_data.get("btc_dominance"),
                "eth_dominance": global_data.get("eth_dominance"),
                "total_volume_24h_usd": global_data.get("total_volume_24h_usd"),
                "market_cap_change_24h_pct": global_data.get("market_cap_change_24h_pct"),
                "active_cryptocurrencies": global_data.get("active_cryptocurrencies"),
            },
            "fear_greed": {
                "value": fear_greed.get("value"),
                "label": fear_greed.get("label"),
            },
            "gainers": [
                {"symbol": c["symbol"], "change_24h": c.get("price_change_24h"), "price": c.get("price_usd")}
                for c in gainers[:5]
            ],
            "losers": [
                {"symbol": c["symbol"], "change_24h": c.get("price_change_24h"), "price": c.get("price_usd")}
                for c in losers[:5]
            ],
            "trending": [
                {"symbol": t["symbol"], "score": t.get("score"), "change_24h": t.get("price_change_24h")}
                for t in trending[:8]
            ],
            "funding": {
                "BTC": {
                    "rates": btc_funding.get("funding_rates", {}),
                    "interpretation": btc_funding.get("_interpretation", ""),
                },
                "ETH": {
                    "rates": eth_funding.get("funding_rates", {}),
                    "interpretation": eth_funding.get("_interpretation", ""),
                },
            },
            "venue_spreads": venue_spreads,
            "alt_signals": alt_signals,
        }

        logger.info(
            f"Prism snapshot: BTC=${batch.get('BTC', {}).get('price_usd', 0):,.0f} | "
            f"F&G={fear_greed.get('value', 'N/A')} ({fear_greed.get('label', '')}) | "
            f"BTC dom={global_data.get('btc_dominance', 0):.1f}%"
        )
        return snap
