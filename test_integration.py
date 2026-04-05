import asyncio
import os
import json
from dotenv import load_dotenv
import time

# Load env before imports
from dotenv import load_dotenv
load_dotenv()

from agent.ai_brain import ContextBuilder, AIBrain
from agent.arb_detector import ArbDetector, ArbOpportunity
from data.dex_price_client import DexPriceClient
from data.prism_client import PrismClient
from kraken_wrappers.rest_client import KrakenRESTClient
from config import config
import api.shared_state as shared_state

class MockSignalEngine:
    def is_dip_window(self): return True, "06:00-06:30 WAT"
    def minutes_to_next_window(self): return 0
    def get_canary_signal(self): return {
        'btc': {'change_1h_pct': -1.6, 'change_15m_pct': -0.5, 'price': 65000, 'threshold': -1.5, 
                'rsi': {'rsi': 28, 'signal': 'oversold'}, 'volume': {'volume_ratio': 1.8, 'spike': True}},
        'eth': {'change_1h_pct': -1.3, 'change_15m_pct': -0.4, 'price': 3300, 'threshold': -1.2,
                'rsi': {'rsi': 29, 'signal': 'oversold'}, 'volume': {'volume_ratio': 1.6, 'spike': True}},
        'dip_triggered': True, 'strong_signal': True
    }
    def classify_regime(self): return {'regime': 'BULL', 'btc_price': 65000, 'sma20_slope_5d_pct': 2.1, 'above_sma20': True, 'above_sma200': True, 'reason': 'Bullish trend'}
    def get_high_correlation_alts(self): return [
        {'symbol': 'PF_SOLUSD', 'correlation': 0.85, 'change_1h_pct': -2.1, 'rsi': 28, 'rsi_oversold': True, 'volume_ratio': 1.9, 'volume_spike': True, 'dipping': True, 'signal_quality': 3}
    ]

class MockCMCClient:
    def get_market_snapshot(self): return {
        'global': {'total_market_cap': 2500000000000, 'total_volume_24h': 100000000000, 'btc_dominance': 52.1, 'market_cap_change_24h': -1.2},
        'fear_and_greed': {'score': 35, 'label': 'Fear'},
        'top20': [{'rank': 5, 'symbol': 'SOL', 'price': 145.2, 'percent_change_1h': -2.1, 'percent_change_24h': -5.4}],
        'coins_dipping_1h': [{'symbol': 'SOL', 'percent_change_1h': -2.1}]
    }

class MockPositionManager:
    def get_account_summary(self): return {
        'capital': 10000.0, 'peak_capital': 10000.0, 'today_pnl': 0.0, 'today_loss_pct': 0.0,
        'daily_loss_limit_hit': False, 'daily_loss_limit_pct': 3, 'open_positions': 0, 'deployed': 0.0,
        'win_rate_pct': 0.0, 'wins': 0, 'losses': 0, 'total_trades': 0, 'trailing_stop_activations': 0
    }
    def get_open_positions(self): return []
    def kelly_position_size(self, confidence): return 0.02

async def main():
    print("--- Testing Issue 3 & 4 (Detector & Prism) ---")
    dex = DexPriceClient()
    rest = KrakenRESTClient()
    pos = MockPositionManager()
    
    # Try fetching real data but fallback gracefully if API fails
    print("Fetching real DEX and Kraken prices...")
    detector = ArbDetector(dex, rest, pos)
    opps = detector.scan()
    print(f"Detector found {len(opps)} real opportunities (needs > 0.4% net gap)")
    
    print("\nFetching real Prism data...")
    prism = PrismClient(api_key="prism_sk_US8dsdhgHcoWzO7APOz_Vjxd4DyCoTTdEeUd4sw5_Wo")
    try:
        venues = prism.get_venue_prices("BTC")
        print(f"BTC Venues from Prism: {list(venues.keys())[:3]}")
    except Exception as e:
        print(f"Prism API failed (expected if key invalid): {e}")

    print("\n--- Testing Issue 7 (AI Context with Arb Signals) ---")
    # Setup fake shared state with arb alerts
    fake_alert = {
        "symbol": "SOL",
        "kraken_pair": "SOLUSD",
        "kraken_price": 145.0,
        "dex_price": 142.0,
        "raw_gap_pct": 2.1,
        "net_gap_pct": 1.5,
        "direction": "buy_dex_sell_cex",
        "dex_liquidity_usd": 2500000,
        "confidence": 0.85,
        "detected_at": time.time(),
        "estimated_profit_usd": 3.0
    }
    shared_state._state["arb_alerts"] = [fake_alert]
    
    builder = ContextBuilder(MockSignalEngine(), MockCMCClient(), MockPositionManager())
    context = builder.build()
    
    print("\nExtracting Arb Section from Context:")
    arb_section = [line for line in context.split('\n') if 'Arb' in line or 'gap=' in line]
    for line in arb_section:
        print(line)
        
    print("\n--- Testing AI Brain with OpenRouter ---")
    brain = AIBrain(MockSignalEngine(), MockCMCClient(), MockPositionManager())
    
    # Force a specific free model
    brain.model = "google/gemma-2-9b-it:free"
    print(f"Using model: {brain.model}")
    print(f"API Key present: {bool(os.getenv('OPENROUTER_API_KEY'))}")
    
    print("Asking AI for trade decision...")
    decision = brain.analyze_and_decide()
    
    print(f"\nAI Decision: {decision.decision}")
    print(f"Confidence: {decision.confidence}")
    print(f"Reasoning: {decision.reasoning}")
    print(f"Trades: {json.dumps(decision.trades, indent=2)}")

if __name__ == "__main__":
    asyncio.run(main())
