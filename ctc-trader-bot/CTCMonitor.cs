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
using System.Collections.Generic;
using System.Net.Http;
using System.Text;
using System.Threading.Tasks;
using cAlgo.API;
using cAlgo.API.Indicators;
using cAlgo.API.Requests;
using Newtonsoft.Json;

namespace cAlgo.Robots
{
    [Robot(TimeZone = TimeZones.UTC, AccessRights = AccessRights.Internet)]
    public class CTCMonitor : Robot
    {
        // ════════════════════════════════════════════════════════════════
        // CONFIGURATION — EDIT THESE VALUES BEFORE RUNNING
        // ════════════════════════════════════════════════════════════════
        
        private const string TelegramBotToken = "8899864917:AAE-8bHbEKTfjzsIenWnacSZ79Gt0SKBdgM";
        private const string TelegramChatId = "1235128870";
        
        // Trend Magic parameters
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
        private const int MinMinutesBetweenAlerts = 10;
        private TimeZoneInfo _estZone;
        private readonly List<Task> _pendingTasks = new List<Task>();
        
        protected override void OnStart()
        {
            _estZone = TimeZoneInfo.FindSystemTimeZoneById("America/New_York");
            Print("🚀 CTC Strategy Monitor started on " + SymbolName + " " + TimeFrame);
            Print("   Telegram alerts enabled — checking every new candle");
        }
        
        protected override void OnBar()
        {
            if (!IsInSession(out string sessionName))
                return;
            
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
                Bars.Last(0).Close,
                result.MagicTrend,
                sessionName,
                result.Cci,
                trend,
                Server.Time);
            
            // Track pending HTTP tasks so we can await them cleanly on stop
            var task = Task.Run(() => SendTelegramAlert(message));
            lock (_pendingTasks)
            {
                _pendingTasks.Add(task);
            }
            
            Print("🚨 " + signalType + " SIGNAL on " + SymbolName + " at " + Bars.Last(0).Close);
        }
        
        private bool IsInSession(out string sessionName)
        {
            DateTime estTime = TimeZoneInfo.ConvertTime(Server.Time, TimeZoneInfo.Utc, _estZone);
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
        
        private async Task SendTelegramAlert(string message)
        {
            try
            {
                string url = string.Format("https://api.telegram.org/bot{0}/sendMessage", TelegramBotToken);
                
                var payload = new
                {
                    chat_id = TelegramChatId,
                    text = message
                };
                
                string json = JsonConvert.SerializeObject(payload);
                var content = new StringContent(json, Encoding.UTF8, "application/json");
                
                var response = await _httpClient.PostAsync(url, content).ConfigureAwait(false);
                
                if (response.IsSuccessStatusCode)
                    Print("✅ Telegram alert sent");
                else
                {
                    string body = await response.Content.ReadAsStringAsync().ConfigureAwait(false);
                    Print("❌ Telegram error: " + response.StatusCode + " - " + body);
                }
            }
            catch (ObjectDisposedException)
            {
                // Bot stopping — ignore
            }
            catch (Exception ex)
            {
                Print("❌ Telegram send failed: " + ex.Message);
            }
            finally
            {
                lock (_pendingTasks)
                {
                    _pendingTasks.RemoveAll(t => t.IsCompleted);
                }
            }
        }
        
        protected override void OnStop()
        {
            Print("CTC Strategy Monitor stopping...");
            
            // Wait for pending HTTP tasks to complete
            Task[] pending;
            lock (_pendingTasks)
            {
                pending = _pendingTasks.ToArray();
            }
            try
            {
                Task.WaitAll(pending, TimeSpan.FromSeconds(5));
            }
            catch { }
            
            _httpClient.Dispose();
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
