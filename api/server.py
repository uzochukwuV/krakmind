"""
ArbMind Dashboard API Server
Provides real-time trading data to the dashboard frontend.
Runs in a background thread alongside the main trading loop.
"""

import time
import threading
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api import shared_state
from utils.logger import get_logger

logger = get_logger("api")

app = FastAPI(
    title="ArbMind API",
    description="Real-time paper trading data for ArbMind dashboard",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok", "timestamp": time.time()}


# ── Full snapshot ───────────────────────────────────────────────────────────────

@app.get("/api/snapshot")
def snapshot():
    """Everything in one call — useful for dashboard initial load."""
    data = shared_state.get_snapshot()
    data["_served_at"] = time.time()
    return JSONResponse(content=data)


# ── Agent status ───────────────────────────────────────────────────────────────

@app.get("/api/status")
def status():
    agent  = shared_state.get_section("agent")
    cap    = shared_state.get_section("capital")
    stats  = shared_state.get_section("stats")
    sigs   = shared_state.get_section("signals")
    arb_st = shared_state.get_section("arb_stats")
    return {
        "agent":   agent,
        "capital": cap,
        "stats":   stats,
        "arb_stats": arb_st,
        "signals": {
            "in_window":              sigs.get("in_window"),
            "window_label":           sigs.get("window_label"),
            "minutes_to_next_window": sigs.get("minutes_to_next_window"),
            "regime":                 sigs.get("regime", {}).get("regime"),
            "canary_dip_triggered":   sigs.get("canary", {}).get("dip_triggered"),
        },
    }


# ── Positions ──────────────────────────────────────────────────────────────────

@app.get("/api/positions")
def positions():
    pos = shared_state.get_section("positions")
    return {"positions": pos, "count": len(pos)}


# ── Trade history ──────────────────────────────────────────────────────────────

@app.get("/api/trades")
def trades(limit: int = 50):
    from data.journal import TradeJournal
    journal = TradeJournal()
    history = journal.get_history(limit)
    analytics = journal.get_analytics()
    
    return {
        "trades": history,
        "analytics": analytics,
        "total":  len(history),
    }


# ── Signals ────────────────────────────────────────────────────────────────────

@app.get("/api/signals")
def signals():
    sigs = shared_state.get_section("signals")
    return sigs


# ── Market data ────────────────────────────────────────────────────────────────

@app.get("/api/market")
def market():
    mkt = shared_state.get_section("market")
    return mkt


# ── Prism intelligence ─────────────────────────────────────────────────────────

@app.get("/api/prism")
def prism():
    return shared_state.get_section("prism")


# ── Per-coin detail ────────────────────────────────────────────────────────────

@app.get("/api/coin/{symbol}")
def coin_detail(symbol: str):
    """
    Detailed data for one instrument, e.g. GET /api/coin/PF_SOLUSD
    """
    symbol = symbol.upper()
    mkt    = shared_state.get_section("market")
    pos    = shared_state.get_section("positions")
    hist   = shared_state.get_section("trade_history")
    prism  = shared_state.get_section("prism")

    price = mkt.get("prices", {}).get(symbol)
    if price is None:
        raise HTTPException(status_code=404, detail=f"{symbol} not found in market data")

    coin_positions = [p for p in pos if p.get("symbol") == symbol]
    coin_trades    = [t for t in hist if t.get("symbol") == symbol]
    coin_signals   = [s for s in prism.get("signals", []) if s.get("symbol") == symbol]

    return {
        "symbol":     symbol,
        "price":      price,
        "positions":  coin_positions,
        "trades":     coin_trades[-10:],
        "prism":      coin_signals[0] if coin_signals else None,
    }


# ── Last AI decision ───────────────────────────────────────────────────────────

@app.get("/api/last_decision")
def last_decision():
    return shared_state.get_section("last_ai_decision") or {"decision": None}


# ── Arbitrage ──────────────────────────────────────────────────────────────────

@app.get("/api/arbitrage")
def arbitrage():
    alerts = shared_state.get_section("arb_alerts")
    stats = shared_state.get_section("arb_stats")
    return {
        "alerts": alerts,
        "stats": stats
    }


# ── Server startup helper ──────────────────────────────────────────────────────

def start_server(host: str = "0.0.0.0", port: int = 8000):
    """Run uvicorn in a daemon thread so it doesn't block the trading loop."""
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", loop="asyncio")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True, name="api-server")
    thread.start()
    logger.info(f"Dashboard API started on http://{host}:{port}")
    return thread
