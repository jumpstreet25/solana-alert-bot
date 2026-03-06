#!/usr/bin/env node
/**
 * xmtp_agent.js — Full AI-powered XMTP agent for OnlyMonkes.
 *
 * Responsibilities:
 *   1. HTTP server on :3001 — receives TA alerts from bot.py and sends them to the group
 *   2. Group stream      — welcomes new users on PROFILE_UPDATE
 *   3. DM stream         — answers user questions via Claude AI (claude-sonnet-4-6)
 *
 * First-time setup (same as xmtp_sender.js):
 *   node xmtp_agent.js --setup   → prints bot inbox ID
 *   Add bot to group via OnlyMonkes Admin Panel → Add User Manually
 *
 * Usage:
 *   node xmtp_agent.js            → start the agent
 *   node xmtp_agent.js --setup    → print inbox ID (no server started)
 *   node xmtp_agent.js --set-pfp  → broadcast bot profile to group
 */

"use strict";

const path        = require("path");
const fs          = require("fs");
const express     = require("express");
const { Client }  = require("@xmtp/node-sdk");
const { encodeText } = require("@xmtp/node-bindings");
const { ethers }  = require("ethers");
const Anthropic   = require("@anthropic-ai/sdk");
require("dotenv").config();

// ─── Constants ────────────────────────────────────────────────────────────────

const KEY_FILE       = path.join(__dirname, ".xmtp_bot_key");
const DB_FILE        = path.join(__dirname, ".xmtp_bot.db3");
const WELCOMED_FILE  = path.join(__dirname, ".xmtp_welcomed.json");
const BOT_STATE_FILE = path.join(__dirname, "bot_state.json");
const BOT_NAME       = "AI Agent #9385";
const BOT_IMAGE      = "https://i.imgur.com/Igyhf3p.jpeg";
const GROUP_ID       = process.env.XMTP_GROUP_ID;
const HTTP_PORT      = parseInt(process.env.AGENT_HTTP_PORT || "3001", 10);
const MAX_DM_HIST    = 20; // max message pairs in memory per DM

const anthropic = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY });

// In-memory DM history: conversationId -> [{role, content}]
const dmHistory = new Map();

// ─── Welcomed-users set ───────────────────────────────────────────────────────

function loadWelcomed() {
  try {
    return new Set(JSON.parse(fs.readFileSync(WELCOMED_FILE, "utf8")));
  } catch {
    return new Set();
  }
}

function saveWelcomed(set) {
  fs.writeFileSync(WELCOMED_FILE, JSON.stringify([...set]), "utf8");
}

// ─── bot_state.json (written by bot.py each cycle) ───────────────────────────

function readBotState() {
  try {
    return JSON.parse(fs.readFileSync(BOT_STATE_FILE, "utf8"));
  } catch {
    return null;
  }
}

// ─── Identity helpers (shared with xmtp_sender.js) ───────────────────────────

function getOrCreateWallet() {
  if (fs.existsSync(KEY_FILE)) {
    const key = fs.readFileSync(KEY_FILE, "utf8").trim();
    return new ethers.Wallet(key);
  }
  const wallet = ethers.Wallet.createRandom();
  fs.writeFileSync(KEY_FILE, wallet.privateKey, { mode: 0o600 });
  console.log("[xmtp_agent] New bot identity created and saved to", KEY_FILE);
  return wallet;
}

function deriveEncryptionKey(wallet) {
  const raw = ethers.getBytes(
    ethers.keccak256(ethers.toUtf8Bytes("xmtp:enc:" + wallet.address))
  );
  return new Uint8Array(raw);
}

async function getClient(wallet) {
  const signer = {
    type: "EOA",
    getIdentifier: () => ({
      identifier:     wallet.address.toLowerCase(),
      identifierKind: 0,
    }),
    signMessage: async (msg) => ethers.getBytes(await wallet.signMessage(msg)),
  };
  return Client.create(signer, {
    env:             "production",
    dbPath:          DB_FILE,
    dbEncryptionKey: deriveEncryptionKey(wallet),
  });
}

// ─── Message helpers ──────────────────────────────────────────────────────────

function extractContent(message) {
  return typeof message.content === "string"
    ? message.content
    : (message.content?.text ?? "");
}

/** Parse MSG:<username>:<content> — returns { username, content } */
function parseMsg(raw) {
  if (!raw.startsWith("MSG:")) return { username: "?", content: raw };
  const rest     = raw.slice(4);
  const colonIdx = rest.indexOf(":");
  if (colonIdx < 0) return { username: "?", content: raw };
  return { username: rest.slice(0, colonIdx), content: rest.slice(colonIdx + 1) };
}

/** Send a formatted bot message to any XMTP conversation or group */
async function botSend(conversation, text) {
  await conversation.send(encodeText(`MSG:${BOT_NAME}:${text}`));
}

// ─── Claude AI integration ────────────────────────────────────────────────────

function buildSystemPrompt(botState) {
  let marketCtx = "";
  if (botState && Array.isArray(botState.tokens) && botState.tokens.length > 0) {
    marketCtx += "\n\n=== LIVE MARKET DATA (updated every 2 min) ===";
    for (const t of botState.tokens) {
      const fmt = (n) => (n || 0).toLocaleString("en-US", { maximumFractionDigits: 6 });
      marketCtx += `\n\n${t.symbol}${t.name ? ` (${t.name})` : ""}`;
      marketCtx += `\n  Price:     $${fmt(t.price)}`;
      marketCtx += `\n  RSI:       ${(t.rsi || 0).toFixed(1)}`;
      marketCtx += `\n  Score:     ${t.score || 0}/7 bullish  |  ${t.bearish_score || 0} bearish`;
      marketCtx += `\n  1h change: ${(t.price_change_1h || 0).toFixed(2)}%`;
      marketCtx += `\n  5m change: ${(t.price_change_5m || 0).toFixed(2)}%`;
      marketCtx += `\n  Vol 24h:   $${(t.volume_24h || 0).toLocaleString()}`;
      marketCtx += `\n  Liquidity: $${(t.liquidity_usd || 0).toLocaleString()}`;
      if (t.is_bullish)  marketCtx += `\n  ✅ BULLISH SIGNAL ACTIVE`;
      if (t.is_bearish)  marketCtx += `\n  ⚠️  BEARISH SIGNAL ACTIVE`;
      if (t.sell_pct != null) marketCtx += `\n  📉 Sell recommendation: ${t.sell_pct}% of position`;
      if (t.reasons?.length)         marketCtx += `\n  Bullish: ${t.reasons.join(" | ")}`;
      if (t.bearish_reasons?.length) marketCtx += `\n  Bearish: ${t.bearish_reasons.join(" | ")}`;
    }
    if (botState.last_updated) marketCtx += `\n\nLast analysis: ${botState.last_updated}`;
  } else {
    marketCtx = "\n\nMarket data not yet available (bot may be starting up — try again shortly).";
  }

  return `You are AI Agent #9385, a Solana DeFi trading assistant embedded in the OnlyMonkes community chat on XMTP. You have access to real-time technical analysis (TA) for watched Solana SPL tokens.

CAPABILITIES:
• Explain TA signals (MACD golden/death cross, RSI, EMA9/21 crossovers, EMA200 trend, volume surges)
• Give buy / hold / sell / stop-loss recommendations grounded in the data provided
• Recommend position sizing and what % of holdings to sell based on RSI overbought + bearish momentum
• Answer questions about Solana DeFi, wallets, DEXes (Raydium, Orca, Jupiter), and trading strategies
• Alert about bearish trend changes and suggest when to de-risk

STOP-LOSS GUIDANCE:
• Conservative: 3–5% below entry
• Moderate: below EMA21 on 15m
• Wide: below EMA200 on 1h (macro stop)

SELL % GUIDANCE (from bot_state data):
• Mild caution (sell_pct 25%): RSI overbought + early bearish signals
• Medium caution (sell_pct 50%): RSI very overbought + confirmed bearish confluence
• Strong caution (sell_pct 75%): RSI extremely overbought + strong bearish reversal signals

RULES:
• Always append "⚠️ Not financial advice — DYOR." at the end of trade recommendations
• Base all analysis on the live data provided above — never speculate beyond TA
• If a user asks about a token NOT in the watch list, say you don't have live data for it
• Keep replies under 200 words unless depth is clearly needed
• This is a chat app — be concise and direct, no lengthy preambles
${marketCtx}`;
}

async function askClaude(conversationId, userMessage) {
  const botState    = readBotState();
  const systemPrompt = buildSystemPrompt(botState);

  if (!dmHistory.has(conversationId)) {
    dmHistory.set(conversationId, []);
  }
  const history = dmHistory.get(conversationId);

  history.push({ role: "user", content: userMessage });
  // Keep bounded
  while (history.length > MAX_DM_HIST * 2) history.splice(0, 2);

  try {
    const response = await anthropic.messages.create({
      model:      "claude-sonnet-4-6",
      max_tokens: 512,
      system:     systemPrompt,
      messages:   history,
    });
    const reply = response.content[0]?.text ?? "I couldn't generate a response. Please try again.";
    history.push({ role: "assistant", content: reply });
    return reply;
  } catch (err) {
    console.error("[xmtp_agent] Claude error:", err.message);
    return "I'm having trouble reaching my AI backend right now. Please try again in a moment.";
  }
}

// ─── Seed welcomed users from history ────────────────────────────────────────

async function seedFromHistory(group, welcomed, botInboxId) {
  try {
    await group.sync();
    const msgs = await group.messages({ limit: 500 });
    let seeded = 0;
    for (const msg of msgs) {
      const content = extractContent(msg);
      if (!content.startsWith("PROFILE_UPDATE:")) continue;
      const inboxId = msg.senderInboxId ?? "";
      if (!inboxId || inboxId === botInboxId) continue;
      if (!welcomed.has(inboxId)) { welcomed.add(inboxId); seeded++; }
    }
    if (seeded > 0) saveWelcomed(welcomed);
    console.log(`[xmtp_agent] Seeded ${seeded} users from history (${welcomed.size} total welcomed).`);
  } catch (err) {
    console.warn("[xmtp_agent] Could not seed from history:", err.message);
  }
}

// ─── Group message stream ─────────────────────────────────────────────────────

async function streamGroupMessages(group, botInboxId, welcomed) {
  console.log("[xmtp_agent] Streaming group messages…");
  for await (const message of await group.stream()) {
    try {
      const raw      = extractContent(message);
      const senderId = message.senderInboxId ?? "";
      if (senderId === botInboxId) continue; // skip own messages

      // ── Welcome new users ──────────────────────────────────────────────────
      if (raw.startsWith("PROFILE_UPDATE:")) {
        const data     = JSON.parse(raw.slice("PROFILE_UPDATE:".length));
        const inboxId  = data.id  ?? "";
        const username = (data.u ?? "").trim();
        if (!inboxId || inboxId === botInboxId || !username) continue;
        if (welcomed.has(inboxId)) continue;
        welcomed.add(inboxId);
        saveWelcomed(welcomed);
        await botSend(group, `Welcome home, GMonke ${username} 🐒`);
        console.log(`[xmtp_agent] Welcomed: ${username}`);
        continue;
      }

      // ── Respond to @-mentions in the group ────────────────────────────────
      if (raw.startsWith("MSG:")) {
        const { username, content } = parseMsg(raw);
        const lower = content.toLowerCase();
        const mentioned = lower.includes("@ai agent") ||
                          lower.includes("@9385")      ||
                          lower.includes("@bot");
        if (!mentioned) continue;

        const userContent = content
          .replace(/@ai agent|@9385|@bot/gi, "")
          .trim();
        if (!userContent) continue;

        console.log(`[xmtp_agent] @mention from ${username}: ${userContent.slice(0, 80)}`);
        const reply = await askClaude(`group-${senderId}`, userContent);
        await botSend(group, reply);
      }
    } catch (err) {
      console.warn("[xmtp_agent] Group msg error:", err.message);
    }
  }
}

// ─── DM message stream ────────────────────────────────────────────────────────

async function streamDmMessages(client, botInboxId) {
  console.log("[xmtp_agent] Streaming DM messages…");
  for await (const message of await client.conversations.streamAllDmMessages()) {
    try {
      const senderId = message.senderInboxId ?? "";
      if (senderId === botInboxId) continue;

      const raw = extractContent(message);
      if (!raw || raw.startsWith("TYPING:")) continue;

      const { content: userContent } = parseMsg(raw);
      const trimmed = userContent.trim();
      if (!trimmed) continue;

      const conversationId = message.conversationId;
      console.log(`[xmtp_agent] DM [${conversationId.slice(0, 8)}…]: ${trimmed.slice(0, 80)}`);

      const reply = await askClaude(conversationId, trimmed);
      const dm    = await client.conversations.getConversationById(conversationId);
      if (dm) {
        await botSend(dm, reply);
      }
    } catch (err) {
      console.warn("[xmtp_agent] DM msg error:", err.message);
    }
  }
}

// ─── HTTP server (receives TA alerts from bot.py) ─────────────────────────────

function startHttpServer(group) {
  const app = express();
  app.use(express.json({ limit: "64kb" }));

  // POST /alert — send alert to the XMTP group
  app.post("/alert", async (req, res) => {
    const { message, type } = req.body ?? {};
    if (!message) {
      return res.status(400).json({ error: "missing 'message' field" });
    }
    try {
      await botSend(group, message);
      console.log(`[xmtp_agent] Alert [${type ?? "?"}]: ${String(message).slice(0, 60)}…`);
      res.json({ ok: true });
    } catch (err) {
      console.error("[xmtp_agent] Alert send failed:", err.message);
      res.status(500).json({ error: err.message });
    }
  });

  // GET /health — simple liveness check
  app.get("/health", (_req, res) => res.json({ ok: true, bot: BOT_NAME }));

  const server = app.listen(HTTP_PORT, "127.0.0.1", () => {
    console.log(`[xmtp_agent] HTTP server listening on 127.0.0.1:${HTTP_PORT}`);
  });
  server.on("error", (err) => {
    if (err.code === "EADDRINUSE") {
      console.error(`[xmtp_agent] Port ${HTTP_PORT} already in use — exiting so bot.py can restart cleanly`);
      process.exit(1);
    }
    throw err;
  });
}

// ─── Setup / set-pfp commands ────────────────────────────────────────────────

async function setup() {
  const wallet = getOrCreateWallet();
  const client = await getClient(wallet);
  console.log("\n✓ Bot XMTP identity ready.");
  console.log(`\n  Inbox ID:\n  ${client.inboxId}\n`);
  console.log("  Steps:");
  console.log("  1. Open OnlyMonkes → tap 👥 → Admin Panel → 'Add User Manually'");
  console.log("     → paste the Inbox ID above → tap Add User");
  console.log("  2. Set XMTP_GROUP_ID and ANTHROPIC_API_KEY in your .env\n");
}

async function setPfp() {
  if (!GROUP_ID) { console.error("[xmtp_agent] XMTP_GROUP_ID not set"); process.exit(1); }
  const wallet = getOrCreateWallet();
  const client = await getClient(wallet);
  await client.conversations.sync();
  const groups = await client.conversations.listGroups();
  const group  = groups.find((g) => g.id === GROUP_ID);
  if (!group) { console.error(`[xmtp_agent] Group ${GROUP_ID.slice(0, 12)}… not found`); process.exit(1); }
  const payload = JSON.stringify({
    id: client.inboxId, u: BOT_NAME, b: "", x: "", w: "", tw: "", ni: BOT_IMAGE,
  });
  await group.send(encodeText(`PROFILE_UPDATE:${payload}`));
  console.log("[xmtp_agent] Bot PFP broadcast to group.");
}

// ─── Main ─────────────────────────────────────────────────────────────────────

async function main() {
  const args = process.argv.slice(2);
  if (args[0] === "--setup")   { await setup();  return; }
  if (args[0] === "--set-pfp") { await setPfp(); return; }

  if (!GROUP_ID) {
    console.error("[xmtp_agent] XMTP_GROUP_ID not set in .env");
    process.exit(1);
  }
  if (!process.env.ANTHROPIC_API_KEY) {
    console.warn("[xmtp_agent] ⚠️  ANTHROPIC_API_KEY not set — DM AI responses will fail");
  }

  const wallet     = getOrCreateWallet();
  const client     = await getClient(wallet);
  const botInboxId = client.inboxId;
  const welcomed   = loadWelcomed();

  console.log(`[xmtp_agent] Bot inbox ID: ${botInboxId}`);

  await client.conversations.sync();
  const groups = await client.conversations.listGroups();
  const group  = groups.find((g) => g.id === GROUP_ID);
  if (!group) {
    console.error(
      `[xmtp_agent] Group ${GROUP_ID.slice(0, 12)}… not found.\n` +
      "  Run: node xmtp_agent.js --setup  then add the bot to the group."
    );
    process.exit(1);
  }

  await seedFromHistory(group, welcomed, botInboxId);
  startHttpServer(group);

  console.log("[xmtp_agent] Ready — streaming group and DMs.");
  await Promise.all([
    streamGroupMessages(group, botInboxId, welcomed),
    streamDmMessages(client, botInboxId),
  ]);
}

main().catch((err) => {
  console.error("[xmtp_agent] Fatal:", err.message);
  process.exit(1);
});
