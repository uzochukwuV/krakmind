# ArbMind: ERC-8004 Hackathon Integration Guide

This document outlines the step-by-step technical implementation plan to integrate the ArbMind autonomous trading agent with the official ERC-8004 Hackathon shared infrastructure on the Sepolia Testnet.

## 🔗 Official Shared Contracts (Sepolia)
- **Network:** Sepolia Testnet (Chain ID: `11155111`)
- **AgentRegistry:** `0x97b07dDc405B0c28B17559aFFE63BdB3632d0ca3`
- **HackathonVault:** `0x0E7CD8ef9743FEcf94f9103033a044caBD45fC90`
- **RiskRouter:** `0xd6A6952545FF6E6E6681c2d15C59f9EB8F40FdBC`
- **ReputationRegistry:** `0x423a9904e39537a9997fbaF0f220d79D7d545763`
- **ValidationRegistry:** `0x92bF63E5C7Ac6980f237a7164Ab413BE226187F1`

---

## 🛠 Integration Roadmap

### Step 1: Environment & Wallet Configuration
ArbMind currently supports Web3 execution via the `ARB_WALLET_PRIVATE_KEY` in the `.env` file. 
1. **Update `.env`**: Add the Sepolia RPC URL and the Hackathon contract addresses.
   ```env
   SEPOLIA_RPC_URL="https://sepolia.infura.io/v3/YOUR_INFURA_KEY"
   AGENT_REGISTRY_ADDR="0x97b07dDc405B0c28B17559aFFE63BdB3632d0ca3"
   HACKATHON_VAULT_ADDR="0x0E7CD8ef9743FEcf94f9103033a044caBD45fC90"
   RISK_ROUTER_ADDR="0xd6A6952545FF6E6E6681c2d15C59f9EB8F40FdBC"
   VALIDATION_REGISTRY_ADDR="0x92bF63E5C7Ac6980f237a7164Ab413BE226187F1"
   ```

### Step 2: Agent Registration & Capital Allocation (Pre-flight)
Before the `loop.py` main cycle starts, ArbMind needs to register itself and claim sandbox capital.
1. **Create `hackathon_client.py`**: Build a Web3 helper class to interact with the ERC-8004 contracts.
2. **Registration**: Call `registerAgent()` on the `AgentRegistry` contract to obtain the unique `agentId`.
3. **Claim Capital**: Call `claimAllocation(agentId)` on the `HackathonVault` to receive the 0.05 ETH sandbox capital.
4. **State Update**: Store the `agentId` in `api/shared_state.py` so the executor can reference it during trades.

### Step 3: Routing Trades through RiskRouter
ArbMind currently executes DEX trades directly in `agent/arb_executor.py` (`_execute_dex_leg`) and CEX trades via Kraken CLI. For the hackathon, **all trade intents must flow through the RiskRouter**.
1. **Modify `ArbExecutor`**: Update `_atomic_execute()` and `_execute_dex_leg()` to route the Aerodrome swap payload through `RiskRouter.submitTradeIntent(agentId, targetContract, payload)`.
2. **Modify `PositionManager`**: If directional mean-reversion trades (Kraken Futures) are part of the judging criteria, we must find a way to submit intent representations to the `RiskRouter` or mock them via Sepolia DEX equivalents, as the RiskRouter is strictly an on-chain proxy.

### Step 4: Posting Validation Checkpoints
The hackathon requires agents to post their reasoning and decisions to the `ValidationRegistry` after every trade.
1. **Intercept AI Decisions**: In `agent/ai_brain.py` (`analyze_and_decide()`), capture the `reasoning` and `confidence` score from the LLM output.
2. **Intercept Arb Executions**: In `agent/arb_executor.py`, capture the `net_gap_pct` and `direction`.
3. **Post Checkpoint**: Inside `agent/position_manager.py` (specifically within `open_position()` and `close_position()`), add a Web3 call to `ValidationRegistry.postCheckpoint(agentId, ipfsHashOrString)`.
   *Note: To save gas, we can compress the AI reasoning into a short string or upload the full JSON decision to IPFS/Arweave and post the CID to the registry.*

### Step 5: Tracking Reputation
The `ReputationRegistry` accumulates reputation based on the validity of checkpoints and profitability.
1. **Update Dashboard**: Modify `api/server.py` to expose an `/api/reputation` endpoint.
2. **Fetch On-Chain Data**: The `hackathon_client.py` will periodically poll `ReputationRegistry.getReputationScore(agentId)` and inject it into `shared_state` so it appears live on the ArbMind frontend.

---

## 🚦 Execution Flow Summary

1. **Boot**: `ArbMind` boots -> Registers on `AgentRegistry` -> Claims 0.05 ETH from `HackathonVault`.
2. **Scan**: `ArbLoop` finds a +1.5% CEX-DEX gap.
3. **Execute**: `ArbExecutor` packages the swap payload -> Sends tx to `RiskRouter`.
4. **Validate**: `PositionManager` takes the trade details -> Sends tx to `ValidationRegistry`.
5. **Score**: Hackathon judges read the `ReputationRegistry` leaderboard automatically.

*All required contract ABIs should be pulled from Etherscan using the verified source code links provided in the brief.*
