import asyncio
import time
from utils.logger import get_logger

logger = get_logger("arb_loop")

ARB_SCAN_INTERVAL = 10  # seconds

class ArbLoop:
    def __init__(self, arb_detector, arb_executor, position_manager):
        self.detector = arb_detector
        self.executor = arb_executor
        self.positions = position_manager
        self._cycle = 0
        self._alerts_today = 0
        self._max_alerts_per_hour = 20  # rate limit paper alerts
        self._alerts_this_hour = 0
        self._hour_started_at = time.time()

    async def run(self):
        logger.info(f"Starting ArbLoop (scan every {ARB_SCAN_INTERVAL}s)")
        
        while True:
            self._cycle += 1
            now = time.time()
            
            # Reset hourly counter
            if now - self._hour_started_at >= 3600:
                self._alerts_this_hour = 0
                self._hour_started_at = now

            try:
                loop = asyncio.get_running_loop()
                # Run scan in executor to avoid blocking the event loop
                opportunities = await loop.run_in_executor(None, self.detector.scan)
                
                # Update total scans in shared state
                from api import shared_state
                stats = shared_state.get_section("arb_stats")
                if isinstance(stats, dict):
                    stats["total_scans"] += 1
                    shared_state.update("arb_stats", stats)

                if opportunities:
                    # Execute or alert
                    for opp in opportunities:
                        if self._alerts_this_hour <= self._max_alerts_per_hour:
                            self.executor.execute(opp)
                            self._alerts_this_hour += 1
                            self._alerts_today += 1
                        else:
                            # Just record it silently
                            self.executor._paper_alert(opp)
                            
                    best_gap = max(opportunities, key=lambda o: o.net_gap_pct)
                    
                    if self._cycle % 6 == 0:  # Every ~60s
                        logger.info(
                            f"[ARB SCAN #{self._cycle}] Scanned pairs | {len(opportunities)} opportunities found | "
                            f"Best gap: {best_gap.symbol} +{best_gap.net_gap_pct:.2f}% net"
                        )
                else:
                    if self._cycle % 6 == 0:  # Every ~60s
                        logger.info(f"[ARB SCAN #{self._cycle}] No opportunities found")
                        
            except Exception as e:
                logger.error(f"Error in ArbLoop cycle {self._cycle}: {e}")
                
            await asyncio.sleep(ARB_SCAN_INTERVAL)
