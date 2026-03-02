#!/usr/bin/env node
/**
 * xmtp_listener.js — Streams the OnlyMonkes XMTP group and sends a one-time
 * welcome message the first time each user ever logs in.
 *
 * Started automatically by bot.py. Restarts itself if it crashes.
 */

"use strict";

const path           = require("path");
const fs             = require("fs");
const { Client }     = require("@xmtp/node-sdk");
const { encodeText } = require("@xmtp/node-bindings");
const { ethers }     = require("ethers");
require("dotenv").config();

// ─── Paths / constants ────────────────────────────────────────────────────────

const KEY_FILE      = path.join(__dirname, ".xmtp_bot_key");
const DB_FILE       = path.join(__dirname, ".xmtp_bot.db3");
const WELCOMED_FILE = path.join(__dirname, ".xmtp_welcomed.json");
const BOT_NAME      = "AI Agent #9385";

// ─── Persistent welcomed-users set ───────────────────────────────────────────
// Survives process restarts so each user is only welcomed once, ever.

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

// ─── Identity helpers (shared with xmtp_sender.js) ───────────────────────────

function getOrCreateWallet() {
  if (fs.existsSync(KEY_FILE)) {
    const key = fs.readFileSync(KEY_FILE, "utf8").trim();
    return new ethers.Wallet(key);
  }
  const wallet = ethers.Wallet.createRandom();
  fs.writeFileSync(KEY_FILE, wallet.privateKey, { mode: 0o600 });
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

// ─── Seed welcomed set from group history ─────────────────────────────────────
// Run once on startup so restarting the bot never re-welcomes existing users.

async function seedFromHistory(group, welcomed, botInboxId) {
  try {
    await group.sync();
    const history = await group.messages({ limit: 500 });
    let seeded = 0;
    for (const msg of history) {
      const content =
        typeof msg.content === "string"
          ? msg.content
          : (msg.content?.text ?? "");
      if (!content.startsWith("PROFILE_UPDATE:")) continue;
      const inboxId = msg.senderInboxId ?? "";
      if (!inboxId || inboxId === botInboxId) continue;
      if (!welcomed.has(inboxId)) {
        welcomed.add(inboxId);
        seeded++;
      }
    }
    if (seeded > 0) saveWelcomed(welcomed);
    console.log(`[xmtp_listener] Seeded ${seeded} users from history (${welcomed.size} total welcomed).`);
  } catch (err) {
    console.warn("[xmtp_listener] Could not seed from history:", err.message);
  }
}

// ─── Main ─────────────────────────────────────────────────────────────────────

async function main() {
  const groupId = process.env.XMTP_GROUP_ID;
  if (!groupId) {
    console.error("[xmtp_listener] XMTP_GROUP_ID not set in .env");
    process.exit(1);
  }

  const wallet     = getOrCreateWallet();
  const client     = await getClient(wallet);
  const botInboxId = client.inboxId;
  const welcomed   = loadWelcomed();

  await client.conversations.sync();
  const groups = await client.conversations.listGroups();
  const group  = groups.find((g) => g.id === groupId);
  if (!group) {
    console.error(`[xmtp_listener] Group ${groupId.slice(0, 12)}… not found.`);
    process.exit(1);
  }

  // Seed from history so restarts don't re-welcome existing users
  await seedFromHistory(group, welcomed, botInboxId);

  console.log(`[xmtp_listener] Streaming group — ${welcomed.size} users already welcomed.`);

  for await (const message of await group.stream()) {
    try {
      const content =
        typeof message.content === "string"
          ? message.content
          : (message.content?.text ?? "");

      if (!content.startsWith("PROFILE_UPDATE:")) continue;

      const data     = JSON.parse(content.slice("PROFILE_UPDATE:".length));
      const inboxId  = data.id  ?? "";
      const username = (data.u  ?? "").trim();

      // Skip the bot's own profile broadcasts and blank usernames
      if (!inboxId || inboxId === botInboxId) continue;
      if (!username) continue;

      // Already welcomed this user — skip
      if (welcomed.has(inboxId)) continue;

      welcomed.add(inboxId);
      saveWelcomed(welcomed);

      const welcome = `Welcome home, GMonke ${username} 🐒`;
      await group.send(encodeText(`MSG:${BOT_NAME}:${welcome}`));
      console.log(`[xmtp_listener] First-time welcome: ${username}`);
    } catch (err) {
      console.warn("[xmtp_listener] Message error:", err.message);
    }
  }
}

main().catch((err) => {
  console.error("[xmtp_listener] Fatal:", err.message);
  process.exit(1);
});
