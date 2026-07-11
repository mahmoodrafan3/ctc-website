require('dotenv').config();
const express = require('express');
const path = require('path');
const { connect, getDb, getDbWithRetry } = require('./lib/db');

const app = express();
const PORT = process.env.PORT || 3000;

// Serve static files from the 'public' directory with cache control
app.use(express.static(path.join(__dirname, 'public'), {
  setHeaders: function (res, path) {
    // HTML files: no cache (so admin.html and prop-firm.html updates are picked up immediately)
    if (path.endsWith('.html')) {
      res.setHeader('Cache-Control', 'no-store, no-cache, must-revalidate, proxy-revalidate');
      res.setHeader('Pragma', 'no-cache');
      res.setHeader('Expires', '0');
    }
  }
}));

// Connect to MongoDB on startup
connect();

// ── Admin auth helpers ────────────────────────────────────────────
const ADMIN_PASSWORD = process.env.ADMIN_PASSWORD || 'admin';

function requireAdmin(req, res, next) {
  const auth = req.headers.authorization || '';
  const token = auth.replace('Bearer ', '');
  if (token !== ADMIN_PASSWORD) {
    return res.status(401).json({ error: 'Unauthorized' });
  }
  next();
}

// ── Public API: Serve prop firm data from MongoDB ─────────────────
// ── Cleanup: strip stale fields from data ──────────────────────
const STALE_KEYS = ['pfpFundedPct1Step', 'pfpFundedPct2Step', 'pfpDeductions1Step', 'pfpDeductions2Step'];

function stripStaleFields(data) {
  if (!data || typeof data !== 'object') return data;
  STALE_KEYS.forEach(function (key) { delete data[key]; });
  return data;
}

app.get('/api/prop-firm-data', async (req, res) => {
  try {
    const db = await getDbWithRetry();
    if (!db) {
      return res.status(503).json({ error: 'Database not available' });
    }
    const doc = await db.collection('propFirmData').findOne({ _key: 'v1' });
    if (!doc || !doc.data) {
      return res.status(404).json({ error: 'No data found' });
    }
    // Strip stale fields from what we send to the frontend
    stripStaleFields(doc.data);
    res.json(doc.data);
  } catch (err) {
    console.error('❌ Error fetching prop firm data:', err.message);
    res.status(500).json({ error: err.message });
  }
});

// ── Admin routes ──────────────────────────────────────────────────
app.get('/admin', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'admin.html'));
});

app.post('/api/admin/login', express.json(), (req, res) => {
  const { password } = req.body;
  if (password === ADMIN_PASSWORD) {
    res.json({ token: password });
  } else {
    res.status(401).json({ error: 'Invalid password' });
  }
});

app.get('/api/admin/prop-firm-data', requireAdmin, async (req, res) => {
  try {
    const db = await getDbWithRetry();
    if (!db) return res.status(503).json({ error: 'Database not available' });
    const doc = await db.collection('propFirmData').findOne({ _key: 'v1' });
    if (!doc || !doc.data) return res.status(404).json({ error: 'No data found' });
    // Strip stale fields from what we send to the admin page
    stripStaleFields(doc.data);
    res.json(doc.data);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.post('/api/admin/prop-firm-data', requireAdmin, express.json(), async (req, res) => {
  try {
    const db = await getDbWithRetry();
    if (!db) return res.status(503).json({ error: 'Database not available' });
    const data = req.body;

    // Strip stale fields from both incoming and existing data so they're removed from DB permanently
    stripStaleFields(data);

    const existing = await db.collection('propFirmData').findOne({ _key: 'v1' });
    if (existing && existing.data) stripStaleFields(existing.data);
    const mergedData = existing && existing.data
      ? Object.assign({}, existing.data, data)
      : data;

    await db.collection('propFirmData').replaceOne(
      { _key: 'v1' },
      { _key: 'v1', updatedAt: new Date(), data: mergedData },
      { upsert: true }
    );
    res.json({ success: true });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});


// ── cTrader Bot Alert Webhook Relay → Telegram ──────────────────────
// cTrader Cloud → HTTP POST → Vercel Serverless Function → HTTPS → Telegram
// The cTrader bot sends alerts here (no Cloudflare on Vercel's infra),
// and this endpoint forwards them to Telegram (clean IP, not blocked).
const TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN || '';

app.post('/api/webhook/ctc-alert', express.json(), async (req, res) => {
  const { chat_id, text } = req.body || {};

  if (!text) {
    return res.status(400).json({ error: 'Missing "text" in request body' });
  }
  if (!chat_id) {
    return res.status(400).json({ error: 'Missing "chat_id" in request body' });
  }
  if (!TELEGRAM_BOT_TOKEN) {
    console.error('❌ TELEGRAM_BOT_TOKEN not configured on server');
    return res.status(500).json({ error: 'Server not configured for Telegram relay' });
  }

  try {
    const url = `https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage`;
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_id, text })
    });

    const data = await response.json();

    if (data.ok) {
      console.log('✅ cTrader alert relayed to Telegram successfully');
      return res.json({ success: true });
    } else {
      console.error('❌ Telegram API error:', data.description);
      return res.status(response.status).json({ error: data.description });
    }
  } catch (err) {
    console.error('❌ Telegram relay failed:', err.message);
    return res.status(502).json({ error: err.message });
  }
});

// ── WhatsApp Webhook (for receiving message status and incoming messages) ──
const WHATSAPP_VERIFY_TOKEN = process.env.WHATSAPP_VERIFY_TOKEN || 'ctc_strategy_wh_2026';

app.get('/api/webhook/whatsapp', (req, res) => {
  // WhatsApp Cloud API verification challenge
  const mode = req.query['hub.mode'];
  const token = req.query['hub.verify_token'];
  const challenge = req.query['hub.challenge'];

  if (mode === 'subscribe' && token === WHATSAPP_VERIFY_TOKEN) {
    console.log('✅ WhatsApp webhook verified');
    return res.status(200).send(challenge);
  }
  res.sendStatus(403);
});

app.post('/api/webhook/whatsapp', express.json(), (req, res) => {
  // Acknowledge receipt immediately (WhatsApp expects 200 within 20s)
  res.sendStatus(200);

  const body = req.body;
  if (!body || !body.entry) return;

  // Process each entry
  for (const entry of body.entry) {
    for (const change of entry.changes || []) {
      if (change.field !== 'messages') continue;
      const value = change.value;

      // Check for incoming messages from customers
      if (value.messages) {
        for (const msg of value.messages) {
          const from = msg.from; // sender phone number
          const text = msg.text?.body || '';
          const msgType = msg.type || 'unknown';

          console.log(`📩 WhatsApp from ${from}: ${text}`);

          // Customer messaged us — conversation window is now open!
          // The monitor will be able to send alerts to this number for 24 hours
        }
      }

      // Check for message status updates (sent, delivered, read, failed)
      if (value.statuses) {
        for (const status of value.statuses) {
          const statusType = status.status; // 'sent', 'delivered', 'read', 'failed'
          const msgId = status.id || '';
          if (statusType === 'failed') {
            console.warn(`❌ WhatsApp msg ${msgId} failed:`, status.errors);
          }
        }
      }
    }
  }
});

// Route for /code - serve code.html
app.get('/code', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'code.html'));
});

// Route for /prop-firm - serve prop-firm.html
app.get('/prop-firm', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'prop-firm.html'));
});

// Route for /trading-journal - serve trading-journal.html
app.get('/trading-journal', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'trading-journal.html'));
});

// All other routes go to index.html
app.get('*', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

app.listen(PORT, () => {
  console.log(`CTC Strategy Website running at http://localhost:${PORT}`);
});
