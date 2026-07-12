// ── Standalone Vercel Function ─────────────────────────────────────
// Relay: cTrader bot → Vercel → Telegram
// Zero heavy dependencies (no Express, no MongoDB) — cold-starts fast.
//
// The cTrader bot sends: { "chat_id": "...", "text": "..." }
// We forward to: https://api.telegram.org/bot<TOKEN>/sendMessage
//
// Requires env var: TELEGRAM_BOT_TOKEN (set in Vercel Dashboard)
// ────────────────────────────────────────────────────────────────────

module.exports = async (req, res) => {
  // Only accept POST
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  const { chat_id, text } = req.body || {};

  if (!chat_id || !text) {
    return res.status(400).json({ error: 'Missing chat_id or text in body' });
  }

  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token) {
    console.error('[ctc-alert] TELEGRAM_BOT_TOKEN not set');
    return res.status(500).json({ error: 'Server config error' });
  }

  try {
    const url = `https://api.telegram.org/bot${token}/sendMessage`;

    const telegramRes = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        chat_id,
        text,
        parse_mode: 'HTML',
      }),
    });

    const body = await telegramRes.text();

    if (telegramRes.ok) {
      console.log('[ctc-alert] ✅ Alert relayed to Telegram');
      return res.status(200).json({ success: true });
    }

    console.error(`[ctc-alert] ❌ Telegram error: ${telegramRes.status} — ${body}`);
    return res.status(502).json({ error: `Telegram error: ${telegramRes.status}` });
  } catch (err) {
    console.error('[ctc-alert] ❌ Network error:', err.message);
    return res.status(502).json({ error: 'Upstream request failed' });
  }
};
