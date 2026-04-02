#!/usr/bin/env python3
import time
import json
import requests
import sys
import subprocess
import os

# Configuration
WEB_API_URL = "https://peter-voice.vercel.app"
OPENCLAW_BIN = "/Users/a111/.nvm/versions/node/v24.13.0/bin/openclaw"
POLL_INTERVAL = 0.5  # 500ms

# Track active sessions
active_sessions = {}  # session_id -> subprocess

def log(msg):
    print(f"[ClaudeBridge] {msg}", flush=True)

def execute_action(action, payload):
    try:
        if action == "start":
            workdir = payload.get("workdir", "/Users/a111/Projects/peter-voice")
            
            # Start Claude Code via subprocess
            log(f"Starting Claude in {workdir}")
            proc = subprocess.Popen(
                ["claude"],
                cwd=workdir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1
            )
            
            session_id = f"claude-{proc.pid}"
            active_sessions[session_id] = {
                "proc": proc,
                "workdir": workdir,
                "output_buffer": "",
                "offset": 0
            }
            
            log(f"Started Claude session: {session_id}")
            return {"sessionId": session_id, "status": "started"}

        elif action == "send":
            session_id = payload.get("sessionId")
            message = payload.get("message")
            
            if not session_id or not message:
                return {"error": "Missing sessionId or message"}
            
            if session_id not in active_sessions:
                return {"error": "Session not found"}
            
            proc = active_sessions[session_id]["proc"]
            
            try:
                proc.stdin.write(message + "\n")
                proc.stdin.flush()
                log(f"Sent message to {session_id}")
                return {"sent": True}
            except Exception as e:
                return {"error": f"Failed to send: {str(e)}"}

        elif action == "poll":
            session_id = payload.get("sessionId")
            offset = payload.get("offset", 0)
            
            if not session_id:
                return {"error": "Missing sessionId"}
            
            if session_id not in active_sessions:
                return {"error": "Session not found"}
            
            session = active_sessions[session_id]
            proc = session["proc"]
            
            # Read available output (non-blocking)
            try:
                import select
                readable, _, _ = select.select([proc.stdout], [], [], 0)
                
                if readable:
                    new_output = proc.stdout.read(4096)
                    if new_output:
                        session["output_buffer"] += new_output
                        session["offset"] += len(new_output)
                
                # Return output from requested offset
                output = session["output_buffer"][offset:]
                running = proc.poll() is None
                
                return {
                    "output": output,
                    "offset": session["offset"],
                    "running": running
                }
            except Exception as e:
                return {"error": f"Failed to poll: {str(e)}"}

        elif action == "kill":
            session_id = payload.get("sessionId")
            
            if not session_id:
                return {"error": "Missing sessionId"}
            
            if session_id not in active_sessions:
                return {"error": "Session not found"}
            
            proc = active_sessions[session_id]["proc"]
            proc.terminate()
            proc.wait(timeout=5)
            del active_sessions[session_id]
            
            log(f"Killed session: {session_id}")
            return {"status": "killed"}

        else:
            return {"error": f"Unknown action: {action}"}

    except Exception as e:
        log(f"Error in execute_action: {str(e)}")
        return {"error": f"Internal error: {str(e)}"}

def main():
    log("Starting Claude Bridge...")
    while True:
        try:
            # Poll for pending actions
            resp = requests.get(f"{WEB_API_URL}/api/claude/pending-actions", timeout=5)
            if resp.status_code == 200:
                pending_actions = resp.json()
                
                for item in pending_actions:
                    req_id = item.get("id")
                    action = item.get("action")
                    payload = item.get("payload", {})
                    
                    log(f"Processing action: {action} (ID: {req_id})")
                    
                    result = execute_action(action, payload)
                    
                    # Send result back
                    result_payload = {
                        "requestId": req_id,
                        "result": result,
                        "status": "completed" if "error" not in result else "failed"
                    }
                    
                    requests.post(f"{WEB_API_URL}/api/claude/results", json=result_payload, timeout=5)
                    log(f"Result sent for ID: {req_id}")

            else:
                if resp.status_code != 200:
                    log(f"Error polling actions: {resp.status_code}")

        except Exception as e:
            log(f"Bridge loop error: {e}")
        
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
