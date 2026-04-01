"""
Trade Journal
Persists every AI decision, trade entry/exit, and reasoning to disk.
Useful for backtesting analysis and strategy refinement.
Run standalone to view performance summary.
"""

import json
import csv
import os
import datetime
from utils.logger import get_logger

logger = get_logger("journal")

JOURNAL_DIR = "data/journal"
DECISIONS_FILE = os.path.join(JOURNAL_DIR, "ai_decisions.jsonl")
TRADES_FILE = os.path.join(JOURNAL_DIR, "trades.csv")


def init_journal():
    os.makedirs(JOURNAL_DIR, exist_ok=True)
    if not os.path.exists(TRADES_FILE):
        with open(TRADES_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp", "symbol", "direction", "entry_price", "exit_price",
                "quantity", "position_value", "pnl", "pnl_pct", "duration_minutes",
                "close_reason", "thesis"
            ])


def log_decision(decision, cycle: int):
    """Append AI decision to JSONL log."""
    os.makedirs(JOURNAL_DIR, exist_ok=True)
    record = {
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "cycle": cycle,
        "decision": decision.decision,
        "confidence": decision.confidence,
        "reasoning": decision.reasoning,
        "market_context": decision.market_context,
        "trades_proposed": len(decision.trades),
        "next_check_minutes": decision.next_check_minutes,
    }
    with open(DECISIONS_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")


def log_closed_trade(closed: dict):
    """Append closed trade to CSV."""
    init_journal()
    with open(TRADES_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            closed.get("opened_at", ""),
            closed.get("symbol", ""),
            closed.get("direction", ""),
            closed.get("entry_price", ""),
            closed.get("exit_price", ""),
            closed.get("quantity", ""),
            closed.get("position_value", ""),
            closed.get("pnl", ""),
            closed.get("pnl_pct", ""),
            closed.get("duration_minutes", ""),
            closed.get("close_reason", ""),
            closed.get("thesis", ""),
        ])


def print_performance_summary():
    """Print a summary of all closed trades."""
    if not os.path.exists(TRADES_FILE):
        print("No trades recorded yet.")
        return

    import pandas as pd
    df = pd.read_csv(TRADES_FILE)
    if df.empty:
        print("No trades recorded yet.")
        return

    total = len(df)
    wins = (df["pnl"] > 0).sum()
    losses = (df["pnl"] <= 0).sum()
    total_pnl = df["pnl"].sum()
    avg_win = df[df["pnl"] > 0]["pnl"].mean() if wins > 0 else 0
    avg_loss = df[df["pnl"] <= 0]["pnl"].mean() if losses > 0 else 0
    win_rate = wins / total * 100 if total > 0 else 0
    avg_duration = df["duration_minutes"].mean() if "duration_minutes" in df else 0

    print("\n" + "="*50)
    print("  ArbMind Paper Trading Summary")
    print("="*50)
    print(f"  Total Trades:    {total}")
    print(f"  Wins/Losses:     {wins}/{losses}")
    print(f"  Win Rate:        {win_rate:.1f}%")
    print(f"  Total P&L:       ${total_pnl:.2f}")
    print(f"  Avg Win:         ${avg_win:.2f}")
    print(f"  Avg Loss:        ${avg_loss:.2f}")
    print(f"  Avg Duration:    {avg_duration:.0f} min")
    if avg_loss != 0:
        print(f"  Profit Factor:   {abs(avg_win / avg_loss):.2f}")
    print("="*50)

    if "close_reason" in df.columns:
        print("\nClose reason breakdown:")
        print(df["close_reason"].value_counts().to_string())

    print("\nLast 10 trades:")
    cols = ["symbol", "direction", "pnl", "pnl_pct", "duration_minutes", "close_reason"]
    print(df[cols].tail(10).to_string(index=False))


if __name__ == "__main__":
    print_performance_summary()
