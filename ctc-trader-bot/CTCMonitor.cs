// ============================================================================
// CTC Strategy Monitor — cTrader Automate Bot
// ============================================================================
// 
// How to use:
// 1. Search @mail2telegrambot on Telegram → Start → copy your unique email
// 2. Paste that email as the "Telegram Email" parameter below
// 3. Set up SMTP in cTrader: Settings → Email (use Gmail SMTP or any provider)
// 4. Compile (F7) and run on BTCUSD M5 chart
// 5. The bot will send alerts via email, which the Telegram bot forwards to you
//
// ── How it works ───────────────────────────────────────────────────
//   cTrader Cloud → Notifications.SendEmail() → SMTP server
//     → mail2telegrambot → Telegram chat ✅
//
//   No Vercel server, no WebSocket, no direct HTTP to Telegram.
//   Email is natively allowed in cTrader Cloud — fastest + most reliable.
// ============================================================================

using System;
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
        
        [Parameter("Telegram Email", Group = "Telegram", DefaultValue = "")]
        public string TelegramEmail { get; set; } = "";
        
        [Parameter("From Email", Group = "Telegram", DefaultValue = "")]
        public string FromEmail { get; set; } = "";
        
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
        
        [Parameter("Email Subject Prefix", Group = "Telegram", DefaultValue = "🤖 CTC Alert")]
        public string EmailSubject { get; set; } = "🤖 CTC Alert";
        
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
        
        protected override void OnStart()
        {
            _estZone = TimeZoneInfo.FindSystemTimeZoneById("America/New_York");
            Print("🚀 CTC Strategy Monitor started on " + SymbolName + " " + TimeFrame);
            
            if (string.IsNullOrEmpty(TelegramEmail))
                Print("⚠️  Telegram Email not set! Get one from @mail2telegrambot on Telegram.");
            else
                Print("   Email alerts to: " + TelegramEmail);
            
            if (string.IsNullOrEmpty(FromEmail))
                Print("⚠️  From Email not set! Use the same email as your cTrader SMTP settings.");
            else
                Print("   Sending from: " + FromEmail);
            
            Print("   Session filter: " + (UseSessionFilter ? "ON (London/NY only)" : "OFF (24/7 mode)"));
            Print("   Weekend filter: " + (SkipWeekends ? "ON (no trading Sat/Sun)" : "OFF (trades 24/7)"));
            Print("   Price level alerts: Sell @ " + PriceLevelSell + " | Buy @ " + PriceLevelBuy);
            if (SendHeartbeat)
                Print("   Heartbeat enabled — sending test email every candle");
        }
        
        protected override void OnBar()
        {
            if (!IsInSession(out string sessionName))
                return;
            
            if (Bars.Count < 2)
                return;
            
            double close = Bars.Last(0).Close;
            
            // ── Heartbeat ──
            if (SendHeartbeat)
            {
                string heartbeatMsg = string.Format(
                    "💓 Heartbeat | {0} | Close: {1:F5} | Time: {2:HH:mm} EST",
                    SymbolName, close, Server.Time);
                SendEmailAlert(heartbeatMsg);
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
            
            SendEmailAlert(message);
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
                    SendEmailAlert(msg);
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
                    SendEmailAlert(msg);
                    Print("🟢 BUY level triggered!");
                }
            }
        }
        
        private bool IsInSession(out string sessionName)
        {
            DateTime estTime = TimeZoneInfo.ConvertTime(Server.Time, TimeZoneInfo.Utc, _estZone);
            
            // Skip weekends if enabled
            if (SkipWeekends && (estTime.DayOfWeek == DayOfWeek.Saturday || estTime.DayOfWeek == DayOfWeek.Sunday))
            {
                sessionName = "Weekend";
                return false;
            }
            
            // If session filter is disabled, run 24/7
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
        
        /// <summary>
        /// Send alert via email → Telegram bridge.
        /// Uses Notifications.SendEmail() which is natively allowed in cTrader Cloud.
        /// The Telegram bot (@mail2telegrambot) receives the email and forwards to your chat.
        /// </summary>
        private void SendEmailAlert(string body)
        {
            if (string.IsNullOrEmpty(TelegramEmail))
            {
                Print("⚠️  Cannot send alert — Telegram Email not configured");
                return;
            }
            if (string.IsNullOrEmpty(FromEmail))
            {
                Print("⚠️  Cannot send alert — From Email not configured");
                return;
            }
            
            Notifications.SendEmail(FromEmail, TelegramEmail, EmailSubject, body);
            Print("📧 Email alert sent to " + TelegramEmail);
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
