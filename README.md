# GCSE Revision Tracker

A GCSE revision tracker with a Windows desktop app and an Android companion app.
It loads a full topic list for each subject, lets you log study sessions with a
green/amber/red confidence rating, tracks minutes per topic, and highlights which
topics need recap work. The desktop app and the phone app sync over your home Wi-Fi
through a dedicated sync hub (a small always-on server).

## Architecture

```
desktop app ─┐
             ├─ HTTP POST /sync (0.0.0.0:8765) ── revision-hub
phone app  ──┘                                        │
                                           revision.db (hub owns the DB)
```

- `revision_core.py` - shared database engine, sync client, and hub auto-discovery
  (`find_hub`) — pure stdlib, used byte-for-byte by all three.
- `hub.py` - the always-on sync hub. Run it on a small server/box on your LAN.
- `revision-hub.service` - systemd unit for the hub (copy to `/etc/systemd/system/`,
  set `WorkingDirectory`/`ExecStart` to your install path, `systemctl enable --now`).
- The desktop app also bundles a built-in hub (`SyncServer`, same port), handy when
  no dedicated server is used.

Hub auto-discovery: right now `find_hub()` in `revision_core.py` checks any
remembered addresses, then scans the local `/24` subnet for something answering
`GET /` with `{"service":"revision-hub"}`. The desktop Sync tab has an **Auto-find**
button and the phone's Sync tab finds the hub automatically when opened.

## Desktop app

- `revision_core.py` - shared database engine and sync client (pure stdlib).
- `revision_tracker.py` - the Windows tkinter UI.
  Tabs: Dashboard, Log Study, Progress, Recap Priority, Manage, Sync.
- `revision.db` - your data (created with seed topics on first run).
- `revision_tracker.exe` - prebuilt binary (built with PyInstaller).

Run directly: `python revision_tracker.py`

Self-test: `python revision_tracker.py --selftest`

The Sync tab lets you sync through the hub's address (its built-in hub, or a
separate server). Click **Auto-find** to locate the hub on the LAN, or type the
address, then press **Sync now**. Sync is two-way: new sessions logged on the phone
appear in the desktop app and vice versa.

## Building the desktop exe

Install PyInstaller, then:

```
python -m PyInstaller --noconfirm revision_tracker.spec
```

Copy the fresh `dist/revision_tracker.exe` over `revision_tracker.exe`.

## Android app (mobile/)

- `mobile/main.py` - Kivy UI (Review / Progress / Sync tabs). On open it searches
  for the hub automatically, remembers the last-used address in `settings.json`
  (inside the app's private storage), and stores its database there too, so it
  survives app restarts.
- `mobile/revision_core.py` - copy of the shared core (kept byte-identical).
- `mobile/buildozer.spec` - buildozer config for the APK.

Local build (needs Linux/macOS or WSL + buildozer, SDK, JDK):

```
cd mobile
buildozer -v android debug
```

The APK lands in `mobile/bin/`. Install it with `adb install bin/revisiontracker-*.apk`.

## GitHub Actions APK build

Push changes under `mobile/` to `main`, or run the **Build Android APK** workflow
manually (Actions tab). The debug APK is uploaded as the `revisiontracker-apk`
artifact; download it, copy it to the phone, and install it.

## Sync protocol

Both apps use `SyncClient` from the core against the hub:

- Hub listens on `0.0.0.0:8765`. `GET /` is a health check and answers
  `{"service":"revision-hub", ...}` (used by auto-discovery). Sync is
  `POST /sync`.
- Body: `{"since": <iso timestamp>, "tables": {...optional outgoing rows...}}`.
- Incoming rows are merged by primary key into all sync tables; rows changed on the
  hub since the requested timestamp are returned.
- `SYNC_TABLES = subjects, units, topics, study_sessions, session_topics`.

Tests: `python test_sync.py` exercises a two-client sync over the wire;
`python test_gui.py` starts the desktop app headlessly and checks the embedded hub.

## Data safety note

`revision_core.Db.connect()` performs a one-way migration from the old single-table
schema (v1) to the relational schema. Migration runs only when the live `subjects`
table lacks the new columns and no partial migration (`subjects__old`) is present,
so it is idempotent and never re-runs against migrated data. `revision.db.pre_heal`
is a leftover safety copy of an earlier repair pass.

## Revision guide PDFs

`gcse_revision_guides_contents.md` documents the page-number scheme used to attach
each topic to its page in the revision guides (a few page-number screenshots live
in `20260911_*.jpg`).