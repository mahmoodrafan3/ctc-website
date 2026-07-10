#!/usr/bin/env python3
"""
Test WhatsApp — sends a demo alert to verify credentials.
Run locally:  python test-whatsapp.py
Or via GitHub Actions (set secrets first).
"""
import os
import sys
import requests

WHATSAPP_TOKEN    = os.environ.get("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_ID = os.environ.get("WHATSAPP_PHONE_ID", "")
ALERT_PHONE       = os.environ.get("ALERT_PHONE", "")

missing = []
if not WHATSAPP_TOKEN:    missing.append("WHATSAPP_TOKEN")
if not WHATSAPP_PHONE_ID: missing.append("WHATSAPP_PHONE_ID")
if not ALERT_PHONE:       missing.append("ALERT_PHONE")

if missing:
    print(f"❌ Missing: {', '.join(missing)}")
    print("   Set them as env vars or in GitHub Secrets")
    sys.exit(1)

url = f"https://graph.facebook.com/v23.0/{WHATSAPP_PHONE_ID}/messages"
headers = {
    "Authorization": f"Bearer {WHATSAPP_TOKEN}",
    "Content-Type": "application/json",
}
payload = {
    "messaging_product": "whatsapp",
    "to": ALERT_PHONE,
    "type": "text",
    "text": {
        "body": "🚨 CTC STRATEGY — DEMO ALERT\n\nThis is a test message from the FX MOZO CTC Strategy Monitor.\n\nIf you receive this, WhatsApp notifications are working! ✅\n\n— CTC Monitor Bot"
    },
}

try:
    resp = requests.post(url, headers=headers, json=payload, timeout=15)
    if resp.status_code in (200, 201):
        print("✅ Demo WhatsApp sent successfully!")
        print(f"   Check your phone: {ALERT_PHONE}")
    else:
        print(f"❌ WhatsApp API error {resp.status_code}")
        print(f"   Response: {resp.text}")
except requests.RequestException as e:
    print(f"❌ Request failed: {e}")
