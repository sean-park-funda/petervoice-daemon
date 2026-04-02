#!/usr/bin/env python3
import argparse
import requests
import sys
import json
import os

# API Configuration
# Default to the deployed URL, but allow override
API_URL = os.environ.get("PETER_VOICE_API_URL", "https://petervoice.vercel.app/api/bot/reply")
# Default key from the old script, but should be updated or use env var
API_KEY = os.environ.get("PETER_VOICE_API_KEY", "JUMSs3mtDbVaEwIxV571MZZxYUgtECaDpIl1BpB6ZLs")

def send_reply(message_id, text, auto_url=None, reload_command=False, call_number=None):
    """
    Sends a reply to the web interface via API.
    """
    payload = {
        "id": message_id,
        "text": text,
        "files": []
    }
    
    if auto_url:
        payload["files"].append({
            "name": "Auto Open",
            "url": auto_url,
            "type": "url/auto-open",
            "size": 0
        })

    if reload_command:
        payload["files"].append({
            "name": "Reload Command",
            "url": "command://reload",
            "type": "command/reload",
            "size": 0
        })

    if call_number:
        # Remove any non-numeric characters except + and - for safety, or just trust the input
        # Simple validation could be added here
        payload["files"].append({
            "name": "Phone Call",
            "url": f"tel:{call_number}",
            "type": "phone/call",
            "size": 0
        })
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
        "User-Agent": "PeterBot-Reply-Tool/1.0"
    }

    print(f"[*] Sending reply to ID: {message_id}...")
    try:
        response = requests.post(API_URL, json=payload, headers=headers, timeout=10)
        
        if response.status_code in [200, 201]:
            print(f"[SUCCESS] Reply sent successfully. API Response: {response.text}")
            return True
        else:
            print(f"[ERROR] Failed to send reply. Status: {response.status_code}, Body: {response.text}")
            return False
            
    except requests.exceptions.RequestException as e:
        print(f"[ERROR] Connection error: {e}")
        return False

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send a reply to Peter Voice.")
    parser.add_argument("--id", required=True, help="The message ID to reply to.")
    parser.add_argument("--text", required=True, help="The text content of the reply.")
    parser.add_argument("--url", help="URL to automatically open in a new tab.")
    parser.add_argument("--reload", action="store_true", help="Send a command to reload the frontend page.")
    parser.add_argument("--call", help="Phone number to automatically call (e.g., 010-1234-5678).")
    
    args = parser.parse_args()
    
    success = send_reply(args.id, args.text, args.url, args.reload, args.call)
    sys.exit(0 if success else 1)
