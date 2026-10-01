"""
Banco de dados do portal (SQLite por padrao).

Tabelas: users, sessions, projects, captures, measurements, vectors, events,
exports -- todas as tabelas de dados apontam para project_id, de modo que cada
captura, medida e vetor desenhado fica vinculado ao projeto correspondente.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import zlib

import numpy as np

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("WSM_DATA", os.path.join(BASE_DIR, "data"))
DB_PATH = os.environ.get("WSM_DB", os.path.join(DATA_DIR, "portal.db"))
EXPORT_DIR = os.path.join(DATA_DIR, "exports")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT UNIQUE COLLATE NOCASE NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  pass_hash TEXT NOT NULL,
  salt TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'user',
  lang TEXT NOT NULL DEFAULT 'pt',
  active INTEGER NOT NULL DEFAULT 1,
  created_at REAL, last_login REAL);

CREATE TABLE IF NOT EXISTS sessions(
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at REAL, expires_at REAL, ip TEXT DEFAULT '', ua TEXT DEFAULT '');

CREATE TABLE IF NOT EXISTS projects(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  description TEXT DEFAULT '',
  tags TEXT DEFAULT '',
  room_x REAL DEFAULT 4.0, room_y REAL DEFAULT 5.0, room_z REAL DEFAULT 2.8,
  router_height REAL DEFAULT 1.4,
  router_xyz TEXT DEFAULT '[0,0,0]',
  grid_step REAL DEFAULT 1.0,
  voxel REAL DEFAULT 0.12,
  source_mode TEXT DEFAULT 'sim',
  preset TEXT DEFAULT 'rapida',
  created_at REAL, updated_at REAL);

CREATE TABLE IF NOT EXISTS captures(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  label TEXT DEFAULT '',
  kind TEXT DEFAULT 'sim',
  method TEXT DEFAULT '',
  created_at REAL,
  duration_s REAL DEFAULT 0,
  n_points INTEGER DEFAULT 0,
  n_triangles INTEGER DEFAULT 0,
  voxel REAL DEFAULT 0.12,
  metrics TEXT DEFAULT '{}',
  calib TEXT DEFAULT '{}',
  traj TEXT DEFAULT '[]',
  points_blob BLOB,
  verts_blob BLOB,
  tris_blob BLOB,
  voxels_blob BLOB);

CREATE TABLE IF NOT EXISTS measurements(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  capture_id INTEGER REFERENCES captures(id) ON DELETE SET NULL,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  label TEXT DEFAULT '',
  p1 TEXT NOT NULL DEFAULT '[0,0,0]',
  p2 TEXT NOT NULL DEFAULT '[0,0,0]',
  value_m REAL DEFAULT 0,
  dx REAL DEFAULT 0, dy REAL DEFAULT 0, dz REAL DEFAULT 0,
  color TEXT DEFAULT '#37a6e0',
  visible INTEGER DEFAULT 1,
  note TEXT DEFAULT '',
  created_at REAL);

CREATE TABLE IF NOT EXISTS vectors(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  capture_id INTEGER REFERENCES captures(id) ON DELETE SET NULL,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT DEFAULT '',
  points TEXT NOT NULL DEFAULT '[]',
  closed INTEGER DEFAULT 0,
  total_m REAL DEFAULT 0,
  perimeter_m REAL DEFAULT 0,
  color TEXT DEFAULT '#ffd166',
  visible INTEGER DEFAULT 1,
  note TEXT DEFAULT '',
  created_at REAL);

CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER, project_id INTEGER, kind TEXT, message TEXT, ts REAL);

CREATE TABLE IF NOT EXISTS exports(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER, project_id INTEGER, fmt TEXT, scope TEXT,
  fields TEXT, filename TEXT, size INTEGER, created_at REAL);

CREATE INDEX IF NOT EXISTS ix_captures_project ON captures(project_id);
CREATE INDEX IF NOT EXISTS ix_meas_project ON measurements(project_id);
CREATE INDEX IF NOT EXISTS ix_vec_project ON vectors(project_id);
CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS ix_events_project ON events(project_id);
"""


def now() -> float:
    return time.time()


def connect() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=8000")
    except sqlite3.Error:
        pass
    return conn


def init_db() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(EXPORT_DIR, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def execute(sql: str, args=()) -> int:
    conn = connect()
    try:
        cur = conn.execute(sql, args)
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def query(sql: str, args=()) -> list:
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def query_one(sql: str, args=()):
    rows = query(sql, args)
    return rows[0] if rows else None


def log_event(user_id, project_id, kind: str, message: str) -> None:
    try:
        execute("INSERT INTO events(user_id,project_id,kind,message,ts) VALUES(?,?,?,?,?)",
                (user_id, project_id, kind, message[:900], now()))
    except sqlite3.Error:
        pass


# --------------------------------------------------------------------------
# (des)empacotamento de arrays em BLOB comprimido
# --------------------------------------------------------------------------
def pack_array(arr) -> bytes:
    a = np.ascontiguousarray(arr)
    return zlib.compress(a.tobytes(), 6)


def unpack_array(blob, dtype, shape=None) -> np.ndarray:
    if not blob:
        return np.zeros(shape or (0, 3), dtype=dtype)
    a = np.frombuffer(zlib.decompress(blob), dtype=dtype)
    if shape:
        a = a.reshape(shape)
    return a


def jloads(text, default):
    try:
        return json.loads(text) if text else default
    except (ValueError, TypeError):
        return default


def arr3(a) -> list:
    return [round(float(v), 4) for v in np.asarray(a, dtype=float).reshape(3)]


def arrN(a) -> list:
    return [[round(float(v), 4) for v in p] for p in np.asarray(a, dtype=float).reshape(-1, 3)]


def as_list(a, default=None) -> list:
    """Converte numpy / None / lista em lista simples (seguro para json.dumps)."""
    if a is None:
        return list(default) if default is not None else []
    arr = np.asarray(a, dtype=float)
    if arr.size == 0:
        return list(default) if default is not None else []
    return arrN(arr)
