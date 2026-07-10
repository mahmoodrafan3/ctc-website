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
