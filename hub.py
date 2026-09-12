#!/usr/bin/env python3
"""revision-hub: always-on sync server for the GCSE Revision Tracker.
Listens on 0.0.0.0:8765 for POST /sync from the desktop app and the phone.
Desktop and phone both push/pull here; hub owns the DB."""

import json
import os
import signal
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from revision_core import SYNC_TABLES, Db, get_local_ip, now_iso

PORT = int(os.environ.get("HUB_PORT", 8765))
DB_PATH = os.environ.get("HUB_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "revision.db"))


class HubHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler: POST /sync only, plus GET / for health checks."""

    db = None
    log_method = None  # set by hub.py on startup

    def do_GET(self):
        if self.path != "/":
            self.send_error(404)
            return
        resp = json.dumps({
            "service": "revision-hub",
            "db_path": DB_PATH,
            "since_start": HubHandler._since_start,
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def do_POST(self):
        if self.path != "/sync":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            data = json.loads(body) if body else {}
            incoming = data.get("tables", {})
            since = data.get("since")

            if incoming:
                print("incoming tables:", list(incoming.keys()))
                HubHandler.db.import_all(incoming)

            if since:
                out = {}
                for t in SYNC_TABLES:
                    out[t] = [dict(r) for r in HubHandler.db.conn.execute(
                        f"SELECT * FROM {t} WHERE updated_at > ?", (since,)).fetchall()]
            else:
                out = HubHandler.db.export_all()

            resp = json.dumps({"tables": out}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
        except Exception as e:
            traceback.print_exc()
            self.send_error(500, str(e))

    def log_message(self, fmt, *args):
        if HubHandler.log_method:
            HubHandler.log_method(fmt, *args)


_since_start = time.time()


def main():
    print(f"revision-hub  port={PORT}  db={DB_PATH}")

    db = Db(DB_PATH)
    HubHandler.db = db
    HubHandler._since_start = now_iso()
    HubHandler.log_method = lambda fmt, *a: print(f"  {fmt % a}") if a else print(f"  {fmt}")

    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), HubHandler)
    print(f"Sync server listening on http://0.0.0.0:{PORT}")

    stop = threading.Event()

    def _shutdown(*_):
        print("\nShutting down...")
        stop.set()
        httpd.shutdown()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    httpd.serve_forever()
    db.close()
    print("Stopped.")


if __name__ == "__main__":
    main()
