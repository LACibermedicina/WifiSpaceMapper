"""Autenticacao: senhas com PBKDF2-HMAC-SHA256 + sessoes em banco."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Optional

from server import db

ITERATIONS = 120_000
SESSION_DAYS = 30


def hash_password(password: str, salt: Optional[str] = None):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             bytes.fromhex(salt), ITERATIONS)
    return dk.hex(), salt


def verify_password(password: str, salt: str, expected: str) -> bool:
    try:
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt), ITERATIONS)
    except ValueError:
        return False
    return hmac.compare_digest(dk.hex(), expected or "")


def create_session(user_id: int, request=None) -> str:
    token = secrets.token_urlsafe(32)
    ip = ""
    ua = ""
    if request is not None:
        ip = (request.client.host if request.client else "") or ""
        ua = (request.headers.get("user-agent") or "")[:250]
    db.execute("INSERT INTO sessions(token,user_id,created_at,expires_at,ip,ua)"
               " VALUES(?,?,?,?,?,?)",
               (token, user_id, db.now(), db.now() + SESSION_DAYS * 86400, ip, ua))
    return token


def drop_session(token: str) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE token=?", (token,))


def user_from_token(token: Optional[str]):
    if not token:
        return None
    row = db.query_one(
        "SELECT u.*, s.expires_at AS _exp FROM sessions s JOIN users u ON u.id=s.user_id"
        " WHERE s.token=?", (token,))
    if not row:
        return None
    if float(row.get("_exp") or 0) < time.time() or not int(row.get("active", 1)):
        db.execute("DELETE FROM sessions WHERE token=?", (token,))
        return None
    row.pop("_exp", None)
    row.pop("pass_hash", None)
    row.pop("salt", None)
    return row


def purge_sessions() -> int:
    conn = db.connect()
    try:
        cur = conn.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def validate_password(password: str) -> Optional[str]:
    if not password or len(password) < 8:
        return "password_too_short"
    if len(password) > 256:
        return "password_too_long"
    return None
