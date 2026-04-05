import time
from dataclasses import dataclass
from config import config
from utils.logger import get_logger
from agent.arb_detector import ArbOpportunity
from api import shared_state

logger = get_logger("arb_executor")

@dataclass
class ArbResult:
    opportunity: ArbOpportunity
    executed: bool
    paper_mode: bool
    alert_logged: bool
    cex_order_id: str | None
    dex_tx_hash: str | None
    actual_profit_usd: float | None
    error: str | None
    timestamp: float

class ArbExecutor:
    """Executes arbitrage opportunities (or logs them in paper mode)."""
    
    def __init__(self, kraken_cli, position_manager):
        self.kraken_cli = kraken_cli
        self.position_manager = position_manager

    def execute(self, opportunity: ArbOpportunity) -> ArbResult:
        if config.paper_mode:
            return self._paper_alert(opportunity)
        else:
            return self._live_execute(opportunity)

    def _paper_alert(self, opportunity: ArbOpportunity) -> ArbResult:
        # 1. Log to console
        logger.info(
            f"[ARB ALERT] {opportunity.symbol} | Kraken=${opportunity.kraken_price:.2f} | Aerodrome=${opportunity.dex_price:.2f}\n"
            f"Gap={opportunity.net_gap_pct:+.2f}% net | Est. profit=${opportunity.estimated_profit_usd:.2f} | Dir={opportunity.direction}\n"
            f"⚠️  PAPER MODE — opportunity logged, no orders placed"
        )
        
        # Convert to dict for state
        opp_dict = {
            "symbol": opportunity.symbol,
            "kraken_pair": opportunity.kraken_pair,
            "kraken_price": opportunity.kraken_price,
            "dex_price": opportunity.dex_price,
            "raw_gap_pct": opportunity.raw_gap_pct,
            "net_gap_pct": opportunity.net_gap_pct,
            "direction": opportunity.direction,
            "dex_liquidity_usd": opportunity.dex_liquidity_usd,
            "confidence": opportunity.confidence,
            "detected_at": opportunity.detected_at,
            "estimated_profit_usd": opportunity.estimated_profit_usd
        }
        
        # 2. Record to position manager (issue 10 related, but we can call it now if it exists, otherwise we'll add it)
        if hasattr(self.position_manager, "record_arb_alert"):
            self.position_manager.record_arb_alert(opp_dict)
            
        # 3. Update shared state
        alerts = shared_state.get_section("arb_alerts") or []
        if isinstance(alerts, dict):  # handle case where get_section returns dict fallback
            alerts = []
        alerts.append(opp_dict)
        alerts = alerts[-50:]  # Keep last 50
        
        # Update shared_state using the set_key pattern which we modified to handle entire objects
        # We need a new method or use a direct way, let's use a workaround since update expects a dict
        shared_state.set_key("arb_alerts", "", alerts) # We need to modify set_key or use a direct approach. Let's use set_key correctly based on our change
        
        # Proper way to update state based on our modified shared_state
        shared_state._state["arb_alerts"] = alerts
        
        stats = shared_state.get_section("arb_stats")
        if not isinstance(stats, dict):
            stats = {
                "total_scans": 0,
                "total_alerts": 0,
                "best_gap_pct": 0.0,
                "best_gap_symbol": "",
                "estimated_pnl_missed": 0.0,
            }
            
        stats["total_alerts"] += 1
        stats["estimated_pnl_missed"] += opportunity.estimated_profit_usd
        if opportunity.net_gap_pct > stats["best_gap_pct"]:
            stats["best_gap_pct"] = opportunity.net_gap_pct
            stats["best_gap_symbol"] = opportunity.symbol
            
        shared_state.update("arb_stats", stats)

        return ArbResult(
            opportunity=opportunity,
            executed=False,
            paper_mode=True,
            alert_logged=True,
            cex_order_id=None,
            dex_tx_hash=None,
            actual_profit_usd=None,
            error=None,
            timestamp=time.time()
        )

    def _live_execute(self, opportunity: ArbOpportunity) -> ArbResult:
        # Step 1: Place Kraken spot order
        # Step 2: Place Aerodrome swap (web3 tx)
        # Step 3: Confirm both legs filled
        # Step 4: Record actual PnL
        raise NotImplementedError("Live arb execution — set PAPER_MODE=false and implement web3 leg")
