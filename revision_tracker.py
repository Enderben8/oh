import ctypes
import datetime
import json
import os
import shutil
import socket
import sys
import threading
import time
import tkinter as tk
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from tkinter import filedialog, messagebox, simpledialog, ttk
from threading import Thread

from revision_core import APP_NAME, DATE_FMT, R, AM, GR, VERSION, Db, SyncClient, find_hub, get_local_ip


def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


DB_PATH = os.path.join(app_dir(), "revision.db")

SYNC_PORT = 8765

from revision_core import SYNC_TABLES

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


class SyncHandler(BaseHTTPRequestHandler):
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
                self.db.import_all(incoming)
            if since:
                out = {}
                for t in SYNC_TABLES:
                    out[t] = [dict(r) for r in self.db.conn.execute(
                        f"SELECT * FROM {t} WHERE updated_at > ?", (since,)).fetchall()]
            else:
                out = self.db.export_all()
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


class SyncServer:
    def __init__(self, db, port=SYNC_PORT):
        self.db = db
        self.port = port
        self.httpd = None
        self.thread = None
        self.running = False

    @property
    def ip(self):
        return get_local_ip()

    @property
    def url(self):
        return f"http://{self.ip}:{self.port}"

    def start(self):
        if self.running:
            return
        SyncHandler.db = self.db
        self.httpd = HTTPServer(("0.0.0.0", self.port), SyncHandler)
        self.thread = Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.running = True

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        self.running = False


class RevisionApp(tk.Tk):
    def __init__(self, db):
        super().__init__()
        self.db = db
        self.title(APP_NAME)
        self.geometry("1120x720")
        self.minsize(900, 600)
        self.configure(bg="#f5f5f5")
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Treeview", rowheight=24, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))
        style.configure("Header.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("Title.TLabel", font=("Segoe UI", 11, "bold"))
        style.configure("Stat.TLabel", font=("Segoe UI", 22, "bold"))
        style.configure("StatName.TLabel", font=("Segoe UI", 9))
        style.configure("Small.TLabel", font=("Segoe UI", 9))

        self.sync_server = SyncServer(db)
        self.sync_server.start()

        self.sync_bar = ttk.Frame(self)
        self.sync_bar.pack(fill="x", side="bottom", padx=8, pady=(4, 0))
        self.sync_status_var = tk.StringVar(value=f"Sync: {self.sync_server.url}")
        ttk.Label(self.sync_bar, textvariable=self.sync_status_var, style="Small.TLabel").pack(side="left", padx=4)
        self.sync_btn = ttk.Button(self.sync_bar, text="Stop", command=self._toggle_sync, width=8)
        self.sync_btn.pack(side="left", padx=4)

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=8)
        self.tab_dash = DashTab(self.notebook, self)
        self.tab_log = LogTab(self.notebook, self)
        self.tab_prog = ProgressTab(self.notebook, self)
        self.tab_prior = PriorityTab(self.notebook, self)
        self.tab_manage = ManageTab(self.notebook, self)
        self.tab_sync = SyncTab(self.notebook, self)
        self.notebook.add(self.tab_dash, text=" Dashboard ")
        self.notebook.add(self.tab_log, text=" Log Study ")
        self.notebook.add(self.tab_prog, text=" Progress ")
        self.notebook.add(self.tab_prior, text=" Recap Priority ")
        self.notebook.add(self.tab_manage, text=" Manage ")
        self.notebook.add(self.tab_sync, text=" Sync ")
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab)

    def _toggle_sync(self):
        if self.sync_server.running:
            self.sync_server.stop()
            self.sync_btn.config(text="Start")
            self.sync_status_var.set("Sync: Stopped")
        else:
            self.sync_server.start()
            self.sync_btn.config(text="Stop")
            self.sync_status_var.set(f"Sync: {self.sync_server.url}")

    def _on_tab(self, _e=None):
        cur = self.nametowidget(self.notebook.select())
        if hasattr(cur, "refresh"):
            cur.refresh()

    def refresh_all(self):
        for tab in (self.tab_dash, self.tab_log, self.tab_prog,
                    self.tab_prior, self.tab_manage, self.tab_sync):
            if hasattr(tab, "refresh"):
                tab.refresh()


class DashTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        ttk.Label(self, text="Revision Dashboard", style="Header.TLabel").pack(pady=(10, 4))
        ttk.Label(self, text="Keep slogging - every session counts.", style="Small.TLabel").pack()

        cards = ttk.Frame(self)
        cards.pack(fill="x", padx=16, pady=10)
        self.card_vars = {}
        for i, (key, name) in enumerate([
            ("sessions", "Sessions logged"), ("minutes", "Minutes studied"),
            ("progress", "Topics studied"), ("streak", "Current streak (days)")]):
            box = ttk.Frame(cards, relief="solid", borderwidth=1, padding=10)
            box.grid(row=0, column=i, padx=6, sticky="nsew")
            cards.columnconfigure(i, weight=1)
            self.card_vars[key] = tk.StringVar(value="-")
            ttk.Label(box, textvariable=self.card_vars[key], style="Stat.TLabel").pack()
            ttk.Label(box, text=name, style="StatName.TLabel").pack()

        mid = ttk.PanedWindow(self, orient="horizontal")
        mid.pack(fill="both", expand=True, padx=12, pady=4)
        left = ttk.LabelFrame(mid, text="Per subject")
        right = ttk.LabelFrame(mid, text="Recent sessions")
        mid.add(left, weight=1)
        mid.add(right, weight=1)
        cols = ("subject", "sessions", "minutes", "studied", "topics", "pct")
        self.subj_tree = ttk.Treeview(left, columns=cols, show="headings", height=12)
        for c, t, w, an in [
            ("subject", "Subject", 160, "w"), ("sessions", "Sessions", 70, "e"),
            ("minutes", "Minutes", 70, "e"), ("studied", "Studied", 70, "e"),
            ("topics", "Topics", 60, "e"), ("pct", "Done %", 60, "e")]:
            self.subj_tree.heading(c, text=t); self.subj_tree.column(c, width=w, anchor=an)
        self.subj_tree.pack(fill="both", expand=True, padx=8, pady=8)

        cols2 = ("date", "subject", "topic", "minutes", "conf")
        self.rec_tree = ttk.Treeview(right, columns=cols2, show="headings", height=12)
        for c, t, w, an in [
            ("date", "Date", 90, "w"), ("subject", "Subject", 100, "w"), ("topic", "Topic", 220, "w"),
            ("minutes", "Min", 50, "e"), ("conf", "Conf", 50, "center")]:
            self.rec_tree.heading(c, text=t); self.rec_tree.column(c, width=w, anchor=an)
        self.rec_tree.pack(fill="both", expand=True, padx=8, pady=8)
        self.rec_tree.tag_configure("R", foreground=R)
        self.rec_tree.tag_configure("A", foreground=AM)
        self.rec_tree.tag_configure("G", foreground=GR)

        self.weak_lbl = tk.Label(self, text="", justify="left", anchor="w", font=("Segoe UI", 10), wraplength=1000)
        self.weak_lbl.pack(fill="x", padx=16, pady=(4, 10))

    def refresh(self):
        dates = self.app.db.all_session_dates()
        today = datetime.date.today().strftime(DATE_FMT)
        streak = 0
        d = today
        while d in dates:
            streak += 1
            d = (datetime.datetime.strptime(d, DATE_FMT).date() - datetime.timedelta(days=1)).strftime(DATE_FMT)
        summ = self.app.db.subject_summary()
        total_sessions = sum(r["sessions"] for r in summ)
        total_minutes = sum(r["minutes"] for r in summ)
        total_topics = sum(r["topics"] for r in summ)
        total_studied = sum(r["studied"] for r in summ)
        self.card_vars["sessions"].set(str(total_sessions))
        self.card_vars["minutes"].set(str(total_minutes))
        self.card_vars["progress"].set(f"{total_studied}/{total_topics}")
        self.card_vars["streak"].set(str(streak))

        self.subj_tree.delete(*self.subj_tree.get_children())
        for r in summ:
            pct = round(100 * r["studied"] / r["topics"]) if r["topics"] else 0
            self.subj_tree.insert("", "end", values=(
                r["name"], r["sessions"], r["minutes"], r["studied"], r["topics"], f"{pct}%"))

        self.rec_tree.delete(*self.rec_tree.get_children())
        for r in self.app.db.recent_sessions():
            tags = (r["confidence"],)
            self.rec_tree.insert("", "end", iid=None,
                                 values=(r["date"], r["subject"], r["topic"], r["minutes"], r["confidence"]),
                                 tags=tags)

        weak = self.app.db.last_conf_weak()
        if weak:
            items = ", ".join(f"'{r['topic']}' ({r['subject']})" for r in weak[:5])
            self.weak_lbl.config(text="Needs attention: " + items + (" ..." if len(weak) > 5 else ""),
                                 fg=R)
        else:
            self.weak_lbl.config(text="No red/amber-rated topics yet. Nice.", fg=GR)


class LogTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.subject_var = tk.StringVar()
        self.unit_var = tk.StringVar(value="(Any unit)")
        self.topic_var = tk.StringVar()
        self.conf_var = tk.StringVar(value="G")
        self.date_var = tk.StringVar(value=datetime.date.today().strftime(DATE_FMT))
        self.minutes_var = tk.StringVar(value="30")
        self.topic_map = {}
        self.added = []
        self.timer_btn = None
        self.timer_running = False
        self.timer_t0 = None
        self.timer_acc = 0.0
        self.timer_after = None
        self.auto_minutes = tk.BooleanVar(value=True)

        self.canvas = tk.Canvas(self, highlightthickness=0)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.scroll_frame = ttk.Frame(self.canvas)
        self.scroll_frame.bind(
            "<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self._canvas_win = self.canvas.create_window((0, 0), window=self.scroll_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.canvas.bind(
            "<Configure>", lambda e: self.canvas.itemconfig(self._canvas_win, width=e.width))
        self.vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.bind("<Enter>", lambda _e: self.bind_all("<MouseWheel>", self._on_mousewheel))
        self.bind("<Leave>", lambda _e: self.unbind_all("<MouseWheel>"))
        sf = self.scroll_frame

        ttk.Label(sf, text="Log a revision session", style="Header.TLabel").pack(pady=(14, 0))

        picker = ttk.LabelFrame(sf, text="Add topics to this session", padding=10)
        picker.pack(fill="x", padx=24, pady=12, anchor="n")
        picker.columnconfigure(1, weight=1)

        ttk.Label(picker, text="Subject", style="Title.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=4)
        self.subj_cb = ttk.Combobox(picker, textvariable=self.subject_var, state="readonly", width=38)
        self.subj_cb.grid(row=0, column=1, sticky="w", pady=4)
        self.subj_cb.bind("<<ComboboxSelected>>", self._subject_changed)

        ttk.Label(picker, text="Unit / section", style="Title.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 12), pady=4)
        self.unit_cb = ttk.Combobox(picker, textvariable=self.unit_var, state="readonly", width=38)
        self.unit_cb.grid(row=1, column=1, sticky="w", pady=4)
        self.unit_cb.bind("<<ComboboxSelected>>", self._unit_changed)

        ttk.Label(picker, text="Topic", style="Title.TLabel").grid(row=2, column=0, sticky="w", padx=(0, 12), pady=4)
        self.topic_cb = ttk.Combobox(picker, textvariable=self.topic_var, state="readonly", width=38)
        self.topic_cb.grid(row=2, column=1, sticky="w", pady=4)

        ttk.Label(picker, text="Confidence", style="Title.TLabel").grid(row=3, column=0, sticky="w", padx=(0, 12), pady=4)
        conf_frame = ttk.Frame(picker)
        conf_frame.grid(row=3, column=1, sticky="w", pady=4)
        for label in ("R - rough", "A - okay", "G - good"):
            key = label[0]
            tk.Radiobutton(conf_frame, text=label, value=key, variable=self.conf_var,
                           font=("Segoe UI", 10)).pack(side="left", padx=4)

        ttk.Button(picker, text="Add topic to session", command=self.add_topic).grid(
            row=4, column=1, sticky="w", pady=(8, 0))

        self.picker_msg = tk.Label(picker, text="", fg=AM, font=("Segoe UI", 9))
        self.picker_msg.grid(row=5, column=1, sticky="w")

        details = ttk.LabelFrame(sf, text="Topics in this session", padding=10)
        details.pack(fill="both", expand=True, padx=24, pady=0, anchor="n")
        details.columnconfigure(0, weight=1)
        details.columnconfigure(2, weight=1)

        self.list_frame = tk.Frame(details)
        self.list_frame.grid(row=0, column=0, columnspan=2, sticky="nsew", padx=(0, 12))
        self.listbox = tk.Listbox(self.list_frame, height=6, font=("Segoe UI", 10),
                                  activestyle="dotbox")
        self.listbox.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(self.list_frame, orient="vertical", command=self.listbox.yview)
        sb.pack(side="left", fill="y")
        self.listbox.configure(yscrollcommand=sb.set)

        side = ttk.Frame(details)
        side.grid(row=0, column=2, sticky="n", padx=(0, 12))
        ttk.Button(side, text="Remove selected", command=self.remove_topic).pack(fill="x", pady=2)
        ttk.Button(side, text="Clear all", command=self.clear_added).pack(fill="x", pady=2)

        timer = ttk.LabelFrame(sf, text="Study timer", padding=10)
        timer.pack(fill="x", padx=24, pady=10, anchor="n")

        self.timer_lbl = tk.Label(timer, text="00:00", font=("Consolas", 24, "bold"), fg="#1565C0")
        self.timer_lbl.pack(side="left", padx=(0, 16))

        self.timer_btn = ttk.Button(timer, text="Start", command=self._toggle_timer, width=10)
        self.timer_btn.pack(side="left", padx=4)
        ttk.Button(timer, text="Reset", command=self._reset_timer).pack(side="left", padx=4)
        ttk.Checkbutton(timer, text="Auto-fill minutes from timer",
                        variable=self.auto_minutes).pack(side="left", padx=12)

        form = ttk.LabelFrame(sf, text="Session details", padding=10)
        form.pack(fill="x", padx=24, pady=12, anchor="n")
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="Date (YYYY-MM-DD)", style="Title.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=4)
        self.date_entry = ttk.Entry(form, textvariable=self.date_var, width=18)
        self.date_entry.grid(row=0, column=1, sticky="w", pady=4)

        ttk.Label(form, text="Minutes", style="Title.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 12), pady=4)
        ttk.Spinbox(form, from_=0, to=600, textvariable=self.minutes_var, width=8).grid(
            row=1, column=1, sticky="w", pady=4)

        ttk.Label(form, text="Notes (optional)", style="Title.TLabel").grid(row=2, column=0, sticky="w", padx=(0, 12), pady=4)
        self.notes_txt = tk.Text(form, width=46, height=3, font=("Segoe UI", 10))
        self.notes_txt.grid(row=2, column=1, sticky="w", pady=4)

        btn_frame = ttk.Frame(form)
        btn_frame.grid(row=3, column=1, sticky="w", pady=10)
        ttk.Button(btn_frame, text="Save session", command=self.save).pack(side="left")
        ttk.Button(btn_frame, text="Today", command=self._set_today).pack(side="left", padx=8)

        self.feedback = tk.Label(sf, text="", font=("Segoe UI", 10))
        self.feedback.pack(anchor="w", padx=24)
        self.winfo_toplevel().bind("<Control-s>", lambda _e: self.save(), add="+")
        self.saved = False

    def _on_mousewheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _update_scrollregion(self):
        self.canvas.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _set_today(self):
        self.date_var.set(datetime.date.today().strftime(DATE_FMT))

    def _format_ts(self, secs):
        secs = int(secs)
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    def _elapsed(self):
        if self.timer_running and self.timer_t0 is not None:
            return self.timer_acc + (time.monotonic() - self.timer_t0)
        return self.timer_acc

    def _toggle_timer(self):
        if self.timer_running:
            self.timer_acc = self._elapsed()
            self.timer_t0 = None
            self.timer_running = False
            if self.timer_after:
                self.after_cancel(self.timer_after)
                self.timer_after = None
            self.timer_btn.config(text="Start")
        else:
            self.timer_t0 = time.monotonic()
            self.timer_running = True
            self.timer_btn.config(text="Pause")
            self._tick_loop()
        self._update_timer_display()

    def _tick_loop(self):
        self._update_timer_display()
        self.timer_after = self.after(250, self._tick_loop)

    def _update_timer_display(self):
        el = self._elapsed()
        self.timer_lbl.config(text=self._format_ts(el))
        if self.auto_minutes.get():
            mins = max(1, int(round(el / 60))) if el >= 30 else 0
            self.minutes_var.set(str(mins))

    def _reset_timer(self):
        if self.timer_after:
            self.after_cancel(self.timer_after)
            self.timer_after = None
        self.timer_running = False
        self.timer_t0 = None
        self.timer_acc = 0.0
        if self.timer_btn:
            self.timer_btn.config(text="Start")
        self._update_timer_display()

    def _subject_changed(self, _e=None):
        sid = self._selected_subject_id()
        units = self.app.db.get_units(sid, direct_topics_only=True) if sid else []
        self.unit_cb["values"] = ["(Any unit)"] + [u["name"] for u in units]
        self.unit_var.set("(Any unit)")
        self._reload_topics()

    def _selected_subject_id(self):
        sname = self.subject_var.get()
        for s in self.app.db.get_subjects():
            if s["name"] == sname:
                return s["id"]
        return None

    def _unit_changed(self, _e=None):
        self._reload_topics()

    def _selected_unit_id(self):
        sid = self._selected_subject_id()
        uname = self.unit_var.get()
        if not sid or uname == "(Any unit)":
            return None
        for u in self.app.db.get_units(sid, direct_topics_only=True):
            if u["name"] == uname:
                return u["id"]
        return None

    def _reload_topics(self):
        sid = self._selected_subject_id()
        uid = self._selected_unit_id()
        self.topic_map = {}
        if sid:
            topics = self.app.db.get_topics(sid, uid)
        else:
            topics = []
        labels = []
        for t in topics:
            label = t["name"] + (f" (p.{t['page']})" if t["page"] else "")
            labels.append(label)
            self.topic_map[label] = t["id"]
        self.topic_cb["values"] = labels
        if labels:
            self.topic_var.set(labels[0])
        else:
            self.topic_var.set("")

    def refresh(self):
        subs = [s["name"] for s in self.app.db.get_subjects()]
        self.subj_cb["values"] = subs
        if not subs:
            return
        if self.subject_var.get() not in subs:
            self.subject_var.set(subs[0])
            self._subject_changed()
        else:
            self._reload_topics()

    def add_topic(self):
        label = self.topic_var.get()
        topic_id = self.topic_map.get(label)
        if not topic_id:
            self.picker_msg.config(text="Pick a topic first.", fg=R)
            return
        if any(a["topic_id"] == topic_id for a in self.added):
            self.picker_msg.config(text="That topic is already in this session.", fg=AM)
            return
        subject = self.subject_var.get()
        conf = self.conf_var.get()
        self.added.append({"label": label, "topic_id": topic_id, "conf": conf, "subject": subject})
        self._render_list()
        self.picker_msg.config(text=f"Added: {label} [{conf}]", fg=GR)
        nxt = self._advance_topic()
        if nxt:
            self.topic_var.set(nxt)
        else:
            self.topic_var.set("")

    def _advance_topic(self):
        vals = list(self.topic_cb["values"])
        cur = self.topic_var.get()
        if cur in vals:
            idx = vals.index(cur)
            if idx + 1 < len(vals):
                return vals[idx + 1]
        return None

    def remove_topic(self):
        sel = self.listbox.curselection()
        if not sel:
            self.picker_msg.config(text="Select a topic in the list to remove it.", fg=AM)
            return
        del self.added[sel[0]]
        self._render_list()
        self.picker_msg.config(text="")

    def clear_added(self):
        self.added = []
        self._render_list()
        self.picker_msg.config(text="")

    def _render_list(self):
        self.listbox.delete(0, "end")
        for i, a in enumerate(self.added, 1):
            self.listbox.insert("end", f"{i}. {a['label']}  [{a['conf']}]  {a['subject']}")
        self._update_scrollregion()

    def preselect(self, subject_id, topic_id):
        subj = self._find_subject_by_id(subject_id)
        if subj is None:
            return
        self.subject_var.set(subj["name"])
        self._subject_changed()
        topic = self.app.db.get_topic(topic_id)
        if topic is None:
            return
        if topic["unit_id"]:
            row = self.app.db.get_unit(topic["unit_id"])
            if row and row["name"] in self.unit_cb["values"]:
                self.unit_var.set(row["name"])
                self._unit_changed()
        for label in self.topic_cb["values"]:
            if label.startswith(topic["name"]):
                self.topic_var.set(label)
                break

    def _find_subject_by_id(self, sid):
        for s in self.app.db.get_subjects():
            if s["id"] == sid:
                return s
        return None

    def save(self):
        if self.timer_running:
            self._toggle_timer()
        if not self.added:
            messagebox.showwarning(APP_NAME, "Add at least one topic to the session first.")
            return
        try:
            parsed = datetime.datetime.strptime(self.date_var.get().strip(), DATE_FMT).date()
        except ValueError:
            messagebox.showwarning(APP_NAME, "Date must be YYYY-MM-DD (e.g. 2026-09-12).")
            return
        try:
            minutes = int(self.minutes_var.get())
        except ValueError:
            minutes = 0
        notes = self.notes_txt.get("1.0", "end").strip()
        topics = [(a["topic_id"], a["conf"]) for a in self.added]
        self.app.db.add_study_session(parsed.strftime(DATE_FMT), minutes, notes, topics)
        n = len(self.added)
        self.feedback.config(
            text=f"Logged {parsed} - {minutes} min - {n} topic{'s' if n != 1 else ''}",
            fg=GR)
        self.added = []
        self._render_list()
        self._reset_timer()
        self._set_today()
        self.notes_txt.delete("1.0", "end")
        self.saved = True
        self.picker_msg.config(text="")


class ProgressTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        top = ttk.Frame(self)
        top.pack(fill="x", padx=12, pady=10)
        ttk.Label(top, text="Subject:", style="Title.TLabel").pack(side="left")
        self.filter_var = tk.StringVar()
        self.filter_cb = ttk.Combobox(top, textvariable=self.filter_var, state="readonly", width=24)
        self.filter_cb.pack(side="left", padx=6)
        self.filter_cb.bind("<<ComboboxSelected>>", lambda _e: self.refresh())

        cols = ("topic", "times", "minutes", "last", "days", "conf", "page")
        self.tree = ttk.Treeview(self, columns=cols, show="tree headings")
        for c, t, w, an in [
            ("topic", "Topic", 340, "w"), ("times", "Times", 60, "e"), ("minutes", "Total min", 70, "e"),
            ("last", "Last revised", 100, "w"), ("days", "Days since", 80, "e"),
            ("conf", "Conf", 60, "center"), ("page", "Page", 50, "e")]:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=an)
        self.tree.column("#0", width=180, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=12, pady=(0, 4))
        self.tree.tag_configure("grp", background="#e8eef7", font=("Segoe UI", 10, "bold"))
        self.tree.tag_configure("sub", background="#dfe7f2", font=("Segoe UI", 11, "bold"))
        self.tree.tag_configure("R", foreground=R, font=("Segoe UI", 10, "bold"))
        self.tree.tag_configure("A", foreground=AM)
        self.tree.tag_configure("G", foreground=GR)
        self.tree.tag_configure("day_hi", foreground=R)
        self.tree.tag_configure("day_mid", foreground=AM)
        self.tree.tag_configure("week_hi", foreground=R)
        self.tree.tag_configure("week_mid", foreground=AM)
        self.summary_lbl = ttk.Label(self, text="", style="Small.TLabel")
        self.summary_lbl.pack(fill="x", padx=12, pady=(0, 8))

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        rows = self.app.db.all_topics_with_stats()
        today = datetime.date.today()
        subjects = self.app.db.get_subjects()
        names = ["(All subjects)"] + [s["name"] for s in subjects]
        if self.filter_var.get() not in names:
            self.filter_var.set("(All subjects)")
        self.filter_cb["values"] = names
        want = self.filter_var.get()

        by_subj = {}
        for r in rows:
            by_subj.setdefault(r["subject"], []).append(r)

        for s in subjects:
            if want != "(All subjects)" and s["name"] != want:
                continue
            sub_rows = by_subj.get(s["name"], [])
            sub_sessions = sum(r["times"] for r in sub_rows)
            sub_minutes = sum(r["minutes"] for r in sub_rows)
            top_iid = self.tree.insert("", "end", text=s["name"], open=True,
                                       values=("", sub_sessions, sub_minutes, "", "", "", ""), tags=("sub",))
            tree = self.app.db.get_units_tree(s["id"])
            flat_units = []
            seen = set()

            def walk(pid, depth):
                for u in tree.get(pid, []):
                    if u["id"] in seen:
                        continue
                    seen.add(u["id"])
                    flat_units.append((u, depth))
                    walk(u["id"], depth + 1)

            walk(None, 0)
            for u, depth in flat_units:
                u_rows = [r for r in sub_rows if r["unit_id"] == u["id"]]
                if not u_rows:
                    continue
                u_sessions = sum(r["times"] for r in u_rows)
                u_minutes = sum(r["minutes"] for r in u_rows)
                u_iid = self.tree.insert(top_iid, "end", text=("  " * depth) + u["name"], open=False,
                                         values=("", u_sessions, u_minutes, "", "", "", ""), tags=("grp",))
                for r in u_rows:
                    self._insert_topic(u_iid, r, today)
            for r in sub_rows:
                if r["unit_id"] is None:
                    self._insert_topic(top_iid, r, today)

        totals = [r for r in rows if want == "(All subjects)" or r["subject"] == want]
        seen = sum(1 for r in totals if r["times"] > 0)
        mins = sum(r["minutes"] for r in totals)
        sessions = sum(r["times"] for r in totals)
        self.summary_lbl.config(
            text=f"{want} - {sessions} sessions - {mins} minutes - {seen}/{len(totals)} topics studied")

    def _insert_topic(self, parent, r, today):
        days = self.app.db.days_since(r["last_date"])
        days_txt = str(days) if days is not None else "-"
        conf = r["last_conf"] if r["last_conf"] else "-"
        tags = []
        if conf == "R":
            tags.append("R")
        elif conf == "A":
            tags.append("A")
        elif conf == "G":
            tags.append("G")
        if days is not None:
            if days >= 14:
                tags.append("week_hi")
            elif days >= 7:
                tags.append("week_mid")
        self.tree.insert(parent, "end", text=r["name"],
                         values=(r["name"], r["times"], r["minutes"], r["last_date"] or "-", days_txt, conf, r["page"] or ""),
                         tags=tuple(tags))


class PriorityTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        top = ttk.Frame(self)
        top.pack(fill="x", padx=12, pady=10)
        ttk.Label(top, text="What to revise next", style="Header.TLabel").pack(side="left")
        self.due_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="Only show due topics", variable=self.due_var,
                        command=self.refresh).pack(side="right", padx=6)
        self.rows = []
        cols = ("subject", "topic", "last", "days", "times", "conf", "score")
        self.tree = ttk.Treeview(self, columns=cols, show="headings")
        for c, t, w, an in [
            ("subject", "Subject", 110, "w"), ("topic", "Topic", 360, "w"), ("last", "Last revised", 100, "w"),
            ("days", "Days", 55, "e"), ("times", "Times", 55, "e"), ("conf", "Conf", 55, "center"),
            ("score", "Priority", 60, "e")]:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=an)
        self.tree.pack(fill="both", expand=True, padx=12, pady=(0, 4))
        self.tree.tag_configure("top", background="#fff3cd")
        self.tree.tag_configure("R", foreground=R)
        self.tree.tag_configure("A", foreground=AM)
        self.tree.tag_configure("G", foreground=GR)
        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=12, pady=8)
        ttk.Button(btns, text="Log study now (30 min)", command=self.quick_log).pack(side="left")
        ttk.Button(btns, text="Log with details...", command=self.detail_log).pack(side="left", padx=8)
        ttk.Label(btns, text="Most overdue and least-revised topics come first.",
                  style="Small.TLabel").pack(side="right")

    def _build(self):
        self.rows = []
        today = datetime.date.today()
        for r in self.app.db.all_topics_with_stats():
            days = self.app.db.days_since(r["last_date"])
            eff_days = days if days is not None else 30
            score = (eff_days * 8.0) / (1 + r["times"])
            conf = r["last_conf"]
            if conf == "R":
                score *= 1.5
            elif conf == "A":
                score *= 1.15
            interval = self.app.db.interval(r["times"])
            due = days is None or days >= interval
            self.rows.append((score, r, days, due))
        self.rows.sort(key=lambda x: -x[0])

    def refresh(self):
        self._build()
        self.tree.delete(*self.tree.get_children())
        show_due = self.due_var.get()
        rank = 0
        for i, (score, r, days, due) in enumerate(self.rows):
            if show_due and not due:
                continue
            rank += 1
            tags = [r["last_conf"]] if r["last_conf"] else []
            if i < 5:
                tags.append("top")
            self.tree.insert("", "end",
                             values=(r["subject"], r["name"], r["last_date"] or "never",
                                     str(days if days is not None else "-"), r["times"],
                                     r["last_conf"] or "-", f"{score:.0f}"),
                             tags=tuple(tags))

    def _selected_rows(self):
        sel = self.tree.selection()
        out = []
        for iid in sel:
            i = int(iid)
            out.append(self.rows[i])
        return out

    def quick_log(self):
        sel = self._selected_rows()
        if not sel:
            messagebox.showinfo(APP_NAME, "Select a topic in the list first.")
            return
        score, r, days, due = sel[0]
        self.app.db.add_study_session(datetime.date.today().strftime(DATE_FMT), 30, "", [(r["id"], "G")])
        self.refresh()
        messagebox.showinfo(APP_NAME, f"Logged 30 min on '{r['name']}'.")

    def detail_log(self):
        sel = self._selected_rows()
        if not sel:
            messagebox.showinfo(APP_NAME, "Select a topic in the list first.")
            return
        score, r, days, due = sel[0]
        self.app.tab_log.preselect(r["subject_id"], r["id"])
        self.app.notebook.select(self.app.tab_log)


class ManageTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", padx=12, pady=8)
        self.btns = [
            ("Add subject", self.add_subject), ("Rename subject", self.rename_subject),
            ("Delete subject", self.delete_subject), ("Add unit", self.add_unit),
            ("Rename unit", self.rename_unit), ("Delete unit", self.delete_unit),
            ("Add topic", self.add_topic), ("Edit topic", self.edit_topic),
            ("Delete topic", self.delete_topic),
        ]
        for i, (txt, cmd) in enumerate(self.btns):
            ttk.Button(toolbar, text=txt, command=cmd).pack(side="left", padx=3)
        ttk.Separator(self).pack(fill="x", padx=8)
        top = ttk.Frame(self)
        top.pack(fill="x", padx=12, pady=6)
        ttk.Button(top, text="Backup data...", command=self.backup).pack(side="left")
        ttk.Button(top, text="Restore data...", command=self.restore).pack(side="left", padx=8)
        ttk.Label(top, text=self.app.db.path, style="Small.TLabel").pack(side="right")
        self.tree = ttk.Treeview(self, columns=("page",), show="tree headings")
        self.tree.heading("#0", text="Subjects, units and topics")
        self.tree.heading("page", text="Page")
        self.tree.column("#0", width=520)
        self.tree.column("page", width=120, anchor="e")
        self.tree.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        self.tree.tag_configure("sub", font=("Segoe UI", 10, "bold"))
        self.tree.tag_configure("unit", font=("Segoe UI", 10, "italic"))

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        subs = self.app.db.get_subjects()
        if not subs:
            return
        if self.app.tab_log.subject_var.get() not in [s["name"] for s in subs]:
            self.app.tab_log.subject_var.set(subs[0]["name"])
            self.app.tab_log._subject_changed()
        for s in subs:
            siid = f"s{s['id']}"
            self.tree.insert("", "end", iid=siid, text=s["name"], open=True, tags=("sub",))
            tree = self.app.db.get_units_tree(s["id"])
            seen = set()

            def walk(pid, depth):
                for u in tree.get(pid, []):
                    if u["id"] in seen:
                        continue
                    seen.add(u["id"])
                    uiid = f"u{u['id']}"
                    self.tree.insert(siid, "end", iid=uiid, open=False,
                                     text=("  " * depth) + u["name"], tags=("unit",))
                    for t in self.app.db.get_topics(s["id"], u["id"]):
                        self.tree.insert(uiid, "end", iid=f"t{t['id']}",
                                         text=("  " * (depth + 1)) + t["name"], values=(t["page"] or "",))
                    walk(u["id"], depth + 1)

            walk(None, 0)
            for t in self.app.db.get_topics(s["id"], None):
                self.tree.insert(siid, "end", iid=f"t{t['id']}",
                                 text="  " + t["name"], values=(t["page"] or "",))

    def _sel(self):
        sel = self.tree.selection()
        return sel[0] if sel else None

    def _target_sid_unit(self):
        iid = self._sel()
        if not iid:
            return None, None
        if iid.startswith("s"):
            return int(iid[1:]), None
        if iid.startswith("u"):
            row = self.app.db.get_unit(int(iid[1:]))
            return row["subject_id"], row["id"]
        if iid.startswith("t"):
            topic = self.app.db.get_topic(int(iid[1:]))
            return topic["subject_id"], topic["unit_id"]
        return None, None

    def _parse_iid(self):
        iid = self._sel()
        if not iid:
            return None
        kind, num = iid[0], int(iid[1:])
        return kind, num

    def add_subject(self):
        name = simpledialog.askstring("Add subject", "Subject name:", parent=self)
        if name and name.strip():
            sid = self.app.db.add_subject(name.strip())
            messagebox.showinfo(APP_NAME, f"Added '{name.strip()}'.")
            self.refresh()

    def rename_subject(self):
        kind, num = self._parse_iid() or (None, None)
        if kind != "s":
            messagebox.showinfo(APP_NAME, "Select a subject first.")
            return
        cur = self.app.db.get_subjects()
        row = next((r for r in cur if r["id"] == num), None)
        name = simpledialog.askstring("Rename subject", "New name:", initialvalue=row["name"], parent=self)
        if name and name.strip():
            self.app.db.rename_subject(num, name.strip())
            self.refresh()

    def delete_subject(self):
        kind, num = self._parse_iid() or (None, None)
        if kind != "s":
            messagebox.showinfo(APP_NAME, "Select a subject first.")
            return
        if messagebox.askyesno(APP_NAME, "Delete this subject and ALL its units, topics and sessions?"):
            self.app.db.delete_subject(num)
            self.refresh()

    def add_unit(self):
        sid, _ = self._target_sid_unit()
        parent = None
        iid = self._sel()
        if iid and iid.startswith("u"):
            parent = int(iid[1:])
        if sid is None:
            messagebox.showinfo(APP_NAME, "Select a subject (or unit) first.")
            return
        name = simpledialog.askstring("Add unit", "Unit / section name:", parent=self)
        if name and name.strip():
            self.app.db.add_unit(sid, name.strip(), parent)
            self.refresh()

    def rename_unit(self):
        kind, num = self._parse_iid() or (None, None)
        if kind != "u":
            messagebox.showinfo(APP_NAME, "Select a unit first.")
            return
        row = self.app.db.get_unit(num)
        name = simpledialog.askstring("Rename unit", "New name:", initialvalue=row["name"], parent=self)
        if name and name.strip():
            self.app.db.rename_unit(num, name.strip())
            self.refresh()

    def delete_unit(self):
        kind, num = self._parse_iid() or (None, None)
        if kind != "u":
            messagebox.showinfo(APP_NAME, "Select a unit first.")
            return
        if messagebox.askyesno(APP_NAME, "Delete this unit and everything under it?"):
            self.app.db.delete_unit(num)
            self.refresh()

    def add_topic(self):
        sid, uid = self._target_sid_unit()
        if sid is None:
            messagebox.showinfo(APP_NAME, "Select a subject, unit or topic first.")
            return
        name = simpledialog.askstring("Add topic", "Topic name:", parent=self)
        if not name or not name.strip():
            return
        page = simpledialog.askstring("Add topic", "Start page (optional):", parent=self)
        self.app.db.add_topic(sid, name.strip(), uid, page or "")
        self.refresh()

    def edit_topic(self):
        kind, num = self._parse_iid() or (None, None)
        if kind != "t":
            messagebox.showinfo(APP_NAME, "Select a topic first.")
            return
        row = self.app.db.get_topic(num)
        name = simpledialog.askstring("Edit topic", "Topic name:", initialvalue=row["name"], parent=self)
        page = simpledialog.askstring("Edit topic", "Start page (optional):", initialvalue=row["page"], parent=self)
        self.app.db.update_topic(num, name=name.strip() if name else None,
                                 page=page if page is not None else None)
        self.refresh()

    def delete_topic(self):
        kind, num = self._parse_iid() or (None, None)
        if kind != "t":
            messagebox.showinfo(APP_NAME, "Select a topic first.")
            return
        if messagebox.askyesno(APP_NAME, "Delete this topic and its sessions?"):
            self.app.db.delete_topic(num)
            self.refresh()

    def backup(self):
        dest = filedialog.asksaveasfilename(
            title="Backup revision data",
            defaultextension=".db",
            initialfile="revision_backup_" + datetime.date.today().strftime("%Y%m%d") + ".db",
            filetypes=[("SQLite database", "*.db"), ("All files", "*.*")])
        if dest:
            try:
                self.app.db.backup(dest)
                messagebox.showinfo(APP_NAME, f"Backup saved to:\n{dest}")
            except Exception as e:
                messagebox.showerror(APP_NAME, f"Backup failed:\n{e}")

    def restore(self):
        src = filedialog.askopenfilename(
            title="Restore revision data",
            filetypes=[("SQLite database", "*.db"), ("All files", "*.*")])
        if not src:
            return
        if messagebox.askyesno(APP_NAME, "Replace current data with the backup?"):
            try:
                self.app.db.close()
                shutil.copyfile(src, self.app.db.path)
                self.app.db = Db(self.app.db.path)
                self.refresh()
                self.app.tab_dash.refresh()
                self.app.tab_prog.refresh()
                self.app.tab_prior.refresh()
                messagebox.showinfo(APP_NAME, "Data restored.")
            except Exception as e:
                messagebox.showerror(APP_NAME, f"Restore failed:\n{e}")


class SyncTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app

        ttk.Label(self, text="LAN Sync",
                  style="Header.TLabel").pack(anchor="w", padx=16, pady=(16, 2))
        ttk.Label(self, text=(
            "Put this PC and your phone on the same Wi-Fi network.\n"
            "Sync through the revision hub (this PC's built-in hub, or a server on your LAN)."
        ), style="Small.TLabel").pack(anchor="w", padx=16)

        card = ttk.Frame(self)
        card.pack(fill="x", padx=16, pady=12)
        self.status_var = tk.StringVar(value="")
        ttk.Label(card, textvariable=self.status_var, style="Title.TLabel").pack(anchor="w")
        ttk.Label(card, textvariable=self.status_var, style="Small.TLabel").pack(anchor="w")

        row = ttk.Frame(self)
        row.pack(fill="x", padx=16)
        ttk.Label(row, text="Hub address (e.g. 192.168.2.187):").pack(side="left")
        self.peer_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.peer_var, width=22).pack(side="left", padx=8)
        self.find_btn = ttk.Button(row, text="Auto-find", command=self.auto_find)
        self.find_btn.pack(side="left", padx=4)
        self.sync_btn = ttk.Button(row, text="Sync now", command=self.sync_now)
        self.sync_btn.pack(side="left", padx=4)

        result = ttk.Frame(self)
        result.pack(fill="both", expand=True, padx=16, pady=12)
        self.result_txt = tk.Text(result, height=8, relief="flat",
                                  bg="#f5f5f5", font=("Consolas", 10))
        self.result_txt.pack(fill="both", expand=True)

        self.refresh()

    def refresh(self):
        srv = self.app.sync_server
        state = f"Built-in hub: RUNNING  →  {srv.url}  (this PC)" if srv.running \
            else "Built-in hub: STOPPED"
        self.status_var.set(state)

    def auto_find(self):
        self.find_btn.config(state="disabled")
        self.status_var.set("Auto-find: searching the LAN...")

        def _run():
            res = find_hub(known=[self.peer_var.get().strip()] if self.peer_var.get().strip() else None)
            srv = self.app.sync_server

            def _apply():
                self.find_btn.config(state="normal")
                if res:
                    self.peer_var.set(res)
                    self.status_var.set(f"Hub found: {res}")
                else:
                    self.status_var.set("No hub found. Check Wi-Fi or enter the address manually.")
            self.after(0, _apply)

        Thread(target=_run, daemon=True).start()

    def sync_now(self):
        srv = self.app.sync_server
        if not srv.running:
            srv.start()
        address = self.peer_var.get().strip() or srv.ip
        if ":" not in address:
            address = f"{address}:{srv.port}"
        try:
            client = SyncClient(address)
            before = self.app.db.totals()
            self.result_txt.delete("1.0", "end")
            self.result_txt.insert("end", f"Syncing with {client.url} ...\n")
            self.result_txt.update()
            def _do():
                try:
                    result = client.sync(self.app.db)
                    rows = sum(len(v) for v in result.get("tables", {}).values())
                    after = self.app.db.totals()
                    ok = f"Done. Synced {rows} rows.\nSessions: {before['sessions']} → {after['sessions']}\nMinutes: {before['minutes']} → {after['minutes']}"
                    self.result_txt.insert("end", ok)
                    self.refresh()
                    self.app.refresh_all()
                except Exception as e:
                    self.result_txt.insert("end", f"Failed:\n{e}")
            thread = Thread(target=_do, daemon=True)
            thread.start()
        except Exception as e:
            self.result_txt.delete("1.0", "end")
            self.result_txt.insert("end", f"Failed:\n{e}")


def selftest():
    test_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_selftest")
    os.makedirs(test_dir, exist_ok=True)
    test_db = os.path.join(test_dir, "selftest.db")
    if os.path.exists(test_db):
        os.remove(test_db)
    db = Db(test_db)
    topics = db.all_topics_with_stats()
    print(f"Subjects: {[s['name'] for s in db.get_subjects()]}")
    print(f"Total topics: {len(topics)}")
    tot0 = db.totals()
    print(f"Fresh totals: {tot0}")
    assert tot0["sessions"] == 0
    db.add_study_session("2026-09-10", 35, "test", [(topics[0]["id"], "G")])
    db.add_study_session("2026-09-11", 20, "", [(topics[1]["id"], "R"), (topics[2]["id"], "G")])
    t0 = dict(db.topic_stats(topics[0]["id"]))
    t1 = dict(db.topic_stats(topics[1]["id"]))
    tot = db.totals()
    print("After adding 2 sessions (second covers 2 topics):")
    print("  topic0:", t0)
    print("  topic1:", t1)
    print("  totals:", tot)
    assert t0["times"] == 1 and t0["minutes"] == 35 and t0["last_conf"] == "G"
    assert t1["times"] == 1 and t1["minutes"] == 20 and t1["last_conf"] == "R"
    assert tot["sessions"] == 2 and tot["minutes"] == 55 and tot["studied"] == 3
    pri = PriorityTab.__mro__
    print(f"PriorityTab class OK: {pri[0].__name__}")
    summary = db.subject_summary()
    print("Subject summary rows:", len(summary))
    db.close()
    shutil.rmtree(test_dir, ignore_errors=True)
    print("SELFTEST OK")


def main():
    if "--selftest" in sys.argv:
        selftest()
        return
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    db = Db()
    app = RevisionApp(db)
    app.mainloop()


if __name__ == "__main__":
    main()