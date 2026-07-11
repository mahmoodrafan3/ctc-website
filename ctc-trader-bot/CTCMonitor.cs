// ============================================================================
// CTC Strategy Monitor — cTrader Automate Bot
// ============================================================================
// 
// How to use:
// 1. Open cTrader → Automate → New Bot
// 2. Copy this entire file into the editor
// 3. Set your TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID below
// 4. Compile (F7) and run on BTCUSD M5 chart
// 5. The bot will send Telegram alerts on Trend Magic crossovers!
//
// ── Delivery modes (order of priority) ────────────────────────────
//
//   1. VERCEL WEBHOOK RELAY ✅ (recommended for cTrader Cloud)
//      ═══════════════════════
//      cTrader Cloud → HTTP POST (no Cloudflare) →
//        Vercel Serverless Function → HTTPS (clean IP) → Telegram
//
//      Set "Webhook URL" in cTrader parameters to:
//        https://ctc-strategy.vercel.app/api/webhook/ctc-alert
//
//      Requires TELEGRAM_BOT_TOKEN set as an env var on Vercel.
//      The bot sends chat_id + text to Vercel, which relays to Telegram.
//
//   2. WebSocket relay (ws://your-server:25345)
//      ═══════════════════════════════════════
//      Works on cTrader Cloud. Requires your own relay server.
//
//   3. Direct Telegram (api.telegram.org)
//      ═════════════════════════════════
//      Best for local execution on your own PC (not cTrader Cloud).
//      cTrader Cloud may block api.telegram.org — use Vercel relay instead.
//
//   4. External webhook relay (Make.com / Pipedream / etc.)
//      ═════════════════════════════════════════════════════
//      Works if you have an existing Zapier/Make/Pipedream workflow.
//
// ============================================================================

using System;
using System.Collections.Generic;
using cAlgo.API;
using cAlgo.API.Indicators;


namespace cAlgo.Robots
{
    [Robot(TimeZone = TimeZones.UTC, AccessRights = AccessRights.None)]
    public class CTCMonitor : Robot
    {
        // ════════════════════════════════════════════════════════════════
        // CONFIGURATION — EDIT THESE VALUES BEFORE RUNNING
        // ════════════════════════════════════════════════════════════════
        
        [Parameter("Telegram Bot Token", Group = "Telegram", DefaultValue = "8899864917:AAE-8bHbEKTfjzsIenWnacSZ79Gt0SKBdgM")]
        public string TelegramBotToken { get; set; } = "8899864917:AAE-8bHbEKTfjzsIenWnacSZ79Gt0SKBdgM";
        
        [Parameter("Telegram Chat ID", Group = "Telegram", DefaultValue = "1235128870")]
        public string TelegramChatId { get; set; } = "1235128870";
        
        [Parameter("Webhook URL", Group = "Telegram", DefaultValue = "")]
        public string WebhookUrl { get; set; } = "";
        
        [Parameter("WebSocket URL", Group = "Telegram", DefaultValue = "")]
        public string WebSocketUrl { get; set; } = "";
        
        // Trend Magic parameters
        private const int CciPeriod = 15;
        private const int AtrPeriod = 5;
        private const double AtrCoeff = 1.0;
        
        [Parameter("Use Session Filter", Group = "Trading", DefaultValue = true)]
        public bool UseSessionFilter { get; set; } = true;
        
        [Parameter("Skip Weekends", Group = "Trading", DefaultValue = true)]
        public bool SkipWeekends { get; set; } = true;
        
        [Parameter("Price Level - Sell", Group = "Price Alerts", DefaultValue = 112024.0)]
        public double PriceLevelSell { get; set; } = 112024.0;
        
        [Parameter("Price Level - Buy", Group = "Price Alerts", DefaultValue = 173799.0)]
        public double PriceLevelBuy { get; set; } = 173799.0;
        
        [Parameter("Send Heartbeat (test)", Group = "Test", DefaultValue = false)]
        public bool SendHeartbeat { get; set; } = false;
        
        // Session times (America/New_York minutes)
        private const int LondonStart = 180;   // 03:00 EST
        private const int LondonEnd = 280;     // 04:40 EST
        private const int NyStart = 480;       // 08:00 EST
        private const int NyEnd = 595;         // 09:55 EST
        
        // ════════════════════════════════════════════════════════════════
        
        private DateTime _lastAlertTime = DateTime.MinValue;
        private DateTime _lastSellLevelAlert = DateTime.MinValue;
        private DateTime _lastBuyLevelAlert = DateTime.MinValue;
        private const int MinMinutesBetweenAlerts = 10;
        private const int MinBarsBeforeLevelAlerts = 5;
        private const int MaxPendingMessages = 100;
        private TimeZoneInfo _estZone;
        private Queue<string> _pendingMessages = new Queue<string>();
        private int _telegramHostIndex = 0;
        private static readonly string[] TelegramHosts = {
            "api.telegram.org", "api2.telegram.org", "api3.telegram.org",
            "api4.telegram.org", "api5.telegram.org"
        };
        private WebSocketClient _webSocketClient;
        private bool _webSocketConnected;
        private const string BrowserUserAgent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36";
        
        protected override void OnStart()
        {
            _estZone = TimeZoneInfo.FindSystemTimeZoneById("America/New_York");
            Print("🚀 CTC Strategy Monitor started on " + SymbolName + " " + TimeFrame);
            
            // ── Connect WebSocket relay if configured ──
            if (!string.IsNullOrEmpty(WebSocketUrl))
            {
                ConnectWebSocket();
            }
            else if (!string.IsNullOrEmpty(WebhookUrl))
            {
                Print("   Telegram alerts via webhook relay: " + WebhookUrl);
            }
            else
            {
                Print("   Telegram alerts enabled — direct to Telegram API");
            }
            Print("   Session filter: " + (UseSessionFilter ? "ON (London/NY only)" : "OFF (24/7 mode)"));
            Print("   Weekend filter: " + (SkipWeekends ? "ON (no trading Sat/Sun)" : "OFF (trades 24/7)"));
            Print("   Price level alerts: Sell @ " + PriceLevelSell + " | Buy @ " + PriceLevelBuy);
            if (SendHeartbeat)
                Print("   Heartbeat enabled — sending Telegram ping every candle");
        }
        
        protected override void OnBar()
        {
            // Try to reconnect WebSocket if disconnected
            if (!string.IsNullOrEmpty(WebSocketUrl) && !_webSocketConnected && _webSocketClient != null)
            {
                Print("   Attempting to reconnect WebSocket...");
                _webSocketClient.Dispose();
                _webSocketClient = null;
                ConnectWebSocket();
            }

            // Try to flush any previously failed messages before processing new signals
            FlushPendingMessages();

            if (!IsInSession(out string sessionName))
                return;
            
            if (Bars.Count < 2)
                return;
            
            double close = Bars.Last(0).Close;
            double low = Bars.Last(0).Low;
            double high = Bars.Last(0).High;
            
            // ── Heartbeat (temp: confirms Telegram works on every candle) ──
            if (SendHeartbeat)
            {
                string heartbeatMsg = string.Format(
                    "💓 Heartbeat | {0} | Close: {1:F5} | Time: {2:HH:mm} EST",
                    SymbolName, close, Server.Time);
                SendTelegramAlert(heartbeatMsg);
            }
            
            // ── Check price level alerts ──
            CheckPriceLevels(close, sessionName);
            
            // ── Check Trend Magic signals ──
            if (Bars.Count < Math.Max(CciPeriod, AtrPeriod) + 2)
                return;
            
            var result = CalculateTrendMagic();
            
            bool signalBuy = result.StrongBuy && result.TrendBull;
            bool signalSell = result.StrongSell && !result.TrendBull;
            
            if (!signalBuy && !signalSell)
                return;
            
            if ((Server.Time - _lastAlertTime).TotalMinutes < MinMinutesBetweenAlerts)
                return;
            
            _lastAlertTime = Server.Time;
            
            string signalType = signalBuy ? "BUY" : "SELL";
            string trend = result.TrendBull ? "BULL" : "BEAR";
            
            string message = string.Format(
                "🚨 FX MOZO {0} SIGNAL\n" +
                "Pair: {1}\n" +
                "Price: {2:F5}\n" +
                "Trend Magic: {3:F5}\n" +
                "Session: {4}\n" +
                "CCI: {5:F2} — {6}\n" +
                "Time: {7:HH:mm} EST",
                signalType,
                SymbolName,
                close,
                result.MagicTrend,
                sessionName,
                result.Cci,
                trend,
                Server.Time);
            
            SendTelegramAlert(message);
            
            Print("🚨 " + signalType + " SIGNAL on " + SymbolName + " at " + close);
        }
        
        private void CheckPriceLevels(double close, string sessionName)
        {
            // Wait for enough bars to avoid false alerts on bot startup
            if (Bars.Count < MinBarsBeforeLevelAlerts)
                return;
            
            double prevClose = Bars.Last(1).Close;
            
            // ── SELL level: close crossed the level in either direction ──
            if (PriceLevelSell > 0)
            {
                bool crossed = (prevClose <= PriceLevelSell && close > PriceLevelSell)
                            || (prevClose >= PriceLevelSell && close < PriceLevelSell);
                bool cooldownOk = (Server.Time - _lastSellLevelAlert).TotalMinutes >= MinMinutesBetweenAlerts;
                
                Print("🔍 SELL: prevC=" + prevClose + " C=" + close + " lvl=" + PriceLevelSell + " crossed=" + crossed + " cooldown=" + cooldownOk);
                
                if (crossed && cooldownOk)
                {
                    _lastSellLevelAlert = Server.Time;
                    string direction = close > PriceLevelSell ? "UP" : "DOWN";
                    string msg = string.Format(
                        "🔴 PRICE LEVEL: SELL ({0})\n" +
                        "Pair: {1}\n" +
                        "Price: {2:F5}\n" +
                        "Level: {3:F5}\n" +
                        "Session: {4}\n" +
                        "Time: {5:HH:mm} EST",
                        direction, SymbolName, close, PriceLevelSell, sessionName, Server.Time);
                    SendTelegramAlert(msg);
                    Print("🔴 SELL level triggered! Alert sent.");
                }
            }
            
            // ── BUY level: close crossed the level in either direction ──
            if (PriceLevelBuy > 0)
            {
                bool crossed = (prevClose <= PriceLevelBuy && close > PriceLevelBuy)
                            || (prevClose >= PriceLevelBuy && close < PriceLevelBuy);
                bool cooldownOk = (Server.Time - _lastBuyLevelAlert).TotalMinutes >= MinMinutesBetweenAlerts;
                
                Print("🔍 BUY: prevC=" + prevClose + " C=" + close + " lvl=" + PriceLevelBuy + " crossed=" + crossed + " cooldown=" + cooldownOk);
                
                if (crossed && cooldownOk)
                {
                    _lastBuyLevelAlert = Server.Time;
                    string direction = close > PriceLevelBuy ? "UP" : "DOWN";
                    string msg = string.Format(
                        "🟢 PRICE LEVEL: BUY ({0})\n" +
                        "Pair: {1}\n" +
                        "Price: {2:F5}\n" +
                        "Level: {3:F5}\n" +
                        "Session: {4}\n" +
                        "Time: {5:HH:mm} EST",
                        direction, SymbolName, close, PriceLevelBuy, sessionName, Server.Time);
                    SendTelegramAlert(msg);
                    Print("🟢 BUY level triggered! Alert sent.");
                }
            }
        }
        
        private bool IsInSession(out string sessionName)
        {
            DateTime estTime = TimeZoneInfo.ConvertTime(Server.Time, TimeZoneInfo.Utc, _estZone);
            
            // Skip weekends if enabled (crypto trades 24/7, so disable for BTCUSD etc.)
            if (SkipWeekends && (estTime.DayOfWeek == DayOfWeek.Saturday || estTime.DayOfWeek == DayOfWeek.Sunday))
            {
                sessionName = "Weekend";
                return false;
            }
            
            // If session filter is disabled, run 24/7 (e.g. for crypto like BTCUSD)
            if (!UseSessionFilter)
            {
                sessionName = "24/7";
                return true;
            }
            
            int minutes = estTime.Hour * 60 + estTime.Minute;
            
            if (minutes >= LondonStart && minutes < LondonEnd)
            {
                sessionName = "London";
                return true;
            }
            if (minutes >= NyStart && minutes < NyEnd)
            {
                sessionName = "New York";
                return true;
            }
            
            sessionName = "";
            return false;
        }
        
        private TrendMagicResult CalculateTrendMagic()
        {
            int startIdx = Math.Max(0, Bars.Count - Math.Max(CciPeriod, AtrPeriod) - 10);
            int len = Bars.Count - startIdx;
            
            // Compute ATR (SMA of True Range)
            double[] atr = new double[len];
            for (int i = AtrPeriod; i < len; i++)
            {
                double sum = 0;
                for (int j = i - AtrPeriod + 1; j <= i; j++)
                {
                    int idx = startIdx + j;
                    double tr = Math.Max(
                        Bars.HighPrices[idx] - Bars.LowPrices[idx],
                        Math.Max(
                            Math.Abs(Bars.HighPrices[idx] - Bars.ClosePrices[idx - 1]),
                            Math.Abs(Bars.LowPrices[idx] - Bars.ClosePrices[idx - 1])));
                    sum += tr;
                }
                atr[i] = sum / AtrPeriod;
            }
            
            // Compute CCI on close
            double[] cci = new double[len];
            for (int i = CciPeriod - 1; i < len; i++)
            {
                double sum = 0;
                for (int j = i - CciPeriod + 1; j <= i; j++)
                    sum += Bars.ClosePrices[startIdx + j];
                double sma = sum / CciPeriod;
                
                double md = 0;
                for (int j = i - CciPeriod + 1; j <= i; j++)
                    md += Math.Abs(Bars.ClosePrices[startIdx + j] - sma);
                md /= CciPeriod;
                
                if (md != 0)
                    cci[i] = (Bars.ClosePrices[startIdx + i] - sma) / (0.015 * md);
            }
            
            // Compute MagicTrend recursively
            double[] magicTrend = new double[len];
            bool firstValid = false;
            
            for (int i = 1; i < len; i++)
            {
                double cciVal = cci[i];
                double atrVal = atr[i];
                if (atrVal == 0) continue;
                
                int idx = startIdx + i;
                double upT = Bars.LowPrices[idx] - atrVal * AtrCoeff;
                double downT = Bars.HighPrices[idx] + atrVal * AtrCoeff;
                
                if (!firstValid)
                {
                    magicTrend[i] = cciVal >= 0 ? upT : downT;
                    firstValid = true;
                }
                else
                {
                    double prev = magicTrend[i - 1];
                    magicTrend[i] = cciVal >= 0
                        ? Math.Max(upT, prev)
                        : Math.Min(downT, prev);
                }
            }
            
            // Check last 2 candles for body crossover against their own magic_trend
            bool strongBuy = false, strongSell = false;
            for (int i = Math.Max(1, len - 2); i < len; i++)
            {
                double mt = magicTrend[i];
                int idx = startIdx + i;
                if (Bars.OpenPrices[idx] < mt && Bars.ClosePrices[idx] > mt)
                    strongBuy = true;
                if (Bars.OpenPrices[idx] > mt && Bars.ClosePrices[idx] < mt)
                    strongSell = true;
            }
            
            return new TrendMagicResult
            {
                MagicTrend = magicTrend[len - 1],
                Cci = cci[len - 1],
                StrongBuy = strongBuy,
                StrongSell = strongSell,
                TrendBull = cci[len - 1] >= 0
            };
        }
        
        /// <summary>
        /// Retry any previously failed messages. Called at the start of each OnBar.
        /// Stops on first failure so remaining queue is retried next candle.
        /// </summary>
        private void FlushPendingMessages()
        {
            while (_pendingMessages.Count > 0)
            {
                string msg = _pendingMessages.Peek();
                if (TrySendTelegramOnce(msg, out bool permanent))
                {
                    _pendingMessages.Dequeue();
                }
                else
                {
                    if (permanent)
                    {
                        // Client error — discard this message permanently
                        _pendingMessages.Dequeue();
                        Print("🗑️ Discarded permanently failed message (" + _pendingMessages.Count + " remaining)");
                    }
                    else
                    {
                        Print("⏳ " + _pendingMessages.Count + " message(s) queued — retrying next candle");
                    }
                    return; // Stop on first failure
                }
            }
        }

        /// <summary>
        /// Connect to the WebSocket relay server.
        /// </summary>
        private void ConnectWebSocket()
        {
            try
            {
                _webSocketClient = new WebSocketClient();
                
                _webSocketClient.Connected += (args) =>
                {
                    _webSocketConnected = true;
                    Print("✅ Connected to WebSocket relay at " + WebSocketUrl);
                    // Flush any queued messages now that we're connected
                    FlushPendingMessages();
                };
                
                _webSocketClient.Disconnected += (args) =>
                {
                    _webSocketConnected = false;
                    Print("⚠️ Disconnected from WebSocket relay — messages will queue");
                };
                
                _webSocketClient.TextReceived += (args) =>
                {
                    string response = args.Text;
                    if (response == "OK")
                        Print("✅ Telegram alert delivered via WebSocket relay");
                    else if (response == "CONNECTED")
                        Print("   WebSocket relay handshake complete");
                    else if (response.StartsWith("ERROR:"))
                        Print("⚠️ WebSocket relay error: " + response.Substring(6));
                };
                
                var uri = new Uri(WebSocketUrl);
                _webSocketClient.Connect(uri);
                Print("   Connecting to WebSocket relay...");
            }
            catch (Exception ex)
            {
                Print("❌ Failed to connect WebSocket: " + ex.Message);
                _webSocketConnected = false;
            }
        }

        /// <summary>
        /// Try sending once. Priority: WebSocket relay > Webhook relay > direct Telegram.
        /// Sets permanent=true for errors that should never be retried.
        /// </summary>
        private bool TrySendTelegramOnce(string message, out bool permanent)
        {
            permanent = false;

            // ── WebSocket relay mode ──
            if (!string.IsNullOrEmpty(WebSocketUrl))
                return TrySendViaWebSocket(message, out permanent);

            // ── Webhook relay mode (Make.com / Pipedream / etc.) ──
            if (!string.IsNullOrEmpty(WebhookUrl))
                return TrySendViaWebhook(message, out permanent);

            // ── Direct Telegram mode ──
            for (int i = 0; i < TelegramHosts.Length; i++)
            {
                string host = TelegramHosts[(_telegramHostIndex + i) % TelegramHosts.Length];

                try
                {
                    var uri = new Uri("https://" + host + "/bot" + TelegramBotToken + "/sendMessage");
                    var request = new HttpRequest(uri);
                    request.Method = HttpMethod.Post;
                    request.Headers.Add("User-Agent", BrowserUserAgent);
                    request.Headers.Add("Content-Type", "application/json");
                    request.Body = BuildTelegramJson(message);

                    var response = Http.Send(request);

                    if (response.IsSuccessful)
                    {
                        _telegramHostIndex = (_telegramHostIndex + i) % TelegramHosts.Length;
                        return true;
                    }

                    string body = response.Body ?? "(empty)";

                    if (response.StatusCode >= 400 && response.StatusCode < 500)
                    {
                        Print("❌ Telegram client error " + response.StatusCode + " on " + host + " - " + body + " (permanent, discarding)");
                        permanent = true;
                        return false;
                    }

                    if (i < TelegramHosts.Length - 1)
                        Print("⚠️ HTTP " + response.StatusCode + " on " + host + " — trying next host...");
                    else
                        Print("⚠️ Telegram send failed on all hosts (HTTP " + response.StatusCode + ") — queuing for retry");
                }
                catch (Exception ex)
                {
                    if (i < TelegramHosts.Length - 1)
                        Print("⚠️ Exception on " + host + ": " + ex.Message + " — trying next host...");
                    else
                        Print("⚠️ Telegram exception on all hosts: " + ex.Message + " — queuing for retry");
                }
            }

            return false;
        }



        /// <summary>
        /// Send message via WebSocket relay. If not connected, queues it.
        /// The relay server forwards to Telegram and sends back OK/ERROR.
        /// </summary>
        private bool TrySendViaWebSocket(string message, out bool permanent)
        {
            permanent = false;

            if (!_webSocketConnected || _webSocketClient == null)
            {
                Print("⚠️ WebSocket not connected — queuing for retry");
                return false;
            }

            try
            {
                _webSocketClient.Send(message);
                return true; // Assume success — relay sends back OK/ERROR via TextReceived
            }
            catch (Exception ex)
            {
                Print("⚠️ WebSocket send failed: " + ex.Message + " — queuing for retry");
                _webSocketConnected = false;
                return false;
            }
        }

        /// <summary>
        /// Send message to a webhook relay URL (Make.com, Pipedream, etc.)
        /// which then forwards to Telegram. Sends chat_id + text as JSON.
        /// </summary>
        private bool TrySendViaWebhook(string message, out bool permanent)
        {
            permanent = false;

            try
            {
                var uri = new Uri(WebhookUrl);
                var request = new HttpRequest(uri);
                request.Method = HttpMethod.Post;
                request.Headers.Add("User-Agent", BrowserUserAgent);
                request.Headers.Add("Content-Type", "application/json");

                string escapedText = EscapeJson(message);
                request.Body = "{\"chat_id\":\"" + TelegramChatId + "\",\"text\":\"" + escapedText + "\"}";

                var response = Http.Send(request);

                if (response.IsSuccessful)
                {
                    Print("✅ Alert sent via webhook relay");
                    return true;
                }

                string body = response.Body ?? "(empty)";

                if (response.StatusCode >= 400 && response.StatusCode < 500)
                {
                    Print("❌ Webhook error " + response.StatusCode + " - " + body + " (permanent, discarding)");
                    permanent = true;
                    return false;
                }

                Print("⚠️ Webhook relay failed (HTTP " + response.StatusCode + ") — queuing for retry");
                return false;
            }
            catch (Exception ex)
            {
                Print("⚠️ Webhook exception: " + ex.Message + " — queuing for retry");
                return false;
            }
        }

        /// <summary>
        /// Build JSON body for direct Telegram API call.
        /// </summary>
        private string BuildTelegramJson(string message)
        {
            string escaped = EscapeJson(message);
            return "{\"chat_id\":\"" + TelegramChatId + "\",\"text\":\"" + escaped + "\"}";
        }

        /// <summary>
        /// Escape a string for safe inclusion in a JSON string value.
        /// </summary>
        private static string EscapeJson(string text)
        {
            return text
                .Replace("\\", "\\\\")
                .Replace("\"", "\\\"")
                .Replace("\n", "\\n")
                .Replace("\r", "\\r")
                .Replace("\t", "\\t");
        }


        /// <summary>
        /// Send a Telegram alert. Tries immediately; on transient failure, queues it
        /// for retry on the next candle. On permanent failure (4xx), discards it.
        /// </summary>
        private void SendTelegramAlert(string message)
        {
            // First, try to send immediately
            if (TrySendTelegramOnce(message, out bool permanent))
                return;

            if (permanent)
                return; // Client error, not worth queuing

            // Queue for retry on subsequent candles
            if (_pendingMessages.Count < MaxPendingMessages)
            {
                _pendingMessages.Enqueue(message);
                Print("📥 Message queued for retry on next candle (" + _pendingMessages.Count + " pending)");
            }
            else
            {
                Print("⚠️ Telegram queue full (" + MaxPendingMessages + "), discarding oldest");
                _pendingMessages.Dequeue();
                _pendingMessages.Enqueue(message);
            }
        }
        
        protected override void OnStop()
        {
            // Close WebSocket connection if active
            if (_webSocketClient != null)
            {
                if (_webSocketConnected)
                    _webSocketClient.Close(WebSocketClientCloseStatus.NormalClosure);
                _webSocketClient.Dispose();
                _webSocketClient = null;
                _webSocketConnected = false;
            }
            Print("CTC Strategy Monitor stopped");
        }
    }
    
    internal class TrendMagicResult
    {
        public double MagicTrend { get; set; }
        public double Cci { get; set; }
        public bool StrongBuy { get; set; }
        public bool StrongSell { get; set; }
        public bool TrendBull { get; set; }
    }
}
