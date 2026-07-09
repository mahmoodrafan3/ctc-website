const { MongoClient } = require('mongodb');

const MONGODB_URI = process.env.MONGODB_URI || '';
const DB_NAME = 'ctc-strategy';

let client = null;
let db = null;
let connecting = false;

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
    console.warn('⚠️ MONGODB_URI not set — skipping MongoDB connection');
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
      autoSelectFamily: false
    });

    // Handle connection close events — auto-reconnect on next request
    client.on('connectionClosed', () => {
      console.warn('⚠️ MongoDB connection closed — will reconnect on next request');
      db = null;
      client = null;
    });
    client.on('close', () => {
      console.warn('⚠️ MongoDB client closed — will reconnect on next request');
      db = null;
      client = null;
    });
    client.on('error', (err) => {
      console.warn('⚠️ MongoDB client error:', err.message);
    });

    await client.connect();
    db = client.db(DB_NAME);
    console.log('✅ Connected to MongoDB — database: ' + DB_NAME);

    // Verify connection is alive
    await db.command({ ping: 1 });
    console.log('✅ MongoDB ping successful');

    connecting = false;
    return db;
  } catch (err) {
    console.warn('⚠️ MongoDB connection failed:', err.message);
    connecting = false;
    client = null;
    db = null;
    return null;
  }
}

async function disconnect() {
  if (client) {
    await client.close();
    client = null;
    db = null;
    connecting = false;
    console.log('🔌 MongoDB disconnected');
  }
}

function getDb() {
  return db;
}

module.exports = { connect, disconnect, getDb };
