# solana-alert-bot

> Original Python TA engine + Node.js XMTP bot powering **AI Agent #9385** in the OnlyMonkes community on Solana Mobile.

---

## Overview

Two-process architecture:

| Process | File | Role |
|---|---|---|
| **TA Engine** | `bot.py` | Python — scans Solana tokens every cycle, computes RSI/MACD/EMA, posts alerts to xmtp_agent.js via HTTP |
| **XMTP Agent** | `xmtp_agent.js` | Node.js — XMTP group+DM bot, AI (Claude), slash commands, FCM push notifications |

> **Note:** The ElizaOS-based replacement lives at [github.com/jumpstreet25/Monke_Eliza](https://github.com/jumpstreet25/Monke_Eliza). Both can run simultaneously — this bot handles legacy Python TA while Monke_Eliza runs the new TypeScript TA Savvy Monke scanner.

---

## Features

- **Bearish/Bullish scoring** — EMA death cross, MACD crossovers, RSI thresholds, volume analysis
- **Saga Monke NFT sale alerts** — monitors Magic Eden for new sales
- **AI DM responses** — Claude AI answers questions in private DMs
- **FCM push notifications** — direct Firebase Cloud Messaging (no Expo relay); sends heads-up alerts to all registered device tokens
- **Slash commands** in group chat and DMs:
  - `/tip @Username [amt]` — $SKR tip via Solana Pay deep link
  - `/buy $TOKEN [SOL]` — Jupiter swap link
  - `/sell $TOKEN [%]` — Jupiter swap link
  - `/swap $A for $B` — Jupiter aggregator
  - `/help` — command list
- **Profile tracking** — captures wallet addresses from `PROFILE_UPDATE` for tip routing
- **bot_state.json** — written after each TA cycle; read by Monke_Eliza's botStateProvider for LLM context

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/jumpstreet25/solana-alert-bot.git
cd solana-alert-bot

# 2. Install Node deps
npm install

# 3. Install Python deps
pip install -r requirements.txt   # or: pip install xmtp-mls anthropic aiohttp

# 4. Configure
cp .env.example .env
# Fill in ANTHROPIC_API_KEY, FCM_SERVER_KEY, XMTP_GROUP_ID

# 5. First-time XMTP setup (prints bot inbox ID)
node xmtp_agent.js --setup

# 6. Broadcast PFP
node xmtp_agent.js --set-pfp

# 7. Start both processes
./start.sh
```

---

## Environment Variables

| Variable | Description |
|---|---|
| `XMTP_GROUP_ID` | OnlyMonkes XMTP group ID |
| `ANTHROPIC_API_KEY` | Claude API key for DM AI responses |
| `FCM_SERVER_KEY` | Firebase Cloud Messaging server key (direct push, no Expo relay) |
| `JUP_API_KEY` | Jupiter API key for swap links |
| `SKR_MINT` | $SKR token mint address |
| `DEV_WALLET` | Dev wallet for tips + Jupiter referral |
| `POLL_INTERVAL` | TA scan interval in seconds (default: 120) |
| `ALERT_COOLDOWN_MINUTES` | Cooldown between alerts for same token (default: 60) |
| `MIN_SCORE` | Min signal score to alert (default: 5) |

---

## Bearish Signal Logic

Score components (each −1 or −2):

| Condition | Score |
|---|---|
| Price below EMA 200 | −2 |
| MACD death cross | −2 |
| RSI < 50 | −1 |
| EMA death cross (20 below 50) | −1 |
| Volume spike on red candle | −1 |

**Alert threshold:** ≤ −3

**Sell size suggestions:**
- RSI > 65 + score ≤ −2 → 25%
- RSI > 75 + score ≤ −3 → 50%
- RSI > 75 + score ≤ −4 → 75%

---

## XMTP Message Format

All messages sent as: `MSG:AI Agent #9385:<content>`

This format is required by the OnlyMonkes app to display messages with the correct sender name and cached NFT avatar.

---

## Files

| File | Description |
|---|---|
| `bot.py` | Python TA engine — scans tokens, writes `bot_state.json`, POSTs to agent |
| `xmtp_agent.js` | Node.js XMTP service — group stream, DM stream, HTTP server, FCM push |
| `start.sh` | Starts both processes |
| `bot_state.json` | Live TA state (written by bot.py, read by ElizaOS botStateProvider) |
| `.xmtp_bot_key` | Bot ETH private key (shared with Monke_Eliza for same XMTP identity) |

---

## License

MIT
