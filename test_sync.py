import json
import os
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(__file__))
from revision_core import Db, SYNC_TABLES, SyncClient, now_iso

TMP = tempfile.mkdtemp(prefix="synctest_")
DB_A = os.path.join(TMP, "a.db")
DB_B = os.path.join(TMP, "b.db")
PORT = 18765


class TestHandler(BaseHTTPRequestHandler):
    db = None

    def do_POST(self):
        if self.path != "/sync":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            data = json.loads(body) if body else {}
            since = data.get("since")
            incoming = data.get("tables", {})
            if incoming:
                TestHandler.db.import_all(incoming)
            if since:
                out = TestHandler.db.snapshots(since)
            else:
                out = TestHandler.db.export_all()
            resp = json.dumps({"tables": out}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
        except Exception as e:
            self.send_error(500, str(e))

    def log_message(self, fmt, *args):
        pass


def main():
    db_a = Db(DB_A)
    db_b = Db(DB_B)

    sub_a = db_a.add_subject("Physics_A")
    top_a = db_a.add_topic(sub_a, "Forces_A")
    sesh_a = db_a.add_study_session(now_iso(), 45, "test session A", [(top_a, 3)])

    sub_b = db_b.add_subject("Maths_B")
    top_b = db_b.add_topic(sub_b, "Algebra_B")
    sesh_b = db_b.add_study_session(now_iso(), 30, "test session B", [(top_b, 4)])

    TestHandler.db = db_a
    httpd = HTTPServer(("127.0.0.1", PORT), TestHandler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    time.sleep(0.3)

    print("=== Sync A <-> B ===")
    client = SyncClient(f"http://127.0.0.1:{PORT}")

    import datetime
    epoch = "1970-01-01T00:00:00"
    result = client.sync(db_b, since=epoch)
    print(f"sync returned {len(result.get('tables', {}))} tables")

    b_subjects = [dict(r) for r in db_b.conn.execute("SELECT * FROM subjects").fetchall()]
    b_topics = [dict(r) for r in db_b.conn.execute("SELECT * FROM topics").fetchall()]
    b_sessions = [dict(r) for r in db_b.conn.execute("SELECT * FROM study_sessions").fetchall()]

    print(f"B now has {len(b_subjects)} subjects, {len(b_topics)} topics, {len(b_sessions)} sessions")
    physics = [s for s in b_subjects if "Physics" in s["name"]]
    maths = [s for s in b_subjects if "Maths" in s["name"]]
    forces = [t for t in b_topics if "Forces" in t["name"]]
    algebra = [t for t in b_topics if "Algebra" in t["name"]]

    ok = True
    if not physics:
        print("FAIL: Physics_A not synced to B"); ok = False
    if not maths:
        print("FAIL: Maths_B not found in B"); ok = False
    if not forces:
        print("FAIL: Forces_A not synced to B"); ok = False
    if not algebra:
        print("FAIL: Algebra_B not found in B"); ok = False

    if len(b_sessions) < 2:
        print("FAIL: expected >= 2 sessions in B"); ok = False

    a_subjects = [dict(r) for r in db_a.conn.execute("SELECT * FROM subjects").fetchall()]
    a_topics = [dict(r) for r in db_a.conn.execute("SELECT * FROM topics").fetchall()]
    print(f"A has {len(a_subjects)} subjects, {len(a_topics)} topics")
    physics_a = [s for s in a_subjects if "Physics" in s["name"]]
    maths_a = [s for s in a_subjects if "Maths" in s["name"]]
    if not physics_a:
        print("FAIL: Physics_A missing from A"); ok = False
    if not maths_a:
        print("FAIL: Maths_B not synced to A"); ok = False

    httpd.shutdown()
    shutil.rmtree(TMP, ignore_errors=True)

    if ok:
        print("\n=== ALL SYNC TESTS PASSED ===")
    else:
        print("\n=== SOME TESTS FAILED ===")
        sys.exit(1)


if __name__ == "__main__":
    main()
