"""SQLite catalog. A connection per call, WAL mode, schema applied on first open.
Every table that refers to a hypervisor is scoped by connection_id; every
restore point by repository id, so multiple repositories cost nothing later."""
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT,
  role TEXT NOT NULL DEFAULT 'viewer', created REAL NOT NULL, disabled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS tokens (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL, prefix TEXT NOT NULL,
  created REAL NOT NULL, last_used REAL);
CREATE TABLE IF NOT EXISTS connections (
  id INTEGER PRIMARY KEY, type TEXT NOT NULL, name TEXT UNIQUE NOT NULL, host TEXT NOT NULL,
  username TEXT NOT NULL, secret_enc TEXT NOT NULL, verify_ssl INTEGER NOT NULL DEFAULT 0,
  created REAL NOT NULL, last_inventory REAL, last_error TEXT, info_json TEXT);
CREATE TABLE IF NOT EXISTS vms (
  id INTEGER PRIMARY KEY, connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
  moref TEXT NOT NULL, name TEXT NOT NULL, uuid TEXT, power_state TEXT, guest TEXT, tools TEXT,
  cbt_enabled INTEGER, is_template INTEGER NOT NULL DEFAULT 0, host TEXT, folder TEXT,
  disks_json TEXT, spec_json TEXT, updated REAL NOT NULL, gone INTEGER NOT NULL DEFAULT 0,
  UNIQUE(connection_id, moref));
CREATE TABLE IF NOT EXISTS proxies (
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, connection_id INTEGER REFERENCES connections(id) ON DELETE SET NULL,
  vm_moref TEXT, bios_uuid TEXT, token_hash TEXT UNIQUE NOT NULL, prefix TEXT NOT NULL,
  created REAL NOT NULL, last_seen REAL, version TEXT, address TEXT, enabled INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
  vm_ids_json TEXT NOT NULL, schedule TEXT, retention_json TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
  quiesce INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL, last_run REAL, next_run REAL, last_status TEXT);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
  vm_id INTEGER NOT NULL REFERENCES vms(id) ON DELETE CASCADE, task_id INTEGER,
  started REAL NOT NULL, finished REAL, status TEXT NOT NULL, kind TEXT, transport TEXT,
  bytes_read INTEGER DEFAULT 0, bytes_stored INTEGER DEFAULT 0, chunks_new INTEGER DEFAULT 0,
  restore_point_id INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS restore_points (
  id INTEGER PRIMARY KEY, vm_id INTEGER NOT NULL REFERENCES vms(id) ON DELETE CASCADE,
  run_id INTEGER, repo_id TEXT NOT NULL, created REAL NOT NULL, manifest TEXT NOT NULL,
  size_bytes INTEGER NOT NULL, allocated_bytes INTEGER NOT NULL, kind TEXT NOT NULL, spec_json TEXT NOT NULL,
  deleted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS cbt_state (
  vm_id INTEGER NOT NULL, disk_key TEXT NOT NULL, change_id TEXT NOT NULL, restore_point_id INTEGER NOT NULL,
  PRIMARY KEY (vm_id, disk_key));
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, subject TEXT, state TEXT NOT NULL, progress REAL DEFAULT 0,
  message TEXT, log TEXT DEFAULT '', created REAL NOT NULL, started REAL, finished REAL,
  created_by TEXT, cancel INTEGER NOT NULL DEFAULT 0, result_json TEXT);
CREATE TABLE IF NOT EXISTS agent_work (
  id INTEGER PRIMARY KEY, proxy_id INTEGER NOT NULL REFERENCES proxies(id) ON DELETE CASCADE,
  task_id INTEGER, kind TEXT NOT NULL, spec_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
  progress_bytes INTEGER DEFAULT 0, result_json TEXT, error TEXT, created REAL NOT NULL, taken REAL, finished REAL);
CREATE TABLE IF NOT EXISTS exports (
  id INTEGER PRIMARY KEY, restore_point_id INTEGER NOT NULL, vm_name TEXT NOT NULL, format TEXT NOT NULL,
  path TEXT NOT NULL, size_bytes INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, task_id INTEGER, status TEXT NOT NULL, error TEXT);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, user TEXT, action TEXT NOT NULL, detail TEXT);
CREATE INDEX IF NOT EXISTS idx_runs_vm ON runs(vm_id, started);
CREATE INDEX IF NOT EXISTS idx_rp_vm ON restore_points(vm_id, created);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state, created);
CREATE INDEX IF NOT EXISTS idx_work_proxy ON agent_work(proxy_id, state);
"""

_init_lock = threading.Lock()
_initialised = False
_tl = threading.local()  # the open connection of this thread, so nested conn() calls share one transaction


def init():
    global _initialised
    with _init_lock:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        c = sqlite3.connect(config.DB_PATH)
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript(SCHEMA)
        for table, col, ddl in (('proxies', 'deploy_task_id', 'INTEGER'), ('proxies', 'vm_name', 'TEXT')):
            if col not in [r[1] for r in c.execute(f'PRAGMA table_info({table})')]:
                c.execute(f'ALTER TABLE {table} ADD COLUMN {col} {ddl}')
        c.commit()
        c.close()
        _initialised = True


@contextmanager
def conn():
    if not _initialised:
        init()
    outer = getattr(_tl, 'c', None)
    if outer is not None:
        yield outer
        return
    c = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    c.execute('PRAGMA busy_timeout=30000')
    _tl.c = c
    try:
        c.execute('BEGIN IMMEDIATE')
        yield c
        c.execute('COMMIT')
    except Exception:
        c.execute('ROLLBACK')
        raise
    finally:
        _tl.c = None
        c.close()


def row_to_dict(r):
    if r is None:
        return None
    d = dict(r)
    for k in list(d):
        if k.endswith('_json') and d[k]:
            try:
                d[k[:-5]] = json.loads(d[k])
            except (TypeError, ValueError):
                pass
            del d[k]
    return d


def audit(user, action, detail=''):
    with conn() as c:
        c.execute('INSERT INTO audit(ts,user,action,detail) VALUES(?,?,?,?)', (time.time(), user, action, detail))
