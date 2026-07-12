# ============================================================================
# CTC Strategy Monitor — Python (yfinance + Render.com)
# ============================================================================
#
# Architecture:
#   Yahoo Finance (free, no API key, no broker)
#     → Python bot on Render.com free tier (polls every 4 min)
#     → Telegram alerts (direct HTTPS API)
#
# Keep-alive:
#   UptimeRobot (free) pings /health every 5 minutes
#   → Prevents Render's 15-min idle spin-down
#
# Strategy (same as cTrader CTCMonitor):
#   Trend Magic: CCI Period 15, ATR Period 5, ATR Coefficient 1.0
#   Sessions: London 03:00-04:40 EST, New York 08:00-09:55 EST
#   Body crossover + CCI trend direction → BUY/SELL signal
#   10-min cooldown between alerts
#   Price level alerts (configurable buy/sell thresholds)
#
# Environment variables (set in Render Dashboard):
#   TELEGRAM_BOT_TOKEN  — from @BotFather
#   TELEGRAM_CHAT_ID    — from @userinfobot
#   PRICE_LEVEL_SELL    — optional, e.g. 1.1200
#   PRICE_LEVEL_BUY     — optional, e.g. 1.0500
# ============================================================================

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
import numpy as np
import pandas as pd
import yfinance as yf
from fastapi import FastAPI

# ── Logging ────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ctc-monitor")

# ── Configuration from environment ─────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
PRICE_LEVEL_SELL = float(os.environ.get("PRICE_LEVEL_SELL", "0"))
PRICE_LEVEL_BUY = float(os.environ.get("PRICE_LEVEL_BUY", "0"))

SYMBOL = "EURUSD=X"  # Yahoo Finance ticker for EUR/USD
TIMEFRAME = "5m"
CCI_PERIOD = 15
ATR_PERIOD = 5
ATR_COEFF = 1.0
MIN_ALERT_INTERVAL_MINUTES = 10
POLL_INTERVAL_SECONDS = 240  # 4 minutes (less than 5 min candle)

# Session times (America/New_York minutes from midnight)
LONDON_START = 180  # 03:00 EST
LONDON_END = 280  # 04:40 EST
NY_START = 480  # 08:00 EST
NY_END = 595  # 09:55 EST

EST_TZ = ZoneInfo("America/New_York")

# ── FastAPI app ────────────────────────────────────────────────────
app = FastAPI(title="CTC Strategy Monitor - Python")

# ── Bot state ──────────────────────────────────────────────────────
bot_instance: Optional["CTCBot"] = None


# ───────────────────────────────────────────────────────────────────
# Trend Magic result container
# ───────────────────────────────────────────────────────────────────
class TrendMagicResult:
    def __init__(
        self,
        magic_trend: float = 0.0,
        cci: float = 0.0,
        strong_buy: bool = False,
        strong_sell: bool = False,
        trend_bull: bool = False,
    ):
        self.magic_trend = magic_trend
        self.cci = cci
        self.strong_buy = strong_buy
        self.strong_sell = strong_sell
        self.trend_bull = trend_bull


# ───────────────────────────────────────────────────────────────────
# Main bot class
# ───────────────────────────────────────────────────────────────────
class CTCBot:
    def __init__(self):
        self._candles: list = []  # list of dicts: {time, open, high, low, close}
        self._last_alert_time = datetime.min
        self._last_sell_alert = datetime.min
        self._last_buy_alert = datetime.min
        self._last_processed_time: Optional[datetime] = None
        self._shutdown_requested = False
        self._poll_task: Optional[asyncio.Task] = None
        self.running = False

    # ════════════════════════════════════════════════════════════════
    # PUBLIC API
    # ════════════════════════════════════════════════════════════════

    async def start(self) -> None:
        """Start the polling loop."""
        self._shutdown_requested = False

        logger.info("=" * 55)
        logger.info("  CTC Strategy Monitor — Python (yfinance)")
        logger.info(f"  Symbol: EUR/USD  Timeframe: {TIMEFRAME}")
        logger.info(f"  Poll interval: {POLL_INTERVAL_SECONDS}s")
        logger.info(f"  Sessions: London ({_fmt_mins(LONDON_START)}-{_fmt_mins(LONDON_END)})")
        logger.info(f"            NY     ({_fmt_mins(NY_START)}-{_fmt_mins(NY_END)}) EST")
        if PRICE_LEVEL_SELL > 0:
            logger.info(f"  Sell level: {PRICE_LEVEL_SELL}")
        if PRICE_LEVEL_BUY > 0:
            logger.info(f"  Buy level:  {PRICE_LEVEL_BUY}")
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            logger.warning("⚠️  TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set!")
        logger.info("=" * 55)

        # Initial fetch to prime candle history
        logger.info("Fetching initial EUR/USD data...")
        await self._fetch_candles()

        self.running = True

        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_loop())

        logger.info("✅ Bot is running — polling EUR/USD every 4 minutes...")

    async def stop(self) -> None:
        """Stop the polling loop."""
        self._shutdown_requested = True
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        self.running = False
        logger.info("Bot stopped")

    # ════════════════════════════════════════════════════════════════
    # POLLING LOOP
    # ════════════════════════════════════════════════════════════════

    async def _poll_loop(self) -> None:
        """Periodically fetch EUR/USD data and check for signals."""
        while not self._shutdown_requested:
            try:
                await self._fetch_and_check()
            except Exception as e:
                logger.error(f"Poll error: {e}", exc_info=True)

            # Wait before next poll — check more frequently during sessions
            for _ in range(POLL_INTERVAL_SECONDS // 10):
                if self._shutdown_requested:
                    return
                await asyncio.sleep(10)

    # ════════════════════════════════════════════════════════════════
    # DATA FETCHING
    # ════════════════════════════════════════════════════════════════

    async def _fetch_candles(self) -> None:
        """Fetch EUR/USD M5 candles from yfinance (non-blocking wrapper)."""
        loop = asyncio.get_event_loop()
        df = await loop.run_in_executor(None, self._do_yfinance_fetch)

        if df is None or df.empty:
            logger.warning("No data returned from yfinance")
            return

        parsed = self._parse_dataframe(df)

        if not parsed:
            logger.warning("Could not parse yfinance data")
            return

        # Merge with existing candles (keep newest 100)
        existing = {c["time"]: c for c in self._candles}
        for c in parsed:
            existing[c["time"]] = c
        sorted_candles = sorted(existing.values(), key=lambda x: x["time"])
        self._candles = sorted_candles[-100:]

        logger.info(f"Candle cache: {len(self._candles)} candles")

    def _do_yfinance_fetch(self):
        """Synchronous yfinance download (runs in executor)."""
        try:
            df = yf.download(
                tickers=SYMBOL,
                period="2d",
                interval=TIMEFRAME,
                progress=False,
                auto_adjust=False,
            )
            return df
        except Exception as e:
            logger.error(f"yfinance download error: {e}")
            return None

    def _parse_dataframe(self, df: pd.DataFrame) -> list:
        """
        Convert yfinance DataFrame into our candle format.
        Handles both flat columns and MultiIndex columns.
        """
        # Handle MultiIndex columns like (('Open', 'EURUSD=X'), ...)
        if isinstance(df.columns, pd.MultiIndex):
            # Try to extract the first level (Open, High, Low, Close)
            try:
                df.columns = df.columns.get_level_values(0)
            except Exception:
                # Fallback: take first column of each pair
                df.columns = [col[0] if isinstance(col, tuple) else col for col in df.columns]

        # Detect columns (case-insensitive)
        col_map = {}
        for col in df.columns:
            col_lower = str(col).lower().strip()
            if col_lower == "open":
                col_map["open"] = col
            elif col_lower == "high":
                col_map["high"] = col
            elif col_lower == "low":
                col_map["low"] = col
            elif col_lower == "close":
                col_map["close"] = col

        if not all(k in col_map for k in ("open", "high", "low", "close")):
            logger.warning(f"Unexpected yfinance columns: {list(df.columns)}")
            return []

        candles = []
        for idx, row in df.iterrows():
            try:
                candle_time = idx.to_pydatetime()
                # Make timezone-aware (UTC)
                if candle_time.tzinfo is None:
                    candle_time = candle_time.replace(tzinfo=timezone.utc)

                open_val = float(row[col_map["open"]])
                high_val = float(row[col_map["high"]])
                low_val = float(row[col_map["low"]])
                close_val = float(row[col_map["close"]])

                # Skip rows with NaN values (incomplete / forming candles)
                if any(np.isnan(v) for v in (open_val, high_val, low_val, close_val)):
                    continue

                candles.append({
                    "time": candle_time,
                    "open": open_val,
                    "high": high_val,
                    "low": low_val,
                    "close": close_val,
                })
            except (ValueError, TypeError) as e:
                logger.warning(f"Skipping bad row: {e}")
                continue

        return candles

    # ════════════════════════════════════════════════════════════════
    # FETCH AND CHECK SIGNALS
    # ════════════════════════════════════════════════════════════════

    async def _fetch_and_check(self) -> None:
        """Fetch latest data and check for new signals."""
        if not self._is_in_session():
            return

        # Fetch latest candles
        await self._fetch_candles()

        if len(self._candles) < max(CCI_PERIOD, ATR_PERIOD) + 2:
            logger.debug(f"Warming up — only {len(self._candles)} candles")
            return

        # Detect if we have a new closed candle since last check
        latest_candle = self._candles[-1]
        if self._last_processed_time == latest_candle["time"]:
            logger.debug("No new candle yet")
            return

        self._last_processed_time = latest_candle["time"]
        close = latest_candle["close"]
        now = datetime.now(EST_TZ)

        # Calculate Trend Magic
        result = self._calculate_trend_magic()
        if result is None:
            return

        logger.info(
            f"📊 Candle {latest_candle['time'].strftime('%H:%M')} | "
            f"Close: {close:.5f} | "
            f"CCI: {result.cci:.2f} | "
            f"Trend: {'BULL' if result.trend_bull else 'BEAR'} | "
            f"BuyX: {result.strong_buy} SellX: {result.strong_sell}"
        )

        # ── Trend Magic signal ──
        if result.strong_buy and result.trend_bull:
            await self._send_signal("BUY", close, result, now)
        elif result.strong_sell and not result.trend_bull:
            await self._send_signal("SELL", close, result, now)

        # ── Price level alerts ──
        await self._check_price_levels(close, now)

    # ════════════════════════════════════════════════════════════════
    # SESSION FILTER
    # ════════════════════════════════════════════════════════════════

    def _is_in_session(self) -> bool:
        now = datetime.now(EST_TZ)

        # Skip weekends
        if now.weekday() >= 5:  # Sat=5, Sun=6
            return False

        minutes = now.hour * 60 + now.minute

        if LONDON_START <= minutes < LONDON_END:
            return True
        if NY_START <= minutes < NY_END:
            return True

        return False

    # ════════════════════════════════════════════════════════════════
    # TREND MAGIC (numpy, matches cTrader logic)
    # ════════════════════════════════════════════════════════════════

    def _calculate_trend_magic(self) -> Optional[TrendMagicResult]:
        closes = np.array([c["close"] for c in self._candles])
        highs = np.array([c["high"] for c in self._candles])
        lows = np.array([c["low"] for c in self._candles])
        opens = np.array([c["open"] for c in self._candles])
        n = len(closes)

        if n < max(CCI_PERIOD, ATR_PERIOD) + 2:
            return None

        # ── CCI ──────────────────────────────────────────────────────
        tp = (highs + lows + closes) / 3
        cci = np.zeros(n)
        for i in range(CCI_PERIOD - 1, n):
            segment = tp[i - CCI_PERIOD + 1 : i + 1]
            sma = np.mean(segment)
            mad = np.mean(np.abs(segment - sma))
            cci[i] = (tp[i] - sma) / (0.015 * mad) if mad != 0 else 0

        # ── ATR ──────────────────────────────────────────────────────
        tr = np.zeros(n)
        for i in range(1, n):
            tr[i] = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        atr = np.zeros(n)
        for i in range(ATR_PERIOD, n):
            atr[i] = np.mean(tr[i - ATR_PERIOD + 1 : i + 1])

        # ── MagicTrend (recursive) ───────────────────────────────────
        mt = np.zeros(n)
        first_valid = False
        for i in range(1, n):
            if atr[i] == 0:
                continue
            up_t = lows[i] - atr[i] * ATR_COEFF
            down_t = highs[i] + atr[i] * ATR_COEFF

            if not first_valid:
                mt[i] = up_t if cci[i] >= 0 else down_t
                first_valid = True
            else:
                mt[i] = (
                    max(up_t, mt[i - 1]) if cci[i] >= 0 else min(down_t, mt[i - 1])
                )

        # ── Body crossover (last 2 candles) ─────────────────────────
        strong_buy = False
        strong_sell = False
        for i in range(max(1, n - 2), n):
            if opens[i] < mt[i] < closes[i]:
                strong_buy = True
            if opens[i] > mt[i] > closes[i]:
                strong_sell = True

        return TrendMagicResult(
            magic_trend=float(mt[-1]) if n > 1 else 0.0,
            cci=float(cci[-1]),
            strong_buy=strong_buy,
            strong_sell=strong_sell,
            trend_bull=cci[-1] >= 0,
        )

    # ════════════════════════════════════════════════════════════════
    # SIGNAL SENDING
    # ════════════════════════════════════════════════════════════════

    def _get_session_name(self, now: datetime) -> str:
        mins = now.hour * 60 + now.minute
        if LONDON_START <= mins < LONDON_END:
            return "London"
        if NY_START <= mins < NY_END:
            return "New York"
        return ""

    async def _send_signal(
        self, signal_type: str, close: float, result: TrendMagicResult, now: datetime
    ) -> None:
        if (now - self._last_alert_time).total_seconds() < MIN_ALERT_INTERVAL_MINUTES * 60:
            logger.debug("Cooldown active — skipping signal")
            return

        self._last_alert_time = now
        trend = "BULL" if result.trend_bull else "BEAR"

        message = (
            f"🚨 FX MOZO {signal_type} SIGNAL\n"
            f"Pair: EUR/USD\n"
            f"Price: {close:.5f}\n"
            f"Trend Magic: {result.magic_trend:.5f}\n"
            f"CCI: {result.cci:.2f} — {trend}\n"
            f"Session: {self._get_session_name(now)}\n"
            f"Time: {now.strftime('%H:%M')} EST"
        )
        await self._send_telegram(message)
        logger.info(f"🚨 {signal_type} SIGNAL sent to Telegram")

    async def _check_price_levels(self, close: float, now: datetime) -> None:
        if len(self._candles) < 5:
            return

        prev_close = self._candles[-2]["close"]

        if PRICE_LEVEL_SELL > 0:
            crossed = (prev_close <= PRICE_LEVEL_SELL < close) or (
                prev_close >= PRICE_LEVEL_SELL > close
            )
            if crossed and (now - self._last_sell_alert).total_seconds() >= MIN_ALERT_INTERVAL_MINUTES * 60:
                self._last_sell_alert = now
                direction = "UP" if close > PRICE_LEVEL_SELL else "DOWN"
                await self._send_telegram(
                    f"🔴 PRICE LEVEL: SELL ({direction})\n"
                    f"Pair: EUR/USD\n"
                    f"Price: {close:.5f}\n"
                    f"Level: {PRICE_LEVEL_SELL:.5f}\n"
                    f"Time: {now.strftime('%H:%M')} EST"
                )
                logger.info("🔴 Sell level triggered")

        if PRICE_LEVEL_BUY > 0:
            crossed = (prev_close <= PRICE_LEVEL_BUY < close) or (
                prev_close >= PRICE_LEVEL_BUY > close
            )
            if crossed and (now - self._last_buy_alert).total_seconds() >= MIN_ALERT_INTERVAL_MINUTES * 60:
                self._last_buy_alert = now
                direction = "UP" if close > PRICE_LEVEL_BUY else "DOWN"
                await self._send_telegram(
                    f"🟢 PRICE LEVEL: BUY ({direction})\n"
                    f"Pair: EUR/USD\n"
                    f"Price: {close:.5f}\n"
                    f"Level: {PRICE_LEVEL_BUY:.5f}\n"
                    f"Time: {now.strftime('%H:%M')} EST"
                )
                logger.info("🟢 Buy level triggered")

    # ════════════════════════════════════════════════════════════════
    # TELEGRAM
    # ════════════════════════════════════════════════════════════════

    async def _send_telegram(self, message: str) -> None:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            logger.warning("Telegram not configured")
            return

        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.post(
                        url,
                        json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
                    )
                if resp.status_code == 200:
                    logger.info("✅ Telegram alert sent")
                    return
                logger.warning(
                    f"Telegram error (attempt {attempt + 1}): "
                    f"{resp.status_code} — {resp.text[:200]}"
                )
            except Exception as e:
                logger.warning(f"Telegram send failed (attempt {attempt + 1}): {e}")
            await asyncio.sleep(1)

        logger.error("❌ Telegram send failed after 3 attempts")


# ── Helper ─────────────────────────────────────────────────────────
def _fmt_mins(m: int) -> str:
    """Format minutes-from-midnight as HH:MM."""
    return f"{m // 60:02d}:{m % 60:02d}"


# ── FastAPI lifecycle hooks ────────────────────────────────────────
@app.on_event("startup")
async def _startup():
    global bot_instance
    logger.info("Starting CTC Monitor (yfinance)...")
    bot_instance = CTCBot()
    asyncio.create_task(bot_instance.start())


@app.on_event("shutdown")
async def _shutdown():
    global bot_instance
    logger.info("Shutting down CTC Monitor...")
    if bot_instance:
        await bot_instance.stop()


@app.get("/health")
async def health():
    """UptimeRobot pings this every 5 min to keep the Render dyno alive."""
    return {
        "status": "ok",
        "running": bot_instance.running if bot_instance else False,
        "candles": len(bot_instance._candles) if bot_instance else 0,
        "time": datetime.now(EST_TZ).isoformat(),
    }


@app.get("/status")
async def status():
    """Return bot state (for debugging)."""
    if not bot_instance:
        return {"status": "not started"}
    return {
        "status": "running" if bot_instance.running else "stopped",
        "candles_cached": len(bot_instance._candles),
        "last_alert": (
            bot_instance._last_alert_time.isoformat()
            if bot_instance._last_alert_time != datetime.min
            else None
        ),
    }
