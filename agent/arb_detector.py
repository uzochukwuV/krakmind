import time
from dataclasses import dataclass
from config import config
from utils.logger import get_logger

logger = get_logger("arb_detector")

KRAKEN_TAKER_FEE = 0.0026   # 0.26%
AERODROME_POOL_FEE = 0.003    # 0.3% volatile pool
SLIPPAGE_ESTIMATE = 0.002    # 0.2% conservative
TOTAL_ROUND_TRIP_COST = (KRAKEN_TAKER_FEE + AERODROME_POOL_FEE + SLIPPAGE_ESTIMATE) * 2
MIN_NET_GAP_PCT = 0.004    # 0.4% min net profit after all costs


@dataclass
class ArbOpportunity:
    symbol: str
    kraken_pair: str
    kraken_price: float
    dex_price: float
    raw_gap_pct: float
    net_gap_pct: float
    direction: str
    dex_liquidity_usd: float
    confidence: float
    detected_at: float
    estimated_profit_usd: float


class ArbDetector:
    """Core arbitrage engine detecting CEX-DEX price gaps."""

    SYMBOL_MAP = {
        "ETH": {"kraken": "XETHZUSD", "dex_addr": "0x4200000000000000000000000000000000000006"},
        "BTC": {"kraken": "XXBTZUSD", "dex_addr": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf"},
    }

    def __init__(self, dex_client, kraken_rest, position_manager):
        self.dex_client = dex_client
        self.kraken_rest = kraken_rest
        self.position_manager = position_manager

    def scan(self) -> list[ArbOpportunity]:
        opportunities = []
        
        for symbol, mapping in self.SYMBOL_MAP.items():
            kraken_pair = mapping["kraken"]
            
            dex_data = self.dex_client.get_price(symbol)
            kraken_price = self.kraken_rest.get_spot_price(kraken_pair)
            
            if not dex_data or not kraken_price:
                continue
                
            dex_price = float(dex_data.get("priceUsd", 0))
            if dex_price <= 0:
                continue
                
            dex_liquidity_usd = float(dex_data.get("liquidity", {}).get("usd", 0))
            
            raw_gap_pct, direction = self._compute_gap(kraken_price, dex_price)
            net_gap_pct = raw_gap_pct - TOTAL_ROUND_TRIP_COST * 100
            
            # Position sizing
            confidence = self.position_manager.kelly_position_size(0.8) # Default confidence for sizing
            position_size_pct = min(confidence, 0.02) # Max 2% for arb
            # We assume a $10000 portfolio if not available directly for calculation
            position_size_usd = config.paper_capital * position_size_pct
            
            if net_gap_pct >= MIN_NET_GAP_PCT * 100 and dex_liquidity_usd >= position_size_usd * 10:
                calc_confidence = self._confidence(net_gap_pct, dex_liquidity_usd, position_size_usd)
                est_profit = self._estimated_profit(net_gap_pct, position_size_usd)
                
                opp = ArbOpportunity(
                    symbol=symbol,
                    kraken_pair=kraken_pair,
                    kraken_price=kraken_price,
                    dex_price=dex_price,
                    raw_gap_pct=raw_gap_pct,
                    net_gap_pct=net_gap_pct,
                    direction=direction,
                    dex_liquidity_usd=dex_liquidity_usd,
                    confidence=calc_confidence,
                    detected_at=time.time(),
                    estimated_profit_usd=est_profit
                )
                opportunities.append(opp)
                
        return opportunities

    def _compute_gap(self, kraken_price: float, dex_price: float) -> tuple[float, str]:
        if kraken_price > dex_price:
            raw_gap_pct = (kraken_price - dex_price) / dex_price * 100
            direction = "buy_dex_sell_cex"
        else:
            raw_gap_pct = (dex_price - kraken_price) / kraken_price * 100
            direction = "buy_cex_sell_dex"
        return raw_gap_pct, direction

    def _confidence(self, net_gap_pct: float, liquidity_usd: float, position_size_usd: float) -> float:
        gap_factor = (net_gap_pct / (MIN_NET_GAP_PCT * 100)) * 0.5
        liquidity_factor = min((liquidity_usd / 1_000_000), 1.0) * 0.5
        return min(1.0, gap_factor + liquidity_factor)

    def _estimated_profit(self, net_gap_pct: float, position_size_usd: float) -> float:
        return (net_gap_pct / 100) * position_size_usd
