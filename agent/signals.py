"""
Signal engine
Computes: regime classification, time window gate, BTC/ETH canary signals,
rolling correlation matrix between BTC and alts.

Improvements (v2):
- RSI (14-period) on BTC, ETH, and each alt candidate
- Volume spike detection (current 1h vol vs 20-period average)
- Both fed to AI brain as confirmation filters
"""

import datetime
from typing import Optional
import pandas as pd
import numpy as np
import pytz
from utils.logger import get_logger
from config import config
from kraken_wrappers.rest_client import KrakenRESTClient

logger = get_logger("signals")

WAT = pytz.timezone("Africa/Lagos")  # UTC+1, same as your dip windows

RSI_PERIOD = 14
RSI_OVERSOLD = 35.0       # RSI below this = oversold = good entry signal
RSI_OVERBOUGHT = 65.0     # RSI above this = overbought = skip
VOLUME_SPIKE_RATIO = 1.5  # current vol > 1.5× 20-period avg = spike confirmation
VOLUME_LOOKBACK = 20      # periods for volume moving average


class SignalEngine:
    def __init__(self):
        self.rest = KrakenRESTClient()
        self._candle_cache: dict[str, list] = {}
        self._cache_ts: dict[str, float] = {}
        self._cache_ttl = 60  # seconds

    # ── Time window gate ────────────────────────────────────────

    def is_dip_window(self) -> tuple[bool, str]:
        """Returns (is_in_window, window_label). Uses WAT (UTC+1)."""
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
        """% price change over last n candles. Negative = dip."""
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

    # ── RSI ─────────────────────────────────────────────────────

    def compute_rsi(self, pair: str, period: int = RSI_PERIOD, interval: int = 15) -> Optional[float]:
        """
        Computes RSI using Wilder's smoothing (standard).
        Returns float 0–100, or None if insufficient data.
        Interpretation: <35 = oversold (entry signal), >65 = overbought (skip).
        """
        candles = self._get_candles(pair, interval)
        if len(candles) < period + 5:
            return None

        df = self._candles_to_df(candles)
        closes = df["close"].dropna()
        if len(closes) < period + 1:
            return None

        delta = closes.diff()
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)

        # Wilder's smoothed averages
        avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()

        rs = avg_gain / avg_loss.replace(0, float("nan"))
        rsi = 100 - (100 / (1 + rs))
        value = rsi.iloc[-1]
        return round(float(value), 2) if not np.isnan(value) else None

    def rsi_signal(self, pair: str, interval: int = 15) -> dict:
        """Returns RSI value plus interpretation for a given pair."""
        rsi = self.compute_rsi(pair, interval=interval)
        if rsi is None:
            return {"rsi": None, "oversold": None, "overbought": None, "signal": "unknown"}
        return {
            "rsi": rsi,
            "oversold": rsi < RSI_OVERSOLD,
            "overbought": rsi > RSI_OVERBOUGHT,
            "signal": "oversold" if rsi < RSI_OVERSOLD else ("overbought" if rsi > RSI_OVERBOUGHT else "neutral"),
        }

    # ── Volume confirmation ──────────────────────────────────────

    def compute_volume_ratio(self, pair: str, interval: int = 60,
                             lookback: int = VOLUME_LOOKBACK) -> Optional[float]:
        """
        Returns current period volume / N-period average volume.
        Ratio > VOLUME_SPIKE_RATIO means elevated volume = confirms the move.
        Uses 60-min candles by default for volume comparison.
        """
        candles = self._get_candles(pair, interval)
        if len(candles) < lookback + 1:
            return None

        df = self._candles_to_df(candles)
        vols = df["volume"].dropna()
        if len(vols) < lookback + 1:
            return None

        current_vol = float(vols.iloc[-1])
        avg_vol = float(vols.iloc[-(lookback + 1):-1].mean())
        if avg_vol == 0:
            return None
        return round(current_vol / avg_vol, 3)

    def volume_signal(self, pair: str, interval: int = 60) -> dict:
        """Returns volume ratio and spike flag."""
        ratio = self.compute_volume_ratio(pair, interval=interval)
        if ratio is None:
            return {"volume_ratio": None, "spike": None, "signal": "unknown"}
        return {
            "volume_ratio": ratio,
            "spike": ratio >= VOLUME_SPIKE_RATIO,
            "signal": "spike" if ratio >= VOLUME_SPIKE_RATIO else "normal",
        }

    # ── Canary signals ──────────────────────────────────────────

    def get_canary_signal(self) -> dict:
        """
        Compute BTC + ETH canary dip signals, now including RSI and volume data.
        The AI uses all of these to make the final entry call.
        """
        btc_spot = config.canary_spot["PF_XBTUSD"]   # XXBTZUSD
        eth_spot = config.canary_spot["PF_ETHUSD"]   # XETHZUSD

        btc_1h  = self.pct_change_n_candles(btc_spot, n=4, interval=15)
        btc_15m = self.pct_change_n_candles(btc_spot, n=1, interval=15)
        eth_1h  = self.pct_change_n_candles(eth_spot, n=4, interval=15)
        eth_15m = self.pct_change_n_candles(eth_spot, n=1, interval=15)

        btc_price = self.current_price(btc_spot)
        eth_price = self.current_price(eth_spot)

        # RSI for BTC and ETH
        btc_rsi = self.rsi_signal(btc_spot)
        eth_rsi = self.rsi_signal(eth_spot)

        # Volume spikes
        btc_vol = self.volume_signal(btc_spot)
        eth_vol = self.volume_signal(eth_spot)

        dip_triggered = (
            btc_1h is not None and btc_1h <= config.btc_dip_threshold and
            eth_1h is not None and eth_1h <= config.eth_dip_threshold
        )

        # Strong signal = dip + RSI oversold + volume spike
        strong_signal = (
            dip_triggered and
            btc_rsi.get("oversold") is True and
            btc_vol.get("spike") is True
        )

        return {
            "dip_triggered": dip_triggered,
            "strong_signal": strong_signal,
            "btc": {
                "price": btc_price,
                "change_1h_pct": btc_1h,
                "change_15m_pct": btc_15m,
                "threshold": config.btc_dip_threshold,
                "breached": btc_1h is not None and btc_1h <= config.btc_dip_threshold,
                "rsi": btc_rsi,
                "volume": btc_vol,
            },
            "eth": {
                "price": eth_price,
                "change_1h_pct": eth_1h,
                "change_15m_pct": eth_15m,
                "threshold": config.eth_dip_threshold,
                "breached": eth_1h is not None and eth_1h <= config.eth_dip_threshold,
                "rsi": eth_rsi,
                "volume": eth_vol,
            }
        }

    # ── Regime classifier ───────────────────────────────────────

    def classify_regime(self) -> dict:
        """
        Classifies market into: BULL / BEAR / SIDEWAYS / UNCERTAIN.
        Uses BTC 20-SMA slope + 200-SMA relationship on daily candles.
        """
        btc_spot = config.canary_spot["PF_XBTUSD"]
        daily = self._get_candles(btc_spot, interval=1440)
        if len(daily) < 25:
            return {"regime": "UNCERTAIN", "reason": "Insufficient daily candle history"}

        df = self._candles_to_df(daily)
        closes = df["close"]

        sma20 = closes.rolling(20).mean().iloc[-1]
        sma200 = closes.rolling(200).mean().iloc[-1] if len(closes) >= 200 else None
        current = closes.iloc[-1]

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
        """Compute rolling correlation of each alt vs BTC over last N candles."""
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

                aligned = pd.concat([btc_returns.rename("btc"), alt_returns.rename("alt")], axis=1).dropna()
                if len(aligned) < 8:
                    continue

                corr = aligned["btc"].corr(aligned["alt"])
                alt_change_1h = self.pct_change_n_candles(spot_pair, n=4, interval=interval)
                alt_price = self.current_price(spot_pair)

                # RSI + volume for each alt
                alt_rsi = self.rsi_signal(spot_pair, interval=interval)
                alt_vol = self.volume_signal(spot_pair, interval=60)

                rows.append({
                    "futures_symbol": futures_sym,
                    "spot_pair": spot_pair,
                    "correlation": round(corr, 3),
                    "change_1h_pct": alt_change_1h,
                    "current_price": alt_price,
                    "rsi": alt_rsi.get("rsi"),
                    "rsi_oversold": alt_rsi.get("oversold"),
                    "volume_ratio": alt_vol.get("volume_ratio"),
                    "volume_spike": alt_vol.get("spike"),
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
        Return alts with correlation >= threshold to BTC.
        Now includes RSI and volume data for each candidate.
        """
        threshold = min_corr or config.min_correlation
        matrix = self.compute_correlation_matrix()
        if matrix.empty:
            return []

        candidates = []
        for sym, row in matrix.iterrows():
            if row["correlation"] >= threshold and row.get("change_1h_pct") is not None:
                rsi_val = row.get("rsi")
                vol_ratio = row.get("volume_ratio")
                candidates.append({
                    "symbol": sym,
                    "spot_pair": row["spot_pair"],
                    "correlation": row["correlation"],
                    "change_1h_pct": row["change_1h_pct"],
                    "current_price": row["current_price"],
                    "dipping": row["change_1h_pct"] < -0.3,
                    "rsi": rsi_val,
                    "rsi_oversold": row.get("rsi_oversold"),
                    "volume_ratio": vol_ratio,
                    "volume_spike": row.get("volume_spike"),
                    # Combined quality score: dipping + oversold RSI + volume spike
                    "signal_quality": sum([
                        1 if row["change_1h_pct"] < -0.3 else 0,
                        1 if row.get("rsi_oversold") is True else 0,
                        1 if row.get("volume_spike") is True else 0,
                    ]),
                })

        # Sort by signal quality first, then correlation
        candidates.sort(key=lambda x: (x["signal_quality"], x["correlation"]), reverse=True)
        return candidates

    # ── Full signal snapshot ────────────────────────────────────

    def get_full_signal_snapshot(self) -> dict:
        """One call returns everything the AI needs to make a decision."""
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

        if in_window or canary.get("dip_triggered"):
            snapshot["correlation_candidates"] = self.get_high_correlation_alts()
        else:
            snapshot["correlation_candidates"] = []

        return snapshot
