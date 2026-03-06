#!/usr/bin/env node
/**
 * xmtp_sender.js — Send a trading alert to the OnlyMonkes XMTP group chat.
 *
 * FIRST-TIME SETUP:
 *   1. node xmtp_sender.js --setup
 *      → prints the bot's XMTP inbox ID
 *   2. Open OnlyMonkes on your admin device → Admin Panel → "Add User Manually"
 *      → paste the bot inbox ID → tap Add User
 *   3. Add XMTP_GROUP_ID to your .env file (copy the group ID from your
 *      GitHub config file: https://raw.githubusercontent.com/jumpstreet25/OnlyMonkes/master/app.config.json)
 *
 * USAGE (called automatically by bot.py):
 *   node xmtp_sender.js "message text"
 */

"use strict";

const path    = require("path");
const fs      = require("fs");
const { Client }     = require("@xmtp/node-sdk");
const { encodeText } = require("@xmtp/node-bindings");
const { ethers }     = require("ethers");
require("dotenv").config();

// ─── Paths ───────────────────────────────────────────────────────────────────

const KEY_FILE  = path.join(__dirname, ".xmtp_bot_key");
const DB_FILE   = path.join(__dirname, ".xmtp_bot.db3");
const BOT_NAME  = "AI Agent #9385";
const BOT_IMAGE = "https://i.imgur.com/Igyhf3p.jpeg";

// ─── Identity helpers ─────────────────────────────────────────────────────────

function getOrCreateWallet() {
  if (fs.existsSync(KEY_FILE)) {
    const key = fs.readFileSync(KEY_FILE, "utf8").trim();
    return new ethers.Wallet(key);
  }
  const wallet = ethers.Wallet.createRandom();
  fs.writeFileSync(KEY_FILE, wallet.privateKey, { mode: 0o600 });
  console.log("[xmtp_sender] New bot identity created and saved to", KEY_FILE);
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
      identifierKind: 0, // Ethereum EOA
    }),
    signMessage: async (msg) => {
      const sig = await wallet.signMessage(msg);
      return ethers.getBytes(sig);
    },
  };
  return Client.create(signer, {
    env:             "production",
    dbPath:          DB_FILE,
    dbEncryptionKey: deriveEncryptionKey(wallet),
  });
}

// ─── Commands ─────────────────────────────────────────────────────────────────

async function setup() {
  const wallet = getOrCreateWallet();
  const client = await getClient(wallet);
  console.log("\n✓ Bot XMTP identity ready.");
  console.log(`\n  Inbox ID:\n  ${client.inboxId}\n`);
  console.log("  Steps:");
  console.log("  1. Open OnlyMonkes on your admin device → tap 👥 → Admin Panel");
  console.log('     → "Add User Manually" → paste the Inbox ID above → tap Add User');
  console.log("  2. Add to your .env file:");
  console.log("     XMTP_GROUP_ID=<group ID from GitHub app.config.json>\n");
}

async function setPfp() {
  const groupId = process.env.XMTP_GROUP_ID;
  if (!groupId) {
    console.error("[xmtp_sender] Error: XMTP_GROUP_ID is not set in .env");
    process.exit(1);
  }

  const wallet = getOrCreateWallet();
  const client = await getClient(wallet);

  await client.conversations.sync();
  const groups = await client.conversations.listGroups();
  const group  = groups.find((g) => g.id === groupId);
  if (!group) {
    console.error(`[xmtp_sender] Group ${groupId.slice(0, 12)}… not found.`);
    process.exit(1);
  }

  const payload = JSON.stringify({
    id: client.inboxId,
    u: BOT_NAME,
    b: "",
    x: "",
    w: "",
    tw: "",
    ni: BOT_IMAGE,
  });
  await group.send(encodeText(`PROFILE_UPDATE:${payload}`));
  console.log("[xmtp_sender] Bot PFP broadcast to group (Saga Monkes #9385).");
}

async function sendAlert(message) {
  const groupId = process.env.XMTP_GROUP_ID;
  if (!groupId) {
    console.error("[xmtp_sender] Error: XMTP_GROUP_ID is not set in .env");
    process.exit(1);
  }

  const wallet = getOrCreateWallet();
  const client = await getClient(wallet);

  await client.conversations.sync();
  const groups = await client.conversations.listGroups();
  const group  = groups.find((g) => g.id === groupId);
  if (!group) {
    console.error(
      `[xmtp_sender] Group ${groupId.slice(0, 12)}… not found.\n` +
      "  Has the bot been added as a member? Run: node xmtp_sender.js --setup"
    );
    process.exit(1);
  }

  // MSG:<username>:<content> — matches the app's message format so it renders
  // with the bot username in the chat bubble.
  const packed = `MSG:${BOT_NAME}:${message}`;
  await group.send(encodeText(packed));
  console.log("[xmtp_sender] Alert delivered to OnlyMonkes chat.");
}

// ─── Entry point ──────────────────────────────────────────────────────────────

async function main() {
  const args = process.argv.slice(2);
  if (args[0] === "--setup") {
    await setup();
  } else if (args[0] === "--set-pfp") {
    await setPfp();
  } else if (args.length > 0) {
    await sendAlert(args.join(" "));
  } else {
    console.log("Usage:");
    console.log("  node xmtp_sender.js --setup     → first-time setup, prints inbox ID");
    console.log("  node xmtp_sender.js --set-pfp   → broadcast bot PFP (Saga Monkes #9385)");
    console.log('  node xmtp_sender.js "message"   → send alert to group (called by bot.py)');
  }
}

main().catch((err) => {
  console.error("[xmtp_sender] Fatal:", err.message);
  process.exit(1);
});
