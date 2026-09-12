import datetime
import json
import os
import shutil
import socket
import sqlite3
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request as _UrllibReq, urlopen as _Urlopen

APP_NAME = "GCSE Revision Tracker"
VERSION = "2.0.0"
DATE_FMT = "%Y-%m-%d"
R, AM, GR = "#D32F2F", "#F9A825", "#2E7D32"
NS = uuid.NAMESPACE_OID
SYNC_PORT = 8765
DISCOVERY_PORT = 8799

SCHEMA = """
CREATE TABLE IF NOT EXISTS subjects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    sort INTEGER DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS units (
    id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    parent_id TEXT REFERENCES units(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    sort INTEGER DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS topics (
    id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    unit_id TEXT REFERENCES units(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    page TEXT DEFAULT '',
    sort INTEGER DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS study_sessions (
    id TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    minutes INTEGER DEFAULT 0,
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_topics (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES study_sessions(id) ON DELETE CASCADE,
    topic_id TEXT NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    confidence TEXT DEFAULT 'G',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_st_session ON session_topics(session_id);
CREATE INDEX IF NOT EXISTS idx_st_topic ON session_topics(topic_id);
CREATE INDEX IF NOT EXISTS idx_st_u ON units(subject_id);
CREATE INDEX IF NOT EXISTS idx_t_u ON topics(subject_id);
"""

SYNC_TABLES = ["subjects", "units", "topics", "study_sessions", "session_topics"]


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def find_hub(known=None, port=SYNC_PORT):
    """Return \"host:port\" of the first reachable revision-hub, or None.

    Probes known/remembered addresses first (fast path), then scans the
    local /24 subnet in parallel. Used by desktop and mobile auto-discovery.
    """
    from concurrent.futures import ThreadPoolExecutor

    def probe(host):
        try:
            with _Urlopen(f"http://{host}:{port}/", timeout=0.3) as resp:
                return json.loads(resp.read().decode()).get("service") == "revision-hub"
        except Exception:
            return False

    known_hosts = []
    if known:
        for addr in known:
            host = addr.replace("http://", "").replace("https://", "").split(":")[0].strip("/")
            if host:
                known_hosts.append(host)

    for host in known_hosts:
        if probe(host):
            return f"{host}:{port}"

    local = get_local_ip()
    if local.startswith("127."):
        return None
    prefix = ".".join(local.split(".")[:3])
    hosts = [f"{prefix}.{i}" for i in range(1, 255) if f"{prefix}.{i}" not in known_hosts]
    with ThreadPoolExecutor(max_workers=64) as ex:
        for host, ok in zip(hosts, ex.map(probe, hosts)):
            if ok:
                return f"{host}:{port}"
    return None


def sid5(key):
    return str(uuid.uuid5(NS, key))


def new_id():
    return str(uuid.uuid4())


class Db:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.connect()

    def connect(self):
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        # Only migrate when the live subjects table is truly legacy and no
        # prior (partial) migration is in flight. A leftover subjects__old
        # means migration started before; finish it via repair/merge instead
        # of renaming again (renaming a table whose __old already exists is
        # what let earlier versions churn and lose data).
        if (self._has_table("subjects") and not self._new_schema("subjects")
                and not self._has_table("subjects__old")):
            self._migrate_legacy()
        else:
            self._repair_migration()
        self._merge_from_leftovers()
        self._drop_leftovers()
        if not self.get_subjects():
            self.seed()
            self.conn.commit()

    def _column_names(self, table):
        return [r[1] for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()]

    def _has_table(self, name):
        return self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

    def _new_schema(self, table):
        cols = self._column_names(table)
        return "updated_at" in cols and "id" in cols

    def _legacy_present(self):
        if not self._has_table("subjects"):
            return False
        return not self._new_schema("subjects")

    def _repair_migration(self):
        if not self._has_table("subjects") and self._has_table("subjects__old") \
                and self._new_schema("subjects__old"):
            self.conn.execute("ALTER TABLE subjects__old RENAME TO subjects")
            self.conn.commit()

    def _merge_from_leftovers(self):
        for t in SYNC_TABLES:
            old = f"{t}__old"
            if not self._has_table(old):
                continue
            n = self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            if n > 0:
                continue
            cols = self._column_names(t)
            rows = self.conn.execute(f"SELECT * FROM {old}").fetchall()
            for r in rows:
                keys = [c for c in cols if c in r.keys()]
                vals = [r[c] for c in keys]
                self.conn.execute(
                    f"INSERT OR IGNORE INTO {t} ({','.join(keys)}) "
                    f"VALUES ({','.join('?' * len(keys))})", vals)
        self.conn.commit()

    def _drop_leftovers(self):
        for t in ["subjects", "units", "topics", "study_sessions", "session_topics", "sessions"]:
            for suffix in ("__old", "__legacy"):
                name = f"{t}{suffix}"
                if not self._has_table(name):
                    continue
                # Only drop a leftover once the live table has rows; otherwise
                # _merge_from_leftovers() hasn't finished and dropping would lose data.
                live = self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                old = self.conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                if live or old == 0:
                    self.conn.execute(f"DROP TABLE IF EXISTS {name}")
        self.conn.commit()

    def _migrate_legacy(self):
        old = [t for t in ["subjects", "units", "topics", "study_sessions", "session_topics"]
               if self._has_table(t)]
        for t in old:
            self.conn.execute(f"ALTER TABLE {t} RENAME TO {t}__old")
        if self._has_table("sessions"):
            self.conn.execute("ALTER TABLE sessions RENAME TO sessions__legacy")
        self.conn.executescript(SCHEMA)
        now = now_iso()

        subj_names = {}
        subj_map = {}
        for r in self.conn.execute("SELECT * FROM subjects__old").fetchall():
            u = sid5(f"subject::{r['name']}")
            self.conn.execute(
                "INSERT INTO subjects(id,name,sort,updated_at) VALUES(?,?,?,?)",
                (u, r["name"], r["sort"], now))
            subj_map[r["id"]] = u
            subj_names[r["id"]] = r["name"]

        unit_map = {}
        units_old = {
            r["id"]: r for r in self.conn.execute("SELECT * FROM units__old").fetchall()
        } if "units__old" in old else {}
        for rid, r in units_old.items():
            name = f"{subj_names.get(r['subject_id'], '')}::{r['name']}"
            u = sid5(f"unit::{name}")
            parent_u = unit_map.get(r["parent_id"]) if r["parent_id"] else None
            self.conn.execute(
                "INSERT INTO units(id,subject_id,parent_id,name,sort,updated_at) VALUES(?,?,?,?,?,?)",
                (u, subj_map.get(r["subject_id"]), parent_u, r["name"],
                 r["sort"] or rid, now))
            unit_map[rid] = u

        sess_src = []
        if "study_sessions__old" in [f"{t}__old" for t in old]:
            sess_src = self.conn.execute(
                "SELECT id, date, minutes, notes FROM study_sessions__old").fetchall()
        elif self._has_table("sessions__legacy"):
            sess_src = self.conn.execute(
                "SELECT DISTINCT id, date, minutes, notes FROM sessions__legacy").fetchall()

        sess_map = {}

        def session_id_for(rid):
            if rid not in sess_map:
                nu = new_id()
                sess_map[rid] = nu
            return sess_map[rid]

        for r in sess_src:
            su = new_id()
            sess_map[r["id"]] = su
            self.conn.execute(
                "INSERT INTO study_sessions(id,date,minutes,notes,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (su, r["date"], r["minutes"], r["notes"], now, now))

        topic_map = {}
        if "topics__old" in [f"{t}__old" for t in old]:
            for r in self.conn.execute(
                    "SELECT * FROM topics__old ORDER BY subject_id, id").fetchall():
                name = f"{subj_names.get(r['subject_id'], '')}::{r['name']}::{r['page'] or ''}"
                u = sid5(f"topic::{name}")
                unit_u = unit_map.get(r["unit_id"]) if r["unit_id"] else None
                self.conn.execute(
                    "INSERT INTO topics(id,subject_id,unit_id,name,page,sort,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (u, subj_map.get(r["subject_id"]), unit_u, r["name"], r["page"] or "",
                     r["sort"] or r["id"], now))
                topic_map[r["id"]] = u

        if self._has_table("sessions__legacy"):
            for r in self.conn.execute(
                    "SELECT topic_id, date, minutes, notes, confidence FROM sessions__legacy").fetchall():
                sid = None
                for rt in self.conn.execute(
                        "SELECT id FROM study_sessions WHERE date=? AND minutes=? AND notes=?",
                        (r["date"], r["minutes"], r["notes"])).fetchall():
                    sid = rt["id"]
                if sid is None:
                    sid = new_id()
                    self.conn.execute(
                        "INSERT INTO study_sessions(id,date,minutes,notes,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                        (sid, r["date"], r["minutes"], r["notes"], now, now))
                self.conn.execute(
                    "INSERT INTO session_topics(id,session_id,topic_id,confidence,updated_at) VALUES(?,?,?,?,?)",
                    (new_id(), sid, topic_map.get(r["topic_id"]), r["confidence"], now))

        if "session_topics__old" in [f"{t}__old" for t in old]:
            for r in self.conn.execute("SELECT * FROM session_topics__old").fetchall():
                self.conn.execute(
                    "INSERT INTO session_topics(id,session_id,topic_id,confidence,updated_at) VALUES(?,?,?,?,?)",
                    (new_id(), session_id_for(r["session_id"]),
                     topic_map.get(r["topic_id"]), r["confidence"], now))

        for t in [f"{n}__old" for n in old] + ["sessions__legacy"]:
            try:
                self.conn.execute(f"DROP TABLE IF EXISTS {t}")
            except Exception:
                pass
        self.conn.commit()

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def seed(self):
        con = self.conn
        now = now_iso()
        self._t_seq = 0
        self._u_seq = 0

        def sseq():
            self._u_seq += 1
            return self._u_seq

        def tseq():
            self._t_seq += 1
            return self._t_seq

        def subject(name):
            u = sid5(f"subject::{name}")
            con.execute(
                "INSERT INTO subjects(id,name,sort,updated_at) VALUES(?,?,(SELECT COALESCE(MAX(sort),0)+1 FROM subjects),?)",
                (u, name, now))
            return u

        def unit(sid, name, parent=None):
            u = sid5(f"unit::{name}::{parent or ''}")
            con.execute(
                "INSERT INTO units(id,subject_id,parent_id,name,sort,updated_at) VALUES(?,?,?,?,?,?)",
                (u, sid, parent, name, sseq(), now))
            return u

        def topic(sid, name, unit_id=None, page=None):
            u = sid5(f"topic::{name}::{page or ''}")
            con.execute(
                "INSERT INTO topics(id,subject_id,unit_id,name,page,sort,updated_at) VALUES(?,?,?,?,?,?,?)",
                (u, sid, unit_id, name, page or "", tseq(), now))

        sid = subject("Biology")
        for name, page in [
            ("B1 Cell biology", "2"), ("B2 Cell transport", "12"), ("B3 Cell division", "24"),
            ("B4 Organisation in animals", "34"), ("B5 Enzymes", "46"), ("B6 Organisation in plants", "60"),
            ("B7 The spread of diseases", "72"), ("B8 Preventing and treating disease", "84"),
            ("B9 Monoclonal antibodies", "96"), ("B10 Non-communicable diseases", "106"),
            ("B11 Photosynthesis", "118"), ("B12 Respiration", "130"), ("B13 Nervous system & homeostasis", "142"),
            ("B14 Hormonal coordination", "156"), ("B15 Variation", "168"), ("B16 Reproduction", "180"),
            ("B17 Evolution", "194"), ("B18 Adaptation", "208"), ("B19 Organising an ecosystem", "220"),
            ("B20 Humans and biodiversity", "232"),
        ]:
            topic(sid, name, page=page)

        sid = subject("Chemistry")
        for name, page in [
            ("C1 The atom", "2"), ("C2 Covalent bonding", "14"), ("C3 Ionic bonding, metallic bonding, and structure", "26"),
            ("C4 The Periodic Table", "38"), ("C5 Transition metals and nanoparticles", "48"),
            ("C6 Chemical calculations with mass", "58"), ("C7 Chemical calculations with moles", "68"),
            ("C8 Reactions of metals", "78"), ("C9 Reactions of acids", "88"), ("C10 Electrolysis", "98"),
            ("C11 Energy changes", "110"), ("C12 Rate of reaction", "120"), ("C13 Equilibrium", "132"),
            ("C14 Crude oil and fuels", "142"), ("C15 Organic reactions", "152"), ("C16 Polymers", "162"),
            ("C17 Chemical analysis", "172"), ("C18 The Earth's atmosphere", "184"),
            ("C19 Using the Earth's resources", "194"), ("C20 Making our resources", "208"),
        ]:
            topic(sid, name, page=page)

        sid = subject("Physics")
        for name, page in [
            ("P1 Energy stores and transfers", "2"), ("P2 Energy transfers by heating", "14"),
            ("P3 National and global energy resources", "26"), ("P4 Supplying energy", "36"),
            ("P5 Electric circuits", "48"), ("P6 Energy of matter", "60"), ("P7 Atoms", "72"),
            ("P8 Radiation", "84"), ("P9 Forces", "96"), ("P10 Pressure in liquids and gases", "108"),
            ("P11 Speed", "116"), ("P12 Newton's laws of motion", "128"), ("P13 Braking and momentum", "140"),
            ("P14 Mechanical waves", "152"), ("P15 Electromagnetic waves", "162"), ("P16 Light and sound", "174"),
            ("P17 Magnets and electromagnets", "186"), ("P18 Induced potential and transformers", "200"),
            ("P19 Space", "212"),
        ]:
            topic(sid, name, page=page)

        sid = subject("Computer Science")
        comp1 = unit(sid, "Component 01 - Computer Systems")
        s1 = unit(sid, "Section One - Components of a Computer System", comp1)
        for t in ["Computer Systems", "The CPU", "Memory", "CPU and System Performance", "Secondary Storage",
                  "Systems Software - The OS", "Systems Software - Utilities"]:
            topic(sid, t, s1)
        s2 = unit(sid, "Section Two - Data Representation", comp1)
        for t in ["Units", "Binary Numbers", "Hexadecimal Numbers", "Characters", "Storing Images",
                  "Storing Sound", "Compression"]:
            topic(sid, t, s2)
        s3 = unit(sid, "Section Three - Networks", comp1)
        for t in ["Networks - LANs and WANs", "Networks Hardware", "Wireless Networks", "Network Topologies",
                  "Client-server and Peer-to-Peer Networks", "Network Protocols", "Networks - The Internet",
                  "Network Security Threats"]:
            topic(sid, t, s3)
        s4 = unit(sid, "Section Four - Issues", comp1)
        for t in ["Ethical and Cultural Issues", "Computer Legislation", "Environmental Issues",
                  "Open Source and Proprietary Software"]:
            topic(sid, t, s4)
        comp2 = unit(sid, "Component 02 - Computational Thinking, Algorithms and Programming")
        s5 = unit(sid, "Section Five - Algorithms", comp2)
        for t in ["Computational Thinking", "Writing Algorithms - Pseudocode", "Writing Algorithms - Flowcharts",
                  "Search Algorithms", "Sorting Algorithms"]:
            topic(sid, t, s5)
        s6 = unit(sid, "Section Six - Programming", comp2)
        for t in ["Programming Basics - Data Types", "Programming Basics - Casting and Operators",
                  "Programming Basics - Operators", "Constants and Variables", "Strings", "Program Flow",
                  "Boolean Logic", "Random Number Generation", "Arrays", "File Handling", "Sub Programs"]:
            topic(sid, t, s6)
        s7 = unit(sid, "Section Seven - Design, Testing and IDEs", comp2)
        for t in ["Structured Programming", "Defensive Design", "Testing", "Trace Tables", "Translators",
                  "Integrated Development Environments"]:
            topic(sid, t, s7)

        sid = subject("Geography")
        u1 = unit(sid, "Unit 1 - Living with the physical environment")
        a = unit(sid, "Section A - The challenge of natural hazards", u1)
        for t, p in [("What are natural hazards?", "15"), ("Plate tectonics theory", "16"),
                     ("Distribution of earthquakes and volcanoes", "17"), ("Physical processes at plate margins", "18"),
                     ("The effects of earthquakes", "19"), ("Responses to earthquakes", "20"),
                     ("Living with the risks from tectonic hazards", "21"), ("Reducing the risks from tectonic hazards", "22"),
                     ("Skills Focus: Dispersion graphs", "23"), ("Global atmospheric circulation", "24"),
                     ("Where are tropical storms formed?", "25"), ("The formation and structure of tropical storms", "26"),
                     ("How might climate change affect tropical storms?", "27"), ("Example: Cyclone Idai - a tropical storm", "28"),
                     ("Reducing the effects of tropical storms", "29"), ("Weather hazards in the UK", "30"),
                     ("Extreme weather in the UK", "31"), ("The Somerset Levels floods, 2014", "32"),
                     ("Skills Focus: OS map (1:25 000) and photo skills", "33"), ("What is the evidence for climate change?", "34"),
                     ("Natural causes of climate change", "35"), ("Human causes of climate change", "36"),
                     ("Managing climate change - mitigation", "37"), ("Managing climate change - adaptation", "38"),
                     ("Skills Focus: Graphs and charts", "39")]:
            topic(sid, t, a, p)
        b = unit(sid, "Section B - The living world", u1)
        for t, p in [("Example: A small-scale UK ecosystem - freshwater pond", "41"), ("How does change affect ecosystems?", "42"),
                     ("Introducing global ecosystems", "43"), ("Physical characteristics of rainforests", "44"),
                     ("Adaptation and biodiversity in rainforests", "45"), ("Case Study: Causes of deforestation in Malaysia", "46"),
                     ("Case Study: Impacts of deforestation in Malaysia", "47"), ("The value of tropical rainforests", "48"),
                     ("Sustainable management of tropical rainforests", "49"), ("Skills Focus: Graphs", "50"),
                     ("Physical characteristics of hot deserts", "51"), ("Adapting to hot desert environments", "52"),
                     ("Case Study: Opportunities for development in hot deserts", "53"),
                     ("Case Study: Challenges of developing hot deserts", "54"), ("Causes of desertification in hot deserts", "55"),
                     ("Reducing the risk of desertification in hot deserts", "56"), ("Physical characteristics of cold environments", "57"),
                     ("Adapting to cold environments", "58"), ("Case Study: Opportunities for development in cold environments", "59"),
                     ("Case Study: Challenges of developing cold environments", "60"),
                     ("Value of cold environments as wilderness areas", "61"), ("Managing cold environments", "62")]:
            topic(sid, t, b, p)
        csec = unit(sid, "Section C - Physical landscapes in the UK", u1)
        for t, p in [("Skills Focus: The UK's diverse landscapes", "64"), ("Wave types and their characteristics", "65"),
                     ("Weathering and mass movement", "66"), ("Coastal processes", "67"), ("Coastal erosion landforms", "68"),
                     ("Coastal deposition landforms", "69"), ("Example: Coastal landforms at Swanage", "70"),
                     ("Skills Focus: Photos and OS maps", "71"), ("Managing coasts - hard engineering", "72"),
                     ("Managing coasts - soft engineering", "73"), ("Managing coasts - managed retreat", "74"),
                     ("Example: Coastal management at Lyme Regis", "75"), ("Changes in rivers and their valleys", "76"),
                     ("Fluvial (river) processes", "77"), ("River erosion landforms", "78"), ("River erosion and deposition landforms", "79"),
                     ("Example: River landforms on the River Tees", "80"), ("Skills Focus: Photos and OS maps (rivers)", "81"),
                     ("Physical and human factors affecting flood risk", "82"), ("Managing floods - hard engineering", "83"),
                     ("Managing floods - soft engineering", "84"), ("Example: Managing floods at Banbury", "85"),
                     ("The role of ice in shaping the UK's landscapes", "86"), ("Glacial erosion landforms", "87"),
                     ("Glacial transportation and deposition landforms", "88"), ("Skills Focus/Example: Photos and OS maps (glaciers)", "89"),
                     ("Economic opportunities in glaciated upland areas", "90"), ("Conflicts in glaciated upland areas", "91"),
                     ("Example: Managing tourism in the Lake District", "92")]:
            topic(sid, t, csec, p)
        u2 = unit(sid, "Unit 2 - Challenges in the human environment")
        seca = unit(sid, "Section A - Urban issues and challenges", u2)
        for t in ["An increasingly urban world", "Factors affecting the rate of urbanisation",
                  "Case Study: Introducing Rio de Janeiro", "Case Study: Social opportunities in Rio",
                  "Case Study: Economic opportunities in Rio", "Case Study: Managing the challenges of urban growth",
                  "Case Study: Managing water, sanitation and energy", "Case Study: Social challenges - access to health and education",
                  "Case Study: Challenges of social and environmental issues", "Case Study/Example: Planning for Rio's urban poor",
                  "Skills Focus/Case Study: Line chart and satellite image", "Where do people live in the UK?",
                  "Case Study: Introducing Bristol", "Case Study: How can urban change create opportunities? (1)",
                  "Case Study: How can urban change create opportunities? (2)",
                  "Case Study: How can urban change create opportunities? (3)",
                  "Case study: Urban change - challenges (1)", "Skills Focus/Case Study: Graphs and tables",
                  "Case study: Urban change (2)", "Case study: Urban change - challenges (3)",
                  "Case Study/Example: Urban regeneration", "Case study: Urban regeneration in Bristol (2)",
                  "Planning for urban sustainability", "Sustainable traffic management strategies"]:
            topic(sid, t, seca)
        secb = unit(sid, "Section B - The changing economic world", u2)
        for t in ["Our unequal world", "Measuring development", "The Demographic Transition Model",
                  "Changing population structures", "Causes of uneven development", "Uneven development - wealth and health",
                  "Uneven development - international migration", "Reducing the development gap",
                  "Reducing the gap - aid and intermediate technology", "Reducing the gap - fair trade",
                  "Reducing the gap - debt relief and loans", "Reducing the development gap - tourism",
                  "Skills Focus: Atlas maps and graphs", "Case Study: Exploring Nigeria (1)", "Case Study: Exploring Nigeria (2)",
                  "Case Study: Balancing a changing industrial structure", "Case Study: The impacts of transnational corporations",
                  "Case Study: Nigeria in the wider world", "Case Study: The impacts of international aid",
                  "Case Study: Managing environmental issues", "Case Study: Quality of life in Nigeria",
                  "Skills Focus/Case Study: Graphs and statistical skills", "Changes in the UK economy",
                  "A post-industrial economy", "Changing rural landscapes in the UK",
                  "The UK's changing transport infrastructure (1)", "The UK's changing transport infrastructure (2)",
                  "The north-south divide", "The UK in the wider world (1)", "The UK in the wider world (2)",
                  "Skills Focus: OS map skills and aerial photo interpretation"]:
            topic(sid, t, secb)
        secc = unit(sid, "Section C - The challenge of resource management", u2)
        for t in ["The global distribution of resources", "Provision of food in the UK (1)", "Provision of food in the UK (2)",
                  "Provision of water in the UK (1)", "Provision of water in the UK (2)", "Provision of energy in the UK (1)",
                  "Provision of energy in the UK (2)", "Skills Focus: OS map and decision-making exercise",
                  "Global food supply", "Factors affecting food supply", "Skills Focus: Bar graphs",
                  "Impacts of food insecurity", "Increasing food supply", "Example: The Indus Basin Irrigation System",
                  "Sustainable food production (1)", "Sustainable food production (2)", "Global water supply",
                  "Factors affecting water availability", "Impacts of water insecurity", "How can water supply be increased?",
                  "The Lesotho Highland Water Project", "Sustainable water supplies", "Example: The Wakal River Basin project",
                  "Global energy supply and demand", "Impacts of energy insecurity", "How can energy supply be increased?",
                  "Example: Gas - a non-renewable resource", "Sustainable energy use",
                  "The Chambamontera micro-hydro scheme"]:
            topic(sid, t, secc)
        u3 = unit(sid, "Unit 3 - Geographical Applications")
        for t in ["Preparing for Paper 3 - the Issue evaluation", "The Christchurch earthquakes",
                  "The Issue evaluation exam", "The Issue evaluation mark scheme", "Fieldwork and the six stages of enquiry",
                  "Strand 1 - Developing questions for your enquiry", "Strand 2 - Selecting, measuring and recording data",
                  "Strand 3 - Processing and presenting fieldwork data", "Strand 4 - Analysing fieldwork data",
                  "Strand 5 - Reaching conclusions", "Strand 6 - Evaluating your geographical enquiry",
                  "How to approach skills-based questions", "How to approach the 4-, 6- and 9-mark questions"]:
            topic(sid, t, u3)

        sid = subject("History")
        g = unit(sid, "Germany, 1890-1945: Democracy and dictatorship")
        for t, p in [("Kaiser Wilhelm and the difficulties of ruling Germany, 1890-1914", "30"),
                     ("The impact of the First World War on Germany", "32"),
                     ("The new Weimar government: initial problems and recovery under Stresemann", "34"),
                     ("The impact of the Depression on Germany", "36"),
                     ("The failure of Weimar democracy: Hitler becomes Chancellor, Jan 1933", "38"),
                     ("The establishment of Hitler's dictatorship, 1933-34", "40"),
                     ("Economic changes: employment and rearmament", "42"),
                     ("The impact of Nazi social policies", "44"), ("The Nazi dictatorship", "46")]:
            topic(sid, t, g, p)
        c = unit(sid, "Conflict and tension, 1894-1918")
        for t, p in [("The alliance system", "76"), ("Anglo-German rivalry", "78"), ("The outbreak of war", "80"),
                     ("Tactics and technology on the Western Front", "82"), ("Key battles on the Western Front", "84"),
                     ("The war on other fronts", "86"), ("Changes in 1917", "88"), ("The war in 1918", "90"),
                     ("German surrender", "92")]:
            topic(sid, t, c, p)
        h = unit(sid, "Health and the people: c1000 to the present day")
        for t, p in [("Key features of British medicine in the Middle Ages", "166"),
                     ("Main influences on British medicine in the Middle Ages", "166"),
                     ("Public health in the Middle Ages", "168"), ("Impact of the Renaissance on medicine in Britain", "170"),
                     ("Dealing with disease", "172"), ("Germ Theory and its impact", "174"),
                     ("A revolution in surgery", "174"), ("Improvements in public health", "176"),
                     ("Modern treatment of disease and surgical advancements", "178"), ("Modern public health", "180")]:
            topic(sid, t, h, p)
        e = unit(sid, "Elizabethan England, c1568-1603")
        for t, p in [("Elizabeth's character and Court life", "234"), ("Elizabeth and Parliament", "236"),
                     ("The Elizabethan 'Golden Age'", "238"), ("Poverty: attitudes and responses", "240"),
                     ("English sailors: Hawkins, Drake and Raleigh", "242"),
                     ("Religion: plots, threats and government responses", "244"),
                     ("Mary, Queen of Scots: threat, plots, execution and impact", "246"),
                     ("Conflict with Spain and the defeat of the Spanish Armada", "248")]:
            topic(sid, t, e, p)

        sid = subject("English Literature")
        topic(sid, "An Inspector Calls (CGP Text Guide)")
        topic(sid, "Macbeth (CGP Text Guide)")
        topic(sid, "A Christmas Carol (CGP Text Guide)")

    def get_subjects(self):
        return self.conn.execute(
            "SELECT * FROM subjects ORDER BY sort, id").fetchall()

    def get_units(self, subject_id, direct_topics_only=False):
        sql = "SELECT * FROM units WHERE subject_id=?"
        if direct_topics_only:
            sql += " AND EXISTS(SELECT 1 FROM topics t WHERE t.unit_id=units.id)"
        sql += " ORDER BY name, id"
        return self.conn.execute(sql, (subject_id,)).fetchall()

    def get_units_tree(self, subject_id):
        rows = self.conn.execute(
            "SELECT * FROM units WHERE subject_id=? ORDER BY sort, id", (subject_id,)).fetchall()
        by_parent = {}
        for r in rows:
            by_parent.setdefault(r["parent_id"], []).append(r)
        return by_parent

    def get_topics(self, subject_id, unit_id=None):
        if unit_id is None:
            return self.conn.execute(
                "SELECT * FROM topics WHERE subject_id=? ORDER BY sort, id", (subject_id,)).fetchall()
        return self.conn.execute(
            "SELECT * FROM topics WHERE subject_id=? AND unit_id=? ORDER BY sort, id",
            (subject_id, unit_id)).fetchall()

    def get_topic(self, topic_id):
        return self.conn.execute("SELECT * FROM topics WHERE id=?", (topic_id,)).fetchone()

    def get_subject(self, subject_id):
        return self.conn.execute("SELECT * FROM subjects WHERE id=?", (subject_id,)).fetchone()

    def get_unit(self, unit_id):
        return self.conn.execute("SELECT * FROM units WHERE id=?", (unit_id,)).fetchone()

    def topic_stats(self, topic_id):
        r = self.conn.execute(
            "SELECT COUNT(st.id) times, COALESCE(SUM(ss.minutes),0) minutes, MAX(ss.date) last_date "
            "FROM session_topics st JOIN study_sessions ss ON ss.id=st.session_id "
            "WHERE st.topic_id=?", (topic_id,)).fetchone()
        out = dict(r)
        conf = self.conn.execute(
            "SELECT st.confidence FROM session_topics st WHERE st.topic_id=? "
            "AND st.session_id=(SELECT ss2.id FROM session_topics st2 "
            "JOIN study_sessions ss2 ON ss2.id=st2.session_id WHERE st2.topic_id=? "
            "ORDER BY ss2.date DESC, ss2.created_at DESC, ss2.id DESC LIMIT 1) "
            "ORDER BY st.updated_at DESC LIMIT 1", (topic_id, topic_id)).fetchone()
        out["last_conf"] = conf["confidence"] if conf else None
        return out

    def all_topics_with_stats(self):
        rows = self.conn.execute(
            "SELECT t.id, t.name, t.page, t.subject_id, t.unit_id, s.name subject, u.name unit, "
            "COUNT(st.id) times, COALESCE(SUM(ss.minutes),0) minutes, MAX(ss.date) last_date "
            "FROM topics t "
            "JOIN subjects s ON s.id=t.subject_id "
            "LEFT JOIN units u ON u.id=t.unit_id "
            "LEFT JOIN session_topics st ON st.topic_id=t.id "
            "LEFT JOIN study_sessions ss ON ss.id=st.session_id "
            "GROUP BY t.id ORDER BY s.sort, s.name, t.sort, t.id").fetchall()
        confs = self.conn.execute(
            "SELECT st.topic_id, st.confidence FROM session_topics st WHERE st.session_id="
            "(SELECT ss2.id FROM session_topics st2 "
            "JOIN study_sessions ss2 ON ss2.id=st2.session_id WHERE st2.topic_id=st.topic_id "
            "ORDER BY ss2.date DESC, ss2.created_at DESC, ss2.id DESC LIMIT 1)").fetchall()
        cmap = {r["topic_id"]: r["confidence"] for r in confs}
        out = []
        for r in rows:
            d = dict(r)
            d["last_conf"] = cmap.get(r["id"])
            out.append(d)
        return out

    def add_study_session(self, date, minutes, notes, topics):
        now = now_iso()
        sid = new_id()
        self.conn.execute(
            "INSERT INTO study_sessions(id,date,minutes,notes,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (sid, date, minutes, notes, now, now))
        for topic_id, conf in topics:
            self.conn.execute(
                "INSERT INTO session_topics(id,session_id,topic_id,confidence,updated_at) VALUES(?,?,?,?,?)",
                (new_id(), sid, topic_id, conf, now))
        self.conn.commit()
        return sid

    def totals(self):
        return dict(self.conn.execute(
            "SELECT COUNT(DISTINCT ss.id) sessions, "
            "(SELECT COALESCE(SUM(minutes),0) FROM study_sessions) minutes, "
            "COUNT(DISTINCT st.topic_id) studied, "
            "(SELECT COUNT(*) FROM topics) topics "
            "FROM study_sessions ss LEFT JOIN session_topics st ON st.session_id=ss.id").fetchone())

    def recent_sessions(self, limit=12):
        return self.conn.execute(
            "SELECT ss.date, ss.minutes, st.confidence, t.name topic, s.name subject "
            "FROM session_topics st "
            "JOIN study_sessions ss ON ss.id=st.session_id "
            "JOIN topics t ON t.id=st.topic_id "
            "JOIN subjects s ON s.id=t.subject_id "
            "ORDER BY ss.date DESC, ss.created_at DESC, ss.id LIMIT ?", (limit,)).fetchall()

    def subject_summary(self):
        tops = self.conn.execute(
            "SELECT subject_id, COUNT(*) topics FROM topics GROUP BY subject_id").fetchall()
        tmap = {r["subject_id"]: r["topics"] for r in tops}
        subs = self.conn.execute(
            "SELECT id, name FROM subjects ORDER BY sort, id").fetchall()
        links = self.conn.execute(
            "SELECT t.subject_id, st.topic_id, st.session_id FROM session_topics st "
            "JOIN topics t ON t.id=st.topic_id").fetchall()
        sess = self.conn.execute("SELECT id, minutes FROM study_sessions").fetchall()
        smin = {r["id"]: r["minutes"] for r in sess}
        out = []
        for s in subs:
            rel = [l for l in links if l["subject_id"] == s["id"]]
            studied = len({l["topic_id"] for l in rel})
            sids = {l["session_id"] for l in rel}
            mins = sum(smin.get(sid, 0) for sid in sids)
            out.append({"id": s["id"], "name": s["name"], "topics": tmap.get(s["id"], 0),
                        "studied": studied, "sessions": len(sids), "minutes": mins})
        return out

    def all_session_dates(self):
        return [r["date"] for r in self.conn.execute("SELECT DISTINCT date FROM study_sessions")]

    def last_conf_weak(self):
        return self.conn.execute(
            "SELECT t.name topic, s.name subject, st.confidence, ss.date "
            "FROM session_topics st "
            "JOIN study_sessions ss ON ss.id=st.session_id "
            "JOIN topics t ON t.id=st.topic_id "
            "JOIN subjects s ON s.id=t.subject_id "
            "WHERE st.session_id=(SELECT ss2.id FROM session_topics st2 "
            "JOIN study_sessions ss2 ON ss2.id=st2.session_id WHERE st2.topic_id=st.topic_id "
            "ORDER BY ss2.date DESC, ss2.created_at DESC, ss2.id DESC LIMIT 1) "
            "AND st.confidence IN ('R','A') ORDER BY ss.date").fetchall()

    def add_subject(self, name):
        u = new_id()
        now = now_iso()
        self.conn.execute(
            "INSERT INTO subjects(id,name,sort,updated_at) VALUES(?,?,(SELECT COALESCE(MAX(sort),0)+1 FROM subjects),?)",
            (u, name, now))
        self.conn.commit()
        return u

    def rename_subject(self, sid, name):
        self.conn.execute(
            "UPDATE subjects SET name=?, updated_at=? WHERE id=?", (name, now_iso(), sid))
        self.conn.commit()

    def delete_subject(self, sid):
        self.conn.execute("DELETE FROM subjects WHERE id=?", (sid,))
        self.conn.commit()

    def add_unit(self, subject_id, name, parent_id=None):
        u = new_id()
        now = now_iso()
        self.conn.execute(
            "INSERT INTO units(id,subject_id,parent_id,name,sort,updated_at) "
            "VALUES(?,?,?,?,(SELECT COALESCE(MAX(sort),0)+1 FROM units),?)",
            (u, subject_id, parent_id, name, now))
        self.conn.commit()
        return u

    def rename_unit(self, uid, name):
        self.conn.execute(
            "UPDATE units SET name=?, updated_at=? WHERE id=?", (name, now_iso(), uid))
        self.conn.commit()

    def delete_unit(self, uid):
        self.conn.execute("DELETE FROM units WHERE id=?", (uid,))
        self.conn.commit()

    def add_topic(self, subject_id, name, unit_id=None, page=""):
        u = new_id()
        now = now_iso()
        self.conn.execute(
            "INSERT INTO topics(id,subject_id,unit_id,name,page,sort,updated_at) "
            "VALUES(?,?,?,?,?,(SELECT COALESCE(MAX(sort),0)+1 FROM topics),?)",
            (u, subject_id, unit_id, name, page or "", now))
        self.conn.commit()
        return u

    def update_topic(self, tid, name=None, page=None):
        sets, vals = [], []
        if name is not None:
            sets.append("name=?"); vals.append(name)
        if page is not None:
            sets.append("page=?"); vals.append(page)
        if sets:
            vals.append(now_iso()); vals.append(tid)
            self.conn.execute(
                "UPDATE topics SET " + ", ".join(sets) + ", updated_at=? WHERE id=?", vals)
            self.conn.commit()

    def delete_topic(self, tid):
        self.conn.execute("DELETE FROM topics WHERE id=?", (tid,))
        self.conn.commit()

    def backup(self, dest):
        self.conn.commit()
        shutil.copyfile(self.path, dest)

    @staticmethod
    def interval(times):
        xs = [2, 4, 7, 12, 20, 30, 45]
        return xs[min(times, len(xs) - 1)]

    @staticmethod
    def days_since(date_str):
        if not date_str:
            return None
        return (datetime.date.today() - datetime.datetime.strptime(date_str, DATE_FMT).date()).days

    def export_all(self):
        out = {}
        for t in SYNC_TABLES:
            out[t] = [dict(r) for r in self.conn.execute(f"SELECT * FROM {t} ORDER BY id").fetchall()]
        out["_app"] = APP_NAME
        out["_version"] = VERSION
        return out

    def import_all(self, data):
        counts = {t: 0 for t in SYNC_TABLES}
        if not isinstance(data, dict):
            return counts
        with self.lock:
            self.conn.execute("BEGIN")
            self.conn.execute("PRAGMA defer_foreign_keys=ON")
            try:
                for t in ("subjects", "units", "topics"):
                    counts[t] += self._merge_table(t, data.get(t) or [])
                counts["study_sessions"] += self._merge_table("study_sessions", data.get("study_sessions") or [])
                counts["session_topics"] += self._merge_table("session_topics", data.get("session_topics") or [])
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return counts

    def _merge_table(self, table, rows):
        applied = 0
        cols = [r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()]
        for row in rows:
            if "id" not in row:
                continue
            rid = row["id"]
            local = self.conn.execute(
                f"SELECT updated_at FROM {table} WHERE id=?", (rid,)).fetchone()
            if local is None:
                keys = [c for c in cols if c in row]
                vals = [row[c] for c in keys]
                self.conn.execute(
                    f"INSERT INTO {table} ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})", vals)
                applied += 1
            elif (row.get("updated_at") or "") > (local["updated_at"] or ""):
                sets = ", ".join(f"{c}=?" for c in cols if c in row and c != "id")
                vals = [row[c] for c in cols if c in row and c != "id"]
                vals.append(rid)
                self.conn.execute(f"UPDATE {table} SET {sets} WHERE id=?", vals)
                applied += 1
        return applied

    def snapshots(self, since):
        out = {}
        for t in SYNC_TABLES:
            rows = self.conn.execute(
                f"SELECT * FROM {t} WHERE updated_at > ? ORDER BY updated_at", (since,)).fetchall()
            out[t] = [dict(r) for r in rows]
        return out


class SyncClient:
    """Shared HTTP client used by both the desktop app and the Android app."""

    def __init__(self, url):
        if not url.startswith("http"):
            url = "http://" + url
        self.url = url.rstrip("/")

    def _post(self, payload, timeout=20):
        body = json.dumps(payload).encode()
        req = _UrllibReq(
            self.url + "/sync", data=body,
            headers={"Content-Type": "application/json"})
        with _Urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())

    def pull(self, db, since="1970-01-01T00:00:00"):
        data = self._post({"since": since})
        return db.import_all(data.get("tables") or {})

    def push(self, db, since="1970-01-01T00:00:00"):
        mine = db.export_all()
        self._post({"since": since, "tables": mine})
        return len(mine.get(SYNC_TABLES[0], []))

    def sync(self, db, since="1970-01-01T00:00:00"):
        mine = db.export_all()
        data = self._post({"since": since, "tables": mine})
        db.import_all(data.get("tables") or {})
        return db.export_all()


def sync_now(peer_url, db, since="1970-01-01T00:00:00"):
    c = SyncClient(peer_url)
    return c.sync(db, since)