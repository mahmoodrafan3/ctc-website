// ============================================================================
// cTrader Alert Webhook → Telegram Relay
// Vercel Serverless Function (standalone — no Express, no MongoDB)
// ============================================================================
//
// cTrader Cloud → HTTP POST → Vercel (this function) → HTTPS → Telegram
//
// This is a minimal function with zero heavy dependencies so it
// cold-starts in milliseconds and never 503s on cTrader Cloud.
// ============================================================================

const TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN || '';

/** @param {import('@vercel/node').VercelRequest} req @param {import('@vercel/node').VercelResponse} res */
module.exports = async (req, res) => {
  // ── Only accept POST ──
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed — use POST' });
  }

  const { chat_id, text } = req.body || {};

  // ── Validate input ──
  if (!text) {
    return res.status(400).json({ error: 'Missing "text" in request body' });
  }
  if (!chat_id) {
    return res.status(400).json({ error: 'Missing "chat_id" in request body' });
  }
  if (!TELEGRAM_BOT_TOKEN) {
    console.error('[ctc-alert] TELEGRAM_BOT_TOKEN not configured');
    return res.status(500).json({ error: 'Server not configured for Telegram relay' });
  }

  try {
    const url = `https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage`;

    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_id, text }),
    });

    const data = await response.json();

    if (data.ok) {
      console.log('[ctc-alert] ✅ Relayed to Telegram');
      return res.status(200).json({ success: true });
    }

    console.error('[ctc-alert] ❌ Telegram API error:', data.description);
    return res.status(response.status).json({ error: data.description });
  } catch (err) {
    console.error('[ctc-alert] ❌ Relay failed:', err.message);
    return res.status(502).json({ error: err.message });
  }
};
