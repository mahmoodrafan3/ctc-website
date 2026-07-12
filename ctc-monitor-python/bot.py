# ============================================================================
# CTC Strategy Monitor — Python (MetaApi + Render.com)
# ============================================================================
#
# Architecture:
#   OANDA MT5 (EUR/USD) → MetaApi Cloud (WebSocket)
#     → Python bot on Render.com free tier
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
#   META_API_TOKEN      — from https://app.metaapi.cloud/token
#   META_ACCOUNT_ID     — MetaApi account ID (from MetaApi dashboard)
#   TELEGRAM_BOT_TOKEN  — from @BotFather
#   TELEGRAM_CHAT_ID    — from @userinfobot
#   PRICE_LEVEL_SELL    — optional, e.g. 1.1200
#   PRICE_LEVEL_BUY     — optional, e.g. 1.0500
# ============================================================================

import asyncio
import logging
import os
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
import numpy as np
from fastapi import FastAPI
from metaapi_cloud_sdk import MetaApi
from metaapi_cloud_sdk.clients.meta_api_client import SynchronizationListener

# ── Logging ────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ctc-monitor")

# ── Configuration from environment ─────────────────────────────────
META_API_TOKEN = os.environ.get("META_API_TOKEN", "")
META_ACCOUNT_ID = os.environ.get("META_ACCOUNT_ID", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
PRICE_LEVEL_SELL = float(os.environ.get("PRICE_LEVEL_SELL", "0"))
PRICE_LEVEL_BUY = float(os.environ.get("PRICE_LEVEL_BUY", "0"))

SYMBOL = "EURUSD"
TIMEFRAME = "5m"
CCI_PERIOD = 15
ATR_PERIOD = 5
ATR_COEFF = 1.0
MIN_ALERT_INTERVAL_MINUTES = 10
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
        self.meta_api: Optional[MetaApi] = None
        self.account = None
        self.connection = None
        self._candles: list = []
        self._last_alert_time = datetime.min
        self._last_sell_alert = datetime.min
        self._last_buy_alert = datetime.min
        self._last_processed_candle_time: Optional[datetime] = None
        self._shutdown_requested = False
        self._watchdog_task: Optional[asyncio.Task] = None
        self.running = False

    # ════════════════════════════════════════════════════════════════
    # PUBLIC API
    # ════════════════════════════════════════════════════════════════

    async def start(self) -> None:
        """Connect to MetaApi, load history, subscribe, and start listening."""
        if not META_API_TOKEN or not META_ACCOUNT_ID:
            logger.error("META_API_TOKEN and META_ACCOUNT_ID must be set.")
            return

        self._shutdown_requested = False

        logger.info("=" * 55)
        logger.info("  CTC Strategy Monitor — Python")
        logger.info(f"  Symbol: {SYMBOL}  Timeframe: {TIMEFRAME}")
        logger.info(f"  Sessions: London ({_fmt_mins(LONDON_START)}-{_fmt_mins(LONDON_END)})")
        logger.info(f"            NY     ({_fmt_mins(NY_START)}-{_fmt_mins(NY_END)}) EST")
        if PRICE_LEVEL_SELL > 0:
            logger.info(f"  Sell level: {PRICE_LEVEL_SELL}")
        if PRICE_LEVEL_BUY > 0:
            logger.info(f"  Buy level:  {PRICE_LEVEL_BUY}")
        logger.info("=" * 55)

        try:
            self.meta_api = MetaApi(META_API_TOKEN)
            self.account = await self.meta_api.metatrader_account_api.get_account(
                META_ACCOUNT_ID
            )
            self.connection = self.account.get_streaming_connection()

            # ── Attach listener for real-time candle updates ──
            # Capture the handler now to avoid `self` confusion inside the listener class.
            on_candle = self._on_candle_received

            class CTCListener(SynchronizationListener):
                async def on_candle_updated(self, symbol, timeframe, candle):
                    if symbol == SYMBOL and str(timeframe) == TIMEFRAME:
                        try:
                            await on_candle(candle)
                        except Exception as e:
                            logger.error(
                                f"Unhandled error in candle handler: {e}", exc_info=True
                            )

            self.connection.add_synchronization_listener(CTCListener())

            logger.info("Connecting to MetaApi...")
            await self.connection.connect()
            await self.connection.wait_synchronized()

            # Load enough historical candles to prime indicators
            await self._load_historical_candles()

            # Subscribe to live candles
            await self._subscribe_candles()

            self.running = True

            # ── Start watchdog for auto-reconnect ──
            if self._watchdog_task is None or self._watchdog_task.done():
                self._watchdog_task = asyncio.create_task(self._watchdog_loop())

            logger.info("✅ Bot is running — waiting for new candles...")

        except Exception as e:
            logger.error(f"❌ Failed to start bot: {e}")
            self.running = False

    async def stop(self) -> None:
        """Disconnect from MetaApi and stop the watchdog."""
        self._shutdown_requested = True

        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass

        if self.connection:
            try:
                await self.connection.unsubscribe_from_market_data(
                    [{"type": "candles", "symbol": SYMBOL, "timeframe": TIMEFRAME}]
                )
            except Exception:
                pass
            try:
                await self.connection.close()
            except Exception:
                pass

        self.running = False
        logger.info("Bot stopped")

    # ════════════════════════════════════════════════════════════════
    # WATCHDOG — reconnect if connection drops
    # ════════════════════════════════════════════════════════════════

    async def _watchdog_loop(self) -> None:
        """Periodically check if the connection is alive; reconnect if needed."""
        while not self._shutdown_requested:
            await asyncio.sleep(30)

            if self._shutdown_requested:
                break

            if not self.running or not self.connection:
                logger.warning("⚠️ Bot not running — attempting reconnection...")
                await self.start()
                continue

            # Check if connection is still alive by trying to get terminal state
            try:
                state = self.connection.terminal_state
                _ = state.connected
            except Exception:
                logger.warning("⚠️ Connection lost — reconnecting...")
                self.running = False
                await self.start()

    # ════════════════════════════════════════════════════════════════
    # HISTORICAL DATA LOADING
    # ════════════════════════════════════════════════════════════════

    async def _load_historical_candles(self) -> None:
        """Fetch enough M5 candles to compute Trend Magic indicators."""
        try:
            candles = await self.meta_api.historical_market_data_api.get_historical_candles(
                META_ACCOUNT_ID,
                SYMBOL,
                TIMEFRAME,
                limit=50,
            )
            self._candles = [
                {
                    "time": self._parse_candle_time(c["time"]),
                    "open": float(c["open"]),
                    "high": float(c["high"]),
                    "low": float(c["low"]),
                    "close": float(c["close"]),
                }
                for c in candles
                if c.get("time") and c.get("open") is not None
            ]
            logger.info(f"Loaded {len(self._candles)} historical candles")
        except Exception as e:
            logger.warning(f"Could not load historical candles: {e}")

    # ════════════════════════════════════════════════════════════════
    # MARKET DATA SUBSCRIPTION
    # ════════════════════════════════════════════════════════════════

    async def _subscribe_candles(self) -> None:
        """Subscribe to real-time candle updates. Tries two API formats."""
        logger.info("Subscribing to market data...")

        # Format 1: List of dicts (newer SDK format)
        try:
            await self.connection.subscribe_to_market_data(
                [{"type": "candles", "symbol": SYMBOL, "timeframe": TIMEFRAME}]
            )
            return
        except Exception as e:
            logger.warning(f"subscribe format 1 failed: {e}")

        # Format 2: Symbol string (older SDK format)
        try:
            await self.connection.subscribe_to_market_data(SYMBOL)
            return
        except Exception as e:
            logger.warning(f"subscribe format 2 failed: {e}")

        logger.warning("Could not subscribe to market data — will poll instead")

    # ════════════════════════════════════════════════════════════════
    # REAL-TIME CANDLE HANDLER
    # ════════════════════════════════════════════════════════════════

    async def _on_candle_received(self, candle) -> None:
        """
        Called on every candle update (including intra-tick updates).
        We only process when we detect a new *closed* candle.
        The first candle received after subscription is treated as
        "still forming" — we skip it to avoid processing an incomplete bar.
        """
        candle_time = self._parse_candle_time(candle["time"])

        # ── Skip the first candle entirely (it's still forming) ──
        if self._last_processed_candle_time is None:
            self._last_processed_candle_time = candle_time
            logger.debug(f"Skipping initial forming candle @ {candle_time}")
            return

        # Only process when the candle time changes (new closed candle)
        if self._last_processed_candle_time == candle_time:
            return

        self._last_processed_candle_time = candle_time

        self._add_or_update_candle({
            "time": candle_time,
            "open": float(candle["open"]),
            "high": float(candle["high"]),
            "low": float(candle["low"]),
            "close": float(candle["close"]),
        })

        # ── Check signals ───────────────────────────────────────────
        n = len(self._candles)
        if n < max(CCI_PERIOD, ATR_PERIOD) + 2:
            logger.debug(f"Warming up — only {n} candles")
            return

        if not self._is_in_session():
            return

        result = self._calculate_trend_magic()
        if result is None:
            return

        close = self._candles[-1]["close"]
        now = datetime.now(EST_TZ)

        logger.info(
            f"📊 Candle | Close: {close:.5f} | "
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
    # CANDLE HISTORY MANAGEMENT
    # ════════════════════════════════════════════════════════════════

    def _parse_candle_time(self, raw_time) -> datetime:
        """Normalize a candle time value into a timezone-aware datetime."""
        if isinstance(raw_time, datetime):
            return raw_time
        if isinstance(raw_time, str):
            return datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
        return datetime.fromisoformat(str(raw_time))

    def _add_or_update_candle(self, candle: dict) -> None:
        """Add a new candle or update the latest if same time."""
        if not self._candles or self._candles[-1]["time"] != candle["time"]:
            self._candles.append(candle)
            if len(self._candles) > 100:
                self._candles = self._candles[-100:]
        else:
            self._candles[-1] = candle

    # ════════════════════════════════════════════════════════════════
    # SESSION FILTER
    # ════════════════════════════════════════════════════════════════

    def _is_in_session(self) -> bool:
        now = datetime.now(EST_TZ)

        # Skip weekends
        if now.weekday() >= 5:  # Sat=5, Sun=6
            logger.debug("⛔ Weekend — no trading")
            return False

        minutes = now.hour * 60 + now.minute

        if LONDON_START <= minutes < LONDON_END:
            return True
        if NY_START <= minutes < NY_END:
            return True

        logger.debug("Outside trading session — skipping")
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
            f"Pair: {SYMBOL}\n"
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
                    f"Pair: {SYMBOL}\n"
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
                    f"Pair: {SYMBOL}\n"
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
    logger.info("Starting CTC Monitor...")
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
        "watchdog_alive": (
            not bot_instance._watchdog_task.done()
            if bot_instance._watchdog_task
            else False
        ),
    }
