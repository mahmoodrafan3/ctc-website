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
    print(f"📡 Status: {resp.status_code}")
    print(f"📄 Full response:\n{resp.text}")
    if resp.status_code in (200, 201):
        data = resp.json()
        if data.get("error"):
            print(f"⚠️  API returned error in body: {data['error']}")
        elif data.get("messages"):
            msg_id = data["messages"][0].get("id", "unknown")
            print(f"✅ WhatsApp API accepted — Message ID: {msg_id}")
            print(f"   Check your phone: {ALERT_PHONE}")
        else:
            print("⚠️  No message ID in response — may not be delivered")
    else:
        print(f"❌ WhatsApp API error")
except requests.RequestException as e:
    print(f"❌ Request failed: {e}")
