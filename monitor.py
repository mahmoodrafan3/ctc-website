#!/usr/bin/env python3
"""
CTC Strategy Monitor
────────────────────
Runs as a GitHub Actions scheduled job (every 5 minutes).

Fetches real-time OHLC data from a free API, calculates the Trend Magic
indicator (CCI + ATR), detects price crossovers during London/New York
sessions with HTF touch confirmation, and sends WhatsApp notifications
via the WhatsApp Cloud API (free tier: 1,000 conversations/month).

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

# ── HTF Touch Filter ──────────────────────────────────────
# Matches Pine Script default: useHtfTouchFilter = true, htfTouchBars = 3
HTF_TOUCH_BARS = int(os.environ.get("HTF_TOUCH_BARS", "3"))
HTF_TIMEFRAME  = os.environ.get("HTF_TIMEFRAME", "1h")

# ── Free data API ─────────────────────────────────────────
# "twelvedata" (default, 800 calls/day free) or "alphavantage"
DATA_API = os.environ.get("DATA_API", "twelvedata")

TWELVEDATA_KEY   = os.environ.get("TWELVEDATA_KEY", "")
ALPHAVANTAGE_KEY = os.environ.get("ALPHAVANTAGE_KEY", "")

# ── WhatsApp Cloud API (free: 1,000 conversations/month) ──
WHATSAPP_TOKEN    = os.environ.get("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_ID = os.environ.get("WHATSAPP_PHONE_ID", "")
ALERT_PHONE       = os.environ.get("ALERT_PHONE", "")

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


def fetch_candles_htf_twelvedata(symbol: str) -> list[dict[str, float]]:
    """Fetch HTF candles (e.g. 1h) for touch confirmation."""
    url = "https://api.twelvedata.com/time_series"
    api_symbol = normalize_symbol(symbol)
    params = {
        "symbol": api_symbol,
        "interval": HTF_TIMEFRAME,
        "outputsize": HTF_TOUCH_BARS + 2,
        "apikey": TWELVEDATA_KEY,
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if "values" not in data or not data["values"]:
        raise ValueError(f"Twelve Data HTF returned no values: {data.get('message', 'unknown')}")
    return _parse_ohlc(data["values"])


def fetch_candles(symbol: str, count: int | None = None) -> list[dict[str, float]]:
    """Fetch main timeframe candles."""
    if DATA_API == "alphavantage":
        return fetch_candles_alphavantage(symbol, count)
    return fetch_candles_twelvedata(symbol, count)


def fetch_candles_htf(symbol: str) -> list[dict[str, float]]:
    """Fetch HTF candles for touch filter."""
    if DATA_API == "alphavantage":
        return fetch_candles_alphavantage(symbol, HTF_TOUCH_BARS + 2)
    return fetch_candles_htf_twelvedata(symbol)


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

    # Recursive MagicTrend calculation
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

    latest = candles[-1]
    cci_latest = cci_arr[-1]
    trend_bull = cci_latest >= 0

    # Strong cross = open on one side, close on the other
    strong_buy  = latest["open"] < magic_trend and latest["close"] > magic_trend
    strong_sell = latest["open"] > magic_trend and latest["close"] < magic_trend

    return {
        "magic_trend": round(magic_trend, 5),
        "cci": round(cci_latest, 2),
        "strong_buy": strong_buy,
        "strong_sell": strong_sell,
        "trend_bull": trend_bull,
    }


# ════════════════════════════════════════════════════════════════
# HTF TOUCH CONFIRMATION
# ── Pine Script default: useHtfTouchFilter = true
#    Checks if any candle in the last N bars touched the HTF line.
# ════════════════════════════════════════════════════════════════

def verify_htf_touch(symbol: str, recent_candles: list[dict[str, float]]) -> bool:
    """
    Check if any of the last N candles' ranges touch the HTF close level.
    
    Matches Pine Script's useHtfTouchFilter (default ON):
      candleTouchesHtf = high >= htfClose and low <= htfClose
      for i = 0 to htfTouchBars - 1
          if candleTouchesHtf[i]
              anyTouchHtf := true
    """
    try:
        htf_candles = fetch_candles_htf(symbol)
        if not htf_candles:
            return True  # Can't verify — assume OK

        htf_close = htf_candles[-1]["close"]

        # Check the last N main-timeframe candles for HTF touch
        for candle in recent_candles[-HTF_TOUCH_BARS:]:
            if candle["high"] >= htf_close and candle["low"] <= htf_close:
                return True

        return False

    except Exception as e:
        print(f"  (HTF check skipped: {e})", end="")
        return True  # Fail open


# ════════════════════════════════════════════════════════════════
# WHATSAPP CLOUD API — free tier (1,000 conversations/month)
# ════════════════════════════════════════════════════════════════

def send_whatsapp(message_body: str) -> bool:
    """Send text message via WhatsApp Cloud API. Returns True on success."""
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_ID or not ALERT_PHONE:
        print("⚠️  WhatsApp credentials not set — skipping notification")
        return False

    url = f"https://graph.facebook.com/v23.0/{WHATSAPP_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": ALERT_PHONE,
        "type": "text",
        "text": {"body": message_body},
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        if resp.status_code in (200, 201):
            print("✅ WhatsApp notification sent")
            return True
        else:
            print(f"❌ WhatsApp API error {resp.status_code}: {resp.text}")
            return False
    except requests.RequestException as e:
        print(f"❌ WhatsApp request failed: {e}")
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
    if not in_session:
        print("⏸️  Outside London/New York hours — skipping")
        return

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
            price = latest["close"]
            magic = result["magic_trend"]
            cci = result["cci"]
            trend = "BULL" if result["trend_bull"] else "BEAR"

            print(f"P={price:.5f} M={magic:.5f} CCI={cci} {trend}", end="")

            # Determine raw signal
            raw_buy  = result["strong_buy"]  and result["trend_bull"]
            raw_sell = result["strong_sell"] and not result["trend_bull"]

            if not raw_buy and not raw_sell:
                print(" ✅")
                continue

            # ── HTF Touch confirmation (ON by default in Pine Script) ──
            print(f"  HTF check...", end=" ", flush=True)
            htf_ok = verify_htf_touch(symbol, candles[-HTF_TOUCH_BARS:])

            if not htf_ok:
                print("⏭️  No HTF touch — filtered")
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

    # ── 3. Send WhatsApp notifications ─────────────────────
    print()
    if alerts:
        print(f"📨 Sending {len(alerts)} notification(s)...")
        for alert in alerts:
            send_whatsapp(alert)
            time.sleep(1)
    else:
        print("✅ No signals found")


if __name__ == "__main__":
    # Early credential validation
    missing = []
    if not WHATSAPP_TOKEN:
        missing.append("WHATSAPP_TOKEN")
    if not WHATSAPP_PHONE_ID:
        missing.append("WHATSAPP_PHONE_ID")
    if not ALERT_PHONE:
        missing.append("ALERT_PHONE")
    if DATA_API == "twelvedata" and not TWELVEDATA_KEY:
        missing.append("TWELVEDATA_KEY")
    if DATA_API == "alphavantage" and not ALPHAVANTAGE_KEY:
        missing.append("ALPHAVANTAGE_KEY")

    if missing:
        print(f"❌ Missing secrets: {', '.join(missing)}")
        print("   Set them in GitHub → Settings → Secrets and variables → Actions")
        sys.exit(1)

    main()
