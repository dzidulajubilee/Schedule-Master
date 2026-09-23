#!/usr/bin/env python3
"""
PlanTHat local server.
Serves PlanTHat.html and its companion files from this folder, and exposes:
  GET  /api/staff  -> reads staff.json ({"staff": [...]})
  PUT  /api/staff  -> writes staff.json (body: {"staff": [...]})

No third-party packages required (standard library only).
Run this file (or double-click Start PlanTHat.bat), then a browser tab opens
automatically at PlanTHat.html.
"""
import http.server
import json
import os
import socket
import sys
import threading
import webbrowser

FOLDER = os.path.dirname(os.path.abspath(__file__))
STAFF_FILE = os.path.join(FOLDER, "staff.json")
HTML_FILE = "ScheduleMaster.html"
PORT = 8743


def read_staff():
    if not os.path.exists(STAFF_FILE):
        return {"staff": []}
    with open(STAFF_FILE, "r", encoding="utf-8") as f:
        raw = f.read().strip()
    if not raw:
        return {"staff": []}
    data = json.loads(raw)  # raises ValueError on bad JSON -> caller reports it, file is left untouched
    if not isinstance(data, dict) or not isinstance(data.get("staff"), list):
        raise ValueError('staff.json must look like {"staff": ["Name One", "Name Two"]}')
    return data


def write_staff(data):
    if not isinstance(data, dict) or not isinstance(data.get("staff"), list):
        raise ValueError('Expected {"staff": [...]}')
    names = []
    seen = set()
    for n in data["staff"]:
        n = str(n).strip()
        if n and n not in seen:
            seen.add(n)
            names.append(n)
    # atomic write: temp file then replace, so a crash mid-write can't corrupt staff.json
    tmp = STAFF_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"staff": sorted(names)}, f, indent=2)
    os.replace(tmp, STAFF_FILE)
    return {"staff": sorted(names)}


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=FOLDER, **kwargs)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def end_headers(self):
        # This file changes often during development. Without this, browsers may keep serving a stale
        # cached copy of PlanTHat.html after it's been updated on disk, until the tab is hard-refreshed.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        super().end_headers()

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") == "/api/staff":
            try:
                self._send_json(200, read_staff())
            except ValueError as e:
                self._send_json(400, {"error": f"staff.json is unreadable: {e}. It was not changed."})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return
        super().do_GET()

    def do_PUT(self):
        if self.path.rstrip("/") == "/api/staff":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length else b"{}"
                data = json.loads(body.decode("utf-8"))
                result = write_staff(data)
                self._send_json(200, result)
            except ValueError as e:
                self._send_json(400, {"error": str(e)})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return
        self._send_json(404, {"error": "not found"})


def find_free_port(start):
    port = start
    for _ in range(20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
        port += 1
    return start


def main():
    port = find_free_port(PORT)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/{HTML_FILE}"
    print(f"Schedule Master server running at {url}")
    print("Press Ctrl+C to stop.")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping PlanTHat server.")
        server.shutdown()


if __name__ == "__main__":
    main()
