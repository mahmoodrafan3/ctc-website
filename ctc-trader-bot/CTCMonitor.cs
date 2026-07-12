// ============================================================================
// CTC Strategy Monitor — cTrader Automate Bot
// ============================================================================
// 
// How to use:
// 1. Get a Telegram bot token from @BotFather (free, 30 seconds)
// 2. Find your chat ID: message @userinfobot or visit
//    https://api.telegram.org/bot<TOKEN>/getUpdates
// 3. Paste both as parameters below
// 4. Compile (F7) and run on BTCUSD M5 chart
// 5. The bot sends alerts directly to Telegram via HTTPS!
//
// ── Delivery modes (in order of priority) ─────────────────────────
//   1. Direct Telegram API  →  api.telegram.org (simplest, no server)
//   2. Webhook URL relay    →  your relay server (Pipedream / Vercel)
//
// Mode 1 is preferred — it works in cTrader Cloud with
// AccessRights.Internet enabled and needs no external services.
// ============================================================================

using System;
using System.Net;
using cAlgo.API;
using cAlgo.API.Indicators;

namespace cAlgo.Robots
{
    [Robot(TimeZone = TimeZones.UTC, AccessRights = AccessRights.Internet)]
    public class CTCMonitor : Robot
    {
        // ════════════════════════════════════════════════════════════════
        // CONFIGURATION — EDIT THESE VALUES BEFORE RUNNING
        // ════════════════════════════════════════════════════════════════
        
        [Parameter("Bot Token", Group = "Telegram", DefaultValue = "")]
        public string BotToken { get; set; } = "";
        
        [Parameter("Chat ID", Group = "Telegram", DefaultValue = "")]
        public string ChatId { get; set; } = "";
        
        // Optional: override Webhook URL (e.g. Pipedream, Vercel relay)
        // Leave empty to use direct Telegram API (recommended)
        [Parameter("Webhook URL", Group = "Telegram", DefaultValue = "")]
        public string WebhookUrl { get; set; } = "";
        
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
        private TimeZoneInfo _estZone;
        private const string UserAgent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)";
        
        protected override void OnStart()
        {
            _estZone = TimeZoneInfo.FindSystemTimeZoneById("America/New_York");
            Print("🚀 CTC Strategy Monitor started on " + SymbolName + " " + TimeFrame);
            
            if (string.IsNullOrEmpty(BotToken) || string.IsNullOrEmpty(ChatId))
                Print("⚠️  Bot Token and/or Chat ID not set! Get a token from @BotFather on Telegram.");
            else
                Print("   Telegram alerts enabled — sending to chat " + ChatId);
            
            if (!string.IsNullOrEmpty(WebhookUrl))
                Print("   Using webhook relay: " + WebhookUrl);
            else
                Print("   Using direct Telegram API (no relay needed)");
            
            Print("   Session filter: " + (UseSessionFilter ? "ON (London/NY only)" : "OFF (24/7 mode)"));
            Print("   Weekend filter: " + (SkipWeekends ? "ON (no trading Sat/Sun)" : "OFF (trades 24/7)"));
            Print("   Price level alerts: Sell @ " + PriceLevelSell + " | Buy @ " + PriceLevelBuy);
            if (SendHeartbeat)
                Print("   Heartbeat enabled — sending test alert every candle");
        }
        
        protected override void OnBar()
        {
            if (!IsInSession(out string sessionName))
                return;
            
            if (Bars.Count < 2)
                return;
            
            double close = Bars.Last(0).Close;
            
            // ── Heartbeat (test: confirms Telegram works every candle) ──
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
            if (Bars.Count < MinBarsBeforeLevelAlerts)
                return;
            
            double prevClose = Bars.Last(1).Close;
            
            // ── SELL level ──
            if (PriceLevelSell > 0)
            {
                bool crossed = (prevClose <= PriceLevelSell && close > PriceLevelSell)
                            || (prevClose >= PriceLevelSell && close < PriceLevelSell);
                bool cooldownOk = (Server.Time - _lastSellLevelAlert).TotalMinutes >= MinMinutesBetweenAlerts;
                
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
                    Print("🔴 SELL level triggered!");
                }
            }
            
            // ── BUY level ──
            if (PriceLevelBuy > 0)
            {
                bool crossed = (prevClose <= PriceLevelBuy && close > PriceLevelBuy)
                            || (prevClose >= PriceLevelBuy && close < PriceLevelBuy);
                bool cooldownOk = (Server.Time - _lastBuyLevelAlert).TotalMinutes >= MinMinutesBetweenAlerts;
                
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
                    Print("🟢 BUY level triggered!");
                }
            }
        }
        
        private bool IsInSession(out string sessionName)
        {
            DateTime estTime = TimeZoneInfo.ConvertTime(Server.Time, TimeZoneInfo.Utc, _estZone);
            
            if (SkipWeekends && (estTime.DayOfWeek == DayOfWeek.Saturday || estTime.DayOfWeek == DayOfWeek.Sunday))
            {
                sessionName = "Weekend";
                return false;
            }
            
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
        
        // ════════════════════════════════════════════════════════════════
        // TELEGRAM ALERT — Two delivery modes
        // ════════════════════════════════════════════════════════════════
        
        /// <summary>
        /// Send a Telegram alert.
        /// Mode 1 (preferred): Direct GET request to api.telegram.org
        /// Mode 2 (fallback):  POST JSON to a webhook relay URL
        /// </summary>
        private void SendTelegramAlert(string message)
        {
            if (string.IsNullOrEmpty(BotToken) || string.IsNullOrEmpty(ChatId))
            {
                Print("⚠️  Cannot send alert — Bot Token or Chat ID not set");
                return;
            }
            
            // Mode 1: Direct Telegram API via GET request
            if (string.IsNullOrEmpty(WebhookUrl))
            {
                SendDirectTelegram(message);
            }
            // Mode 2: Webhook relay (Pipedream / Vercel / etc.)
            else
            {
                SendViaWebhook(message);
            }
        }
        
        /// <summary>
        /// Send alert directly to Telegram API via HTTPS GET.
        /// Uses cTrader's native Http.Send() — works in cTrader Cloud
        /// with AccessRights.Internet enabled.
        /// </summary>
        private void SendDirectTelegram(string message)
        {
            try
            {
                // URL-encode the message text for safe inclusion in a query string
                string encoded = Uri.EscapeDataString(message);
                string url = "https://api.telegram.org/bot" + BotToken
                    + "/sendMessage?chat_id=" + ChatId
                    + "&text=" + encoded
                    + "&parse_mode=HTML";
                
                var request = new HttpRequest(url);
                request.Method = HttpMethod.Get;
                
                // Set a browser-like User-Agent to avoid any blocking
                request.Headers.Add("User-Agent", UserAgent);
                
                var response = Http.Send(request);
                
                if (response.IsSuccessful)
                {
                    Print("✅ Telegram alert sent");
                }
                else
                {
                    string body = response.Body ?? "(empty)";
                    Print("❌ Telegram error: HTTP " + response.StatusCode + " — " + body);
                }
            }
            catch (Exception ex)
            {
                Print("❌ Telegram send failed: " + ex.Message);
            }
        }
        
        /// <summary>
        /// Send alert via webhook relay URL (e.g. Pipedream, Vercel).
        /// Sends chat_id + text as JSON body via HTTP POST.
        /// </summary>
        private void SendViaWebhook(string message)
        {
            try
            {
                var uri = new Uri(WebhookUrl);
                var request = new HttpRequest(uri);
                request.Method = HttpMethod.Post;
                request.Headers.Add("User-Agent", UserAgent);
                request.Headers.Add("Content-Type", "application/json");
                
                // Simple JSON: { "chat_id": "...", "text": "..." }
                string escaped = message
                    .Replace("\\", "\\\\")
                    .Replace("\"", "\\\"")
                    .Replace("\n", "\\n")
                    .Replace("\r", "\\r")
                    .Replace("\t", "\\t");
                request.Body = "{\"chat_id\":\"" + ChatId + "\",\"text\":\"" + escaped + "\"}";
                
                var response = Http.Send(request);
                
                if (response.IsSuccessful)
                {
                    Print("✅ Alert sent via webhook relay");
                }
                else
                {
                    string body = response.Body ?? "(empty)";
                    Print("⚠️ Webhook relay error: HTTP " + response.StatusCode + " — " + body);
                }
            }
            catch (Exception ex)
            {
                Print("⚠️ Webhook relay failed: " + ex.Message);
            }
        }
        
        // ════════════════════════════════════════════════════════════════
        
        private TrendMagicResult CalculateTrendMagic()
        {
            int startIdx = Math.Max(0, Bars.Count - Math.Max(CciPeriod, AtrPeriod) - 10);
            int len = Bars.Count - startIdx;
            
            // Compute ATR
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
            
            // Check last 2 candles for body crossover
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
        
        protected override void OnStop()
        {
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
