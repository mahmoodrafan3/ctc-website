// ============================================================================
// CTC Strategy Monitor — cTrader Automate Bot
// ============================================================================
// 
// How to use:
// 1. Open cTrader → Automate → New Bot
// 2. Copy this entire file into the editor
// 3. Set your TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID below
// 4. Compile (F7) and run on EURUSD M5 chart
// 5. The bot will send Telegram alerts on Trend Magic crossovers!
//
// Note: Requires AccessRights.Internet to send HTTP requests to Telegram.
// ============================================================================

using System;
using System.Net.Http;
using System.Text;
using System.Threading.Tasks;
using cAlgo.API;
using cAlgo.API.Indicators;
using cAlgo.API.Requests;

namespace cAlgo.Robots
{
    [Robot(TimeZone = TimeZones.UTC, AccessRights = AccessRights.Internet)]
    public class CTCMonitor : Robot
    {
        // ════════════════════════════════════════════════════════════════
        // CONFIGURATION — EDIT THESE VALUES
        // ════════════════════════════════════════════════════════════════
        
        // Telegram Bot credentials (from @BotFather)
        private const string TelegramBotToken = "8899864917:AAE-8bHbEKTfjzsIenWnacSZ79Gt0SKBdgM";
        private const string TelegramChatId = "1235128870";
        
        // Trend Magic parameters (match Pine Script defaults)
        private const int CciPeriod = 15;
        private const int AtrPeriod = 5;
        private const double AtrCoeff = 1.0;
        
        // Session times (America/New_York minutes)
        private const int LondonStart = 180;   // 03:00 EST
        private const int LondonEnd = 280;     // 04:40 EST
        private const int NyStart = 480;       // 08:00 EST
        private const int NyEnd = 595;         // 09:55 EST
        
        // ════════════════════════════════════════════════════════════════
        
        private readonly HttpClient _httpClient = new HttpClient();
        private DateTime _lastAlertTime = DateTime.MinValue;
        private const int MinMinutesBetweenAlerts = 10; // Avoid duplicate alerts
        
        protected override void OnStart()
        {
            Print("🚀 CTC Strategy Monitor started on " + SymbolName + " " + TimeFrame);
            Print("   Telegram alerts enabled — checking every new candle");
        }
        
        protected override void OnBar()
        {
            // Check session time
            if (!IsInSession())
            {
                return;
            }
            
            // Need enough bars for calculation
            int neededBars = Math.Max(CciPeriod, AtrPeriod) + 2;
            if (Bars.Count < neededBars)
                return;
            
            // Calculate Trend Magic
            var result = CalculateTrendMagic();
            
            // Check for signal
            bool signalBuy = result.StrongBuy && result.TrendBull;
            bool signalSell = result.StrongSell && !result.TrendBull;
            
            if (!signalBuy && !signalSell)
                return;
            
            // Avoid duplicate alerts within MinMinutesBetweenAlerts
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
                Bars.Last(0).Close,
                result.MagicTrend,
                GetSessionName(),
                result.Cci,
                trend,
                Server.Time);
            
            // Send alert asynchronously (fire-and-forget)
            Task.Run(() => SendTelegramAlert(message));
            
            Print("🚨 " + signalType + " SIGNAL on " + SymbolName + " at " + Bars.Last(0).Close);
        }
        
        private bool IsInSession()
        {
            DateTime nyTime = Server.Time; // cTrader handles timezone conversion via Robot TimeZone
            
            // Since we set TimeZone = UTC, we need to convert to EST
            TimeZoneInfo estZone = TimeZoneInfo.FindSystemTimeZoneById("America/New_York");
            DateTime estTime = TimeZoneInfo.ConvertTime(Server.Time, TimeZoneInfo.Utc, estZone);
            
            int minutes = estTime.Hour * 60 + estTime.Minute;
            
            return (minutes >= LondonStart && minutes < LondonEnd) ||
                   (minutes >= NyStart && minutes < NyEnd);
        }
        
        private string GetSessionName()
        {
            TimeZoneInfo estZone = TimeZoneInfo.FindSystemTimeZoneById("America/New_York");
            DateTime estTime = TimeZoneInfo.ConvertTime(Server.Time, TimeZoneInfo.Utc, estZone);
            int minutes = estTime.Hour * 60 + estTime.Minute;
            
            if (minutes >= LondonStart && minutes < LondonEnd)
                return "London";
            if (minutes >= NyStart && minutes < NyEnd)
                return "New York";
            return "Outside";
        }
        
        private TrendMagicResult CalculateTrendMagic()
        {
            int n = Bars.Count;
            int startIdx = n - Math.Max(CciPeriod, AtrPeriod) - 10;
            if (startIdx < 0) startIdx = 0;
            int len = n - startIdx;
            
            // Extract candle arrays from the last N bars
            double[] opens = new double[len];
            double[] highs = new double[len];
            double[] lows = new double[len];
            double[] closes = new double[len];
            
            for (int i = 0; i < len; i++)
            {
                int barIdx = startIdx + i;
                opens[i] = Bars.OpenPrices[barIdx];
                highs[i] = Bars.HighPrices[barIdx];
                lows[i] = Bars.LowPrices[barIdx];
                closes[i] = Bars.ClosePrices[barIdx];
            }
            
            // Compute ATR
            double[] atr = new double[len];
            for (int i = AtrPeriod; i < len; i++)
            {
                double sum = 0;
                for (int j = i - AtrPeriod + 1; j <= i; j++)
                {
                    double tr = Math.Max(highs[j] - lows[j],
                        Math.Max(Math.Abs(highs[j] - closes[j - 1]),
                                 Math.Abs(lows[j] - closes[j - 1])));
                    sum += tr;
                }
                atr[i] = sum / AtrPeriod;
            }
            
            // Compute CCI
            double[] cci = new double[len];
            for (int i = CciPeriod - 1; i < len; i++)
            {
                double sum = 0;
                for (int j = i - CciPeriod + 1; j <= i; j++)
                    sum += closes[j];
                double sma = sum / CciPeriod;
                
                double md = 0;
                for (int j = i - CciPeriod + 1; j <= i; j++)
                    md += Math.Abs(closes[j] - sma);
                md /= CciPeriod;
                
                if (md != 0)
                    cci[i] = (closes[i] - sma) / (0.015 * md);
            }
            
            // Compute MagicTrend recursively
            double[] magicTrend = new double[len];
            bool firstValid = false;
            
            for (int i = 1; i < len; i++)
            {
                double cciVal = cci[i];
                double atrVal = atr[i];
                if (atrVal == 0) continue;
                
                double upT = lows[i] - atrVal * AtrCoeff;
                double downT = highs[i] + atrVal * AtrCoeff;
                
                if (!firstValid)
                {
                    magicTrend[i] = cciVal >= 0 ? upT : downT;
                    firstValid = true;
                }
                else
                {
                    double prev = magicTrend[i - 1];
                    if (cciVal >= 0)
                        magicTrend[i] = Math.Max(upT, prev);
                    else
                        magicTrend[i] = Math.Min(downT, prev);
                }
            }
            
            // Get latest values
            double cciLatest = cci[len - 1];
            bool trendBull = cciLatest >= 0;
            double mtLatest = magicTrend[len - 1];
            
            // Check last 2 candles for body crossover
            bool strongBuy = false;
            bool strongSell = false;
            
            int checkStart = Math.Max(1, len - 2);
            for (int i = checkStart; i < len; i++)
            {
                double mt = magicTrend[i];
                if (opens[i] < mt && closes[i] > mt)
                    strongBuy = true;
                if (opens[i] > mt && closes[i] < mt)
                    strongSell = true;
            }
            
            return new TrendMagicResult
            {
                MagicTrend = mtLatest,
                Cci = cciLatest,
                StrongBuy = strongBuy,
                StrongSell = strongSell,
                TrendBull = trendBull
            };
        }
        
        private async Task SendTelegramAlert(string message)
        {
            try
            {
                string url = string.Format("https://api.telegram.org/bot{0}/sendMessage", TelegramBotToken);
                
                string json = string.Format(
                    "{{\"chat_id\":\"{0}\",\"text\":\"{1}\"}}",
                    TelegramChatId,
                    message.Replace("\"", "\\\"").Replace("\n", "\\n"));
                
                var content = new StringContent(json, Encoding.UTF8, "application/json");
                
                var response = await _httpClient.PostAsync(url, content);
                
                if (response.IsSuccessStatusCode)
                    Print("✅ Telegram alert sent");
                else
                    Print("❌ Telegram error: " + response.StatusCode);
            }
            catch (Exception ex)
            {
                Print("❌ Telegram send failed: " + ex.Message);
            }
        }
        
        protected override void OnStop()
        {
            Print("CTC Strategy Monitor stopped");
            _httpClient.Dispose();
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
