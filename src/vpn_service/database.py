"""SQLite persistence layer for Argos VPN bot."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional


class Database:
    """Thread-safe-ish SQLite store for WireGuard VPN clients."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = Path(db_path or os.getenv("ARGOS_VPN_DB_PATH", "/var/lib/argos/vpn.db"))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER UNIQUE NOT NULL,
                    username TEXT,
                    created_at INTEGER NOT NULL,
                    is_active INTEGER DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS wg_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    private_key TEXT NOT NULL,
                    public_key TEXT NOT NULL UNIQUE,
                    ip_address TEXT NOT NULL UNIQUE,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    active INTEGER DEFAULT 1,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS ip_pool (
                    ip TEXT PRIMARY KEY,
                    allocated INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS traffic (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL UNIQUE,
                    bytes INTEGER DEFAULT 0,
                    updated_at INTEGER NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS admins (
                    telegram_id INTEGER PRIMARY KEY
                );

                INSERT OR IGNORE INTO ip_pool (ip) VALUES
                    ('10.0.0.2'), ('10.0.0.3'), ('10.0.0.4'), ('10.0.0.5'),
                    ('10.0.0.6'), ('10.0.0.7'), ('10.0.0.8'), ('10.0.0.9'),
                    ('10.0.0.10'), ('10.0.0.11');
                """
            )
            conn.commit()

    def get_user(self, telegram_id: int) -> Optional[dict[str, Any]]:
        if not isinstance(telegram_id, int):
            raise TypeError("telegram_id must be int")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_user_by_pubkey(self, public_key: str) -> Optional[dict[str, Any]]:
        if not public_key or not isinstance(public_key, str):
            return None
        public_key = public_key.strip()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT u.* FROM users u
                JOIN wg_keys k ON u.id = k.user_id
                WHERE k.public_key = ?
                """,
                (public_key,),
            ).fetchone()
            return dict(row) if row else None

    def create_user(self, telegram_id: int, username: Optional[str] = None) -> dict[str, Any]:
        if not isinstance(telegram_id, int):
            raise TypeError("telegram_id must be int")
        username = (username or "").strip()[:64]
        with self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO users (telegram_id, username, created_at) VALUES (?,?,?)",
                    (telegram_id, username, int(time.time())),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                pass
            return self.get_user(telegram_id)

    def allocate_ip(self) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT ip FROM ip_pool WHERE allocated=0 LIMIT 1"
            ).fetchone()
            if not row:
                raise RuntimeError("IP pool exhausted")
            ip = row["ip"]
            conn.execute("UPDATE ip_pool SET allocated=1 WHERE ip=?", (ip,))
            conn.commit()
            return ip

    def release_ip(self, ip: str) -> None:
        if not ip or not isinstance(ip, str):
            return
        with self._connect() as conn:
            conn.execute("UPDATE ip_pool SET allocated=0 WHERE ip=?", (ip,))
            conn.commit()

    def create_key(
        self,
        user_id: int,
        private_key: str,
        public_key: str,
        ip_address: str,
        ttl_days: int = 3,
    ) -> None:
        if not all([user_id, private_key, public_key, ip_address]):
            raise ValueError("create_key: missing required fields")
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO wg_keys
                (user_id, private_key, public_key, ip_address, created_at, expires_at)
                VALUES (?,?,?,?,?,?)
                """,
                (user_id, private_key, public_key, ip_address, now, now + ttl_days * 86400),
            )
            conn.commit()

    def get_active_key(self, user_id: int) -> Optional[dict[str, Any]]:
        if not isinstance(user_id, int):
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM wg_keys
                WHERE user_id=? AND active=1 AND expires_at > ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (user_id, int(time.time())),
            ).fetchone()
            return dict(row) if row else None

    def get_key_by_public_key(self, public_key: str) -> Optional[dict[str, Any]]:
        if not public_key or not isinstance(public_key, str):
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM wg_keys WHERE public_key=?", (public_key.strip(),)
            ).fetchone()
            return dict(row) if row else None

    def get_last_key_time(self, telegram_id: int) -> int:
        if not isinstance(telegram_id, int):
            return 0
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT MAX(k.created_at) FROM wg_keys k
                JOIN users u ON u.id = k.user_id
                WHERE u.telegram_id = ?
                """,
                (telegram_id,),
            ).fetchone()
            return row[0] if row and row[0] else 0

    def update_traffic(self, telegram_id: int, delta_bytes: int) -> None:
        if not isinstance(telegram_id, int) or not isinstance(delta_bytes, int):
            raise TypeError("update_traffic: telegram_id and delta_bytes must be int")
        if delta_bytes < 0:
            return
        with self._connect() as conn:
            uid_row = conn.execute(
                "SELECT id FROM users WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
            if not uid_row:
                return
            uid = uid_row["id"]
            row = conn.execute(
                "SELECT bytes FROM traffic WHERE user_id=?", (uid,)
            ).fetchone()
            now = int(time.time())
            if row:
                conn.execute(
                    "UPDATE traffic SET bytes=?, updated_at=? WHERE user_id=?",
                    (row["bytes"] + delta_bytes, now, uid),
                )
            else:
                conn.execute(
                    "INSERT INTO traffic (user_id, bytes, updated_at) VALUES (?,?,?)",
                    (uid, delta_bytes, now),
                )
            conn.commit()

    def get_traffic(self, telegram_id: int) -> int:
        if not isinstance(telegram_id, int):
            return 0
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT bytes FROM traffic
                WHERE user_id=(SELECT id FROM users WHERE telegram_id=?)
                """,
                (telegram_id,),
            ).fetchone()
            return row["bytes"] if row and row["bytes"] else 0

    def deactivate_key(self, public_key: str) -> Optional[str]:
        if not public_key or not isinstance(public_key, str):
            return None
        public_key = public_key.strip()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, ip_address FROM wg_keys WHERE public_key=?", (public_key,)
            ).fetchone()
            if not row:
                return None
            conn.execute("UPDATE wg_keys SET active=0 WHERE id=?", (row["id"],))
            conn.execute(
                "UPDATE ip_pool SET allocated=0 WHERE ip=?", (row["ip_address"],)
            )
            conn.commit()
            return row["ip_address"]

    def cleanup_expired_keys(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, public_key, ip_address FROM wg_keys
                WHERE active=1 AND expires_at < ?
                """,
                (int(time.time()),),
            ).fetchall()
            released: list[str] = []
            for row in rows:
                conn.execute("UPDATE wg_keys SET active=0 WHERE id=?", (row["id"],))
                conn.execute(
                    "UPDATE ip_pool SET allocated=0 WHERE ip=?", (row["ip_address"],)
                )
                released.append(row["public_key"])
            conn.commit()
            return released

    def list_active_keys(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT k.*, u.telegram_id, u.username FROM wg_keys k
                JOIN users u ON u.id = k.user_id
                WHERE k.active=1 AND k.expires_at > ?
                ORDER BY k.created_at DESC
                """,
                (int(time.time()),),
            ).fetchall()
            return [dict(row) for row in rows]

    def is_admin(self, telegram_id: int) -> bool:
        if not isinstance(telegram_id, int):
            return False
        with self._connect() as conn:
            row = conn.execute(
                "SELECT telegram_id FROM admins WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
            return row is not None

    def add_admin(self, telegram_id: int) -> None:
        if not isinstance(telegram_id, int):
            raise TypeError("telegram_id must be int")
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO admins (telegram_id) VALUES (?)", (telegram_id,)
            )
            conn.commit()
