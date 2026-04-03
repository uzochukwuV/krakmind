"""
Central config — loads from .env and exposes typed settings
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # Keys
    openrouter_api_key: str = field(default_factory=lambda: os.getenv("OPENROUTER_API_KEY", ""))
    openrouter_model: str = field(default_factory=lambda: os.getenv("OPENROUTER_MODEL", "qwen/qwen3.6-plus:free"))
    kraken_api_key: str = field(default_factory=lambda: os.getenv("KRAKEN_API_KEY", ""))
    kraken_api_secret: str = field(default_factory=lambda: os.getenv("KRAKEN_API_SECRET", ""))
    kraken_futures_key: str = field(default_factory=lambda: os.getenv("KRAKEN_FUTURES_API_KEY", ""))
    kraken_futures_secret: str = field(default_factory=lambda: os.getenv("KRAKEN_FUTURES_API_SECRET", ""))
    cmc_api_key: str = field(default_factory=lambda: os.getenv("CMC_API_KEY", ""))
    prism_api_key: str = field(default_factory=lambda: os.getenv("PRISM_API_KEY", ""))

    # Mode
    paper_mode: bool = field(default_factory=lambda: os.getenv("PAPER_MODE", "true").lower() == "true")

    # Timing
    loop_interval: int = field(default_factory=lambda: int(os.getenv("LOOP_INTERVAL_SECONDS", "60")))
    position_check_interval: int = field(default_factory=lambda: int(os.getenv("POSITION_CHECK_INTERVAL", "30")))

    # Risk params
    max_position_pct: float = field(default_factory=lambda: float(os.getenv("MAX_POSITION_PCT", "0.05")))
    stop_loss_pct: float = field(default_factory=lambda: float(os.getenv("STOP_LOSS_PCT", "0.02")))
    take_profit_pct: float = field(default_factory=lambda: float(os.getenv("TAKE_PROFIT_PCT", "0.04")))
    paper_capital: float = field(default_factory=lambda: float(os.getenv("PAPER_CAPITAL", "10000.0")))

    # Signal thresholds
    btc_dip_threshold: float = field(default_factory=lambda: float(os.getenv("BTC_DIP_THRESHOLD", "-1.5")))
    eth_dip_threshold: float = field(default_factory=lambda: float(os.getenv("ETH_DIP_THRESHOLD", "-1.2")))
    min_correlation: float = field(default_factory=lambda: float(os.getenv("MIN_CORRELATION", "0.6")))

    # Instruments — futures symbols on Kraken
    # Canary markers (never traded directly, only as signals)
    canary_symbols: list = field(default_factory=lambda: ["PF_XBTUSD", "PF_ETHUSD"])

    # Spot pairs for OHLC canary data
    canary_spot: dict = field(default_factory=lambda: {
        "PF_XBTUSD": "XXBTZUSD",
        "PF_ETHUSD": "XETHZUSD",
    })

    # Tradeable alts on Kraken Futures (excludes BTC/ETH canaries)
    tradeable_alts: list = field(default_factory=lambda: [
        # Existing core alts
        "PF_SOLUSD",    # Solana
        "PF_BNBUSD",    # BNB
        "PF_XRPUSD",    # XRP
        "PF_ADAUSD",    # Cardano
        "PF_AVAXUSD",   # Avalanche
        "PF_DOTUSD",    # Polkadot
        "PF_MATICUSD",  # Polygon
        "PF_LINKUSD",   # Chainlink
        "PF_LTCUSD",    # Litecoin
        "PF_UNIUSD",    # Uniswap
        # User-requested expansions (all confirmed on Kraken Futures)
        "PF_DOGEUSD",   # Dogecoin
        "PF_XLMUSD",    # Stellar
        "PF_TONUSD",    # Toncoin
        "PF_FLOWUSD",   # Flow
        "PF_ASTERUSD",  # Aster Network
        "PF_KAVAUSD",   # Kava
        "PF_ARCUSD",    # Arc
        "PF_GMXUSD",    # GMX
        # Bonus high-liquidity alts
        "PF_ATOMUSD",   # Cosmos
        "PF_NEARUSD",   # NEAR Protocol
    ])

    # CMC slugs for market data (aligned with tradeable_alts + canaries)
    cmc_slugs: list = field(default_factory=lambda: [
        "bitcoin", "ethereum", "solana", "bnb", "xrp",
        "cardano", "avalanche-2", "polkadot", "matic-network",
        "chainlink", "litecoin", "uniswap", "cosmos",
        "near", "stellar", "dogecoin", "toncoin",
        "flow", "kava", "gmx",
    ])

    # DIP windows in UTC+1 (hour_start, min_start, hour_end, min_end)
    dip_windows_utc1: list = field(default_factory=lambda: [
        (5, 30, 6, 30),   # 6AM UTC+1 window
        (15, 0, 16, 30),  # 4PM UTC+1 window
    ])


# Singleton
config = Config()
