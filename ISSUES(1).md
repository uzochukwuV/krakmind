# ArbMind — CEX-DEX Arbitrage Implementation Issues

> **Build philosophy:** Paper mode first. Every issue is fully implemented, but a `PAPER_MODE` guard
> replaces trade execution with an opportunity alert. When you flip `PAPER_MODE=false`, the exact
> same code path executes the real trade — no rewrites needed.

---

## Research Summary

### Prism API (prismapi.ai)

Your existing `data/prism_client.py` already calls several endpoints. The full surface relevant to
arbitrage is:

| Endpoint | Method | Purpose |
|---|---|---|
| `/resolve/{symbol}` | GET | Canonical asset identity + venue list with trade URLs |
| `/crypto/price/{symbol}` | GET | Single consensus price (multi-source aggregated) |
| `/crypto/prices/batch` | GET | Batch prices — `?symbols=BTC,ETH,SOL` |
| `/dex/pairs/{address}` | GET | DEX pair data by contract address |
| `/dex/tokens/trending` | GET | Trending tokens by volume/social score |
| `/dex/pools/{chain}` | GET | Pool list for a chain (e.g. `base`) |
| `/dex/search` | POST | Search DEX pairs by token symbol |
| `/market/fear-greed` | GET | Real Fear & Greed index |
| `/social/{symbol}/sentiment` | GET | Sentiment score + label per token |
| `/dex/{symbol}/funding/all` | GET | Funding rates across DEX perps |

**Key insight from `/resolve/BTC` response (already tested in your repo):**
The response includes a `venues` array with objects containing `type` (`cex_spot`, `cex_perp`,
`dex_perp`), `trade_url`, and `leverage`. This is the bridge between CEX price (Kraken) and DEX
price (Aerodrome on Base) — Prism gives you both in one call.

**Auth:** `X-API-Key: prism_sk_...` header. Your key: `prism_sk_US8dsdhgHcoWzO7APOz_Vjxd4DyCoTTdEeUd4sw5_Wo`

**Rate limits:** Free tier = 1 QPS / 1,000 req/day. You already have a `_min_call_gap` throttle in
`prism_client.py` — keep it.

**Docs:** `https://api.prismapi.ai/docs` (Swagger UI — browse all 100+ endpoints live)

---

### Aerodrome Finance (Base network)

Aerodrome is the dominant DEX on Base (~40% of all Base liquidity, $1.29B TVL). It is your DEX
price leg for BTC/ETH/alt arbitrage against Kraken.

**Three ways to get Aerodrome prices (pick by latency need):**

| Method | Latency | Complexity | Use for |
|---|---|---|---|
| **DexScreener REST API** (free, no key) | ~200ms | Low | Price polling every 30s |
| **Aerodrome Subgraph via The Graph / Goldsky** (free) | ~500ms | Medium | Pool reserve data |
| **QuickNode Aerodrome Swap API** (paid addon) | ~50ms | Low | Production swap quotes |

**For this hackathon: DexScreener is the fastest no-auth path.**

DexScreener API (no key needed, free):
```
GET https://api.dexscreener.com/latest/dex/tokens/{tokenAddress}
GET https://api.dexscreener.com/latest/dex/search?q=WETH+USDC+base
```

Response includes `priceUsd`, `liquidity.usd`, `volume.h24`, `priceChange.h1` per pair.

**Key Aerodrome token addresses on Base (mainnet):**

| Token | Address |
|---|---|
| WETH | `0x4200000000000000000000000000000000000006` |
| USDC | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` |
| cbBTC | `0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf` |
| AERO | `0x940181a94A35A4569E4529A3CDfB74e38FD98631` |

**Aerodrome factory (for pool discovery):** `0x420DD381b31aEf6683db6B9020084cb0FFECe40dA`

**Aerodrome Subgraph (The Graph — free tier):**
```
https://api.thegraph.com/subgraphs/name/aerodrome-finance/aerodrome-v2
```
GraphQL query to get pool price:
```graphql
{
  pools(where: { token0: "0x4200...0006", token1: "0x8335...3913" }) {
    token0Price
    token1Price
    liquidity
    volumeUSD
  }
}
```

**Docs:**
- DexScreener API: `https://docs.dexscreener.com/api/reference`
- Aerodrome GitHub: `https://github.com/aerodrome-finance`
- Aerodrome docs: `https://aerodrome.finance/docs`
- QuickNode Swap API (swap quotes): `https://marketplace.quicknode.com/add-on/aerodrome-swap-api`
- Bitquery GraphQL (real-time trades): `https://docs.bitquery.io/docs/blockchain/Base/aerodrome-base-api/`

---

## Issue Tracker

---

### Issue 1 — DEX Price Feed Client (`data/dex_price_client.py`)

**What:** Create a new client that fetches real-time Aerodrome/DEX prices for your tradeable
symbols using DexScreener's free REST API. This is the "DEX leg" price for every arbitrage
comparison.

**Why first:** Everything else depends on knowing the DEX price. No DEX price = no arb signal.

**Where to get info:**
- DexScreener API reference: `https://docs.dexscreener.com/api/reference`
- No auth required. Base URL: `https://api.dexscreener.com`

**How to implement:**

1. Create `data/dex_price_client.py`. Import `requests`, `time`, your logger.

2. Define a symbol → Base token address mapping for your tradeable alts (start with
   WETH/cbBTC/USDC as the main arb pairs, then add SOL, LINK, etc. via their Base bridge
   addresses).

3. Implement `DexPriceClient` class with:
   - `__init__`: set `_cache: dict` and `_cache_ttl = 15` (seconds — DexScreener updates ~15s)
   - `get_price(symbol: str) -> dict | None`: calls
     `GET /latest/dex/tokens/{token_address}` and returns the best (highest liquidity) pair's
     `priceUsd`, `liquidity.usd`, `volume.h24`, `priceChange.h1`
   - `get_batch_prices(symbols: list) -> dict[str, dict]`: loops `get_price` with cache
   - `_best_pair(pairs: list) -> dict`: picks pair with highest `liquidity.usd` from response

4. The response shape for each pair:
   ```json
   {
     "chainId": "base",
     "dexId": "aerodrome",
     "priceUsd": "3245.12",
     "liquidity": { "usd": 4500000 },
     "volume": { "h24": 12000000 },
     "priceChange": { "h1": -0.42 }
   }
   ```

5. Filter pairs: only keep `chainId == "base"` and `dexId == "aerodrome"` to ensure you're
   comparing Aerodrome specifically (not another Base DEX).

6. Add to `data/__init__.py` exports.

**Paper mode consideration:** This client is read-only — no guard needed here. It runs in both
paper and live mode identically.

**Test it:**
```bash
curl "https://api.dexscreener.com/latest/dex/tokens/0x4200000000000000000000000000000000000006"
```

---

### Issue 2 — Kraken CEX Price Feed (extend `kraken_wrappers/rest_client.py`)

**What:** Add a `get_spot_price(pair: str) -> float | None` convenience method that returns a
single float (current mid-price) for a Kraken spot pair. You already have `get_ohlc` but the
arb engine needs a sub-second price snapshot, not a full candle list.

**Why:** The arbitrage gap is calculated as `(kraken_price - aerodrome_price) / aerodrome_price`.
You need both prices at the same moment with the freshest data possible. OHLC candles have up to
60s lag — the ticker endpoint is real-time.

**Where to get info:**
- Kraken REST ticker: `GET https://api.kraken.com/0/public/Ticker?pair=XXBTZUSD`
- python-kraken-sdk docs: `https://python-krakenex.readthedocs.io`
- Your existing `KrakenRESTClient` in `kraken_wrappers/rest_client.py`

**How to implement:**

1. Open `kraken_wrappers/rest_client.py`.

2. Add method to `KrakenRESTClient`:
   ```python
   def get_spot_price(self, pair: str) -> float | None:
       """Returns current mid-price for a spot pair via the ticker endpoint."""
   ```

3. Call `self._spot_market.get_ticker(pair=pair)`. The SDK returns:
   ```json
   { "XXBTZUSD": { "b": ["84250.10", 1, "1.000"], "a": ["84250.20", ...], "c": ["84250.10", ...] } }
   ```
   Use `c[0]` (last trade price) as the price. Mid = `(float(b[0]) + float(a[0])) / 2` is more
   accurate for arb calculations.

4. Wrap in try/except, return `None` on failure.

5. Add a simple `_price_cache: dict` with 5-second TTL to avoid hammering Kraken during rapid
   polling (the arb loop runs every 10s, but position monitor also calls this).

**Paper mode consideration:** Read-only — no guard needed.

---

### Issue 3 — Arb Opportunity Detector (`agent/arb_detector.py`)

**What:** Create the core arbitrage engine. It compares Kraken CEX price vs Aerodrome DEX price
for each tracked symbol, computes the net gap after fees, and emits a structured
`ArbOpportunity` object when the gap exceeds threshold.

**Why:** This is the brain of the arb strategy. The AI and the loop both consume `ArbOpportunity`
objects — clean separation of detection from execution.

**Where to get info:**
- CEX fees: Kraken maker/taker = 0.16%/0.26% (spot), use 0.26% (taker) to be conservative
- DEX fees: Aerodrome volatile pools = 0.3%, stable pools = 0.02%
- Slippage estimate: 0.1–0.3% depending on position size vs pool liquidity
- Your `data/dex_price_client.py` (Issue 1) and `kraken_wrappers/rest_client.py` (Issue 2)

**How to implement:**

1. Create `agent/arb_detector.py`.

2. Define constants at top:
   ```python
   KRAKEN_TAKER_FEE   = 0.0026   # 0.26%
   AERODROME_POOL_FEE = 0.003    # 0.3% volatile pool
   SLIPPAGE_ESTIMATE  = 0.002    # 0.2% conservative
   TOTAL_ROUND_TRIP_COST = (KRAKEN_TAKER_FEE + AERODROME_POOL_FEE + SLIPPAGE_ESTIMATE) * 2
   MIN_NET_GAP_PCT    = 0.004    # 0.4% min net profit after all costs
   ```

3. Define `ArbOpportunity` dataclass:
   ```python
   @dataclass
   class ArbOpportunity:
       symbol: str              # e.g. "SOL"
       kraken_pair: str         # e.g. "SOLUSD"
       kraken_price: float
       dex_price: float
       raw_gap_pct: float       # (kraken - dex) / dex * 100
       net_gap_pct: float       # raw_gap_pct - costs
       direction: str           # "buy_dex_sell_cex" | "buy_cex_sell_dex"
       dex_liquidity_usd: float
       confidence: float        # 0-1 based on gap size and liquidity depth
       detected_at: float       # time.time()
       estimated_profit_usd: float  # at default position size
   ```

4. Implement `ArbDetector` class:
   - `__init__(dex_client, kraken_rest, position_manager)`:
     Store references. Define symbol map:
     ```python
     self.SYMBOL_MAP = {
         "ETH":  {"kraken": "XETHZUSD", "dex_addr": "0x4200...0006"},
         "BTC":  {"kraken": "XXBTZUSD", "dex_addr": "0xcbB7...Bf"},
         "SOL":  {"kraken": "SOLUSD",   "dex_addr": "0x..."},
         # add more as you find Base bridge addresses
     }
     ```
   - `scan() -> list[ArbOpportunity]`: iterates symbol map, calls both price feeds, computes gap
   - `_compute_gap(kraken_price, dex_price) -> tuple[float, str]`: returns (raw_gap_pct, direction)
   - `_confidence(net_gap_pct, liquidity_usd, position_size_usd) -> float`:
     Confidence = min(1.0, (net_gap_pct / MIN_NET_GAP_PCT) * 0.5 + (liquidity_usd / 1_000_000) * 0.5)
   - `_estimated_profit(net_gap_pct, position_size_usd) -> float`:
     Simply `net_gap_pct / 100 * position_size_usd`

5. Make `scan()` return only opportunities where:
   - `net_gap_pct >= MIN_NET_GAP_PCT`
   - `dex_liquidity_usd >= position_size_usd * 10` (10× liquidity depth requirement)
   - Both price feeds returned valid data within the last 30s

**Paper mode consideration:** `scan()` is pure detection — no guard needed here. The guard lives
in Issue 5 (execution).

---

### Issue 4 — Extend Prism Client for DEX Intelligence (`data/prism_client.py`)

**What:** Add two new methods to `PrismClient` that specifically support the arbitrage workflow:
`get_dex_price(symbol)` and `get_venue_prices(symbol)`. These use Prism's `/resolve/{symbol}`
endpoint (which already returns `venues` array with prices) as a second price source to validate
DexScreener prices and detect cross-venue gaps.

**Why:** DexScreener gives you Aerodrome prices. Prism gives you a multi-venue consensus price
AND a list of all venues trading that asset. When Prism's venue list shows a Kraken price AND a
Uniswap/Aerodrome price simultaneously, that IS the arbitrage signal — pre-detected for you.

**Where to get info:**
- Already tested in your repo: `attached_assets/Pasted-ASUSFX95G...txt` shows the full
  `/resolve/BTC` response with 12 venues including Kraken and Hyperliquid
- Prism docs: `https://api.prismapi.ai/docs` → look for `/resolve`, `/dex/pairs`, `/dex/search`
- Your existing `PrismClient` in `data/prism_client.py`

**How to implement:**

1. Open `data/prism_client.py`.

2. Add `get_venue_prices(symbol: str) -> dict` method:
   - Calls `self._get(f"/resolve/{symbol}", ttl=10)` (10s TTL — short for arb use)
   - From response, extract `venues` list
   - Filter to relevant venue types: `cex_spot`, `dex_perp`, `dex_spot`
   - Return dict: `{"kraken": price, "aerodrome": price, "binance": price, ...}`
   - Note: `/resolve` returns `price_usd` at top level = Prism's consensus price. Individual
     venue prices require separate calls to each venue's endpoint — use the consensus as proxy
     for now, flagging when venue spread > 0.5%

3. Add `get_dex_search(symbol: str, chain: str = "base") -> list[dict]` method:
   - Calls `POST /dex/search` with body `{"q": symbol, "chain": chain}`
   - Returns list of pool dicts with `priceUsd`, `liquidity`, `dexId`
   - Filter to `dexId == "aerodrome"` for our purposes

4. Add `get_funding_rates_all(symbol: str) -> dict` (already partially there but expand it):
   - `GET /dex/{symbol}/funding/all`
   - Extract `best_for_long`, `best_for_short`, `interpretation`
   - Use to determine if funding rate harvest is simultaneously viable with arb opportunity

5. Update `get_signal_snapshot()` to call `get_venue_prices` for BTC and ETH and include
   the venue spread in the returned snapshot under key `"venue_spreads"`.

**Paper mode consideration:** Read-only — no guard needed.

---

### Issue 5 — Arbitrage Execution Engine with Paper Guard (`agent/arb_executor.py`)

**What:** Create the execution layer that takes an `ArbOpportunity` and either:
- **Paper mode:** Logs the opportunity as an alert, records it in a paper journal, pushes it
  to shared state for the dashboard — no real orders placed.
- **Live mode:** Places simultaneous orders on Kraken (spot) and Aerodrome (via web3/API) to
  capture the gap.

**Why this is the critical paper guard issue:** This is where you protect capital while
building the complete production-ready code path. Everything compiles and runs — only the final
order submission is gated.

**Where to get info:**
- Kraken paper buy/sell: already in `kraken_wrappers/cli_wrapper.py` (`paper_buy`, `paper_sell`)
- Kraken live order: `futures_send_order` in same file (spot equivalent: `order buy` CLI command)
- Aerodrome swap: QuickNode Swap API `https://marketplace.quicknode.com/add-on/aerodrome-swap-api`
  OR direct web3 call using `web3.py` + Aerodrome Router ABI
- Aerodrome Router address on Base: `0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43`
- Your `config.py` for `paper_mode` flag

**How to implement:**

1. Create `agent/arb_executor.py`.

2. Import: `from config import config`, `from utils.logger import get_logger`, dataclasses, time.

3. Define `ArbResult` dataclass:
   ```python
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
   ```

4. Implement `ArbExecutor` class with `__init__(kraken_cli, position_manager)`.

5. Core method `execute(opportunity: ArbOpportunity) -> ArbResult`:
   ```python
   def execute(self, opportunity: ArbOpportunity) -> ArbResult:
       if config.paper_mode:
           return self._paper_alert(opportunity)
       else:
           return self._live_execute(opportunity)
   ```

6. Implement `_paper_alert(opportunity) -> ArbResult`:
   - Log the full opportunity with `logger.info` using Rich formatting:
     ```
     [ARB ALERT] ETH | Kraken=$3245.10 | Aerodrome=$3238.50
     Gap=+0.20% net | Est. profit=$12.30 on $5000 | Dir=buy_dex_sell_cex
     ⚠️  PAPER MODE — opportunity logged, no orders placed
     ```
   - Call `self.position_manager` to record as a "virtual arb" in a new section of
     `paper_positions.json` under key `"arb_alerts"` with all opportunity fields + timestamp
   - Push to `api/shared_state.py` under key `"arb_alerts"` (list, keep last 50)
   - Return `ArbResult(executed=False, paper_mode=True, alert_logged=True, ...)`

7. Implement `_live_execute(opportunity) -> ArbResult` (scaffold for production):
   ```python
   def _live_execute(self, opportunity: ArbOpportunity) -> ArbResult:
       # Step 1: Place Kraken spot order
       # Step 2: Place Aerodrome swap (web3 tx)
       # Step 3: Confirm both legs filled
       # Step 4: Record actual PnL
       # TODO: implement when PAPER_MODE=false
       raise NotImplementedError("Live arb execution — set PAPER_MODE=false and implement web3 leg")
   ```

8. Add `_size_position(opportunity) -> float`:
   Use Kelly sizing from `position_manager.kelly_position_size(confidence=opportunity.confidence)`
   but cap at `min(kelly_size, 0.02)` for arb (2% max — arb is fast, not directional).

**Paper mode guard summary:**
```
config.paper_mode = True  → _paper_alert() → logs + records, NO orders
config.paper_mode = False → _live_execute() → real CEX + DEX orders
```
The guard is a single `if config.paper_mode` check. Flipping the env var is all that's needed
to go live — the full code path is already built.

---

### Issue 6 — Arb Async Loop (`agent/arb_loop.py`)

**What:** Create a fourth async coroutine alongside the existing three loops in `agent/loop.py`.
The arb loop runs every 10 seconds (much faster than the 60s main loop), scans for arb
opportunities, and calls the executor.

**Why separate from main loop:** The main loop runs AI analysis every 60s minimum and burns
tokens. Arbitrage is time-sensitive (gaps close in seconds) and doesn't need an LLM — pure
signal detection → execution. The two loops run in parallel via `asyncio.gather`.

**Where to get info:**
- Your existing `agent/prism_loop.py` — copy the pattern (async loop with poll interval)
- Python `asyncio` docs: `https://docs.python.org/3/library/asyncio.html`
- `agent/loop.py` — where you'll add the new coroutine to `asyncio.gather`

**How to implement:**

1. Create `agent/arb_loop.py`.

2. Define `ARB_SCAN_INTERVAL = 10` (seconds) at top.

3. Implement `ArbLoop` class:
   ```python
   class ArbLoop:
       def __init__(self, arb_detector, arb_executor, position_manager):
           self.detector   = arb_detector
           self.executor   = arb_executor
           self.positions  = position_manager
           self._cycle     = 0
           self._alerts_today = 0
           self._max_alerts_per_hour = 20  # rate limit paper alerts
   ```

4. Implement `async def run(self)`:
   - Infinite loop with `await asyncio.sleep(ARB_SCAN_INTERVAL)`
   - Call `opportunities = self.detector.scan()` (sync — wrap in `run_in_executor`)
   - For each opportunity, call `self.executor.execute(opportunity)`
   - Track alert rate — if `_alerts_per_hour > _max_alerts_per_hour`, skip logging but still
     record (prevents console spam during volatile periods)

5. Display summary every 60s via Rich console:
   ```
   [ARB SCAN #42] Scanned 8 pairs | 2 opportunities found | Best gap: ETH +0.31% net
   ```

6. Open `agent/loop.py`. In `TradingLoop.__init__`, add:
   ```python
   from agent.arb_loop import ArbLoop
   from agent.arb_detector import ArbDetector
   from agent.arb_executor import ArbExecutor
   from data.dex_price_client import DexPriceClient

   self.dex_prices = DexPriceClient()
   self.arb_detector = ArbDetector(self.dex_prices, self.rest, self.positions)
   self.arb_executor = ArbExecutor(self.cli, self.positions)
   self.arb_loop = ArbLoop(self.arb_detector, self.arb_executor, self.positions)
   ```

7. In `TradingLoop.run()`, add `self.arb_loop.run()` to `asyncio.gather(...)`:
   ```python
   await asyncio.gather(
       self._main_loop(),
       self._position_monitor(),
       self.prism.run(),
       self.arb_loop.run(),   # ← add this
   )
   ```

**Paper mode consideration:** The loop itself has no guard. The guard lives inside
`ArbExecutor.execute()` from Issue 5.

---

### Issue 7 — Inject Arb Signals into AI Brain Context

**What:** Extend `agent/ai_brain.py`'s `ContextBuilder.build()` to include the latest arb
opportunities in the prompt injected to the LLM. The AI should be aware of active arb gaps when
making its directional mean-reversion decisions (if ETH has a 0.4% arb gap favoring buy-DEX, it
validates a long thesis).

**Why:** Right now the AI knows about Fear & Greed, canary dips, RSI, volume, and Prism signals,
but not DEX pricing. Adding arb context lets the AI say "ETH is dipping on Kraken AND Aerodrome
DEX price is higher → mean-reversion long is confirmed by arb signal."

**Where to get info:**
- `agent/ai_brain.py` — specifically `ContextBuilder.build()` method
- `api/shared_state.py` — where arb alerts are written (Issue 5 step 6)
- `agent/prism_loop.py` — `prism_store` singleton pattern to copy for arb state

**How to implement:**

1. In `agent/ai_brain.py`, import `from api import shared_state`.

2. In `ContextBuilder.build()`, add a new section after the Prism section:
   ```python
   # Arb signals section
   try:
       arb_alerts = shared_state.get_section("arb_alerts") or []
       recent = [a for a in arb_alerts if time.time() - a.get("detected_at", 0) < 300]
       if recent:
           rows = "\n".join(
               f"  {a['symbol']}: gap={a['net_gap_pct']:+.2f}% | dir={a['direction']} | "
               f"liquidity=${a['dex_liquidity_usd']/1e6:.1f}M | confidence={a['confidence']:.2f}"
               for a in recent[:5]
           )
           sections.append(f"## Live Arb Opportunities (last 5min)\n{rows}")
       else:
           sections.append("## Live Arb Opportunities\n  None detected in last 5min")
   except Exception as e:
       sections.append(f"## Arb Opportunities\nError: {e}")
   ```

3. Update `SYSTEM_PROMPT` in `agent/ai_brain.py` to add arb awareness:
   Add this bullet to the decision rules section:
   ```
   - Arb signal validation: if an arb opportunity exists for your trade candidate with
     net_gap_pct > 0.3% in the same direction, add 0.1 to confidence score.
   - If arb gap direction CONTRADICTS your mean-reversion trade, reduce confidence by 0.15.
   ```

4. No structural changes to `TradeDecision` or `PositionDecision` needed — this is pure context
   enrichment.

---

### Issue 8 — Dashboard API: Arb Alert Endpoints (`api/server.py`)

**What:** Add two new FastAPI endpoints to expose arb data to the dashboard frontend:
- `GET /api/arb/alerts` — recent arb opportunities (paper alerts)
- `GET /api/arb/stats` — cumulative arb stats (total alerts, best gap seen, etc.)

**Why:** The hackathon judges need to see your arb engine working. The dashboard is proof. Also,
the `shared_state` arb data sits in memory — this endpoint makes it queryable externally.

**Where to get info:**
- `api/server.py` — existing FastAPI app, follow the same pattern as `/api/prism`
- `api/shared_state.py` — `get_section("arb_alerts")` is where data lives
- FastAPI docs: `https://fastapi.tiangolo.com/`

**How to implement:**

1. Open `api/server.py`.

2. Add `arb_alerts` key to `_state` dict in `api/shared_state.py`:
   ```python
   "arb_alerts": [],   # list of ArbOpportunity dicts, last 50
   "arb_stats": {
       "total_scans": 0,
       "total_alerts": 0,
       "best_gap_pct": 0.0,
       "best_gap_symbol": "",
       "estimated_pnl_missed": 0.0,   # paper mode: what we would have earned
   }
   ```

3. In `api/server.py`, add after the prism endpoint:
   ```python
   @app.get("/api/arb/alerts")
   def arb_alerts(limit: int = 20):
       alerts = shared_state.get_section("arb_alerts")
       alerts = sorted(alerts, key=lambda a: a.get("detected_at", 0), reverse=True)
       return {"alerts": alerts[:limit], "total": len(alerts), "paper_mode": True}

   @app.get("/api/arb/stats")
   def arb_stats():
       return shared_state.get_section("arb_stats")
   ```

4. Update `ArbExecutor._paper_alert()` (Issue 5) to also update `arb_stats`:
   - Increment `total_alerts`
   - Update `best_gap_pct` if current > stored
   - Accumulate `estimated_pnl_missed`

5. Add arb summary to `/api/snapshot` endpoint:
   ```python
   @app.get("/api/snapshot")
   def snapshot():
       data = shared_state.get_snapshot()
       data["_served_at"] = time.time()
       return JSONResponse(content=data)
   ```
   (already includes all sections — just make sure `arb_alerts` and `arb_stats` are in state)

---

### Issue 9 — Funding Rate Harvest Strategy (`agent/funding_harvester.py`)

**What:** Implement the "boring alpha" strategy: when BTC or ETH perpetual funding rates on
Kraken Futures are significantly positive (longs paying shorts > 0.03%/8h), record a
delta-neutral harvest opportunity. In paper mode: alert. In live mode: short futures + note
spot hedge requirement.

**Why separate issue:** This is Path B from the strategy analysis — simpler than arb (no
simultaneous execution), runs on an 8-hour cycle, and stacks on top of both the arb loop and
the existing mean-reversion strategy.

**Where to get info:**
- Kraken Futures funding rates: `GET https://futures.kraken.com/derivatives/api/v3/tickers`
  — field `fundingRate` per instrument (e.g. `PF_XBTUSD`)
- Your `kraken_wrappers/rest_client.py` — add `get_futures_funding_rates()` method
- Prism funding rates: `GET /dex/{symbol}/funding/all` — already in `PrismClient`
- Your existing `prism_loop.py` for funding rate data already being fetched

**How to implement:**

1. Create `agent/funding_harvester.py`.

2. Define constants:
   ```python
   MIN_FUNDING_RATE_8H   = 0.0003   # 0.03% per 8h = ~0.11%/day = ~40%/year annualized
   FUNDING_INTERVAL_HOURS = 8
   HARVEST_SYMBOLS = ["PF_XBTUSD", "PF_ETHUSD"]
   ```

3. Add `get_futures_funding_rates() -> dict` to `KrakenRESTClient`:
   - Call Kraken Futures REST: `GET https://futures.kraken.com/derivatives/api/v3/tickers`
   - Parse `tickers` list, filter for `HARVEST_SYMBOLS`
   - Return `{"PF_XBTUSD": {"fundingRate": 0.0005, "nextFundingRateTime": "..."}}`

4. Implement `FundingHarvester` class:
   - `scan() -> list[FundingOpportunity]`: gets funding rates, flags when > `MIN_FUNDING_RATE_8H`
   - `alert_or_execute(opp)`: same paper guard pattern as Issue 5
     - Paper: log `[FUNDING ALERT] BTC funding=+0.05%/8h → Short PF_XBTUSD, hedge with spot`
     - Live: place short futures order via `kraken_cli.futures_send_order`

5. Define `FundingOpportunity` dataclass:
   ```python
   @dataclass
   class FundingOpportunity:
       symbol: str
       funding_rate_8h: float
       annualized_yield_pct: float   # funding_rate_8h * 3 * 365 * 100
       next_funding_time: str
       direction: str                # "short_futures_long_spot"
       estimated_daily_usd: float
   ```

6. Add to `agent/loop.py` `TradingLoop.run()` — but on a slower poll:
   Check funding rates every 30 minutes (not 10s like arb). Add as a check inside the main loop
   rather than a separate coroutine (lower urgency):
   ```python
   if self._cycle % 30 == 0:   # every 30 cycles at 60s = every 30min
       await self._check_funding_rates()
   ```

---

### Issue 10 — Position State: Track Arb Alerts in `paper_positions.json`

**What:** Extend `agent/position_manager.py` to persist arb alerts and funding opportunities
to `data/paper_positions.json` so they survive restarts and are visible in the journal.

**Why:** Right now `paper_positions.json` only tracks directional positions. For the hackathon
leaderboard you want to show "here are 47 arb opportunities our agent detected, here's what
the estimated PnL would have been." This is your proof-of-concept even in paper mode.

**Where to get info:**
- `agent/position_manager.py` — `_load_state`, `_save_state` methods
- `data/paper_positions.json` — existing schema to extend
- `data/journal.py` — add `log_arb_alert` function parallel to `log_closed_trade`

**How to implement:**

1. Open `agent/position_manager.py`. In `_load_state()`, add to the default state dict:
   ```python
   "arb_alerts": [],        # list of ArbOpportunity dicts
   "arb_stats": {
       "total_alerts": 0,
       "total_estimated_pnl": 0.0,
       "best_gap_pct": 0.0,
       "best_gap_symbol": "",
   },
   "funding_alerts": [],    # list of FundingOpportunity dicts
   ```

2. Add method `record_arb_alert(opportunity_dict: dict)`:
   ```python
   def record_arb_alert(self, opp: dict):
       self._state["arb_alerts"].append(opp)
       self._state["arb_alerts"] = self._state["arb_alerts"][-200:]  # keep last 200
       stats = self._state["arb_stats"]
       stats["total_alerts"] += 1
       stats["total_estimated_pnl"] += opp.get("estimated_profit_usd", 0)
       if opp.get("net_gap_pct", 0) > stats["best_gap_pct"]:
           stats["best_gap_pct"] = opp["net_gap_pct"]
           stats["best_gap_symbol"] = opp["symbol"]
       self._save_state()
   ```

3. Open `data/journal.py`. Add `log_arb_alert(alert: dict)` function:
   - Appends to `data/journal/arb_alerts.jsonl`
   - Fields: timestamp, symbol, kraken_price, dex_price, net_gap_pct, direction,
     estimated_profit_usd, paper_mode=True

4. Update `print_performance_summary()` in `data/journal.py` to also print arb summary:
   ```
   == ARB OPPORTUNITIES (paper) ==
   Total alerts: 47
   Estimated PnL if live: $284.50
   Best gap seen: ETH +0.61% net
   ```

---

### Issue 11 — README + `replit.md` Update

**What:** Update `README.md` and `replit.md` to document the new arbitrage architecture,
paper guard behavior, new env vars, and how to move to production.

**Why:** Required for hackathon submission. Judges read the README. Also required by the
hackathon's build-in-public culture (social engagement score).

**How to implement:**

1. Add new section to `README.md`:

   ```markdown
   ## Arbitrage Engine (CEX-DEX)

   ArbMind v2 adds a signal-based CEX-DEX arbitrage detector running as a fourth async loop.

   ### Paper mode behavior
   When `PAPER_MODE=true` (default), the arb engine:
   - Scans Kraken spot vs Aerodrome (Base) every 10 seconds
   - Logs every opportunity with estimated PnL to console + `data/paper_positions.json`
   - Exposes alerts at `GET /api/arb/alerts`
   - Does NOT place any orders

   When `PAPER_MODE=false`:
   - Places simultaneous spot order on Kraken + swap on Aerodrome
   - Records actual realized PnL

   ### Moving to production
   1. Set `PAPER_MODE=false` in `.env`
   2. Add a QuickNode Base RPC endpoint (for Aerodrome web3 calls)
   3. Add wallet private key for Aerodrome swap signing
   4. Reduce `ARB_MIN_SIZE_USD` to 100 for first week live

   ### New env vars
   | Variable | Default | Description |
   |---|---|---|
   | `ARB_SCAN_INTERVAL` | `10` | Arb scan frequency (seconds) |
   | `ARB_MIN_NET_GAP_PCT` | `0.004` | Min 0.4% net gap to alert |
   | `ARB_MIN_LIQUIDITY_USD` | `500000` | Min DEX pool liquidity |
   | `ARB_MAX_POSITION_USD` | `500` | Max capital per arb trade |
   | `FUNDING_MIN_RATE_8H` | `0.0003` | Min funding rate to alert |
   | `DEXSCREENER_TTL` | `15` | DexScreener cache TTL (seconds) |
   ```

2. Update `replit.md` package structure section to add new files.

3. Update `requirements.txt` to add any new deps (only `requests` is new, already present).

---

## Build Order

Follow this sequence — each issue depends on the previous:

```
Issue 1  →  Issue 2  →  Issue 3  →  Issue 4
                              ↓
                         Issue 5 (paper guard)
                              ↓
                         Issue 6 (arb loop)
                              ↓
        Issue 7 ──────── Issue 8 ──────── Issue 10
        (AI context)  (API endpoints)  (persistence)
                              ↓
                         Issue 9 (funding harvest — parallel)
                              ↓
                         Issue 11 (docs)
```

**Estimated build time:** 2–3 days working through issues in order.

**Minimum viable for demo:** Issues 1, 2, 3, 5, 6 — this gives you a working arb scanner with
paper alerts running in 4 async loops. Issues 4, 7, 8, 9, 10, 11 add polish and hackathon
scoring surface.

---

## Going Live Checklist

When you are satisfied with paper results and want real execution:

- [ ] Set `PAPER_MODE=false` in `.env`
- [ ] Get QuickNode Base endpoint (`https://www.quicknode.com/`) — free tier available
- [ ] Set `BASE_RPC_URL` env var to your QuickNode endpoint
- [ ] Generate a separate wallet for arb (never use main wallet)
- [ ] Set `ARB_WALLET_PRIVATE_KEY` env var (keep it in Replit Secrets, never in code)
- [ ] Fund arb wallet with small USDC amount on Base (~$50 to start)
- [ ] Set `ARB_MAX_POSITION_USD=50` for first live week
- [ ] Implement `_live_execute()` in `arb_executor.py` using `web3.py`:
  ```python
  pip install web3
  from web3 import Web3
  w3 = Web3(Web3.HTTPProvider(os.getenv("BASE_RPC_URL")))
  ```
- [ ] Aerodrome Router ABI: `https://github.com/aerodrome-finance/contracts`
- [ ] Test with 1 USDC swap first before scaling up
- [ ] Monitor `GET /api/arb/stats` for actual vs estimated PnL drift
