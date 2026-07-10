const { MongoClient } = require('mongodb');

const MONGODB_URI = process.env.MONGODB_URI || '';
const DB_NAME = 'ctc-strategy';

let client = null;
let db = null;
let connecting = false;
let reconnectTimer = null;
let reconnectAttempt = 2; // Start at 2 so first retry is ~4s, not 1s
const MAX_RECONNECT_INTERVAL = 30000; // 30 seconds max

async function connect() {
  if (db) return db;
  if (connecting) {
    // Wait for an in-progress connection attempt
    for (let i = 0; i < 50; i++) {
      await new Promise(r => setTimeout(r, 200));
      if (db) return db;
    }
    return null;
  }
  if (!MONGODB_URI) {
    console.warn('\u26a0\ufe0f MONGODB_URI not set \u2014 skipping MongoDB connection');
    return null;
  }
  connecting = true;
  try {
    client = new MongoClient(MONGODB_URI, {
      serverSelectionTimeoutMS: 5000,
      connectTimeoutMS: 5000,
      socketTimeoutMS: 30000,
      maxPoolSize: 5,
      minPoolSize: 0,
      // Fix SSL/TLS handshake errors on Windows (alert number 80)
      tlsInsecure: true,
      // Prevent IPv6 resolution issues with recent Node.js drivers
      autoSelectFamily: false,
      // Enable automatic retry of writes and reads on transient errors
      retryWrites: true,
      retryReads: true
    });

    // Handle connection close — auto-reconnect with exponential backoff
    client.on('close', () => {
      // Guard: if already reset by a previous event, do nothing
      if (db === null && client === null) return;
      console.warn('\u26a0\ufe0f MongoDB connection closed \u2014 reconnecting...');
      db = null;
      client = null;
      connecting = false;
      scheduleReconnect();
    });

    client.on('error', (err) => {
      console.warn('\u26a0\ufe0f MongoDB client error:', err.message);
    });

    await client.connect();
    db = client.db(DB_NAME);
    console.log('\u2705 Connected to MongoDB \u2014 database: ' + DB_NAME);

    // Verify connection is alive
    await db.command({ ping: 1 });
    console.log('\u2705 MongoDB ping successful');

    // Reset reconnect counter on successful connection
    reconnectAttempt = 0;
    connecting = false;
    return db;
  } catch (err) {
    console.warn('\u26a0\ufe0f MongoDB connection failed:', err.message);
    connecting = false;
    client = null;
    db = null;
    // Schedule retry with backoff
    scheduleReconnect();
    return null;
  }
}

function scheduleReconnect() {
  if (!MONGODB_URI) return;
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }

  const delay = Math.min(1000 * Math.pow(2, reconnectAttempt), MAX_RECONNECT_INTERVAL);
  reconnectAttempt++;

  console.log('\u23f3 Reconnecting in ' + Math.round(delay / 1000) + 's (attempt ' + reconnectAttempt + ')');

  reconnectTimer = setTimeout(async () => {
    reconnectTimer = null;
    if (db) return; // Already connected while waiting
    console.log('\ud83d\udd04 Reconnecting to MongoDB...');
    await connect();
  }, delay);
}

async function disconnect() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  if (client) {
    await client.close();
    client = null;
    db = null;
    connecting = false;
    reconnectAttempt = 0;
    console.log('\ud83d\udd0c MongoDB disconnected');
  }
}

function getDb() {
  return db;
}

// For serverless environments (Vercel): wait for the initial connection to complete
// before giving up. Retries up to ~10 seconds so cold starts have time to connect.
async function getDbWithRetry(maxRetries = 20, delayMs = 500) {
  if (db) return db;
  // If no URI configured, return immediately
  if (!MONGODB_URI) return null;
  // Wait for the background connect() call to finish
  for (let i = 0; i < maxRetries; i++) {
    if (db) return db;
    await new Promise(r => setTimeout(r, delayMs));
  }
  return db;
}

module.exports = { connect, disconnect, getDb, getDbWithRetry };
