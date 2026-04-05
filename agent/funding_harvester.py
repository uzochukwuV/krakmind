import asyncio
import time
from utils.logger import get_logger
from config import config
from api import shared_state

logger = get_logger("funding_harvester")

class FundingRateHarvester:
    """
    Monitors DEX perps via Prism for high funding rate yield opportunities.
    """
    def __init__(self, prism_client, position_manager):
        self.prism = prism_client
        self.positions = position_manager
        self.symbols = ["BTC", "ETH", "SOL", "WIF", "AERO"]
        self.check_interval = 3600  # 1 hour
        self.min_apr_threshold = 0.50 # 50% APR

    async def run(self):
        logger.info("Starting FundingRateHarvester loop (1h interval)")
        while True:
            try:
                await self._harvest_cycle()
            except Exception as e:
                logger.error(f"Error in FundingRateHarvester: {e}")
            await asyncio.sleep(self.check_interval)

    async def _harvest_cycle(self):
        alerts = []
        for symbol in self.symbols:
            try:
                loop = asyncio.get_running_loop()
                data = await loop.run_in_executor(None, self.prism.get_funding_rates_all, symbol)
                
                if not data:
                    continue
                    
                best_long = data.get("best_for_long", {})
                best_short = data.get("best_for_short", {})
                
                # Check for extreme negative funding (pays longs)
                if best_long and float(best_long.get("apr", 0)) > self.min_apr_threshold * 100:
                    alerts.append({
                        "symbol": symbol,
                        "direction": "long",
                        "venue": best_long.get("venue"),
                        "apr": float(best_long.get("apr")),
                        "timestamp": time.time()
                    })
                    self._execute_delta_neutral(symbol, "long", best_long.get("venue"), float(best_long.get("apr")))
                    
                # Check for extreme positive funding (pays shorts)
                if best_short and float(best_short.get("apr", 0)) > self.min_apr_threshold * 100:
                    alerts.append({
                        "symbol": symbol,
                        "direction": "short",
                        "venue": best_short.get("venue"),
                        "apr": float(best_short.get("apr")),
                        "timestamp": time.time()
                    })
                    self._execute_delta_neutral(symbol, "short", best_short.get("venue"), float(best_short.get("apr")))
            except Exception as e:
                logger.error(f"Error harvesting funding for {symbol}: {e}")
                
        if alerts:
            logger.info(f"Detected {len(alerts)} high-yield funding opportunities")
            current_alerts = shared_state.get_section("funding_alerts")
            if not isinstance(current_alerts, list):
                current_alerts = []
            current_alerts.extend(alerts)
            shared_state._state["funding_alerts"] = current_alerts[-20:]
            
    def _execute_delta_neutral(self, symbol: str, direction: str, venue: str, apr: float):
        """
        Executes a Delta-Neutral Cash-and-Carry trade to lock in risk-free funding yield.
        - If 'long' pays high APR: Short Spot (or hold USDC) + Long Perp on Venue
        - If 'short' pays high APR: Long Spot (buy on Kraken) + Short Perp on Venue
        """
        logger.warning(f"💰 DELTA-NEUTRAL HARVEST TRIGGERED: {symbol} | Dir: {direction} | Venue: {venue} | APR: {apr:.2f}%")
        
        # Position Sizing for Risk-Free Yield (up to 20% of capital)
        capital = self.positions._state["capital"]
        size_pct = 0.20 
        
        if config.paper_mode:
            logger.info(f"[PAPER] Would execute Delta-Neutral on {symbol}: Size={size_pct*100}% | Perp={direction}")
            return
            
        # In live mode, execute the dual legs
        # 1. Spot Leg via Kraken CLI
        # 2. Perp Leg via Web3 (e.g., GMX/Hyperliquid depending on Venue)
        logger.info(f"Delta-Neutral live execution logic goes here for {venue}.")
