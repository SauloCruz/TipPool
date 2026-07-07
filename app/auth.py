"""Session-cookie auth with stdlib scrypt password hashing (no extra deps).

Roles: 'manager' (daily entry, finalize) and 'admin' (everything + employees,
users, reopen). Sessions are opaque tokens stored server-side."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from .db import utcnow

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
        )
        return hmac.compare_digest(digest, bytes.fromhex(digest_hex))
    except (ValueError, TypeError):
        return False


def create_session(conn: sqlite3.Connection, user_id: int, days: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(
        timespec="seconds"
    )
    conn.execute(
        "INSERT INTO session (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (token, user_id, utcnow(), expires),
    )
    return token


def get_session_user(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    if not token:
        return None
    row = conn.execute(
        """SELECT u.* FROM session s JOIN user u ON u.id = s.user_id
           WHERE s.token = ? AND s.expires_at > ? AND u.active = 1""",
        (token, utcnow()),
    ).fetchone()
    return row


def delete_session(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM session WHERE token = ?", (token,))


def prune_expired_sessions(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM session WHERE expires_at <= ?", (utcnow(),))


def bootstrap_admin(
    conn: sqlite3.Connection, venue_id: int, email: str, password: str
) -> bool:
    """Create the first admin from .env if no users exist yet. Returns True
    if a user was created."""
    if conn.execute("SELECT COUNT(*) FROM user").fetchone()[0] > 0:
        return False
    if not email or not password:
        raise RuntimeError(
            "No users exist and ADMIN_EMAIL/ADMIN_PASSWORD are not set in .env — "
            "set them for first boot, then remove them."
        )
    conn.execute(
        "INSERT INTO user (venue_id, email, password_hash, role, super_admin, created_at)"
        " VALUES (?, ?, ?, 'admin', 1, ?)",
        (venue_id, email, hash_password(password), utcnow()),
    )
    return True
