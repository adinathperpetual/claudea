"""SQLite storage for the Client Master.

Tables
------
clients               every client fetched from Finesse, keyed by trading code.
                      PAN is encrypted at rest. Clients that disappear from Finesse
                      are flagged (active = 0, missing_since) — never deleted.
exceptional_passwords contract-note passwords that do NOT follow the default rule
                      (PAN in uppercase), keyed by trading code, encrypted at rest.
sync_log              one row per sync run.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    trading_code   TEXT PRIMARY KEY,
    client_name    TEXT NOT NULL,
    pan_enc        TEXT,
    pan_valid      INTEGER NOT NULL DEFAULT 0,
    active         INTEGER NOT NULL DEFAULT 1,
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    missing_since  TEXT,
    updated_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exceptional_passwords (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trading_code  TEXT NOT NULL,
    client_name   TEXT,
    password_enc  TEXT NOT NULL,
    note          TEXT,
    created_by    TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_exc_code ON exceptional_passwords(trading_code);
CREATE TABLE IF NOT EXISTS sync_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at       TEXT NOT NULL,
    ended_at         TEXT,
    trigger          TEXT NOT NULL,
    status           TEXT NOT NULL,
    method           TEXT,
    fetched          INTEGER DEFAULT 0,
    added            INTEGER DEFAULT 0,
    updated          INTEGER DEFAULT 0,
    unchanged        INTEGER DEFAULT 0,
    flagged_missing  INTEGER DEFAULT 0,
    reactivated      INTEGER DEFAULT 0,
    invalid_pan      INTEGER DEFAULT 0,
    error_count      INTEGER DEFAULT 0,
    errors           TEXT,
    message          TEXT
);
"""

_lock = threading.RLock()


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def db_path() -> Path:
    return get_settings().db_path


def init_db() -> None:
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with connect() as c:
        c.executescript(SCHEMA)


@contextmanager
def connect():
    with _lock:
        conn = sqlite3.connect(db_path(), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# ---------------------------------------------------------------- clients
def all_clients(conn) -> dict[str, sqlite3.Row]:
    return {r["trading_code"]: r for r in conn.execute("SELECT * FROM clients")}


def get_client(conn, code: str):
    return conn.execute("SELECT * FROM clients WHERE trading_code = ?", (code,)).fetchone()


def insert_client(conn, code, name, pan_enc, pan_valid, ts):
    conn.execute(
        "INSERT INTO clients (trading_code, client_name, pan_enc, pan_valid, active, first_seen_at, last_seen_at, updated_at)"
        " VALUES (?,?,?,?,1,?,?,?)", (code, name, pan_enc, int(pan_valid), ts, ts, ts))


def update_client(conn, code, name, pan_enc, pan_valid, ts):
    conn.execute(
        "UPDATE clients SET client_name=?, pan_enc=?, pan_valid=?, active=1, missing_since=NULL,"
        " last_seen_at=?, updated_at=? WHERE trading_code=?", (name, pan_enc, int(pan_valid), ts, ts, code))


def touch_client(conn, code, ts, reactivate: bool):
    if reactivate:
        conn.execute("UPDATE clients SET active=1, missing_since=NULL, last_seen_at=?, updated_at=? WHERE trading_code=?",
                     (ts, ts, code))
    else:
        conn.execute("UPDATE clients SET last_seen_at=? WHERE trading_code=?", (ts, code))


def flag_missing(conn, codes: list[str], ts) -> None:
    conn.executemany("UPDATE clients SET active=0, missing_since=?, updated_at=? WHERE trading_code=?",
                     [(ts, ts, c) for c in codes])


# ---------------------------------------------------------------- exceptional passwords
def exceptional_rows(conn, code: str | None = None):
    if code:
        return conn.execute("SELECT * FROM exceptional_passwords WHERE trading_code=? ORDER BY id", (code,)).fetchall()
    return conn.execute("SELECT * FROM exceptional_passwords ORDER BY trading_code, id").fetchall()


def add_exceptional(conn, code, name, password_enc, note, user) -> int:
    ts = now_iso()
    cur = conn.execute(
        "INSERT INTO exceptional_passwords (trading_code, client_name, password_enc, note, created_by, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?)", (code, name, password_enc, note, user, ts, ts))
    return int(cur.lastrowid)


def update_exceptional(conn, row_id: int, password_enc: str | None, note: str | None) -> bool:
    sets, args = ["updated_at=?"], [now_iso()]
    if password_enc is not None:
        sets.append("password_enc=?")
        args.append(password_enc)
    if note is not None:
        sets.append("note=?")
        args.append(note)
    cur = conn.execute(f"UPDATE exceptional_passwords SET {', '.join(sets)} WHERE id=?", (*args, row_id))
    return cur.rowcount > 0


def delete_exceptional(conn, row_id: int) -> bool:
    return conn.execute("DELETE FROM exceptional_passwords WHERE id=?", (row_id,)).rowcount > 0


# ---------------------------------------------------------------- sync log
def start_log(conn, trigger: str) -> int:
    cur = conn.execute("INSERT INTO sync_log (started_at, trigger, status) VALUES (?,?,?)",
                       (now_iso(), trigger, "running"))
    return int(cur.lastrowid)


def finish_log(conn, log_id: int, *, status: str, method: str | None, stats: dict, errors: list[str], message: str) -> None:
    conn.execute(
        "UPDATE sync_log SET ended_at=?, status=?, method=?, fetched=?, added=?, updated=?, unchanged=?,"
        " flagged_missing=?, reactivated=?, invalid_pan=?, error_count=?, errors=?, message=? WHERE id=?",
        (now_iso(), status, method, stats.get("fetched", 0), stats.get("added", 0), stats.get("updated", 0),
         stats.get("unchanged", 0), stats.get("flagged_missing", 0), stats.get("reactivated", 0),
         stats.get("invalid_pan", 0), len(errors), json.dumps(errors[:500]), message, log_id))


def recent_logs(conn, limit: int = 50) -> list[dict]:
    rows = conn.execute("SELECT * FROM sync_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["errors"] = json.loads(d["errors"] or "[]")
        out.append(d)
    return out


def last_successful_sync(conn):
    return conn.execute("SELECT * FROM sync_log WHERE status IN ('success','partial') ORDER BY id DESC LIMIT 1").fetchone()


def mark_stale_runs(conn) -> None:
    """A run left 'running' by a crash/restart is closed as failed on startup."""
    conn.execute("UPDATE sync_log SET status='failed', ended_at=?, message='Interrupted (service restarted)'"
                 " WHERE status='running'", (now_iso(),))
