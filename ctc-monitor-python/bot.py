# ============================================================================
# CTC Strategy Monitor — Python (Finnhub WebSocket + Render.com)
# ============================================================================
#
# Architecture:
#   Finnhub WebSocket (free, no credit card, real-time EUR/USD trades)
#     → Build M5 OHLC candles in memory from tick stream
#     → Detect candle close in real time (< 1 sec latency)
#     → Trend Magic signal detection
#     → Telegram alerts via direct HTTPS API
#
# Keep-alive:
#   UptimeRobot (free) pings /health every 5 minutes
#   → Prevents Render's 15-min idle spin-down
#
# Strategy (same as cTrader CTCMonitor):
#   Trend Magic: CCI Period 15, ATR Period 5, ATR Coefficient 1.0
#   Sessions: London 03:00-04:40 EST, New York 08:00-09:55 EST
#   Body crossover (last 2 candles) + CCI trend direction → BUY/SELL signal
#   10-min cooldown between alerts
#   Price level alerts (configurable buy/sell thresholds)
#
# Environment variables (set in Render Dashboard):
#   FINNHUB_API_KEY     — from finnhub.io (free signup, no credit card)
#   TELEGRAM_BOT_TOKEN  — from @BotFather
#   TELEGRAM_CHAT_ID    — from @userinfobot
#   PRICE_LEVEL_SELL    — optional, e.g. 1.1200
#   PRICE_LEVEL_BUY     — optional, e.g. 1.0500
# ============================================================================

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional, List, Dict
from zoneinfo import ZoneInfo

import httpx
import numpy as np
import websockets
from fastapi import FastAPI

# ── Logging ────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ctc-monitor")

# ── Configuration from environment ─────────────────────────────────
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
PRICE_LEVEL_SELL = float(os.environ.get("PRICE_LEVEL_SELL", "0"))
PRICE_LEVEL_BUY = float(os.environ.get("PRICE_LEVEL_BUY", "0"))

SYMBOL = "OANDA:EUR/USD"
RESOLUTION = 5  # 5-minute candles
CCI_PERIOD = 15
ATR_PERIOD = 5
ATR_COEFF = 1.0
MIN_ALERT_INTERVAL_MINUTES = 10
MAX_CANDLES = 100
RECONNECT_BASE_DELAY = 2     # seconds
MAX_RECONNECT_DELAY = 60     # seconds

# Session times (America/New_York minutes from midnight)
LONDON_START = 180   # 03:00 EST
LONDON_END = 280     # 04:40 EST
NY_START = 480       # 08:00 EST
NY_END = 595         # 09:55 EST

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
# Candle Builder — builds M5 OHLC candles from real-time trade ticks
# ───────────────────────────────────────────────────────────────────
class CandleBuilder:
    """Accumulates trade ticks into 5-minute OHLC candles.

    Call add_trade() for each incoming tick. When a trade belongs to a
    new 5-minute window, the previous candle is returned as "completed".
    This gives us real-time candle closure detection (< 1 sec latency).
    """

    def __init__(self):
        self._candles: List[Dict] = []       # completed candles
        self._current: Optional[Dict] = None  # forming candle
        self._current_period_sec: Optional[int] = None

    def set_historical(self, candles: List[Dict]) -> None:
        """Load historical completed candles (e.g. from REST API on startup)."""
        self._candles = sorted(candles, key=lambda x: x["time"])[-MAX_CANDLES:]
        self._current = None
        self._current_period_sec = None
        logger.info(f"CandleBuilder loaded {len(self._candles)} historical candles")

    def add_trade(self, price: float, ts_ms: int) -> Optional[Dict]:
        """Process one trade tick.

        Args:
            price: Trade price
            ts_ms: Trade timestamp in milliseconds (UNIX)

        Returns:
            The completed candle dict if a period just closed, else None.
        """
        # Truncate millisecond timestamp to 5-minute boundary (in seconds)
        period_sec = (ts_ms // 300_000) * 300

        if self._current_period_sec is None:
            # First trade ever — start forming candle
            self._current_period_sec = period_sec
            self._current = {
                "time": datetime.fromtimestamp(period_sec, tz=timezone.utc),
                "open": price,
                "high": price,
                "low": price,
                "close": price,
            }
            return None

        if period_sec == self._current_period_sec:
            # Same 5-min window — update current candle
            c = self._current
            if price > c["high"]:
                c["high"] = price
            if price < c["low"]:
                c["low"] = price
            c["close"] = price  # latest price is the close
            return None

        # New 5-min window — previous candle is completed
        completed = self._current

        # Start new candle
        self._current_period_sec = period_sec
        utc_time = datetime.fromtimestamp(period_sec, tz=timezone.utc)
        self._current = {
            "time": utc_time,
            "open": price,
            "high": price,
            "low": price,
            "close": price,
        }

        # Archive completed candle
        if completed is not None:
            self._candles.append(completed)
            if len(self._candles) > MAX_CANDLES:
                self._candles = self._candles[-MAX_CANDLES:]

        return completed

    def get_all_candles(self) -> List[Dict]:
        """Return all completed candles plus the forming one."""
        result = list(self._candles)
        if self._current is not None:
            result.append(self._current)
        return result

    @property
    def candle_count(self) -> int:
        return len(self._candles) + (1 if self._current else 0)


# ───────────────────────────────────────────────────────────────────
# Main bot class
# ───────────────────────────────────────────────────────────────────
class CTCBot:
    def __init__(self):
        self._candle_builder = CandleBuilder()
        self._last_alert_time = datetime.min.replace(tzinfo=timezone.utc)
        self._last_sell_alert = datetime.min.replace(tzinfo=timezone.utc)
        self._last_buy_alert = datetime.min.replace(tzinfo=timezone.utc)
        self._shutdown_requested = False
        self._ws_task: Optional[asyncio.Task] = None
        self._signal_count = 0
        self.running = False

    # ════════════════════════════════════════════════════════════════
    # PUBLIC API
    # ════════════════════════════════════════════════════════════════

    async def start(self) -> None:
        """Start the bot: load history, connect WebSocket."""
        self._shutdown_requested = False
        self._signal_count = 0

        logger.info("=" * 55)
        logger.info("  CTC Strategy Monitor — Python (Finnhub WebSocket)")
        logger.info(f"  Symbol: EUR/USD  Timeframe: M5")
        logger.info(f"  Data: Real-time trade ticks → M5 candles in memory")
        logger.info(f"  Sessions: London ({_fmt_mins(LONDON_START)}-{_fmt_mins(LONDON_END)})")
        logger.info(f"            NY     ({_fmt_mins(NY_START)}-{_fmt_mins(NY_END)}) EST")
        if PRICE_LEVEL_SELL > 0:
            logger.info(f"  Sell level: {PRICE_LEVEL_SELL}")
        if PRICE_LEVEL_BUY > 0:
            logger.info(f"  Buy level:  {PRICE_LEVEL_BUY}")
        if not FINNHUB_API_KEY:
            logger.error("❌ FINNHUB_API_KEY not set!")
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            logger.warning("⚠️  TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set!")
        logger.info("=" * 55)

        # Step 1: Load historical M5 candles
        logger.info("Loading historical M5 candles from Finnhub REST API...")
        await self._load_historical_candles()

        # Step 2: Start WebSocket stream
        logger.info("Connecting to Finnhub WebSocket for real-time trades...")
        self.running = True
        if self._ws_task is None or self._ws_task.done():
            self._ws_task = asyncio.create_task(self._websocket_loop())

        logger.info("✅ Bot is running — real-time EUR/USD via Finnhub WebSocket")

    async def stop(self) -> None:
        """Stop the bot gracefully."""
        self._shutdown_requested = True
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        self.running = False
        logger.info(f"Bot stopped. Total signals sent: {self._signal_count}")

    # ════════════════════════════════════════════════════════════════
    # HISTORICAL DATA LOADING (Finnhub REST API)
    # ════════════════════════════════════════════════════════════════

    async def _load_historical_candles(self) -> None:
        """Fetch last 2 days of M5 candles via Finnhub REST API."""
        try:
            now_utc = datetime.now(timezone.utc)
            to_ts = int(now_utc.timestamp())
            from_ts = to_ts - 2 * 86400  # 2 days ago

            url = "https://finnhub.io/api/v1/forex/candle"
            params = {
                "symbol": SYMBOL,
                "resolution": str(RESOLUTION),
                "from": from_ts,
                "to": to_ts,
                "token": FINNHUB_API_KEY,
            }

            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()

            if data.get("s") != "ok":
                logger.warning(f"Finnhub REST returned status: {data.get('s')}")
                return

            timestamps = data["t"]
            opens = data["o"]
            highs = data["h"]
            lows = data["l"]
            closes = data["c"]

            candles = []
            for i in range(len(timestamps)):
                o, h, l, c = opens[i], highs[i], lows[i], closes[i]
                if any(v is None or (isinstance(v, float) and np.isnan(v)) for v in (o, h, l, c)):
                    continue
                candles.append({
                    "time": datetime.fromtimestamp(timestamps[i], tz=timezone.utc),
                    "open": float(o),
                    "high": float(h),
                    "low": float(l),
                    "close": float(c),
                })

            self._candle_builder.set_historical(candles)
            logger.info(f"✅ Loaded {len(candles)} historical M5 candles")

        except Exception as e:
            logger.error(f"Failed to load historical candles: {e}")

    # ════════════════════════════════════════════════════════════════
    # WEBSOCKET LOOP (with auto-reconnect)
    # ════════════════════════════════════════════════════════════════

    async def _websocket_loop(self) -> None:
        """Connect to Finnhub WebSocket, subscribe to EUR/USD, process ticks forever."""
        uri = f"wss://ws.finnhub.io?token={FINNHUB_API_KEY}"
        delay = RECONNECT_BASE_DELAY

        while not self._shutdown_requested:
            try:
                async with websockets.connect(uri) as ws:
                    logger.info("✅ WebSocket connected to Finnhub")
                    await ws.send(json.dumps({"type": "subscribe", "symbol": SYMBOL}))
                    logger.info(f"   Subscribed to {SYMBOL}")

                    # Reset backoff on successful connect
                    delay = RECONNECT_BASE_DELAY

                    async for raw_message in ws:
                        if self._shutdown_requested:
                            break
                        await self._handle_ws_message(raw_message)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"WebSocket error: {e}")

            if self._shutdown_requested:
                break

            # Exponential backoff reconnection
            logger.info(f"Reconnecting in {delay}s...")
            for _ in range(delay):
                if self._shutdown_requested:
                    break
                await asyncio.sleep(1)
            delay = min(delay * 2, MAX_RECONNECT_DELAY)

        logger.info("WebSocket loop ended")

    async def _handle_ws_message(self, raw: str) -> None:
        """Parse a Finnhub WebSocket message and process trade ticks."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        if msg.get("type") == "trade":
            trades = msg.get("data", [])
            for trade in trades:
                price = trade.get("p")
                ts_ms = trade.get("t")
                if price is None or ts_ms is None:
                    continue
                await self._process_trade(float(price), int(ts_ms))

    # ════════════════════════════════════════════════════════════════
    # TRADE PROCESSING → CANDLE CLOSURE DETECTION
    # ════════════════════════════════════════════════════════════════

    async def _process_trade(self, price: float, ts_ms: int) -> None:
        """Feed a trade tick to the CandleBuilder. If a candle closes, check signals."""
        if self._candle_builder.candle_count < CCI_PERIOD + 2:
            # Still warming up — just accumulate ticks
            self._candle_builder.add_trade(price, ts_ms)
            return

        completed = self._candle_builder.add_trade(price, ts_ms)

        if completed is not None:
            await self._on_candle_closed(completed)

    # ════════════════════════════════════════════════════════════════
    # CANDLE CLOSED — CHECK SIGNALS (real-time, < 1 sec from close)
    # ════════════════════════════════════════════════════════════════

    async def _on_candle_closed(self, candle: Dict) -> None:
        """An M5 candle just closed. Run Trend Magic + price checks."""
        close = candle["close"]
        candle_est = candle["time"].astimezone(EST_TZ)

        # Only alert during active London / NY sessions
        if not self._is_in_session(candle_est):
            logger.debug(f"Skipping {candle_est.strftime('%H:%M')} — outside session hours")
            return

        # Calculate Trend Magic on all available candles
        all_candles = self._candle_builder.get_all_candles()
        result = self._calculate_trend_magic(all_candles)
        if result is None:
            return

        logger.info(
            f"📊 M5 Close {candle_est.strftime('%H:%M')} | "
            f"Close: {close:.5f} | "
            f"MT: {result.magic_trend:.5f} | "
            f"CCI: {result.cci:.2f} | "
            f"Trend: {'BULL' if result.trend_bull else 'BEAR'} | "
            f"BuyX: {result.strong_buy} SellX: {result.strong_sell}"
        )

        # ── Trend Magic signal ──
        now_utc = datetime.now(timezone.utc)
        if result.strong_buy and result.trend_bull:
            await self._send_signal("BUY", close, result, candle_est, now_utc)
        elif result.strong_sell and not result.trend_bull:
            await self._send_signal("SELL", close, result, candle_est, now_utc)

        # ── Price level alerts ──
        await self._check_price_levels(close, candle_est, now_utc)

    # ════════════════════════════════════════════════════════════════
    # SESSION FILTER
    # ════════════════════════════════════════════════════════════════

    def _is_in_session(self, dt: Optional[datetime] = None) -> bool:
        """Check if the given time is within trading sessions."""
        if dt is None:
            dt = datetime.now(EST_TZ)
        elif dt.tzinfo is None:
            dt = dt.replace(tzinfo=EST_TZ)
        else:
            dt = dt.astimezone(EST_TZ)

        # Skip weekends
        if dt.weekday() >= 5:  # Sat=5, Sun=6
            return False

        minutes = dt.hour * 60 + dt.minute
        return (LONDON_START <= minutes < LONDON_END) or (NY_START <= minutes < NY_END)

    def _get_session_name(self, dt: datetime) -> str:
        mins = dt.hour * 60 + dt.minute
        if LONDON_START <= mins < LONDON_END:
            return "London"
        if NY_START <= mins < NY_END:
            return "New York"
        return ""

    # ════════════════════════════════════════════════════════════════
    # TREND MAGIC (numpy, matches cTrader logic exactly)
    # ════════════════════════════════════════════════════════════════

    def _calculate_trend_magic(self, candles: List[Dict]) -> Optional[TrendMagicResult]:
        closes = np.array([c["close"] for c in candles])
        highs = np.array([c["high"] for c in candles])
        lows = np.array([c["low"] for c in candles])
        opens = np.array([c["open"] for c in candles])
        n = len(closes)

        if n < max(CCI_PERIOD, ATR_PERIOD) + 2:
            return None

        # ── CCI ──
        tp = (highs + lows + closes) / 3
        cci = np.zeros(n)
        for i in range(CCI_PERIOD - 1, n):
            segment = tp[i - CCI_PERIOD + 1 : i + 1]
            sma = np.mean(segment)
            mad = np.mean(np.abs(segment - sma))
            cci[i] = (tp[i] - sma) / (0.015 * mad) if mad != 0 else 0

        # ── ATR ──
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

        # ── MagicTrend (recursive) ──
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

        # ── Body crossover (last 2 candles) ──
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

    async def _send_signal(
        self,
        signal_type: str,
        close: float,
        result: TrendMagicResult,
        candle_time_est: datetime,
        now_utc: datetime,
    ) -> None:
        # Cooldown check
        if (now_utc - self._last_alert_time).total_seconds() < MIN_ALERT_INTERVAL_MINUTES * 60:
            logger.debug("Cooldown active — skipping signal")
            return

        self._last_alert_time = now_utc
        self._signal_count += 1
        trend = "BULL" if result.trend_bull else "BEAR"

        message = (
            f"🚨 FX MOZO {signal_type} SIGNAL\n"
            f"Pair: EUR/USD\n"
            f"Price: {close:.5f}\n"
            f"Trend Magic: {result.magic_trend:.5f}\n"
            f"CCI: {result.cci:.2f} — {trend}\n"
            f"Session: {self._get_session_name(candle_time_est)}\n"
            f"Time: {candle_time_est.strftime('%H:%M')} EST"
        )
        await self._send_telegram(message)
        logger.info(f"🚨 Signal #{self._signal_count}: {signal_type} @ {close:.5f}")

    async def _check_price_levels(
        self, close: float, candle_time_est: datetime, now_utc: datetime
    ) -> None:
        all_candles = self._candle_builder.get_all_candles()
        if len(all_candles) < 5:
            return

        prev_close = all_candles[-2]["close"] if len(all_candles) >= 2 else close

        if PRICE_LEVEL_SELL > 0:
            crossed = (prev_close <= PRICE_LEVEL_SELL < close) or (
                prev_close >= PRICE_LEVEL_SELL > close
            )
            if crossed and (now_utc - self._last_sell_alert).total_seconds() >= MIN_ALERT_INTERVAL_MINUTES * 60:
                self._last_sell_alert = now_utc
                direction = "UP" if close > PRICE_LEVEL_SELL else "DOWN"
                await self._send_telegram(
                    f"🔴 PRICE LEVEL: SELL ({direction})\n"
                    f"Pair: EUR/USD\n"
                    f"Price: {close:.5f}\n"
                    f"Level: {PRICE_LEVEL_SELL:.5f}\n"
                    f"Time: {candle_time_est.strftime('%H:%M')} EST"
                )
                logger.info("🔴 Sell level triggered")

        if PRICE_LEVEL_BUY > 0:
            crossed = (prev_close <= PRICE_LEVEL_BUY < close) or (
                prev_close >= PRICE_LEVEL_BUY > close
            )
            if crossed and (now_utc - self._last_buy_alert).total_seconds() >= MIN_ALERT_INTERVAL_MINUTES * 60:
                self._last_buy_alert = now_utc
                direction = "UP" if close > PRICE_LEVEL_BUY else "DOWN"
                await self._send_telegram(
                    f"🟢 PRICE LEVEL: BUY ({direction})\n"
                    f"Pair: EUR/USD\n"
                    f"Price: {close:.5f}\n"
                    f"Level: {PRICE_LEVEL_BUY:.5f}\n"
                    f"Time: {candle_time_est.strftime('%H:%M')} EST"
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
    return f"{m // 60:02d}:{m % 60:02d}"


# ── FastAPI lifecycle hooks ────────────────────────────────────────
@app.on_event("startup")
async def _startup():
    global bot_instance
    logger.info("Starting CTC Monitor (Finnhub WebSocket)...")
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
        "candles": bot_instance._candle_builder.candle_count if bot_instance else 0,
        "signals": bot_instance._signal_count if bot_instance else 0,
        "time": datetime.now(EST_TZ).isoformat(),
    }


@app.get("/status")
async def status():
    """Return bot state (for debugging)."""
    if not bot_instance:
        return {"status": "not started"}
    return {
        "status": "running" if bot_instance.running else "stopped",
        "candles_cached": bot_instance._candle_builder.candle_count,
        "signals_sent": bot_instance._signal_count,
        "ws_connected": bot_instance._ws_task is not None and not bot_instance._ws_task.done(),
        "last_alert": (
            bot_instance._last_alert_time.isoformat()
            if bot_instance._last_alert_time != datetime.min.replace(tzinfo=timezone.utc)
            else None
        ),
    }
