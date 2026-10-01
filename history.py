"""
Small history store (SQLite, standard library only) — remembers what happened between checks.

Holds: each item's last status (+ since when), a timeline of events, per-check numbers
(RAM / CPU / restart count per item, memory + disk for the host), and hourly "did the site
answer" counts for uptime %. Everything is pruned automatically so it can't fill the disk.
"""
import os
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS levels(key TEXT PRIMARY KEY, level TEXT, since REAL, known INTEGER, name TEXT, env TEXT);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, key TEXT, name TEXT,
                                  env TEXT, frm TEXT, "to" TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS samples(ts INTEGER, key TEXT, mem REAL, cpu REAL, restarts INTEGER);
CREATE INDEX IF NOT EXISTS samples_k ON samples(key, ts);
CREATE INDEX IF NOT EXISTS samples_ts ON samples(ts);
CREATE TABLE IF NOT EXISTS host_samples(ts INTEGER PRIMARY KEY, mem_used REAL, mem_total REAL, swap_used REAL,
                                        disk_pct INTEGER, disk_free_gb REAL, load1 REAL);
CREATE TABLE IF NOT EXISTS probe_hours(hour INTEGER, host TEXT, n INTEGER, fails INTEGER, PRIMARY KEY(hour, host));
CREATE TABLE IF NOT EXISTS flags(flag TEXT PRIMARY KEY, on_ INTEGER, since REAL, count INTEGER);
"""


class History:
    def __init__(self, path, retention_days=14, max_mb=100):
        self.path, self.retention, self.max_mb = Path(path), retention_days, max_mb
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        new = not self.path.exists()
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        if new:
            self.db.execute("PRAGMA auto_vacuum=INCREMENTAL")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    # ── small key/value + flags ──
    def meta(self, k, default=None):
        with self.lock:
            r = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def set_meta(self, k, v):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, str(v)))

    def flag(self, name):
        with self.lock:
            r = self.db.execute("SELECT on_, since, count FROM flags WHERE flag=?", (name,)).fetchone()
        return dict(on=bool(r[0]), since=r[1], count=r[2]) if r else dict(on=False, since=None, count=0)

    def set_flag(self, name, on, count, ts):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO flags VALUES(?,?,?,?)", (name, int(on), ts, count))

    # ── status per item ──
    def levels(self):
        with self.lock:
            return {r[0]: dict(level=r[1], since=r[2], known=bool(r[3]), name=r[4], env=r[5])
                    for r in self.db.execute("SELECT key, level, since, known, name, env FROM levels")}

    def save_levels(self, d):
        with self.lock:
            self.db.execute("BEGIN")
            self.db.execute("DELETE FROM levels")
            self.db.executemany("INSERT INTO levels VALUES(?,?,?,?,?,?)",
                                [(k, v["level"], v["since"], int(v["known"]), v["name"], v["env"]) for k, v in d.items()])
            self.db.execute("COMMIT")

    # ── timeline ──
    def add_event(self, ts, kind, key, name, env, frm=None, to=None, detail=""):
        with self.lock:
            self.db.execute('INSERT INTO events(ts,kind,key,name,env,frm,"to",detail) VALUES(?,?,?,?,?,?,?,?)',
                            (ts, kind, key, name, env, frm, to, detail))

    def recent_events(self, n=12):
        with self.lock:
            rows = self.db.execute('SELECT ts,kind,key,name,env,frm,"to",detail FROM events ORDER BY ts DESC, id DESC LIMIT ?', (n,)).fetchall()
        return [dict(ts=r[0], kind=r[1], key=r[2], name=r[3], env=r[4], frm=r[5], to=r[6], detail=r[7]) for r in rows]

    # ── numbers over time ──
    def last_restarts(self):
        with self.lock:
            return {r[0]: r[1] for r in self.db.execute(
                "SELECT s.key, s.restarts FROM samples s JOIN (SELECT key, MAX(ts) m FROM samples GROUP BY key) x "
                "ON s.key=x.key AND s.ts=x.m")}

    def add_samples(self, ts, items, host):
        with self.lock:
            self.db.execute("BEGIN")
            self.db.executemany("INSERT INTO samples VALUES(?,?,?,?,?)", [(int(ts), k, m, c, r) for k, m, c, r in items])
            self.db.execute("INSERT OR REPLACE INTO host_samples VALUES(?,?,?,?,?,?,?)",
                            (int(ts), host["mem_used"], host["mem_total"], host["swap_used"],
                             host["disk_pct"], host["disk_free_gb"], host["load1"]))
            self.db.execute("COMMIT")

    def series(self, hours=24, points=48):
        """key -> list of RAM values (MB), thinned to at most `points` per item."""
        since = time.time() - hours * 3600
        out = {}
        with self.lock:
            for k, m in self.db.execute("SELECT key, mem FROM samples WHERE ts>? AND mem IS NOT NULL ORDER BY key, ts", (since,)):
                out.setdefault(k, []).append(m)
        for k, v in out.items():
            if len(v) > points:
                step = len(v) / points
                out[k] = [v[int(i * step)] for i in range(points)]
        return out

    def host_series(self, hours=72):
        since = time.time() - hours * 3600
        with self.lock:
            return self.db.execute("SELECT ts, disk_free_gb, disk_pct, mem_used FROM host_samples WHERE ts>? ORDER BY ts", (since,)).fetchall()

    # ── uptime ──
    def probe(self, host, ts, ok):
        hour = int(ts // 3600)
        with self.lock:
            self.db.execute("INSERT INTO probe_hours VALUES(?,?,1,?) ON CONFLICT(hour,host) DO UPDATE SET n=n+1, fails=fails+?",
                            (hour, host, 0 if ok else 1, 0 if ok else 1))

    def uptime(self, host, days=7):
        """(percent up, hours of data) over the last `days`, or None when nothing recorded yet."""
        since = int((time.time() - days * 86400) // 3600)
        with self.lock:
            r = self.db.execute("SELECT SUM(n), SUM(fails), COUNT(*) FROM probe_hours WHERE host=? AND hour>=?", (host, since)).fetchone()
        if not r or not r[0]:
            return None
        return (100.0 * (r[0] - r[1]) / r[0], r[2])

    # ── keep it small ──
    def size_mb(self):
        return sum(os.path.getsize(f) for f in (str(self.path), str(self.path) + "-wal") if os.path.exists(f)) / 1048576

    def prune(self):
        now = time.time()
        days = self.retention
        with self.lock:
            while True:
                self.db.execute("DELETE FROM samples WHERE ts<?", (now - days * 86400,))
                self.db.execute("DELETE FROM host_samples WHERE ts<?", (now - days * 86400,))
                self.db.execute("DELETE FROM probe_hours WHERE hour<?", (int((now - 60 * 86400) // 3600),))
                self.db.execute("DELETE FROM events WHERE ts<?", (now - 90 * 86400,))
                self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self.db.execute("PRAGMA incremental_vacuum")
                if self.size_mb() <= self.max_mb or days <= 1:
                    break
                days = max(1, days // 2)       # over the size cap: keep less detail until it fits
