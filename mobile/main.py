"""GCSE Revision Tracker - mobile companion (Kivy).
Browses the offline topic database, logs study sessions, shows progress,
and syncs with the desktop hub over LAN."""

import json
import os
from datetime import date
from threading import Thread

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.graphics import Color, RoundedRectangle
from kivy.metrics import dp, sp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner
from kivy.uix.tabbedpanel import TabbedPanel, TabbedPanelItem
from kivy.uix.textinput import TextInput

from revision_core import Db, SyncClient, find_hub

APP_DIR = os.path.dirname(os.path.abspath(__file__))
FALLBACK_DB_PATH = os.path.join(APP_DIR, "revision.db")


def default_db_path(app=None):
    app = app or App.get_running_app()
    if app is not None and getattr(app, "user_data_dir", None):
        return os.path.join(app.user_data_dir, "revision.db")
    return FALLBACK_DB_PATH


def settings_path(app):
    return os.path.join(app.user_data_dir, "settings.json")


def load_settings(app):
    try:
        with open(settings_path(app), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(app, data):
    try:
        import os as _os
        _os.makedirs(app.user_data_dir, exist_ok=True)
        with open(settings_path(app), "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass

GR = (0.18, 0.49, 0.22, 1)
AM = (0.97, 0.66, 0.15, 1)
RD = (0.83, 0.18, 0.18, 1)
GRAY = (0.4, 0.4, 0.4, 1)
CONF_LABELS = ["Green (got it)", "Red (stuck)", "Amber (nearly)"]


def conf_key(label):
    for k, v in (("G", "Green (got it)"), ("R", "Red (stuck)"), ("AM", "Amber (nearly)")):
        if v == label:
            return k
    return "G"


def conf_color(key):
    return {"G": GR, "R": RD, "AM": AM}.get(key, GRAY)


class Card(BoxLayout):
    def __init__(self, **kw):
        super().__init__(orientation="horizontal", padding=dp(8), spacing=dp(8), **kw)
        with self.canvas.before:
            Color(*self._bg())
            self._rect = RoundedRectangle(pos=self.pos, size=self.size, radius=[dp(8)])
        self.bind(pos=lambda w, v: setattr(self._rect, "pos", v),
                  size=lambda w, v: setattr(self._rect, "size", v))

    def _bg(self):
        return (0.95, 0.95, 0.95, 1)


class ReviewScreen(BoxLayout):
    def __init__(self, app, **kw):
        self.app = app
        super().__init__(orientation="vertical", padding=dp(10), spacing=dp(6), **kw)
        self.subj = Spinner(size_hint_y=None, height=dp(42), text="", values=[])
        self.unit = Spinner(size_hint_y=None, height=dp(38), text="", values=["All"])
        self.unit.text = "All"
        self.count = Label(size_hint_y=None, height=dp(24), font_size=sp(13), color=GRAY)
        self.scroll = ScrollView()
        self.list = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(6), padding=dp(4))
        self.list.bind(minimum_height=self.list.setter("height"))
        self.scroll.add_widget(self.list)

        self.add_widget(self.subj)
        self.add_widget(self.unit)
        self.add_widget(self.count)
        self.add_widget(self.scroll)
        self.subj.bind(text=lambda *a: self.load_unit_options())
        self.unit.bind(text=lambda *a: self.load_topics())
        Clock.schedule_once(lambda dt: self.init_subjects(), 0)

    def init_subjects(self):
        subs = self.app.db.get_subjects()
        self.subjects = list(subs)
        self.subj.values[:] = [s["name"] for s in subs]
        self.subj.text = ""
        if subs:
            self.subj.text = subs[0]["name"]
        else:
            self.count.text = "No subjects yet - seed data missing."

    def load_unit_options(self):
        subj = self.current_subject()
        if not subj:
            return
        tree = self.app.db.get_units_tree(subj["id"])
        flat = [u for lst in sorted(tree.values()) for u in lst]
        self.unit.values[:] = ["All"] + [u["name"] for u in flat]
        self.unit.text = "All"

    def load_topics(self):
        subj = self.current_subject()
        self.list.clear_widgets()
        if not subj:
            return
        unit_id = None
        if self.unit.text and self.unit.text != "All":
            u = self.unit_item(self.unit.text)
            unit_id = u["id"] if u else None
        topics = self.app.db.get_topics(subj["id"], unit_id)
        self.count.text = f"{len(topics)} topics"
        if not topics:
            self.list.add_widget(Label(text="No topics here yet.", color=GRAY, size_hint_y=None, height=dp(36)))
            return
        for t in topics:
            st = self.app.db.topic_stats(t["id"])
            color = conf_color(st["last_conf"])
            card = Card(size_hint_y=None, height=dp(64))
            col = BoxLayout(orientation="vertical", size_hint_x=1)
            ttl = Label(text=t["name"], halign="left", valign="middle", font_size=sp(14),
                        color=color, size_hint_y=None, height=dp(30))
            ttl.bind(size=lambda w, s: setattr(w, "text_size", s))
            sub = Label(text=f"{st['times']} session(s)  |  {st['minutes']} min  |  page {t['page'] or '-'}",
                        halign="left", valign="middle", font_size=sp(11), color=GRAY,
                        size_hint_y=None, height=dp(20))
            sub.bind(size=lambda w, s: setattr(w, "text_size", s))
            col.add_widget(ttl)
            col.add_widget(sub)
            btn = Button(text="Log", size_hint_x=None, width=dp(64), on_release=lambda b, tt=t: self.log_session(tt))
            card.add_widget(col)
            card.add_widget(btn)
            self.list.add_widget(card)
        self.scroll.scroll_y = 1.0

    def current_subject(self):
        idx = self.subj.values.index(self.subj.text) if self.subj.text in self.subj.values else -1
        return self.subjects[idx] if 0 <= idx < len(self.subjects) else None

    def unit_item(self, name):
        subj = self.current_subject()
        if not subj:
            return None
        for lst in sorted(self.app.db.get_units_tree(subj["id"]).values()):
            for u in lst:
                if u["name"] == name:
                    return u
        return None

    def log_session(self, topic):
        box = BoxLayout(orientation="vertical", spacing=dp(6), padding=dp(10))
        title = Label(text=topic["name"], font_size=sp(15), bold=True, size_hint_y=None, height=dp(36))
        minutes = TextInput(text="30", input_filter="int", size_hint_y=None, height=dp(40))
        conf = Spinner(text=CONF_LABELS[0], values=CONF_LABELS, size_hint_y=None, height=dp(38))
        notes = TextInput(text="", hint_text="Notes (optional)", size_hint_y=None, height=dp(50))
        row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        save = Button(text="Save")
        cancel = Button(text="Cancel")
        row.add_widget(save)
        row.add_widget(cancel)
        for w in (title, minutes, conf, notes, row):
            box.add_widget(w)
        pop = Popup(title="Log study", content=box, size_hint=(0.9, 0.75))
        save.bind(on_release=lambda *a: self._save(pop, topic, minutes.text, conf.text, notes.text))
        cancel.bind(on_release=pop.dismiss)
        pop.open()

    def _save(self, pop, topic, minutes, conf, notes):
        try:
            mins = int(minutes or 0)
        except ValueError:
            mins = 0
        self.app.db.add_study_session(
            date.today().isoformat(), mins, notes, [(topic["id"], conf_key(conf))])
        pop.dismiss()
        self.load_topics()
        self.app.progress.populate()


class ProgressScreen(BoxLayout):
    def __init__(self, app, **kw):
        self.app = app
        super().__init__(orientation="vertical", padding=dp(10), spacing=dp(6), **kw)
        self.header = Label(size_hint_y=None, height=dp(28), font_size=sp(15), bold=True, color=GR)
        self.scroll = ScrollView()
        self.list = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(4), padding=dp(4))
        self.list.bind(minimum_height=self.list.setter("height"))
        self.scroll.add_widget(self.list)
        self.add_widget(self.header)
        self.add_widget(self.scroll)
        Clock.schedule_once(lambda dt: self.populate(), 0)

    def populate(self):
        self.list.clear_widgets()
        tot = self.app.db.totals()
        self.header.text = f"{tot['sessions']} sessions  |  {tot['minutes']} min  |  {tot['studied']} topics studied"
        for r in self.app.db.all_topics_with_stats():
            if not r["times"]:
                continue
            color = conf_color(r["last_conf"])
            line = Label(
                text=f"{r['subject']}  |  {r['name']}  |  {r['times']}x {r['minutes']}m{('  |  ' + r['last_date']) if r.get('last_date') else ''}",
                halign="left", valign="middle", font_size=sp(12), color=color,
                size_hint_y=None, height=dp(28))
            line.bind(size=lambda w, s: setattr(w, "text_size", s))
            self.list.add_widget(line)


class SyncScreen(BoxLayout):
    def __init__(self, app, **kw):
        self.app = app
        super().__init__(orientation="vertical", padding=dp(12), spacing=dp(8), **kw)
        self.info = Label(halign="left", valign="middle", font_size=sp(12), color=GRAY,
                          text="Put this phone and the hub on the same Wi-Fi network.\n"
                               "Tap Auto-find, or type the hub address (shown in the hub's config).",
                          size_hint_y=None, height=dp(56))
        self.info.bind(size=lambda w, s: setattr(w, "text_size", s))
        self.add_widget(self.info)
        self.url = TextInput(text=self.app.hub_url or "", size_hint_y=None, height=dp(44),
                             hint_text="hub address, e.g. 192.168.2.187")
        self.add_widget(self.url)
        row = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(8))
        find = Button(text="Auto-find hub")
        find.bind(on_release=lambda *a: self.find_hub())
        row.add_widget(find)
        btn = Button(text="Sync now")
        row.add_widget(btn)
        self.add_widget(row)
        self.status = Label(halign="left", valign="top", text=self.app.sync_status or "Not synced yet.",
                            font_size=sp(12), color=GRAY)
        self.status.bind(size=lambda w, s: setattr(w, "text_size", s))
        self.add_widget(self.status)
        btn.bind(on_release=lambda *a: self.do_sync())
        Clock.schedule_once(lambda dt: self.find_hub(), 0.6)

    def find_hub(self, *_):
        self.status.text = "Searching for hub..."
        known = self.app.known_hubs.get("known_hubs", []) if isinstance(self.app.known_hubs, dict) else self.app.known_hubs

        def _run():
            res = find_hub(known=known)
            Clock.schedule_once(lambda dt: self._found(res))

        Thread(target=_run, daemon=True).start()

    def _found(self, res, *_):
        if res:
            self.app.remember_hub(res)
            if not self.url.text.strip():
                self.url.text = res
            self.status.text = f"Hub found: {res}"
        else:
            self.status.text = "No hub found. Enter the address manually or check Wi-Fi."

    def do_sync(self):
        url = self.url.text.strip()
        if not url:
            self.status.text = "Enter the hub address first."
            return
        try:
            client = SyncClient(url)
            res = client.sync(self.app.db)
            rows = sum(len(v) for v in res.get("tables", {}).values())
            self.app.remember_hub(url)
            self.app.sync_status = f"Synced {rows} rows from {client.url} at {date.today().isoformat()}"
            self.status.text = self.app.sync_status
            self.app.progress.populate()
        except Exception as e:
            self.status.text = f"Sync failed: {e}"


class RevisionApp(App):
    title = "GCSE Revision"

    def __init__(self, db_path=None, **kw):
        super().__init__(**kw)
        self.db_path = db_path

    def build(self):
        self.db = Db(self.db_path or default_db_path(self))
        self.hub_url = ""
        self.sync_status = ""

        def _known():
            return load_settings(self).get("known_hubs", [])

        self.known_hubs = {"known_hubs": _known()}

        def remember(res):
            if not res:
                return
            res = res.replace("http://", "").rstrip("/")
            cur = self.known_hubs["known_hubs"]
            if res not in cur:
                cur.insert(0, res)
            self.known_hubs["known_hubs"] = cur[:5]
            save_settings(self, self.known_hubs)

        self.remember_hub = remember
        panel = TabbedPanel(do_default_tab=False)
        self.review = ReviewScreen(self)
        self.progress = ProgressScreen(self)
        self.sync = SyncScreen(self)
        for text, scr in (("Review", self.review), ("Progress", self.progress), ("Sync", self.sync)):
            item = TabbedPanelItem(text=text)
            item.add_widget(scr)
            panel.add_widget(item)
        return panel


if __name__ == "__main__":
    Window.minimum_height = 480
    Window.minimum_width = 320
    RevisionApp().run()