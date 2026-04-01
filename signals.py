"""
Signal engine
Computes: regime classification, time window gate, BTC/ETH canary signals,
rolling correlation matrix between BTC and alts.
All outputs feed the AI brain — the AI makes the final call.
"""

import datetime
from typing import Optional
import pandas as pd
import numpy as np
import pytz
from utils.logger import get_logger
from config import config
from kraken.rest_client import KrakenRESTClient

logger = get_logger("signals")

WAT = pytz.timezone("Africa/Lagos")  # UTC+1, same as your dip windows


class SignalEngine:
    def __init__(self):
        self.rest = KrakenRESTClient()
        # Rolling candle store: pair → list of candle dicts
        self._candle_cache: dict[str, list] = {}
        self._cache_ts: dict[str, float] = {}
        self._cache_ttl = 60  # seconds

    # ── Time window gate ────────────────────────────────────────

    def is_dip_window(self) -> tuple[bool, str]:
        """
        Returns (is_in_window, window_label).
        Uses UTC+1 (WAT / Lagos time) matching your 4PM and 6AM windows.
        """
        now = datetime.datetime.now(tz=WAT)
        for (sh, sm, eh, em) in config.dip_windows_utc1:
            start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
            end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
            if start <= now <= end:
                label = f"{sh:02d}:{sm:02d}–{eh:02d}:{em:02d} WAT"
                return True, label
        return False, ""

    def minutes_to_next_window(self) -> int:
        """How many minutes until the next dip window opens."""
        now = datetime.datetime.now(tz=WAT)
        mins = []
        for (sh, sm, eh, em) in config.dip_windows_utc1:
            start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
            if start < now:
                start += datetime.timedelta(days=1)
            delta = (start - now).total_seconds() / 60
            mins.append(int(delta))
        return min(mins)

    # ── OHLC helpers ────────────────────────────────────────────

    def _get_candles(self, pair: str, interval: int = 15) -> list:
        """Cached OHLC fetch. Returns list of candle lists."""
        import time
        now = time.time()
        key = f"{pair}:{interval}"
        if key in self._candle_cache and (now - self._cache_ts.get(key, 0)) < self._cache_ttl:
            return self._candle_cache[key]
        candles = self.rest.get_ohlc(pair=pair, interval=interval)
        if candles:
            self._candle_cache[key] = candles
            self._cache_ts[key] = now
        return candles

    def _candles_to_df(self, candles: list) -> pd.DataFrame:
        """Convert raw candle list to DataFrame with typed columns."""
        if not candles:
            return pd.DataFrame()
        df = pd.DataFrame(candles, columns=["time", "open", "high", "low", "close", "vwap", "volume", "count"])
        for col in ["open", "high", "low", "close", "vwap", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["time"] = pd.to_datetime(df["time"], unit="s")
        return df.set_index("time").sort_index()

    def pct_change_n_candles(self, pair: str, n: int = 4, interval: int = 15) -> Optional[float]:
        """
        % price change over last n candles of given interval.
        Positive = price up, negative = dip.
        """
        candles = self._get_candles(pair, interval)
        if len(candles) < n + 1:
            return None
        prev_close = float(candles[-(n + 1)][4])
        curr_close = float(candles[-1][4])
        if prev_close == 0:
            return None
        return (curr_close - prev_close) / prev_close * 100

    def current_price(self, pair: str, interval: int = 15) -> Optional[float]:
        candles = self._get_candles(pair, interval)
        if not candles:
            return None
        return float(candles[-1][4])

    # ── Canary signals ──────────────────────────────────────────

    def get_canary_signal(self) -> dict:
        """
        Compute BTC + ETH canary dip signals.
        Returns structured dict the AI will evaluate.
        """
        btc_spot = config.canary_spot["PF_XBTUSD"]   # XXBTZUSD
        eth_spot = config.canary_spot["PF_ETHUSD"]   # XETHZUSD

        btc_1h  = self.pct_change_n_candles(btc_spot, n=4, interval=15)   # 4×15 = 60m
        btc_15m = self.pct_change_n_candles(btc_spot, n=1, interval=15)
        eth_1h  = self.pct_change_n_candles(eth_spot, n=4, interval=15)
        eth_15m = self.pct_change_n_candles(eth_spot, n=1, interval=15)

        btc_price = self.current_price(btc_spot)
        eth_price = self.current_price(eth_spot)

        dip_triggered = (
            btc_1h is not None and btc_1h <= config.btc_dip_threshold and
            eth_1h is not None and eth_1h <= config.eth_dip_threshold
        )

        return {
            "dip_triggered": dip_triggered,
            "btc": {
                "price": btc_price,
                "change_1h_pct": btc_1h,
                "change_15m_pct": btc_15m,
                "threshold": config.btc_dip_threshold,
                "breached": btc_1h is not None and btc_1h <= config.btc_dip_threshold,
            },
            "eth": {
                "price": eth_price,
                "change_1h_pct": eth_1h,
                "change_15m_pct": eth_15m,
                "threshold": config.eth_dip_threshold,
                "breached": eth_1h is not None and eth_1h <= config.eth_dip_threshold,
            }
        }

    # ── Regime classifier ───────────────────────────────────────

    def classify_regime(self) -> dict:
        """
        Classifies market into: BULL / BEAR / SIDEWAYS / UNCERTAIN.
        Uses BTC 20-SMA slope + 200-SMA relationship on daily candles.
        """
        btc_spot = config.canary_spot["PF_XBTUSD"]
        daily = self._get_candles(btc_spot, interval=1440)  # 1D candles
        if len(daily) < 25:
            return {"regime": "UNCERTAIN", "reason": "Insufficient daily candle history"}

        df = self._candles_to_df(daily)
        closes = df["close"]

        sma20 = closes.rolling(20).mean().iloc[-1]
        sma200 = closes.rolling(200).mean().iloc[-1] if len(closes) >= 200 else None
        current = closes.iloc[-1]

        # SMA slope over 5 days
        sma20_5d_ago = closes.rolling(20).mean().iloc[-6] if len(closes) >= 25 else sma20
        sma20_slope = (sma20 - sma20_5d_ago) / sma20_5d_ago * 100

        above_sma20 = current > sma20
        above_sma200 = current > sma200 if sma200 else None

        if sma20_slope > 1.0 and above_sma20:
            regime = "BULL"
        elif sma20_slope < -1.0 and not above_sma20:
            regime = "BEAR"
        else:
            regime = "SIDEWAYS"

        return {
            "regime": regime,
            "btc_price": float(current),
            "sma20": float(sma20),
            "sma20_slope_5d_pct": float(sma20_slope),
            "above_sma20": bool(above_sma20),
            "above_sma200": bool(above_sma200) if above_sma200 is not None else "insufficient_data",
            "sma200": float(sma200) if sma200 else None,
            "trade_enabled": regime in ("BULL", "SIDEWAYS"),
        }

    # ── Correlation matrix ──────────────────────────────────────

    # Spot pair mapping for alts (Kraken spot pairs for OHLC)
    ALT_SPOT_PAIRS = {
        "PF_SOLUSD":   "SOLUSD",
        "PF_XRPUSD":   "XXRPZUSD",
        "PF_ADAUSD":   "ADAUSD",
        "PF_AVAXUSD":  "AVAXUSD",
        "PF_DOTUSD":   "DOTUSD",
        "PF_LTCUSD":   "XLTCZUSD",
        "PF_LINKUSD":  "LINKUSD",
        "PF_UNIUSD":   "UNIUSD",
        "PF_MATICUSD": "MATICUSD",
    }

    def compute_correlation_matrix(self, interval: int = 15, lookback_candles: int = 48) -> pd.DataFrame:
        """
        Compute rolling correlation of each alt vs BTC over last N candles.
        interval: candle interval in minutes
        lookback_candles: number of candles to use (48 × 15min = 12 hours)
        Returns DataFrame: index=alt_symbol, columns=['correlation', 'pct_change_1h', 'current_price']
        """
        btc_candles = self._get_candles(config.canary_spot["PF_XBTUSD"], interval)
        if not btc_candles:
            return pd.DataFrame()

        btc_df = self._candles_to_df(btc_candles).tail(lookback_candles)
        btc_returns = btc_df["close"].pct_change().dropna()

        rows = []
        for futures_sym, spot_pair in self.ALT_SPOT_PAIRS.items():
            try:
                alt_candles = self._get_candles(spot_pair, interval)
                if len(alt_candles) < 10:
                    continue
                alt_df = self._candles_to_df(alt_candles).tail(lookback_candles)
                alt_returns = alt_df["close"].pct_change().dropna()

                # Align on common timestamps
                aligned = pd.concat([btc_returns.rename("btc"), alt_returns.rename("alt")], axis=1).dropna()
                if len(aligned) < 8:
                    continue

                corr = aligned["btc"].corr(aligned["alt"])
                alt_change_1h = self.pct_change_n_candles(spot_pair, n=4, interval=interval)
                alt_price = self.current_price(spot_pair)

                rows.append({
                    "futures_symbol": futures_sym,
                    "spot_pair": spot_pair,
                    "correlation": round(corr, 3),
                    "change_1h_pct": alt_change_1h,
                    "current_price": alt_price,
                    "lookback_candles": len(aligned),
                })
            except Exception as e:
                logger.debug(f"Correlation error for {spot_pair}: {e}")
                continue

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows).set_index("futures_symbol")
        df = df.sort_values("correlation", ascending=False)
        return df

    def get_high_correlation_alts(self, min_corr: Optional[float] = None) -> list[dict]:
        """
        Return alts with correlation >= threshold to BTC, currently dipping.
        These are the best candidates for mean-reversion longs.
        """
        threshold = min_corr or config.min_correlation
        matrix = self.compute_correlation_matrix()
        if matrix.empty:
            return []

        candidates = []
        for sym, row in matrix.iterrows():
            if row["correlation"] >= threshold and row.get("change_1h_pct") is not None:
                candidates.append({
                    "symbol": sym,
                    "spot_pair": row["spot_pair"],
                    "correlation": row["correlation"],
                    "change_1h_pct": row["change_1h_pct"],
                    "current_price": row["current_price"],
                    "dipping": row["change_1h_pct"] < -0.3,
                })
        return candidates

    # ── Full signal snapshot ────────────────────────────────────

    def get_full_signal_snapshot(self) -> dict:
        """
        One call returns everything the AI needs to make a decision.
        """
        in_window, window_label = self.is_dip_window()
        canary = self.get_canary_signal()
        regime = self.classify_regime()

        snapshot = {
            "timestamp": datetime.datetime.now(tz=WAT).isoformat(),
            "time_window": {
                "active": in_window,
                "label": window_label,
                "minutes_to_next": None if in_window else self.minutes_to_next_window(),
            },
            "regime": regime,
            "canary": canary,
            "pre_conditions_met": (
                in_window and
                regime.get("trade_enabled", False) and
                canary.get("dip_triggered", False)
            ),
        }

        # Only compute correlation when pre-conditions are close
        if in_window or canary.get("dip_triggered"):
            snapshot["correlation_candidates"] = self.get_high_correlation_alts()
        else:
            snapshot["correlation_candidates"] = []

        return snapshot
