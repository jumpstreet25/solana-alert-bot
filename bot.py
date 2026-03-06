#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════╗
║                     SOLANA TOKEN ALERT BOT  🚀                         ║
║         High-conviction bullish TA signals for SPL tokens               ║
╚══════════════════════════════════════════════════════════════════════════╝

HOW TO USE ON MACOS
───────────────────
1. Install Python 3.11+:
       brew install python@3.11

2. Create a virtual environment and activate it:
       python3 -m venv venv && source venv/bin/activate

3. Install dependencies:
       pip install -r requirements.txt

4. Copy and fill in environment variables:
       cp .env.example .env
       # Edit .env with your FCM / Telegram credentials

5. Add tokens to watch (auto-discovers the best pool):
       python bot.py add EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v
       python bot.py add <YOUR_TOKEN_MINT> --symbol SOL --pool <POOL_ADDR>

6. Verify the watch list:
       python bot.py list

7. Run a one-shot TA check (good for testing):
       python bot.py test

8. Start the bot:
       python bot.py
       # Or in a persistent tmux session (recommended):
       tmux new -s solana-bot
       python bot.py
       # Ctrl+B then D to detach; `tmux attach -t solana-bot` to re-attach


FINDING POOL ADDRESSES (if auto-discovery fails)
─────────────────────────────────────────────────
1. Go to https://dexscreener.com and search for the token by name or mint.
2. Click on the pair you want (highest volume / liquidity pair).
3. The URL contains the pool address:
       https://dexscreener.com/solana/<POOL_ADDRESS>
4. Pass it when adding the token:
       python bot.py add <MINT> --pool <POOL_ADDRESS>


FIREBASE CLOUD MESSAGING SETUP
────────────────────────────────
1. Create a Firebase project at https://console.firebase.google.com
2. Go to Project Settings → Service Accounts → Generate new private key.
3. Save the downloaded JSON as  firebase-credentials.json  in this folder.
4. In your mobile app, subscribe to the topic "solana_alerts":
       FirebaseMessaging.getInstance().subscribeToTopic("solana_alerts")
   Or use FCM_DEVICE_TOKEN in .env for a specific device.
5. Set FCM_CREDENTIALS_FILE in .env (default: firebase-credentials.json).


TELEGRAM SETUP (optional)
──────────────────────────
1. Message @BotFather → /newbot → copy the token.
2. Set TELEGRAM_BOT_TOKEN in .env.
3. Message @userinfobot to get your numeric chat/user ID.
4. Set TELEGRAM_CHAT_ID in .env.
   No extra packages needed — the bot uses the Telegram HTTP API directly.


SIGNAL SCORING (max 7 points — alert fires at >= 5)
─────────────────────────────────────────────────────
  +2  1h  Price > EMA(200)           — strong macro uptrend
  +2  15m MACD(12,26,9) golden cross — momentum turning bullish
  +1  15m RSI(14) crosses above 40   — or >50 and rising (not >75)
  +1  15m EMA(9) crosses above EMA(21)
  +1  15m Volume > 1.5× 20-period avg — institutional interest

DATA SOURCES (100% free, no API keys required)
───────────────────────────────────────────────
  • DexScreener  https://api.dexscreener.com   — real-time price/volume/liquidity
  • GeckoTerminal https://api.geckoterminal.com — historical OHLCV candles
"""

import argparse
import asyncio
import concurrent.futures
import json
import logging
import os
import subprocess
import sys
import time
import warnings

# Silence urllib3's complaint about LibreSSL on older macOS — purely cosmetic,
# all HTTPS requests work fine.
warnings.filterwarnings("ignore", message="urllib3 v2.0 only supports OpenSSL")
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# asyncio.to_thread was added in Python 3.9; polyfill for 3.7/3.8
if not hasattr(asyncio, "to_thread"):
    _executor = concurrent.futures.ThreadPoolExecutor()
    async def _to_thread(fn, *args, **kwargs):
        loop = asyncio.get_event_loop()
        import functools
        return await loop.run_in_executor(_executor, functools.partial(fn, *args, **kwargs))
    asyncio.to_thread = _to_thread  # type: ignore

import re
import requests
import pandas as pd
from dotenv import load_dotenv

# TA indicators — using the `ta` library (pip install ta); compatible with Python 3.7+
import ta as ta_lib

# ── Load .env file ────────────────────────────────────────────────────────────
load_dotenv()

# ── Optional: Firebase Admin SDK ──────────────────────────────────────────────
try:
    import firebase_admin
    from firebase_admin import credentials as fb_credentials, messaging
    _FCM_AVAILABLE = True
except ImportError:
    _FCM_AVAILABLE = False

# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-5s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", mode="a", encoding="utf-8"),
    ],
)
log = logging.getLogger("solana-bot")

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Config:
    # ── Polling ───────────────────────────────────────────────────────────────
    poll_interval: int = int(os.getenv("POLL_INTERVAL", "120"))       # seconds
    alert_cooldown: int = int(os.getenv("ALERT_COOLDOWN_MINUTES", "60"))  # minutes

    # ── Signal thresholds ─────────────────────────────────────────────────────
    min_score: int        = int(os.getenv("MIN_SCORE", "5"))     # out of 7
    rsi_min: float        = 40.0    # RSI must cross above this
    rsi_max: float        = 75.0    # Ignore if already overbought
    vol_multiplier: float = 1.5     # Volume > X × 20-period average

    # ── Indicator periods ─────────────────────────────────────────────────────
    ema200_period: int   = 200
    ema_fast: int        = 9
    ema_slow: int        = 21
    macd_fast: int       = 12
    macd_slow: int       = 26
    macd_signal: int     = 9
    rsi_period: int      = 14
    vol_avg_period: int  = 20

    # ── Candle counts to fetch ────────────────────────────────────────────────
    candles_15m: int = 300   # ~3 days
    candles_1h:  int = 220   # ~9 days (need 200 for EMA200)

    # ── API base URLs ─────────────────────────────────────────────────────────
    gecko_base: str = "https://api.geckoterminal.com/api/v2"
    dex_base:   str = "https://api.dexscreener.com/latest/dex"

    # ── HTTP ──────────────────────────────────────────────────────────────────
    request_timeout: int  = 15
    max_retries: int      = 3
    retry_base_delay: float = 2.0

    # ── Firebase ──────────────────────────────────────────────────────────────
    fcm_creds_file:   str = os.getenv("FCM_CREDENTIALS_FILE", "firebase-credentials.json")
    fcm_topic:        str = os.getenv("FCM_TOPIC", "solana_alerts")
    fcm_device_token: str = os.getenv("FCM_DEVICE_TOKEN", "")

    # ── Telegram ──────────────────────────────────────────────────────────────
    telegram_token:   str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # ── Storage ───────────────────────────────────────────────────────────────
    tokens_file: str = os.getenv("TOKENS_FILE", "tokens.json")


CFG = Config()


# ═══════════════════════════════════════════════════════════════════════════════
# DATA MODELS
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class TokenInfo:
    """Persisted token watch entry."""
    mint:             str
    symbol:           str   = "UNKNOWN"
    name:             str   = "Unknown"
    pool_address:     str   = ""
    chain:            str   = "solana"
    last_alert_time:  float = 0.0
    enabled:          bool  = True

    # Keep only recognised fields when loading from JSON (forward-compat).
    _FIELDS = {"mint", "symbol", "name", "pool_address", "chain",
               "last_alert_time", "enabled"}

    @classmethod
    def from_dict(cls, d: dict) -> "TokenInfo":
        return cls(**{k: v for k, v in d.items() if k in cls._FIELDS})


@dataclass
class TAResult:
    """Outcome of a single TA computation pass."""
    token: TokenInfo

    # Real-time market data
    price:             float = 0.0
    price_change_5m:   float = 0.0
    price_change_1h:   float = 0.0
    volume_24h:        float = 0.0
    liquidity_usd:     float = 0.0

    # Individual signal flags
    above_ema200_1h:   bool = False
    macd_cross_15m:    bool = False
    rsi_signal_15m:    bool = False
    ema_cross_15m:     bool = False
    volume_surge:      bool = False

    # Diagnostic values
    rsi_value:         float = 0.0
    macd_histogram:    float = 0.0
    score:             int   = 0
    reasons:           List[str] = field(default_factory=list)

    # Bearish signals (negative scoring)
    bearish_score:     int   = 0
    bearish_reasons:   List[str] = field(default_factory=list)
    sell_pct:          Optional[int] = None   # 25 / 50 / 75 recommendation

    @property
    def is_bullish(self) -> bool:
        return self.score >= CFG.min_score

    @property
    def is_bearish(self) -> bool:
        """Bearish confluence fires when bearish_score <= -3."""
        return self.bearish_score <= -3


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

class _RateLimiter:
    """Minimum-interval token bucket (thread-safe enough for single thread)."""

    def __init__(self, calls_per_second: float):
        self._interval = 1.0 / calls_per_second
        self._last = 0.0

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last
        if elapsed < self._interval:
            time.sleep(self._interval - elapsed)
        self._last = time.monotonic()


# GeckoTerminal public tier is ~15 req/min; leave headroom at 0.18 req/s (~11/min)
# DexScreener is more generous — 0.8 req/s is fine
_gecko_rl = _RateLimiter(calls_per_second=0.18)
_dex_rl   = _RateLimiter(calls_per_second=0.8)

_HEADERS = {"Accept": "application/json", "User-Agent": "SolanaAlertBot/1.0"}


def _get(
    url:     str,
    params:  Optional[dict] = None,
    limiter: Optional[_RateLimiter] = None,
) -> Optional[dict]:
    """
    HTTP GET with automatic retry + exponential back-off.
    Returns parsed JSON dict, or None on failure.
    """
    for attempt in range(CFG.max_retries):
        try:
            if limiter:
                limiter.wait()
            resp = requests.get(
                url,
                params=params,
                headers=_HEADERS,
                timeout=CFG.request_timeout,
            )

            if resp.status_code == 429:
                wait = 5 * (2 ** attempt)
                log.warning("Rate-limited by %s — sleeping %ds", url, wait)
                time.sleep(wait)
                continue

            if resp.status_code == 404:
                log.debug("404 from %s", url)
                return None

            resp.raise_for_status()
            return resp.json()

        except requests.exceptions.RequestException as exc:
            delay = CFG.retry_base_delay * (2 ** attempt)
            log.warning(
                "Request failed (attempt %d/%d, retry in %.1fs): %s — %s",
                attempt + 1, CFG.max_retries, delay, url, exc,
            )
            if attempt < CFG.max_retries - 1:
                time.sleep(delay)

    return None


# ═══════════════════════════════════════════════════════════════════════════════
# DEXSCREENER  —  real-time price / volume / pool discovery
# ═══════════════════════════════════════════════════════════════════════════════

class DexScreener:
    """
    Free public API. No key needed.
    Docs: https://docs.dexscreener.com/
    """

    def _pairs_for_mint(self, mint: str) -> List[dict]:
        """Return all Solana pairs for a token mint, sorted by USD liquidity."""
        data = _get(f"{CFG.dex_base}/tokens/{mint}", limiter=_dex_rl)
        if not data:
            return []
        pairs = [
            p for p in (data.get("pairs") or [])
            if p.get("chainId") == "solana"
        ]
        pairs.sort(
            key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0),
            reverse=True,
        )
        return pairs

    def best_pool(self, mint: str) -> Optional[Tuple[str, dict]]:
        """
        Return (pool_address, pair_dict) for the most liquid Solana pool,
        or None if no pairs found.
        """
        pairs = self._pairs_for_mint(mint)
        if not pairs:
            return None
        best = pairs[0]
        return best.get("pairAddress", ""), best

    def realtime(self, mint: str) -> Optional[dict]:
        """
        Fetch real-time snapshot for the best pool.
        Returns a normalised dict or None.
        """
        pairs = self._pairs_for_mint(mint)
        if not pairs:
            return None
        p = pairs[0]

        try:
            pc = p.get("priceChange") or {}
            vol = p.get("volume") or {}
            liq = p.get("liquidity") or {}
            return {
                "price":             float(p.get("priceUsd") or 0),
                "price_change_5m":   float(pc.get("m5") or 0),
                "price_change_1h":   float(pc.get("h1") or 0),
                "price_change_24h":  float(pc.get("h24") or 0),
                "volume_24h":        float(vol.get("h24") or 0),
                "liquidity_usd":     float(liq.get("usd") or 0),
                "symbol":            p.get("baseToken", {}).get("symbol", ""),
                "name":              p.get("baseToken", {}).get("name", ""),
                "pool_address":      p.get("pairAddress", ""),
                "dex_id":            p.get("dexId", ""),
            }
        except (KeyError, ValueError, TypeError) as exc:
            log.error("DexScreener parse error for %s: %s", mint[:8], exc)
            return None


# ═══════════════════════════════════════════════════════════════════════════════
# GECKOTERMINAL  —  historical OHLCV candles
# ═══════════════════════════════════════════════════════════════════════════════

class GeckoTerminal:
    """
    Free public API. No key needed.
    Docs: https://www.geckoterminal.com/dex-api

    OHLCV endpoint:
        GET /networks/{network}/pools/{pool}/ohlcv/{timeframe}
        ?aggregate=15&limit=300&currency=usd&token=base

    Supported timeframes: 'minute' | 'hour' | 'day'
    Supported aggregates: minute → 1,5,15; hour → 1,4,12; day → 1
    Response ohlcv_list: [[unix_seconds, open, high, low, close, volume], ...]
    """

    _NETWORK = "solana"

    def ohlcv(
        self,
        pool_address: str,
        timeframe:    str = "minute",
        aggregate:    int = 15,
        limit:        int = 300,
    ) -> Optional[pd.DataFrame]:
        """
        Fetch candles and return a DataFrame (oldest → newest) with columns:
          timestamp (UTC datetime), open, high, low, close, volume
        Returns None on failure or empty data.
        """
        url = (
            f"{CFG.gecko_base}/networks/{self._NETWORK}"
            f"/pools/{pool_address}/ohlcv/{timeframe}"
        )
        params = {
            "aggregate": aggregate,
            "limit":     limit,
            "currency":  "usd",
            "token":     "base",
        }
        data = _get(url, params=params, limiter=_gecko_rl)
        if not data:
            return None

        try:
            rows = (
                data.get("data", {})
                    .get("attributes", {})
                    .get("ohlcv_list", [])
            )
            if not rows:
                log.debug("GeckoTerminal: empty ohlcv_list for pool %s…", pool_address[:8])
                return None

            df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
            df = df.sort_values("timestamp").reset_index(drop=True)
            df[["open", "high", "low", "close", "volume"]] = (
                df[["open", "high", "low", "close", "volume"]].astype(float)
            )
            log.debug(
                "GeckoTerminal: %d ×%d%s candles for pool %s…",
                len(df), aggregate, timeframe[:1].upper(), pool_address[:8],
            )
            return df

        except (KeyError, ValueError, AttributeError) as exc:
            log.error("GeckoTerminal parse error (pool %s…): %s", pool_address[:8], exc)
            return None


# ═══════════════════════════════════════════════════════════════════════════════
# TECHNICAL ANALYSIS ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

class TAEngine:
    """
    Computes bullish confluence signals using the `ta` library + plain pandas.

    Scoring:
      +2  1h   Price > EMA(200)            macro uptrend
      +2  15m  MACD(12,26,9) golden cross   momentum
      +1  15m  RSI(14) crosses >40 or >50 rising (not >75)
      +1  15m  EMA(9) crosses above EMA(21)
      +1  15m  Volume > 1.5× 20-candle avg
      ──
      7   max   (alert fires at score >= CFG.min_score, default 5)
    """

    def __init__(self):
        self._dex   = DexScreener()
        self._gecko = GeckoTerminal()

    # ── Public ────────────────────────────────────────────────────────────────

    def compute(self, token: TokenInfo) -> Optional[TAResult]:
        """
        Run the full pipeline for one token.
        Returns TAResult (check .is_bullish) or None if data unavailable.
        """
        result = TAResult(token=token)

        # ── Step 1: real-time data from DexScreener ───────────────────────────
        rt = self._dex.realtime(token.mint)
        if rt:
            result.price           = rt["price"]
            result.price_change_5m = rt["price_change_5m"]
            result.price_change_1h = rt["price_change_1h"]
            result.volume_24h      = rt["volume_24h"]
            result.liquidity_usd   = rt["liquidity_usd"]
            if rt["symbol"]: token.symbol = rt["symbol"]
            if rt["name"]:   token.name   = rt["name"]

        # ── Step 2: 1h candles for EMA(200) ───────────────────────────────────
        df_1h = self._gecko.ohlcv(
            token.pool_address, timeframe="hour", aggregate=1, limit=CFG.candles_1h
        )
        self._check_ema200(result, df_1h)

        # ── Step 3: 15m candles (fall back to 1h if unavailable) ─────────────
        df_15m = self._gecko.ohlcv(
            token.pool_address, timeframe="minute", aggregate=15, limit=CFG.candles_15m
        )

        if df_15m is not None and len(df_15m) >= 50:
            tf_label = "15m"
        else:
            # GeckoTerminal rate-limits the /minute endpoint harder than /hour.
            # Fall back to 1h candles — coarser but still valid signals.
            log.warning(
                "15m data unavailable for %s — falling back to 1h candles for signals",
                token.symbol,
            )
            df_15m = df_1h  # reuse the already-fetched 1h frame
            tf_label = "1h"
            if df_15m is None or len(df_15m) < 50:
                log.warning("Insufficient data for %s — skipping", token.symbol)
                return None

        self._add_indicators(df_15m)
        self._check_macd(result, df_15m, tf_label)
        self._check_rsi(result, df_15m, tf_label)
        self._check_ema_cross(result, df_15m, tf_label)
        self._check_volume(result, df_15m, tf_label)

        # ── Step 4: bearish signals ───────────────────────────────────────────
        self._check_bear_ema200(result, df_1h)
        self._check_bear_macd(result, df_15m, tf_label)
        self._check_bear_rsi(result, df_15m, tf_label)
        self._check_bear_ema_cross(result, df_15m, tf_label)
        self._check_bear_volume_down(result, df_15m, tf_label)
        self._compute_sell_pct(result)

        return result

    # ── Indicator computation (ta library + plain pandas EWM) ─────────────────

    @staticmethod
    def _ema(series: pd.Series, period: int) -> pd.Series:
        """Exponential moving average via pandas ewm (identical to ta library)."""
        return series.ewm(span=period, adjust=False).mean()

    def _add_indicators(self, df: pd.DataFrame) -> None:
        """Compute and attach MACD, RSI, EMA9, EMA21 columns in-place."""
        close = df["close"]

        # MACD
        macd_obj = ta_lib.trend.MACD(
            close,
            window_slow=CFG.macd_slow,
            window_fast=CFG.macd_fast,
            window_sign=CFG.macd_signal,
        )
        df["macd"]      = macd_obj.macd()
        df["macd_sig"]  = macd_obj.macd_signal()
        df["macd_hist"] = macd_obj.macd_diff()

        # RSI
        df["rsi"] = ta_lib.momentum.RSIIndicator(close, window=CFG.rsi_period).rsi()

        # EMAs
        df["ema_fast"] = self._ema(close, CFG.ema_fast)
        df["ema_slow"] = self._ema(close, CFG.ema_slow)

    # ── 1h EMA200 (plain pandas — no ta library needed) ───────────────────────

    def _check_ema200(self, result: TAResult, df: Optional[pd.DataFrame]) -> None:
        if df is None or len(df) < CFG.ema200_period:
            log.debug(
                "Skipping EMA200 for %s (need %d 1h candles, have %d)",
                result.token.symbol, CFG.ema200_period,
                len(df) if df is not None else 0,
            )
            return

        ema200    = self._ema(df["close"], CFG.ema200_period).iloc[-1]
        last_close = df["close"].iloc[-1]

        if pd.isna(ema200):
            return

        if last_close > ema200:
            result.above_ema200_1h = True
            result.score += 2
            pct = (last_close / ema200 - 1) * 100
            result.reasons.append(
                f"Price {pct:+.1f}% above EMA200 on 1h (macro uptrend confirmed)"
            )

    # ── 15m signal checkers ───────────────────────────────────────────────────

    def _check_macd(self, result: TAResult, df: pd.DataFrame, tf: str = "15m") -> None:
        curr_macd, prev_macd = df["macd"].iloc[-1],      df["macd"].iloc[-2]
        curr_sig,  prev_sig  = df["macd_sig"].iloc[-1],  df["macd_sig"].iloc[-2]
        curr_hist, prev_hist = df["macd_hist"].iloc[-1], df["macd_hist"].iloc[-2]

        if any(pd.isna(v) for v in [curr_macd, prev_macd, curr_sig, prev_sig]):
            return

        result.macd_histogram = float(curr_hist) if pd.notna(curr_hist) else 0.0

        just_crossed   = curr_macd > curr_sig and prev_macd <= prev_sig
        hist_expanding = (
            pd.notna(curr_hist) and pd.notna(prev_hist)
            and curr_hist > 0 and curr_hist > prev_hist
            and curr_macd > curr_sig
        )

        if just_crossed or hist_expanding:
            result.macd_cross_15m = True
            result.score += 2
            kind = "golden cross" if just_crossed else "bullish momentum"
            result.reasons.append(
                f"MACD {kind} on {tf} (hist: {result.macd_histogram:+.6f})"
            )

    def _check_rsi(self, result: TAResult, df: pd.DataFrame, tf: str = "15m") -> None:
        curr = df["rsi"].iloc[-1]
        prev = df["rsi"].iloc[-2]

        if pd.isna(curr) or pd.isna(prev):
            return

        result.rsi_value = float(curr)

        cross_above_40  = prev < CFG.rsi_min <= curr <= CFG.rsi_max
        above_50_rising = 50 < curr <= CFG.rsi_max and curr > prev

        if cross_above_40:
            result.rsi_signal_15m = True
            result.score += 1
            result.reasons.append(
                f"RSI crossed above {CFG.rsi_min:.0f} ({prev:.1f} → {curr:.1f}) on {tf}"
            )
        elif above_50_rising:
            result.rsi_signal_15m = True
            result.score += 1
            result.reasons.append(f"RSI >50 and rising ({curr:.1f}) on {tf}")

    def _check_ema_cross(self, result: TAResult, df: pd.DataFrame, tf: str = "15m") -> None:
        cf, pf = df["ema_fast"].iloc[-1], df["ema_fast"].iloc[-2]
        cs, ps = df["ema_slow"].iloc[-1], df["ema_slow"].iloc[-2]

        if any(pd.isna(v) for v in [cf, pf, cs, ps]):
            return

        just_crossed = cf > cs and pf <= ps
        gap_widening = cf > cs and (cf - cs) > (pf - ps)

        if just_crossed or gap_widening:
            result.ema_cross_15m = True
            result.score += 1
            kind = "crossed" if just_crossed else "widening gap"
            result.reasons.append(
                f"EMA{CFG.ema_fast} {kind} above EMA{CFG.ema_slow} on {tf}"
            )

    def _check_volume(self, result: TAResult, df: pd.DataFrame, tf: str = "15m") -> None:
        if len(df) <= CFG.vol_avg_period:
            return

        avg  = df["volume"].iloc[-(CFG.vol_avg_period + 1):-1].mean()
        curr = df["volume"].iloc[-1]

        if pd.isna(avg) or avg <= 0 or pd.isna(curr):
            return

        ratio = curr / avg
        if ratio >= CFG.vol_multiplier:
            result.volume_surge = True
            result.score += 1
            result.reasons.append(
                f"Volume surge {ratio:.1f}× above {CFG.vol_avg_period}-candle avg on {tf}"
            )

    # ── Bearish signal checkers ───────────────────────────────────────────────
    # Mirror of bullish checkers — each subtracts from bearish_score.
    #
    # Scoring:
    #   -2  1h   Price < EMA(200)                 macro downtrend confirmed
    #   -2  15m  MACD death cross                 momentum turning bearish
    #   -1  15m  RSI drops below 50 or < 40       weakening momentum
    #   -1  15m  EMA(9) crosses below EMA(21)     short-term trend reversal
    #   -1  15m  Volume surge on a down candle    sellers stepping in
    #   ───
    #   -7  min  (bearish alert fires at score <= -3)

    def _check_bear_ema200(self, result: TAResult, df: Optional[pd.DataFrame]) -> None:
        if df is None or len(df) < CFG.ema200_period:
            return
        ema200     = self._ema(df["close"], CFG.ema200_period).iloc[-1]
        last_close = df["close"].iloc[-1]
        if pd.isna(ema200):
            return
        if last_close < ema200:
            result.bearish_score -= 2
            pct = (1 - last_close / ema200) * 100
            result.bearish_reasons.append(
                f"Price {pct:.1f}% below EMA200 on 1h (macro downtrend)"
            )

    def _check_bear_macd(self, result: TAResult, df: pd.DataFrame, tf: str = "15m") -> None:
        curr_macd, prev_macd = df["macd"].iloc[-1],     df["macd"].iloc[-2]
        curr_sig,  prev_sig  = df["macd_sig"].iloc[-1], df["macd_sig"].iloc[-2]
        curr_hist, prev_hist = df["macd_hist"].iloc[-1], df["macd_hist"].iloc[-2]
        if any(pd.isna(v) for v in [curr_macd, prev_macd, curr_sig, prev_sig]):
            return
        death_cross      = curr_macd < curr_sig and prev_macd >= prev_sig
        hist_contracting = (
            pd.notna(curr_hist) and pd.notna(prev_hist)
            and curr_hist < 0 and curr_hist < prev_hist
            and curr_macd < curr_sig
        )
        if death_cross or hist_contracting:
            result.bearish_score -= 2
            kind = "death cross" if death_cross else "bearish momentum"
            result.bearish_reasons.append(
                f"MACD {kind} on {tf} (hist: {float(curr_hist):+.6f})"
            )

    def _check_bear_rsi(self, result: TAResult, df: pd.DataFrame, tf: str = "15m") -> None:
        curr = df["rsi"].iloc[-1]
        prev = df["rsi"].iloc[-2]
        if pd.isna(curr) or pd.isna(prev):
            return
        # Ensure rsi_value is populated (may already be set by bullish check)
        if result.rsi_value == 0.0:
            result.rsi_value = float(curr)
        cross_below_50 = prev > 50 >= curr
        below_40       = curr < 40 and curr < prev
        if cross_below_50:
            result.bearish_score -= 1
            result.bearish_reasons.append(
                f"RSI crossed below 50 ({prev:.1f} → {curr:.1f}) on {tf}"
            )
        elif below_40:
            result.bearish_score -= 1
            result.bearish_reasons.append(f"RSI below 40 and falling ({curr:.1f}) on {tf}")

    def _check_bear_ema_cross(self, result: TAResult, df: pd.DataFrame, tf: str = "15m") -> None:
        cf, pf = df["ema_fast"].iloc[-1], df["ema_fast"].iloc[-2]
        cs, ps = df["ema_slow"].iloc[-1], df["ema_slow"].iloc[-2]
        if any(pd.isna(v) for v in [cf, pf, cs, ps]):
            return
        death_cross  = cf < cs and pf >= ps
        gap_widening = cf < cs and (cs - cf) > (ps - pf)
        if death_cross or gap_widening:
            result.bearish_score -= 1
            kind = "death cross" if death_cross else "widening bearish gap"
            result.bearish_reasons.append(
                f"EMA{CFG.ema_fast} {kind} below EMA{CFG.ema_slow} on {tf}"
            )

    def _check_bear_volume_down(self, result: TAResult, df: pd.DataFrame, tf: str = "15m") -> None:
        """Volume surge on a down candle = institutional selling."""
        if len(df) <= CFG.vol_avg_period:
            return
        avg       = df["volume"].iloc[-(CFG.vol_avg_period + 1):-1].mean()
        curr_vol  = df["volume"].iloc[-1]
        curr_open = df["open"].iloc[-1]
        curr_cls  = df["close"].iloc[-1]
        if pd.isna(avg) or avg <= 0 or pd.isna(curr_vol):
            return
        is_red_candle = curr_cls < curr_open
        ratio         = curr_vol / avg
        if is_red_candle and ratio >= CFG.vol_multiplier:
            result.bearish_score -= 1
            result.bearish_reasons.append(
                f"Volume surge {ratio:.1f}× on a down candle on {tf} (selling pressure)"
            )

    def _compute_sell_pct(self, result: TAResult) -> None:
        """
        Recommend what % of a position to sell based on RSI overbought + bearish score.
        Only fires when there are at least some bearish signals.
        """
        if result.bearish_score >= 0:
            return
        rsi = result.rsi_value
        bs  = result.bearish_score
        if rsi > 75 and bs <= -4:
            result.sell_pct = 75
        elif rsi > 75 and bs <= -3:
            result.sell_pct = 50
        elif rsi > 65 and bs <= -2:
            result.sell_pct = 25
        elif bs <= -5:
            result.sell_pct = 50


# ═══════════════════════════════════════════════════════════════════════════════
# ALERT SENDER
# ═══════════════════════════════════════════════════════════════════════════════

class AlertSender:
    """Dispatches alerts via console, FCM push, and/or Telegram."""

    def __init__(self):
        self._fcm_ready = False
        self._init_fcm()

    def _init_fcm(self) -> None:
        if not _FCM_AVAILABLE:
            log.info("firebase-admin not installed — FCM disabled")
            return
        if not Path(CFG.fcm_creds_file).exists():
            log.warning(
                "FCM credentials not found at '%s' — FCM disabled. "
                "Download from Firebase Console → Project Settings → Service Accounts.",
                CFG.fcm_creds_file,
            )
            return
        try:
            cred = fb_credentials.Certificate(CFG.fcm_creds_file)
            firebase_admin.initialize_app(cred)
            self._fcm_ready = True
            log.info("Firebase Admin SDK initialised — FCM ready")
        except Exception as exc:
            log.error("Firebase init failed: %s", exc)

    # ── Formatting helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _fmt_price(price: float) -> str:
        if price == 0:
            return "$0"
        if price < 0.000001:
            return f"${price:.10f}"
        if price < 0.001:
            return f"${price:.8f}"
        if price < 1:
            return f"${price:.6f}"
        return f"${price:,.4f}"

    def _title_body(self, r: TAResult) -> Tuple[str, str]:
        title = f"🚀 {r.token.symbol} Bullish Signal  [{r.score}/7]"
        body = "\n".join([
            f"Price: {self._fmt_price(r.price)}  ({r.price_change_1h:+.1f}% 1h / {r.price_change_5m:+.1f}% 5m)",
            f"RSI: {r.rsi_value:.1f}  |  Vol 24h: ${r.volume_24h:,.0f}  |  Liq: ${r.liquidity_usd:,.0f}",
            "",
            *[f"• {reason}" for reason in r.reasons],
        ])
        return title, body

    def _xmtp_message(self, r: TAResult) -> str:
        """Format a bullish alert as a single string for the OnlyMonkes chat."""
        lines = [
            f"🚀 {r.token.symbol} Bullish Signal  [{r.score}/7]",
            f"Price: {self._fmt_price(r.price)}  ({r.price_change_1h:+.1f}% 1h / {r.price_change_5m:+.1f}% 5m)",
            f"RSI: {r.rsi_value:.1f}  |  Vol 24h: ${r.volume_24h:,.0f}  |  Liq: ${r.liquidity_usd:,.0f}",
            f"CA: {r.token.mint}",
            "",
            *[f"• {reason}" for reason in r.reasons],
        ]
        return "\n".join(lines)

    def _xmtp_bearish_message(self, r: TAResult) -> str:
        """Format a bearish alert for the OnlyMonkes chat."""
        sell_note = f"\n⚠️ Consider selling {r.sell_pct}% of your {r.token.symbol} position." if r.sell_pct else ""
        lines = [
            f"⚠️ {r.token.symbol} Bearish Signal  [{r.bearish_score}]",
            f"Price: {self._fmt_price(r.price)}  ({r.price_change_1h:+.1f}% 1h / {r.price_change_5m:+.1f}% 5m)",
            f"RSI: {r.rsi_value:.1f}  |  Vol 24h: ${r.volume_24h:,.0f}  |  Liq: ${r.liquidity_usd:,.0f}",
            f"CA: {r.token.mint}",
            "",
            *[f"• {reason}" for reason in r.bearish_reasons],
        ]
        if sell_note:
            lines.append(sell_note)
        return "\n".join(lines)

    # ── Console ────────────────────────────────────────────────────────────────

    def console(self, r: TAResult) -> None:
        sep = "═" * 62
        title, body = self._title_body(r)
        print(f"\n{sep}")
        print(f"  {title}")
        print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  |  Mint: {r.token.mint}")
        print(sep)
        for line in body.splitlines():
            print(f"  {line}")
        print(f"{sep}\n")

    # ── FCM ────────────────────────────────────────────────────────────────────

    def fcm(self, r: TAResult) -> None:
        if not self._fcm_ready:
            return
        title, body = self._title_body(r)
        data_payload = {
            "mint":   r.token.mint,
            "symbol": r.token.symbol,
            "price":  str(r.price),
            "score":  str(r.score),
        }
        notification = messaging.Notification(title=title, body=body)

        if CFG.fcm_device_token:
            msg = messaging.Message(
                notification=notification,
                data=data_payload,
                token=CFG.fcm_device_token,
            )
        else:
            msg = messaging.Message(
                notification=notification,
                data=data_payload,
                topic=CFG.fcm_topic,
            )
        try:
            resp = messaging.send(msg)
            log.info("FCM sent: %s", resp)
        except Exception as exc:
            log.error("FCM send failed for %s: %s", r.token.symbol, exc)

    # ── Telegram (raw HTTP — no extra packages) ────────────────────────────────

    def telegram(self, r: TAResult) -> None:
        if not CFG.telegram_token or not CFG.telegram_chat_id:
            return
        title, body = self._title_body(r)
        # Escape underscores for Markdown
        text = f"*{title}*\n\n{body}".replace("_", r"\_")
        url  = f"https://api.telegram.org/bot{CFG.telegram_token}/sendMessage"
        try:
            resp = requests.post(
                url,
                json={"chat_id": CFG.telegram_chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=10,
            )
            resp.raise_for_status()
            log.info("Telegram message sent to chat %s", CFG.telegram_chat_id)
        except Exception as exc:
            log.error("Telegram send failed for %s: %s", r.token.symbol, exc)

    # ── XMTP via HTTP (xmtp_agent.js server on :3001) ─────────────────────────

    def _post_to_agent(self, message: str, alert_type: str = "alert") -> bool:
        """
        POST an alert to the xmtp_agent HTTP server.
        Returns True on success.  Falls back to console-only on failure.
        """
        import urllib.request as _urllib_req
        url  = "http://127.0.0.1:3001/alert"
        data = json.dumps({"message": message, "type": alert_type}).encode()
        try:
            req = _urllib_req.Request(
                url, data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with _urllib_req.urlopen(req, timeout=10):
                pass
            return True
        except Exception as exc:
            log.error("Failed to POST alert to xmtp_agent (%s): %s", url, exc)
            return False

    def xmtp(self, r: TAResult) -> None:
        """Send a bullish alert to the OnlyMonkes XMTP group via xmtp_agent."""
        message = self._xmtp_message(r)
        if not self._post_to_agent(message, "bullish"):
            log.warning("XMTP bullish alert could not be delivered for %s", r.token.symbol)
        else:
            log.info("XMTP bullish alert delivered: %s", r.token.symbol)

    def xmtp_bearish(self, r: TAResult) -> None:
        """Send a bearish alert to the OnlyMonkes XMTP group via xmtp_agent."""
        message = self._xmtp_bearish_message(r)
        if not self._post_to_agent(message, "bearish"):
            log.warning("XMTP bearish alert could not be delivered for %s", r.token.symbol)
        else:
            log.info("XMTP bearish alert delivered: %s", r.token.symbol)

    # ── XMTP NFT sale alert ────────────────────────────────────────────────────

    def xmtp_nft_sale(self, sale: dict, name: str) -> None:
        """Send a Saga Monkes sale alert to the OnlyMonkes XMTP group."""
        buyer  = sale.get("buyer",  "")
        seller = sale.get("seller", "")
        price  = sale.get("price",  0.0)
        buyer_short  = f"{buyer[:4]}…{buyer[-4:]}"  if len(buyer)  > 8 else buyer
        seller_short = f"{seller[:4]}…{seller[-4:]}" if len(seller) > 8 else seller
        message = "\n".join([
            f"🐒 {name} just sold for {price:.4f} SOL",
            f"Buyer:  {buyer_short}",
            f"Seller: {seller_short}",
        ])
        if not self._post_to_agent(message, "nft_sale"):
            log.warning("XMTP NFT sale alert could not be delivered for %s", name)
        else:
            log.info("XMTP NFT sale alert delivered: %s", name)

    # ── Dispatch ───────────────────────────────────────────────────────────────

    def send_all(self, r: TAResult) -> None:
        """Dispatch a bullish alert via all channels."""
        self.console(r)
        self.fcm(r)
        self.telegram(r)
        self.xmtp(r)

    def send_all_bearish(self, r: TAResult) -> None:
        """Dispatch a bearish alert via console + XMTP."""
        sep = "═" * 62
        print(f"\n{sep}")
        print(f"  ⚠️  {r.token.symbol} Bearish Signal  [{r.bearish_score}]")
        print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(sep)
        price_line = (
            f"  Price: {self._fmt_price(r.price)}"
            f"  ({r.price_change_1h:+.1f}% 1h / {r.price_change_5m:+.1f}% 5m)"
        )
        print(price_line)
        print(f"  RSI: {r.rsi_value:.1f}")
        for reason in r.bearish_reasons:
            print(f"  • {reason}")
        if r.sell_pct:
            print(f"  ⚠️ Sell recommendation: {r.sell_pct}% of position")
        print(f"{sep}\n")
        self.xmtp_bearish(r)


# ═══════════════════════════════════════════════════════════════════════════════
# TOKEN STORE  —  simple JSON persistence
# ═══════════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════════
# Saga Monkes NFT Sales Monitor
# ═══════════════════════════════════════════════════════════════════════════════

class SagaMonkesSalesMonitor:
    """Polls Magic Eden for new Saga Monkes buyNow sales and fires XMTP alerts."""

    _ME_URL = (
        "https://api-mainnet.magiceden.dev/v2/collections/sagamonkes/activities"
        "?offset=0&limit=20&type=buyNow"
    )

    def __init__(self, alerts: AlertSender):
        self._alerts      = alerts
        self._seen: set   = set()
        self._initialized = False

    @staticmethod
    def _nft_name(sale: dict) -> str:
        """Derive 'MONKE #NNNN' from image URL, e.g. '.../7114.png'."""
        m = re.search(r"/(\d+)\.png$", sale.get("image", ""))
        return f"MONKE #{m.group(1)}" if m else "Saga Monke"

    def check(self) -> None:
        try:
            resp = requests.get(
                self._ME_URL,
                timeout=10,
                headers={"User-Agent": "OnlyMonkes-Bot/1.0"},
            )
            resp.raise_for_status()
            sales: List[dict] = resp.json()
        except Exception as exc:
            log.warning("SagaMonkes sales fetch failed: %s", exc)
            return

        if not sales:
            return

        # First run: seed seen set without alerting
        if not self._initialized:
            for sale in sales:
                self._seen.add(sale["signature"])
            self._initialized = True
            log.info("SagaMonkes sales monitor active — watching for new sales")
            return

        new_sales = [s for s in sales if s["signature"] not in self._seen]
        for sale in reversed(new_sales):  # oldest first
            self._seen.add(sale["signature"])
            name = self._nft_name(sale)
            log.info(
                "🐒 New Saga Monkes sale: %s  %.4f SOL  buyer %s…",
                name, sale.get("price", 0), sale.get("buyer", "?")[:8],
            )
            self._alerts.xmtp_nft_sale(sale, name)

        # Keep seen set bounded
        if len(self._seen) > 500:
            self._seen = set(list(self._seen)[-200:])


class TokenStore:
    """Loads and saves the watched-token list from a local JSON file."""

    def __init__(self, path: str = CFG.tokens_file):
        self._path = Path(path)

    def load(self) -> List[TokenInfo]:
        if not self._path.exists():
            log.info("No token store found — creating empty %s", self._path)
            self._path.write_text("[]", encoding="utf-8")
            return []
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            return [TokenInfo.from_dict(t) for t in raw]
        except json.JSONDecodeError as exc:
            log.error("Failed to parse %s: %s", self._path, exc)
            return []

    def save(self, tokens: List[TokenInfo]) -> None:
        data = [asdict(t) for t in tokens]
        self._path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def add(self, mint: str, pool_address: str = "", symbol: str = "") -> TokenInfo:
        tokens = self.load()
        for t in tokens:
            if t.mint == mint:
                log.info("Token %s is already in the watch list", mint[:12])
                return t
        token = TokenInfo(mint=mint, pool_address=pool_address, symbol=symbol or "UNKNOWN")
        tokens.append(token)
        self.save(tokens)
        log.info("Added %s to watch list", mint[:12])
        return token


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN BOT
# ═══════════════════════════════════════════════════════════════════════════════

class SolanaAlertBot:
    """
    Orchestrates periodic TA checks for all watched tokens and
    dispatches alerts when bullish confluence is detected.
    """

    def __init__(self):
        self._store       = TokenStore()
        self._dex         = DexScreener()
        self._ta          = TAEngine()
        self._alerts      = AlertSender()
        self._tokens: List[TokenInfo] = []
        self._nft_monitor = SagaMonkesSalesMonitor(self._alerts)
        # Separate cooldown tracking for bearish alerts (keyed by mint)
        self._last_bearish_alert: Dict[str, float] = {}
        # Latest TA results for bot_state.json (keyed by mint)
        self._latest_results: Dict[str, TAResult] = {}

    # ── Pool discovery ─────────────────────────────────────────────────────────

    def _resolve_pool(self, token: TokenInfo) -> bool:
        """Auto-discover the best pool address if not already set. Returns True if ready."""
        if token.pool_address:
            return True

        log.info("Discovering pool for %s (%s…)…", token.symbol, token.mint[:8])
        result = self._dex.best_pool(token.mint)
        if not result:
            log.warning("No pool found for mint %s — token will be skipped", token.mint[:8])
            return False

        pool_addr, pair = result
        token.pool_address = pool_addr
        token.symbol = pair.get("baseToken", {}).get("symbol", token.symbol)
        token.name   = pair.get("baseToken", {}).get("name",   token.name)
        # Persist the discovered pool address
        self._store.save(self._tokens)
        log.info("Discovered pool for %s: %s…", token.symbol, pool_addr[:14])
        return True

    # ── Alert gate ─────────────────────────────────────────────────────────────

    def _cooldown_ok(self, result: TAResult) -> bool:
        """Return True if enough time has passed since the last bullish alert."""
        cooldown = CFG.alert_cooldown * 60
        elapsed  = time.time() - result.token.last_alert_time
        if elapsed < cooldown:
            log.debug(
                "Cooldown active for %s (%.0f min left)",
                result.token.symbol, (cooldown - elapsed) / 60,
            )
            return False
        return True

    def _bearish_cooldown_ok(self, mint: str) -> bool:
        """Return True if enough time has passed since the last bearish alert."""
        cooldown = CFG.alert_cooldown * 60
        last     = self._last_bearish_alert.get(mint, 0.0)
        elapsed  = time.time() - last
        if elapsed < cooldown:
            return False
        return True

    # ── Bot state writer ───────────────────────────────────────────────────────

    def _write_bot_state(self) -> None:
        """
        Serialise the latest TA results to bot_state.json so xmtp_agent.js
        can provide Claude with live market context when answering DMs.
        """
        tokens_data = []
        for result in self._latest_results.values():
            tokens_data.append({
                "symbol":          result.token.symbol,
                "name":            result.token.name,
                "mint":            result.token.mint,
                "price":           result.price,
                "price_change_1h": result.price_change_1h,
                "price_change_5m": result.price_change_5m,
                "volume_24h":      result.volume_24h,
                "liquidity_usd":   result.liquidity_usd,
                "rsi":             result.rsi_value,
                "macd_histogram":  result.macd_histogram,
                "score":           result.score,
                "bearish_score":   result.bearish_score,
                "is_bullish":      result.is_bullish,
                "is_bearish":      result.is_bearish,
                "sell_pct":        result.sell_pct,
                "reasons":         result.reasons,
                "bearish_reasons": result.bearish_reasons,
            })
        state = {
            "last_updated": datetime.utcnow().isoformat() + "Z",
            "tokens":       tokens_data,
        }
        try:
            bot_state_path = os.path.join(os.path.dirname(__file__), "bot_state.json")
            with open(bot_state_path, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except Exception as exc:
            log.warning("Failed to write bot_state.json: %s", exc)

    # ── Single-token check ─────────────────────────────────────────────────────

    def _check(self, token: TokenInfo) -> None:
        if not token.enabled:
            return
        if not self._resolve_pool(token):
            return

        log.info("→ Checking %-10s  pool %s…", token.symbol, token.pool_address[:10])

        try:
            result = self._ta.compute(token)
        except Exception as exc:
            log.error("TA error for %s: %s", token.symbol, exc, exc_info=True)
            return

        if result is None:
            return

        # Store latest result for bot_state.json
        self._latest_results[token.mint] = result

        fire_bullish = result.is_bullish and self._cooldown_ok(result)
        fire_bearish = result.is_bearish and self._bearish_cooldown_ok(token.mint)

        log.info(
            "  %-10s  bull=%d/7  bear=%d  RSI=%4.1f  price=%s  sell=%s  %s",
            result.token.symbol,
            result.score,
            result.bearish_score,
            result.rsi_value,
            AlertSender._fmt_price(result.price),
            f"{result.sell_pct}%" if result.sell_pct else "—",
            " ".join(filter(None, [
                "🚀 BULLISH" if fire_bullish else "",
                "⚠️ BEARISH" if fire_bearish else "",
            ])),
        )

        if fire_bullish:
            self._alerts.send_all(result)
            token.last_alert_time = time.time()
            self._store.save(self._tokens)

        if fire_bearish:
            self._alerts.send_all_bearish(result)
            self._last_bearish_alert[token.mint] = time.time()

    # ── Main async loop ────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        log.info(
            "Bot running — %d token(s) watched — poll interval: %ds — min score: %d/7",
            len(self._tokens), CFG.poll_interval, CFG.min_score,
        )

        while True:
            cycle_start = time.monotonic()
            log.info("── Cycle start ──────────────────────────────")

            for token in self._tokens:
                try:
                    # Run blocking I/O in a thread pool; keeps the event loop alive
                    await asyncio.to_thread(self._check, token)
                    # Small pause between tokens to respect rate limits
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.error("Unexpected error for %s: %s", token.mint[:8], exc, exc_info=True)

            elapsed  = time.monotonic() - cycle_start
            # Write latest TA data for xmtp_agent.js / Claude context
            self._write_bot_state()
            sleep_for = max(0.0, CFG.poll_interval - elapsed)
            log.info("── Cycle done in %.1fs — next in %.0fs ─────", elapsed, sleep_for)
            await asyncio.sleep(sleep_for)

    # ── NFT sales loop ─────────────────────────────────────────────────────────

    async def _nft_monitor_loop(self) -> None:
        """Poll for Saga Monkes sales every 30 seconds."""
        while True:
            try:
                await asyncio.to_thread(self._nft_monitor.check)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("NFT sales monitor error: %s", exc)
            await asyncio.sleep(30)

    # ── Public API ─────────────────────────────────────────────────────────────

    def add_token(self, mint: str, pool_address: str = "", symbol: str = "") -> TokenInfo:
        token = self._store.add(mint, pool_address=pool_address, symbol=symbol)
        if not any(t.mint == mint for t in self._tokens):
            self._tokens.append(token)
        return token

    def run(self) -> None:
        self._tokens = self._store.load()
        if not self._tokens:
            print(
                "\n⚠️  No tokens in watch list!\n"
                "  Add one with:  python bot.py add <MINT_ADDRESS>\n"
            )

        async def _run_all():
            await asyncio.gather(self._loop(), self._nft_monitor_loop())

        try:
            asyncio.run(_run_all())
        except KeyboardInterrupt:
            log.info("Stopped by user (Ctrl+C)")
            print("\n👋 Bot stopped.")


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

_BANNER = """
╔══════════════════════════════════════════════════════════╗
║         SOLANA TOKEN ALERT BOT  🚀                      ║
║  High-conviction bullish TA — low noise, high signal    ║
╚══════════════════════════════════════════════════════════╝
"""


def _cmd_add(args: argparse.Namespace) -> None:
    bot = SolanaAlertBot()
    token = bot.add_token(args.mint, pool_address=args.pool or "", symbol=args.symbol or "")
    print(f"\n✓ Added: {token.symbol or '??'}  |  mint: {token.mint}")
    if token.pool_address:
        print(f"  Pool: {token.pool_address}")
    else:
        print("  Pool: will be auto-discovered on first run")


def _cmd_list(_args: argparse.Namespace) -> None:
    tokens = TokenStore().load()
    if not tokens:
        print("Watch list is empty. Use: python bot.py add <MINT>")
        return
    print(f"\nWatching {len(tokens)} token(s):\n")
    print(f"  {'ST':<3} {'SYMBOL':<12} {'MINT':<46} {'POOL (first 20)'}")
    print(f"  {'──':<3} {'──────':<12} {'────':<46} {'───────────────'}")
    for t in tokens:
        st = "✓" if t.enabled else "✗"
        pool = (t.pool_address[:20] + "…") if t.pool_address else "auto-discover"
        print(f"  {st:<3} {t.symbol:<12} {t.mint:<46} {pool}")
    print()


def _cmd_test(_args: argparse.Namespace) -> None:
    """One-shot TA check — useful for verifying your setup."""
    tokens = TokenStore().load()
    if not tokens:
        print("Watch list is empty. Use: python bot.py add <MINT>")
        return

    dex = DexScreener()
    ta  = TAEngine()

    for i, token in enumerate(tokens):
        if i > 0:
            print("   (waiting 8s to respect GeckoTerminal rate limit…)")
            time.sleep(8)

        print(f"\n── {token.symbol or token.mint[:16]}… ──────────────────────")
        if not token.pool_address:
            result = dex.best_pool(token.mint)
            if result:
                token.pool_address, _ = result
                print(f"   Pool discovered: {token.pool_address[:20]}…")
            else:
                print("   ✗ Could not discover pool — skipping")
                continue

        result = ta.compute(token)
        if result is None:
            print("   ✗ TA failed (not enough data?)")
            continue

        print(f"   Score:  {result.score}/7")
        print(f"   RSI:    {result.rsi_value:.1f}")
        print(f"   Price:  {AlertSender._fmt_price(result.price)}")
        print(f"   Signals:")
        for r in result.reasons:
            print(f"     ✓ {r}")
        if not result.reasons:
            print("     (no signals triggered this candle)")
        print(f"   Bullish: {'YES 🚀' if result.is_bullish else 'no'}")

    print("\nTest done.")


def _cmd_run(_args: argparse.Namespace) -> None:
    SolanaAlertBot().run()


def main() -> None:
    print(_BANNER)

    parser = argparse.ArgumentParser(
        description="Solana Token Alert Bot — bullish TA signal monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("run",  help="Start the polling loop (default if no command given)")
    sub.add_parser("list", help="List all watched tokens")
    sub.add_parser("test", help="Run one TA pass for all tokens and exit")

    add_p = sub.add_parser("add", help="Add a token to the watch list")
    add_p.add_argument("mint",     help="SPL token mint address")
    add_p.add_argument("--pool",   default="", help="Pool address (optional — auto-discovered)")
    add_p.add_argument("--symbol", default="", help="Token symbol (optional — auto-discovered)")

    args = parser.parse_args()
    cmd  = args.cmd or "run"

    dispatch = {
        "run":  _cmd_run,
        "list": _cmd_list,
        "test": _cmd_test,
        "add":  _cmd_add,
    }
    dispatch[cmd](args)


if __name__ == "__main__":
    main()
