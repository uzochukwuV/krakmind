import asyncio
import time
from agent.arb_detector import ArbOpportunity
from agent.arb_executor import ArbExecutor
from config import config

class MockPositionManager:
    def record_arb_alert(self, opp):
        print(f"Mock recorded alert: {opp['symbol']}")

async def main():
    pos = MockPositionManager()
    config.paper_mode = True
    
    executor = ArbExecutor(None, pos)
    
    # Create fake opportunity
    opp = ArbOpportunity(
        symbol="ETH",
        kraken_pair="XETHZUSD",
        kraken_price=3000.0,
        dex_price=2900.0,
        raw_gap_pct=3.44,
        net_gap_pct=3.0,
        direction="buy_dex_sell_cex",
        dex_liquidity_usd=1000000.0,
        confidence=0.8,
        detected_at=time.time(),
        estimated_profit_usd=6.0
    )
    
    print(f"Executing fake opportunity: {opp.symbol} at gap {opp.net_gap_pct}%")
    result = executor.execute(opp)
    print(f"Executed: {result.executed}, Paper Mode: {result.paper_mode}")

    print("Checking shared state...")
    import api.shared_state as shared_state
    print(f"Alerts in state: {len(shared_state.get_section('arb_alerts'))}")
    print(f"Stats in state: {shared_state.get_section('arb_stats')}")

if __name__ == "__main__":
    asyncio.run(main())
