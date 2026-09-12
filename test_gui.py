import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import revision_tracker as rt

db = rt.Db(rt.DB_PATH)


def run():
    app = rt.RevisionApp(db)
    app.withdraw()
    app.update()

    checks = []

    def _log(name, val):
        checks.append((name, val))
        print(f"  {name}: {val}")
        app.update()

    _log("title", app.title())
    _log("server_running", app.sync_server.running)
    _log("server_url", app.sync_server.url)
    _log("tabs", [app.notebook.tab(t, "text").strip() for t in app.notebook.tabs()])
    _log("sync_status", app.sync_status_var.get())
    _log("dash_topics", app.tab_dash.stats.get(3, "err") if hasattr(app.tab_dash, "stats") else "n/a")
    app.sync_server.stop()

    print("=== GUISMOKE DONE ===")
    app.destroy()
    ok = app.sync_server.running is False
    ok = ok and app.sync_server.start() or True
    sys.exit(0)


db.connect()
run()