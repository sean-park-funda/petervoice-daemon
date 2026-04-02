#!/usr/bin/env python3
import os
import urllib.request
import urllib.error
import json
import time
import subprocess
import sys
from datetime import datetime

# --- Configuration ---
WEB_API_URL = "https://peter-voice.vercel.app"
BOT_API_KEY = "JUMSs3mtDbVaEwIxV571MZZxYUgtECaDpIl1BpB6ZLs"
OPENCLAW_PATH = "/Users/a111/.nvm/versions/node/v24.13.0/bin/openclaw"
REPLY_TOOL_PATH = "/Users/a111/.openclaw/workspace/scripts/web_reply.py"
SESSION_ID_FILE = os.path.expanduser("~/.openclaw/workspace/scripts/current_session_id.txt")
SESSIONS_JSON_PATH = os.path.expanduser("~/.openclaw/agents/main/sessions/sessions.json")
DEFAULT_SESSION_ID = "agent:main:main"

LOG_FILE = os.path.expanduser("~/.openclaw/workspace/scripts/web_poller.log")
PROCESSED_IDS_FILE = os.path.expanduser("~/.openclaw/workspace/scripts/processed_ids.json")

def get_active_session_id():
    if os.path.exists(SESSION_ID_FILE):
        try:
            with open(SESSION_ID_FILE, "r") as f:
                return f.read().strip()
        except:
            pass
    return DEFAULT_SESSION_ID

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except:
        pass

def load_processed_ids():
    if os.path.exists(PROCESSED_IDS_FILE):
        try:
            with open(PROCESSED_IDS_FILE, "r") as f:
                return set(str(x) for x in json.load(f))
        except:
            return set()
    return set()

def save_processed_ids(ids_set):
    try:
        with open(PROCESSED_IDS_FILE, "w") as f:
            json.dump(list(ids_set)[-100:], f)
    except:
        pass

def get_context_usage():
    """Reads session stats directly from sessions.json"""
    try:
        if os.path.exists(SESSIONS_JSON_PATH):
            with open(SESSIONS_JSON_PATH, "r") as f:
                data = json.load(f)
                # Target the main session
                session_key = "agent:main:main"
                if session_key in data:
                    s = data[session_key]
                    return {
                        "inputTokens": s.get("inputTokens", 0),
                        "totalTokens": s.get("totalTokens", 0),
                        "contextTokens": s.get("contextTokens", 1000000), # Default 1M
                        "model": s.get("model", "unknown")
                    }
    except Exception as e:
        log(f"Error reading context usage: {e}")
    return None

def send_heartbeat():
    """Sends heartbeat with context usage to the web API"""
    usage = get_context_usage()
    if not usage:
        return

    url = f"{WEB_API_URL}/api/bot/heartbeat"
    payload = {
        "status": "online",
        "contextUsage": usage,
        "timestamp": datetime.now().isoformat()
    }
    
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Bearer {BOT_API_KEY}",
        "Content-Type": "application/json"
    })
    
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            pass # Success
    except Exception as e:
        log(f"Heartbeat Error: {e}")

def get_pending_messages():
    url = f"{WEB_API_URL}/api/bot/poll"
    headers = {
        "Authorization": f"Bearer {BOT_API_KEY}",
        "Content-Type": "application/json"
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
            if isinstance(data, dict) and "pending" in data:
                return data["pending"]
            return []
    except Exception as e:
        log(f"Poll Error: {e}")
        return []

def inject_message_to_peter(user_text, msg_id):
    venv_python = "/Users/a111/.openclaw/workspace/venv/bin/python3"
    session_id = get_active_session_id()
    system_instruction = (
        f"[SYSTEM] 이 메시지는 웹에서 수신되었습니다. "
        f"답변 시 반드시 다음 명령을 실행하여 웹에 답장을 남기세요: "
        f"{venv_python} {REPLY_TOOL_PATH} --id {msg_id} --text '당신의 답변'"
    )
    full_message = f"{system_instruction}\n\n사용자 메시지: {user_text}"
    
    cmd = [
        OPENCLAW_PATH, "agent", 
        "--session-id", session_id, 
        "--message", full_message
    ]
    
    log(f"Relaying msg {msg_id} to Peter (Session: {session_id})...")
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        log(f"Injection Error: {e}")
        return False

if __name__ == "__main__":
    log("=== Peter Web Poller v6 (Context Aware) Started ===")
    processed_ids = load_processed_ids()
    last_heartbeat = 0
    
    while True:
        # 1. Heartbeat every 30 seconds
        now = time.time()
        if now - last_heartbeat > 30:
            send_heartbeat()
            last_heartbeat = now
            
        # 2. Poll messages
        pending = get_pending_messages()
        for msg in pending:
            msg_id = str(msg.get("id"))
            if msg_id not in processed_ids:
                if inject_message_to_peter(msg.get("text", ""), msg_id):
                    processed_ids.add(msg_id)
                    save_processed_ids(processed_ids)
        
        # 3. Sleep
        time.sleep(1)
