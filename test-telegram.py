#!/usr/bin/env python3
"""
Test Telegram — sends a demo alert to verify your bot works.
Run locally:  python test-telegram.py
Or via GitHub Actions (set secrets TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first).
"""
import os
import sys
import requests

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

missing = []
if not TELEGRAM_BOT_TOKEN: missing.append("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_CHAT_ID:   missing.append("TELEGRAM_CHAT_ID")

if missing:
    print(f"❌ Missing: {', '.join(missing)}")
    print("   Set them as env vars or in GitHub Secrets")
    sys.exit(1)

url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
payload = {
    "chat_id": TELEGRAM_CHAT_ID,
    "text": "🚀 CTC STRATEGY — DEMO ALERT\n\nThis is a test from the FX MOZO CTC Strategy Monitor.\n\nIf you receive this, Telegram notifications are working! ✅\n\n— CTC Monitor Bot",
}

try:
    resp = requests.post(url, json=payload, timeout=15)
    data = resp.json()
    print(f"📡 Status: {resp.status_code}")
    print(f"📄 Response: {resp.text}")
    if data.get("ok"):
        print(f"✅ Telegram test sent successfully!")
        print(f"   Check your Telegram chat!")
    else:
        print(f"❌ Telegram API error")
except requests.RequestException as e:
    print(f"❌ Request failed: {e}")
