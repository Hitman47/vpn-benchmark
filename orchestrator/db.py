"""Stockage des mesures : SQLite en format long (une ligne = une metrique).

Le format long evite toute migration de schema quand on ajoute une mesure,
et se re-agrege trivialement pour le rapport.
"""
import csv
import json
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    started_at  REAL,
    finished_at REAL,
    mode        TEXT,
    config_json TEXT
);
CREATE TABLE IF NOT EXISTS cases (
    case_id    TEXT PRIMARY KEY,
    run_id     TEXT,
    round      INTEGER,
    provider   TEXT,          -- nordvpn | protonvpn | baseline
    variant    TEXT,          -- base | nopf (bras A/B du port forwarding)
    country    TEXT,
    server     TEXT,
    started_at REAL,
    ok         INTEGER,
    exit_ip    TEXT,
    exit_asn   INTEGER,
    exit_org   TEXT,
    exit_country TEXT,
    note       TEXT
);
CREATE TABLE IF NOT EXISTS metrics (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id  TEXT,
    ts       REAL,
    metric   TEXT,
    value    REAL,
    unit     TEXT,
    extra    TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL,
    run_id  TEXT,
    case_id TEXT,
    level   TEXT,
    message TEXT
);
CREATE INDEX IF NOT EXISTS idx_metrics_case ON metrics(case_id, metric);
CREATE INDEX IF NOT EXISTS idx_cases_run ON cases(run_id, provider);
"""


class DB:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self):
        """Ajoute les colonnes apparues apres coup, sans casser une base
        existante (le volume results est conserve entre deux campagnes)."""
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(cases)")}
        for col, decl in (("variant", "TEXT"),):
            if col not in have:
                self.conn.execute("ALTER TABLE cases ADD COLUMN %s %s"
                                  % (col, decl))

    # --- ecriture -------------------------------------------------------
    def start_run(self, run_id, mode, config):
        self.conn.execute(
            "INSERT OR REPLACE INTO runs(run_id, started_at, mode, config_json)"
            " VALUES (?,?,?,?)",
            (run_id, time.time(), mode, json.dumps(config)[:200000]))
        self.conn.commit()

    def finish_run(self, run_id):
        self.conn.execute("UPDATE runs SET finished_at=? WHERE run_id=?",
                          (time.time(), run_id))
        self.conn.commit()

    def add_case(self, case):
        cols = ("case_id", "run_id", "round", "provider", "variant", "country",
                "server", "started_at", "ok", "exit_ip", "exit_asn", "exit_org",
                "exit_country", "note")
        case = dict(case)
        case.setdefault("variant", "base")
        self.conn.execute(
            "INSERT OR REPLACE INTO cases(%s) VALUES (%s)"
            % (",".join(cols), ",".join("?" * len(cols))),
            tuple(case.get(c) for c in cols))
        self.conn.commit()

    def add_metric(self, case_id, metric, value, unit=None, extra=None):
        if value is None:
            return
        self.conn.execute(
            "INSERT INTO metrics(case_id, ts, metric, value, unit, extra)"
            " VALUES (?,?,?,?,?,?)",
            (case_id, time.time(), metric, float(value), unit,
             json.dumps(extra)[:100000] if extra is not None else None))
        self.conn.commit()

    def log(self, run_id, case_id, level, message):
        self.conn.execute(
            "INSERT INTO events(ts, run_id, case_id, level, message)"
            " VALUES (?,?,?,?,?)",
            (time.time(), run_id, case_id, level, str(message)[:4000]))
        self.conn.commit()

    # --- lecture --------------------------------------------------------
    def metric_values(self, provider, metric, run_id=None, countries=None,
                      variant="base"):
        """variant=None pour ne pas filtrer, sinon 'base' (bras principal) ou
        'nopf' (meme provider, port forwarding coupe)."""
        q = ("SELECT m.value FROM metrics m JOIN cases c ON c.case_id=m.case_id"
             " WHERE c.provider=? AND m.metric=?")
        p = [provider, metric]
        if variant is not None:
            q += " AND COALESCE(c.variant,'base')=?"
            p.append(variant)
        if run_id:
            q += " AND c.run_id=?"
            p.append(run_id)
        if countries is not None:
            if not countries:
                return []
            q += " AND c.country IN (%s)" % ",".join("?" * len(countries))
            p.extend(countries)
        return [r[0] for r in self.conn.execute(q, p)]

    def metric_series(self, provider, metric, run_id=None):
        q = ("SELECT m.ts, m.value, c.country, c.server, c.round"
             " FROM metrics m JOIN cases c ON c.case_id=m.case_id"
             " WHERE c.provider=? AND m.metric=?")
        p = [provider, metric]
        if run_id:
            q += " AND c.run_id=?"
            p.append(run_id)
        q += " ORDER BY m.ts"
        return [dict(r) for r in self.conn.execute(q, p)]

    def providers_seen(self, run_id=None):
        q = "SELECT DISTINCT provider FROM cases WHERE COALESCE(variant,'base')='base'"
        p = []
        if run_id:
            q += " AND run_id=?"
            p.append(run_id)
        return [r[0] for r in self.conn.execute(q, p)]

    def cases(self, run_id=None, variant="base"):
        q = "SELECT * FROM cases WHERE 1=1"
        p = []
        if variant is not None:
            q += " AND COALESCE(variant,'base')=?"
            p.append(variant)
        if run_id:
            q += " AND run_id=?"
            p.append(run_id)
        q += " ORDER BY started_at"
        return [dict(r) for r in self.conn.execute(q, p)]

    def events(self, run_id=None, level=None):
        q = "SELECT * FROM events WHERE 1=1"
        p = []
        if run_id:
            q += " AND run_id=?"
            p.append(run_id)
        if level:
            q += " AND level=?"
            p.append(level)
        q += " ORDER BY ts"
        return [dict(r) for r in self.conn.execute(q, p)]

    def last_run_id(self):
        r = self.conn.execute(
            "SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
        return r[0] if r else None

    # --- export ---------------------------------------------------------
    def export_csv(self, directory):
        os.makedirs(directory, exist_ok=True)
        paths = []
        rows = self.conn.execute(
            "SELECT c.run_id, c.round, c.provider,"
            " COALESCE(c.variant,'base') AS variant, c.country, c.server,"
            " c.exit_ip, c.exit_org, c.exit_country, c.ok,"
            " m.ts, m.metric, m.value, m.unit"
            " FROM metrics m JOIN cases c ON c.case_id=m.case_id"
            " ORDER BY m.ts").fetchall()
        p = os.path.join(directory, "measurements.csv")
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["run_id", "round", "provider", "variant", "country", "server",
                        "exit_ip", "exit_org", "exit_country", "case_ok",
                        "ts", "metric", "value", "unit"])
            for r in rows:
                w.writerow(list(r))
        paths.append(p)

        p = os.path.join(directory, "cases.csv")
        cs = self.cases(variant=None)
        with open(p, "w", newline="", encoding="utf-8") as f:
            if cs:
                w = csv.DictWriter(f, fieldnames=list(cs[0].keys()))
                w.writeheader()
                w.writerows(cs)
        paths.append(p)
        return paths
