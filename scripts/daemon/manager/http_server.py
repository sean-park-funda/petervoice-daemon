"""Manager status HTTP server."""

import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

import daemon.globals as g
from daemon.globals import manager_wake_event, logger


def start_manager_http_server(port: int = 7777):
    """Start a lightweight HTTP server for manager status API."""

    class ManagerStatusHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/manager/state":
                if g._manager_instance:
                    data = g._manager_instance.get_snapshot()
                    self._json_response(200, data)
                else:
                    self._json_response(503, {"error": "manager not running"})
            elif self.path.startswith("/api/manager/"):
                project = self.path.split("/api/manager/")[1].strip("/")
                if g._manager_instance and project in g._manager_instance.workflows:
                    wf = g._manager_instance.workflows[project]
                    retry = g._manager_instance.state.get("retry_queue", {}).get(project)
                    data = {
                        "project": project,
                        "description": wf.get("description"),
                        "schedule": wf.get("schedule"),
                        "agent": wf.get("agent"),
                        "retry": retry,
                        "hints": [h for h in g._manager_instance.state.get("hints", [])
                                  if h.get("project") == project],
                    }
                    self._json_response(200, data)
                else:
                    self._json_response(404, {"error": "project not found"})
            else:
                self._json_response(404, {"error": "not found"})

        def do_POST(self):
            if self.path == "/api/manager/refresh":
                manager_wake_event.set()
                self._json_response(202, {"queued": True})
            else:
                self._json_response(405, {"error": "method not allowed"})

        def _json_response(self, code: int, data: dict):
            body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            pass

    server = HTTPServer(("127.0.0.1", port), ManagerStatusHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="manager-http")
    thread.start()
    logger.info(f"[manager] Status API started on http://127.0.0.1:{port}/api/manager/state")
    return server
