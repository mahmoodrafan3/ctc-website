#!/usr/bin/env python3
"""
CTC Strategy Monitor
────────────────────
Runs as a GitHub Actions scheduled job (every 5 minutes).

Fetches real-time OHLC data from the Twelve Data free API, calculates the
Trend Magic indicator (CCI + ATR), detects candle body crossovers of the
Trend Magic line during London/New York sessions, and sends alerts via
Telegram Bot API.

Port of the TradingView Pine Script indicator from code/code.html.
"""

import os
import time
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any

import requests


# ════════════════════════════════════════════════════════════════
# CONFIGURATION — set via GitHub Secrets / Variables
# ════════════════════════════════════════════════════════════════

# Forex pairs to monitor (comma-separated)
SYMBOLS = os.environ.get("MONITOR_SYMBOLS", "EURUSD").split(",")

# Data timeframe: 1min, 5min, 15min (match your trading chart)
TIMEFRAME = os.environ.get("MONITOR_TIMEFRAME", "5min")

# ── Trend Magic parameters (match Pine Script) ─────────────
CCI_PERIOD = int(os.environ.get("CCI_PERIOD", "15"))
ATR_PERIOD = int(os.environ.get("ATR_PERIOD", "5"))
ATR_COEFF  = float(os.environ.get("ATR_COEFF", "1.0"))

# ── Session times (America/New_York) ──────────────────────
# London: 03:00-04:40 EST  →  minutes 180 - 280
LONDON_START = int(os.environ.get("LONDON_START", "180"))
LONDON_END   = int(os.environ.get("LONDON_END", "280"))
# New York: 08:00-09:55 EST  →  minutes 480 - 595
NY_START     = int(os.environ.get("NY_START", "480"))
NY_END       = int(os.environ.get("NY_END", "595"))
# Set FORCE_SESSION=true to bypass session time check (for testing)
FORCE_SESSION = os.environ.get("FORCE_SESSION", "").lower() == "true"

# ── Free data API ─────────────────────────────────────────
# "twelvedata" (default, 800 calls/day free) or "alphavantage"
DATA_API = os.environ.get("DATA_API", "twelvedata")

TWELVEDATA_KEY   = os.environ.get("TWELVEDATA_KEY", "")
ALPHAVANTAGE_KEY = os.environ.get("ALPHAVANTAGE_KEY", "")

# ── Telegram Bot API (free, no approval needed) ─────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# How many candles to fetch for the main timeframe
CANDLE_COUNT = CCI_PERIOD + ATR_PERIOD + 10


# ════════════════════════════════════════════════════════════════
# SESSION DETECTION
# ════════════════════════════════════════════════════════════════

def get_ny_minutes() -> int:
    """Minutes since midnight in America/New_York (handles DST automatically)."""
    now = datetime.now(ZoneInfo("America/New_York"))
    return now.hour * 60 + now.minute


def check_session() -> tuple[bool, str]:
    """Returns (is_active, session_name)."""
    mins = get_ny_minutes()
    if LONDON_START <= mins < LONDON_END:
        return True, "London"
    if NY_START <= mins < NY_END:
        return True, "New York"
    return False, ""


# ════════════════════════════════════════════════════════════════
# FREE DATA API
# ════════════════════════════════════════════════════════════════

def normalize_symbol(symbol: str) -> str:
    """Convert forex symbol format for Twelve Data API.
    Handles both EURUSD and EUR/USD formats."""
    s = symbol.strip().upper()
    if '/' not in s and len(s) == 6 and s.isalpha():
        return f"{s[:3]}/{s[3:]}"
    return s


def _parse_ohlc(raw_list: list[dict]) -> list[dict[str, float]]:
    """Convert raw API candles (newest-first) to OHLC dicts (oldest-first)."""
    candles = []
    for c in reversed(raw_list):
        candles.append({
            "open":  float(c["open"]),
            "high":  float(c["high"]),
            "low":   float(c["low"]),
            "close": float(c["close"]),
        })
    return candles


def fetch_candles_twelvedata(symbol: str, count: int | None = None) -> list[dict[str, float]]:
    """
    Twelve Data free tier: 8 req/min, 800 req/day.
    Returns OHLC dicts oldest → newest.
    ⚠️ Free forex data may be delayed ~15 min.
    """
    url = "https://api.twelvedata.com/time_series"
    # Twelve Data expects forex symbols in EUR/USD format
    api_symbol = normalize_symbol(symbol)
    params = {
        "symbol": api_symbol,
        "interval": TIMEFRAME,
        "outputsize": count or CANDLE_COUNT,
        "apikey": TWELVEDATA_KEY,
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    if "values" not in data or not data["values"]:
        raise ValueError(f"Twelve Data returned no values: {data.get('message', 'unknown')}")

    return _parse_ohlc(data["values"])


def fetch_candles_alphavantage(symbol: str, count: int | None = None) -> list[dict[str, float]]:
    """
    Alpha Vantage free tier: 5 req/min, 500 req/day.
    Returns OHLC dicts oldest → newest.
    ⚠️ Free forex data may be delayed ~15 min.
    """
    interval_map = {"1min": "1min", "5min": "5min", "15min": "15min"}
    interval = interval_map.get(TIMEFRAME, "1min")

    url = "https://www.alphavantage.co/query"
    params = {
        "function": "FX_INTRADAY",
        "from_symbol": symbol[:3],
        "to_symbol": symbol[3:],
        "interval": interval,
        "outputsize": "compact",
        "apikey": ALPHAVANTAGE_KEY,
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    key = f"Time Series FX ({interval})"
    if key not in data:
        raise ValueError(f"Alpha Vantage returned no data: {data.get('Note', 'unknown')}")

    raw = list(data[key].items())
    needed = count or CANDLE_COUNT
    if len(raw) < needed:
        raise ValueError(f"Not enough candles: got {len(raw)}, need {needed}")

    candles = _parse_ohlc([v for _, v in raw])
    return candles[-needed:]


def fetch_candles(symbol: str, count: int | None = None) -> list[dict[str, float]]:
    """Fetch main timeframe candles."""
    if DATA_API == "alphavantage":
        return fetch_candles_alphavantage(symbol, count)
    return fetch_candles_twelvedata(symbol, count)


# ════════════════════════════════════════════════════════════════
# TREND MAGIC — ported from Pine Script
# ════════════════════════════════════════════════════════════════

def compute_true_range(candles: list[dict[str, float]], i: int) -> float:
    """True Range at position i (needs i >= 1)."""
    h = candles[i]["high"]
    l = candles[i]["low"]
    pc = candles[i - 1]["close"]
    return max(h - l, abs(h - pc), abs(l - pc))


def compute_atr_array(candles: list[dict[str, float]], period: int) -> list[float]:
    """SMA of True Range — return one value per candle index."""
    n = len(candles)
    atr = [0.0] * n
    for i in range(period, n):
        s = sum(compute_true_range(candles, j) for j in range(i - period + 1, i + 1))
        atr[i] = s / period
    return atr


def compute_cci_array(candles: list[dict[str, float]], period: int) -> list[float]:
    """CCI on close — return one value per candle index."""
    n = len(candles)
    cci = [0.0] * n
    for i in range(period - 1, n):
        start = i - period + 1
        closes = [candles[k]["close"] for k in range(start, i + 1)]
        sma = sum(closes) / period
        md = sum(abs(c - sma) for c in closes) / period
        if md != 0:
            cci[i] = (candles[i]["close"] - sma) / (0.015 * md)
    return cci


def calc_trend_magic_full(candles: list[dict[str, float]]) -> dict[str, Any]:
    """
    Calculate Trend Magic and detect crossovers.
    
    Recursively computes MagicTrend through the entire candle history,
    so no cross-run state persistence is needed.
    
    Pine Script reference:
        MagicTrend := cci >= 0
            ? (upT < nz(MagicTrend[1]) ? nz(MagicTrend[1]) : upT)
            : (downT > nz(MagicTrend[1]) ? nz(MagicTrend[1]) : downT)
    
    Returns dict with latest values and signal flags.
    """
    n = len(candles)
    min_needed = max(CCI_PERIOD, ATR_PERIOD) + 1
    if n < min_needed:
        return {"magic_trend": 0.0, "cci": 0.0,
                "strong_buy": False, "strong_sell": False,
                "trend_bull": True}

    # Precompute once for efficiency
    atr_arr = compute_atr_array(candles, ATR_PERIOD)
    cci_arr = compute_cci_array(candles, CCI_PERIOD)

    # Recursive MagicTrend calculation — store value at each candle
    magic_trend_arr = [0.0] * n
    magic_trend = 0.0
    prev_magic = 0.0
    first_valid = False

    for i in range(1, n):
        cci_val = cci_arr[i]
        atr_val = atr_arr[i]
        if atr_val == 0:
            continue

        up_t   = candles[i]["low"]  - atr_val * ATR_COEFF
        down_t = candles[i]["high"] + atr_val * ATR_COEFF

        if not first_valid:
            magic_trend = up_t if cci_val >= 0 else down_t
            prev_magic = magic_trend
            first_valid = True
        else:
            prev_magic = magic_trend
            if cci_val >= 0:
                # Bullish: only moves UP
                magic_trend = up_t if up_t > prev_magic else prev_magic
            else:
                # Bearish: only moves DOWN
                magic_trend = down_t if down_t < prev_magic else prev_magic

        magic_trend_arr[i] = magic_trend

    cci_latest = cci_arr[-1]
    trend_bull = cci_latest >= 0

    # Check last 2 candles for body crossover against their own magic_trend
    # This catches crossovers on the just-closed candle even if a new candle opened
    strong_buy = False
    strong_sell = False
    for i in range(max(1, n - 2), n):
        c = candles[i]
        mt = magic_trend_arr[i]
        if c["open"] < mt and c["close"] > mt:
            strong_buy = True
        if c["open"] > mt and c["close"] < mt:
            strong_sell = True

    return {
        "magic_trend": round(magic_trend_arr[-1], 5),
        "cci": round(cci_latest, 2),
        "strong_buy": strong_buy,
        "strong_sell": strong_sell,
        "trend_bull": trend_bull,
    }


# ════════════════════════════════════════════════════════════════
# TELEGRAM BOT API — free and instant
# ════════════════════════════════════════════════════════════════

def send_telegram(message_body: str) -> bool:
    """Send message via Telegram Bot API. Returns True on success."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️  Telegram credentials not set — skipping notification")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message_body,
        "parse_mode": "HTML",
    }

    try:
        resp = requests.post(url, json=payload, timeout=15)
        data = resp.json()
        if data.get("ok"):
            print("✅ Telegram notification sent")
            return True
        else:
            print(f"❌ Telegram API error: {data}")
            return False
    except requests.RequestException as e:
        print(f"❌ Telegram request failed: {e}")
        return False


# ════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════

def main():
    ny_time = datetime.now(ZoneInfo("America/New_York")).strftime("%H:%M %Z")
    print(f"🚀 CTC Strategy Monitor — {ny_time}")
    print(f"   Symbols: {', '.join(SYMBOLS)}  Timeframe: {TIMEFRAME}")
    print()

    # ── 1. Session check ───────────────────────────────────
    in_session, session_name = check_session()
    if FORCE_SESSION:
        session_name = "FORCED (testing)"
        print(f"⚡ FORCE_SESSION enabled — checking anyway...")
    elif not in_session:
        print("⏸️  Outside London/New York hours — skipping")
        return
    else:
        print(f"✅ {session_name} session — monitoring...")
    print()

    # ── 2. Check each symbol ───────────────────────────────
    alerts: list[str] = []

    for raw_symbol in SYMBOLS:
        symbol = raw_symbol.strip().upper()
        if not symbol:
            continue

        print(f"🔍 {symbol} ...", end=" ", flush=True)

        try:
            candles = fetch_candles(symbol)
            if len(candles) < max(CCI_PERIOD, ATR_PERIOD) + 2:
                print(f"⚠️  Need more data ({len(candles)} candles)")
                continue

            result = calc_trend_magic_full(candles)
            latest = candles[-1]
            prev = candles[-2] if len(candles) >= 2 else latest
            price = latest["close"]
            magic = result["magic_trend"]
            cci = result["cci"]
            trend = "BULL" if result["trend_bull"] else "BEAR"

            # Print last 2 candles for comparison with TradingView
            print(f"P={price:.5f} M={magic:.5f} CCI={cci} {trend}", end="")
            print(f"  |  Candle-1: O={prev['open']:.5f} H={prev['high']:.5f} L={prev['low']:.5f} C={prev['close']:.5f}", end="")
            print(f"  |  Candle-0: O={latest['open']:.5f} H={latest['high']:.5f} L={latest['low']:.5f} C={latest['close']:.5f}", end="")

            # Determine raw signal
            raw_buy  = result["strong_buy"]  and result["trend_bull"]
            raw_sell = result["strong_sell"] and not result["trend_bull"]

            if not raw_buy and not raw_sell:
                print(" ✅")
                continue

            cross_type = "BUY" if raw_buy else "SELL"
            print(f"🚨 {cross_type}!")

            msg = (
                f"🚨 FX MOZO {cross_type} SIGNAL\n"
                f"Pair: {symbol}\n"
                f"Price: {price:.5f}\n"
                f"Trend Magic: {magic:.5f}\n"
                f"Session: {session_name}\n"
                f"CCI: {cci} — {trend}\n"
                f"Time: {ny_time}"
            )
            alerts.append(msg)

        except Exception as e:
            print(f"❌ Error: {e}")

    # ── 3. Send Telegram notifications ────────────────────
    print()
    if alerts:
        print(f"📨 Sending {len(alerts)} notification(s)...")
        for alert in alerts:
            send_telegram(alert)
            time.sleep(1)
    else:
        print("✅ No signals found")


if __name__ == "__main__":
    # Early credential validation
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if DATA_API == "twelvedata" and not TWELVEDATA_KEY:
        missing.append("TWELVEDATA_KEY")
    if DATA_API == "alphavantage" and not ALPHAVANTAGE_KEY:
        missing.append("ALPHAVANTAGE_KEY")

    if missing:
        print(f"❌ Missing secrets: {', '.join(missing)}")
        print("   Set them in GitHub → Settings → Secrets and variables → Actions")
        sys.exit(1)

    main()
