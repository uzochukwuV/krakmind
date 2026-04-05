"""
Shared in-memory state between the trading loop and the API server.
The trading loop writes to this on every cycle; the API reads from it.
Thread-safe via a simple dict + lock.
"""

import threading
import time
from typing import Any, Dict

_lock = threading.Lock()

_state: Dict[str, Any] = {
    "agent": {
        "started_at": time.time(),
        "mode": "PAPER",
        "status": "starting",
        "cycle": 0,
        "last_update": None,
    },
    "capital": {
        "total": 10000.0,
        "deployed": 0.0,
        "available": 10000.0,
        "today_pnl": 0.0,
        "today_pnl_pct": 0.0,
        "all_time_pnl": 0.0,
    },
    "positions": [],
    "trade_history": [],
    "stats": {
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "avg_win_pct": 0.0,
        "avg_loss_pct": 0.0,
    },
    "signals": {
        "canary": {},
        "regime": {},
        "in_window": False,
        "window_label": None,
        "minutes_to_next_window": None,
    },
    "prism": {
        "fear_greed": None,
        "fear_greed_label": None,
        "btc_dominance": None,
        "market_change_24h": None,
        "signals": [],
        "last_updated": None,
        "is_fresh": False,
    },
    "market": {
        "btc_price": None,
        "eth_price": None,
        "prices": {},
    },
    "last_ai_decision": None,
    "arb_alerts": [],   # list of ArbOpportunity dicts, last 50
    "arb_stats": {
        "total_scans": 0,
        "total_alerts": 0,
        "best_gap_pct": 0.0,
        "best_gap_symbol": "",
        "estimated_pnl_missed": 0.0,   # paper mode: what we would have earned
    }
}


def update(section: str, data: Dict[str, Any]) -> None:
    with _lock:
        if isinstance(_state.get(section), dict):
            _state[section].update(data)
        else:
            _state[section] = data


def set_key(section: str, key: str, value: Any) -> None:
    with _lock:
        if isinstance(_state.get(section), dict):
            _state[section][key] = value
        else:
            _state[section] = value


def get_snapshot() -> Dict[str, Any]:
    with _lock:
        import copy
        return copy.deepcopy(_state)


def get_section(section: str) -> Dict[str, Any]:
    with _lock:
        import copy
        return copy.deepcopy(_state.get(section, {}))
