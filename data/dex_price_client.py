import time
import requests
from utils.logger import get_logger
from .prism_client import PrismClient

logger = get_logger("DexPriceClient")

class DexPriceClient:
    """Client for fetching DEX prices from DexScreener."""

    SYMBOL_MAP = {
        "WETH": "0x4200000000000000000000000000000000000006",
        "ETH": "0x4200000000000000000000000000000000000006",
        "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "CBBTC": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
        "BTC": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
        "AERO": "0x940181a94A35A4569E4529A3CDfB74e38FD98631",
    }

    def __init__(self):
        self._cache = {}
        self._cache_ttl = 15  # seconds
        self._prism = PrismClient() # For dynamic resolution

    def get_price(self, symbol: str) -> dict | None:
        """
        Fetches the best Aerodrome pair for a given symbol from DexScreener.
        Returns a dict with priceUsd, liquidity.usd, volume.h24, priceChange.h1.
        """
        symbol = symbol.upper()
        address = self.SYMBOL_MAP.get(symbol)
        
        # Check cache early if possible
        now = time.time()
        if symbol in self._cache:
            cached_data, timestamp = self._cache[symbol]
            if now - timestamp < self._cache_ttl:
                return cached_data

        if not address:
            logger.info(f"No static Base token address for {symbol}, trying dynamic resolution via Prism...")
            try:
                pairs = self._prism.get_dex_search(symbol, "base")
                if pairs:
                    # Filter for aerodrome pairs specifically
                    aero_pairs = [p for p in pairs if p.get("dexId") == "aerodrome"]
                    if aero_pairs:
                        best = self._best_pair(aero_pairs)
                        address = best.get("baseToken", {}).get("address")
                        if address:
                            self.SYMBOL_MAP[symbol] = address
                            logger.info(f"Dynamically mapped {symbol} to {address} on Base")
            except Exception as e:
                logger.error(f"Dynamic resolution failed for {symbol}: {e}")
                
        if not address:
            logger.warning(f"Could not resolve Base token address for {symbol}")
            return None

        # Add exponential backoff logic for DexScreener requests
        max_retries = 3
        for attempt in range(max_retries):
            try:
                url = f"https://api.dexscreener.com/latest/dex/tokens/{address}"
                response = requests.get(url, timeout=10)
                
                if response.status_code == 429:
                    backoff = 2 ** attempt
                    logger.warning(f"DexScreener rate limited. Backing off for {backoff}s...")
                    time.sleep(backoff)
                    continue
                    
                response.raise_for_status()
                data = response.json()
                
                pairs = data.get("pairs", [])
                # Filter for base chain and aerodrome dex
                valid_pairs = [
                    p for p in pairs 
                    if p.get("chainId") == "base" and p.get("dexId") == "aerodrome"
                ]
                
                if not valid_pairs:
                    logger.warning(f"No valid Aerodrome pairs found for {symbol} on Base")
                    return None
                    
                best_pair = self._best_pair(valid_pairs)
                
                result = {
                    "chainId": best_pair.get("chainId"),
                    "dexId": best_pair.get("dexId"),
                    "priceUsd": best_pair.get("priceUsd"),
                    "liquidity": best_pair.get("liquidity", {}),
                    "volume": best_pair.get("volume", {}),
                    "priceChange": best_pair.get("priceChange", {})
                }
                
                self._cache[symbol] = (result, now)
                return result
                
            except requests.RequestException as e:
                if attempt == max_retries - 1:
                    logger.error(f"Error fetching DEX price for {symbol} after {max_retries} attempts: {e}")
                else:
                    time.sleep(2 ** attempt)
                    
        return None

    def get_batch_prices(self, symbols: list) -> dict[str, dict]:
        """Fetches prices for multiple symbols."""
        results = {}
        for sym in symbols:
            price_data = self.get_price(sym)
            if price_data:
                results[sym] = price_data
        return results

    def _best_pair(self, pairs: list) -> dict:
        """Picks the pair with the highest USD liquidity."""
        return max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
