"""Hive's local memory: one SQLite file under the plugin's data folder.

antibodies  one row per (error signature, fix). ``ok``/``fail`` count what happened on THIS machine.
votes       what other installs said about a (signature, fix): one row per remote key, newest event wins.
outbox      fixes waiting to be shared (only when sharing is on), with the event id once published.
hints       which hint was shown for which task, so the next command can confirm or refute it.
meta        small settings and cursors.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS antibodies (
    sig TEXT NOT NULL, fix_id TEXT NOT NULL, program TEXT, error TEXT, fix TEXT NOT NULL,
    ok INTEGER DEFAULT 0, fail INTEGER DEFAULT 0, source TEXT DEFAULT 'live', os TEXT,
    first_seen REAL, last_seen REAL, shareable INTEGER DEFAULT 0, share_note TEXT,
    PRIMARY KEY (sig, fix_id));
CREATE TABLE IF NOT EXISTS votes (
    sig TEXT NOT NULL, fix_id TEXT NOT NULL, pubkey TEXT NOT NULL, ok INTEGER, fail INTEGER,
    program TEXT, error TEXT, fix TEXT, os TEXT, created_at INTEGER, event_id TEXT,
    PRIMARY KEY (sig, fix_id, pubkey));
CREATE TABLE IF NOT EXISTS outbox (
    sig TEXT NOT NULL, fix_id TEXT NOT NULL, queued_at REAL, published_at REAL, event_id TEXT, relays TEXT,
    PRIMARY KEY (sig, fix_id));
CREATE TABLE IF NOT EXISTS hints (
    task TEXT NOT NULL, sig TEXT NOT NULL, fix_id TEXT NOT NULL, shown_at REAL, origin TEXT,
    PRIMARY KEY (task, sig, fix_id));
CREATE TABLE IF NOT EXISTS lookups (sig TEXT PRIMARY KEY, at REAL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS votes_sig ON votes (sig);
"""


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ── helpers ──
    def _x(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.db.execute(sql, tuple(args))
            self.db.commit()
            return cur

    def rows(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.db.execute(sql, tuple(args)).fetchall()

    def get_meta(self, key: str, default: Any = None) -> Any:
        r = self.rows("SELECT value FROM meta WHERE key=?", (key,))
        return json.loads(r[0]["value"]) if r else default

    def set_meta(self, key: str, value: Any) -> None:
        self._x("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)))

    # ── antibodies ──
    def learn(self, sig: str, fix_id: str, program: str, error: str, fix: list[str], *, source: str, os_name: str,
              shareable: bool, share_note: str, ok: int = 1, at: Optional[float] = None) -> bool:
        """Record a fix that worked here. Returns True when it is new."""
        at = at or time.time()
        new = not self.rows("SELECT 1 FROM antibodies WHERE sig=? AND fix_id=?", (sig, fix_id))
        self._x("""INSERT INTO antibodies(sig,fix_id,program,error,fix,ok,fail,source,os,first_seen,last_seen,
                   shareable,share_note) VALUES(?,?,?,?,?,?,0,?,?,?,?,?,?)
                   ON CONFLICT(sig,fix_id) DO UPDATE SET ok=ok+excluded.ok, last_seen=excluded.last_seen""",
                (sig, fix_id, program, error, json.dumps(fix), ok, source, os_name, at, at, int(shareable),
                 share_note))
        return new

    def outcome(self, sig: str, fix_id: str, worked: bool) -> None:
        col = "ok" if worked else "fail"
        self._x(f"UPDATE antibodies SET {col}={col}+1, last_seen=? WHERE sig=? AND fix_id=?", (time.time(), sig, fix_id))

    def local_fixes(self, sig: str) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM antibodies WHERE sig=? ORDER BY (ok-fail) DESC, last_seen DESC", (sig,))

    def antibody(self, sig: str, fix_id: str) -> Optional[sqlite3.Row]:
        r = self.rows("SELECT * FROM antibodies WHERE sig=? AND fix_id=?", (sig, fix_id))
        return r[0] if r else None

    def all_antibodies(self) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM antibodies ORDER BY last_seen DESC")

    def forget(self, sig: Optional[str] = None) -> int:
        if sig:
            n = self._x("DELETE FROM antibodies WHERE sig LIKE ?", (sig + "%",)).rowcount
            self._x("DELETE FROM outbox WHERE sig LIKE ?", (sig + "%",))
            return n
        n = self._x("DELETE FROM antibodies").rowcount
        self._x("DELETE FROM outbox")
        return n

    # ── network votes ──
    def add_vote(self, sig: str, fix_id: str, pubkey: str, ok: int, fail: int, program: str, error: str,
                 fix: list[str], os_name: str, created_at: int, event_id: str) -> bool:
        cur = self.rows("SELECT created_at FROM votes WHERE sig=? AND fix_id=? AND pubkey=?", (sig, fix_id, pubkey))
        if cur and cur[0]["created_at"] >= created_at:
            return False
        self._x("""INSERT INTO votes(sig,fix_id,pubkey,ok,fail,program,error,fix,os,created_at,event_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(sig,fix_id,pubkey) DO UPDATE SET ok=excluded.ok,
                   fail=excluded.fail, created_at=excluded.created_at, event_id=excluded.event_id""",
                (sig, fix_id, pubkey, ok, fail, program, error, json.dumps(fix), os_name, created_at, event_id))
        return True

    def votes(self, sig: str) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM votes WHERE sig=?", (sig,))

    def vote_count(self) -> tuple[int, int]:
        r = self.rows("SELECT COUNT(DISTINCT pubkey) AS k, COUNT(DISTINCT sig) AS s FROM votes")[0]
        return r["k"], r["s"]

    # ── outbox ──
    def queue(self, sig: str, fix_id: str) -> None:
        self._x("""INSERT INTO outbox(sig,fix_id,queued_at) VALUES(?,?,?)
                   ON CONFLICT(sig,fix_id) DO UPDATE SET queued_at=excluded.queued_at, published_at=NULL""",
                (sig, fix_id, time.time()))

    def pending(self) -> list[sqlite3.Row]:
        return self.rows("""SELECT a.* , o.queued_at FROM outbox o JOIN antibodies a USING (sig, fix_id)
                            WHERE o.published_at IS NULL ORDER BY o.queued_at""")

    def published(self, sig: str, fix_id: str, event_id: str, relays: list[str]) -> None:
        self._x("UPDATE outbox SET published_at=?, event_id=?, relays=? WHERE sig=? AND fix_id=?",
                (time.time(), event_id, json.dumps(relays), sig, fix_id))

    def outbox_rows(self) -> list[sqlite3.Row]:
        return self.rows("SELECT a.*, o.queued_at, o.published_at, o.event_id, o.relays FROM outbox o "
                         "JOIN antibodies a USING (sig, fix_id) ORDER BY o.queued_at DESC")

    # ── hints and lookups ──
    def hint_shown(self, task: str, sig: str, fix_id: str, origin: str) -> None:
        self._x("INSERT OR REPLACE INTO hints(task,sig,fix_id,shown_at,origin) VALUES(?,?,?,?,?)",
                (task, sig, fix_id, time.time(), origin))

    def open_hints(self, task: str, within: float = 3600) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM hints WHERE task=? AND shown_at>?", (task, time.time() - within))

    def close_hint(self, task: str, sig: str, fix_id: str) -> None:
        self._x("DELETE FROM hints WHERE task=? AND sig=? AND fix_id=?", (task, sig, fix_id))

    def looked_up(self, sig: str, within: float) -> bool:
        r = self.rows("SELECT at FROM lookups WHERE sig=?", (sig,))
        return bool(r and r[0]["at"] > time.time() - within)

    def mark_lookup(self, sig: str) -> None:
        self._x("INSERT OR REPLACE INTO lookups(sig,at) VALUES(?,?)", (sig, time.time()))
