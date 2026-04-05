import asyncio
import time
from data.dex_price_client import DexPriceClient
from kraken_wrappers.rest_client import KrakenRESTClient
from agent.arb_detector import ArbDetector
from agent.arb_executor import ArbExecutor
from agent.arb_loop import ArbLoop
from config import config

class MockPositionManager:
    def kelly_position_size(self, confidence):
        return 0.02
    def record_arb_alert(self, opp):
        print(f"Mock recorded alert: {opp['symbol']}")

async def main():
    dex = DexPriceClient()
    rest = KrakenRESTClient()
    pos = MockPositionManager()
    
    # Enable paper mode explicitly
    config.paper_mode = True
    config.paper_capital = 10000
    
    detector = ArbDetector(dex, rest, pos)
    executor = ArbExecutor(None, pos)
    
    print("Running detector scan...")
    opps = detector.scan()
    print(f"Found {len(opps)} opportunities")
    
    for opp in opps:
        print(f"Executing: {opp.symbol} at gap {opp.net_gap_pct}%")
        result = executor.execute(opp)
        print(f"Executed: {result.executed}, Paper Mode: {result.paper_mode}")

    print("Checking shared state...")
    import api.shared_state as shared_state
    print(f"Alerts in state: {len(shared_state.get_section('arb_alerts'))}")
    print(f"Stats in state: {shared_state.get_section('arb_stats')}")

if __name__ == "__main__":
    asyncio.run(main())
