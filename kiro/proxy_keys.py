# -*- coding: utf-8 -*-
"""
Per-user proxy API keys.

The gateway historically authenticated every caller with one global
PROXY_API_KEY, which made per-user attribution impossible: by the time a
request was logged, the caller's identity was already gone.

This module stores one key per user so each request can be attributed. The
global PROXY_API_KEY keeps working and is attributed to the unassigned bucket,
so existing clients are not broken while users are migrated over.

Keys are provisioned by ai-console (the system that owns the user table) and
pushed here; this module is the gateway-side store, not the source of truth.
"""

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

# Identity used for traffic authenticated with the legacy global PROXY_API_KEY.
# Reports show it as a distinct bucket rather than hiding it or guessing a user.
UNASSIGNED_USER_ID = "__unassigned__"
UNASSIGNED_USER_NAME = "未分配（全局 Key）"


class ProxyKeyStore:
    """Stores and resolves per-user proxy API keys."""

    def __init__(self, db_path: str = "data/token_usage.db"):
        self._db_path = db_path
        self._lock = asyncio.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        # Key lookup happens on every authenticated request, so the table is
        # mirrored in memory and refreshed on write.
        self._cache: Dict[str, dict] = {}

    async def init_db(self) -> None:
        """Create the proxy_keys table and warm the lookup cache."""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")

        # user_id is UNIQUE: one key per user, enforced by the schema rather
        # than by callers remembering to check.
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS proxy_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                api_key TEXT NOT NULL UNIQUE,
                user_id TEXT NOT NULL UNIQUE,
                user_name TEXT,
                enabled INTEGER DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_used_at DATETIME
            );
            CREATE INDEX IF NOT EXISTS idx_proxy_keys_api_key ON proxy_keys(api_key);
            CREATE INDEX IF NOT EXISTS idx_proxy_keys_user_id ON proxy_keys(user_id);
            """
        )
        self._conn.commit()
        await self._reload_cache()
        logger.info(f"ProxyKeyStore initialized ({len(self._cache)} key(s))")

    async def _reload_cache(self) -> None:
        if not self._conn:
            return
        rows = self._conn.execute(
            "SELECT api_key, user_id, user_name, enabled FROM proxy_keys"
        ).fetchall()
        self._cache = {
            r["api_key"]: {
                "user_id": r["user_id"],
                "user_name": r["user_name"] or r["user_id"],
                "enabled": bool(r["enabled"]),
            }
            for r in rows
        }

    def resolve(self, api_key: str) -> Optional[dict]:
        """
        Return the identity behind a key, or None when it is unknown/disabled.

        Reads the in-memory cache so the auth path stays synchronous and cheap.
        A disabled key is treated as unknown: it must not authenticate.
        """
        if not api_key:
            return None
        entry = self._cache.get(api_key)
        if not entry or not entry["enabled"]:
            return None
        return {"user_id": entry["user_id"], "user_name": entry["user_name"]}

    async def upsert(self, api_key: str, user_id: str, user_name: str = "",
                     enabled: bool = True) -> dict:
        """
        Create or replace the key for one user.

        Keyed on user_id so re-issuing a key for an existing user rotates it
        instead of leaving the old key valid - the one-key-per-user rule would
        otherwise be silently broken by a second insert.
        """
        if not api_key or not user_id:
            raise ValueError("api_key and user_id are required")
        if user_id == UNASSIGNED_USER_ID:
            raise ValueError("user_id is reserved for legacy global-key traffic")

        async with self._lock:
            self._conn.execute(
                """INSERT INTO proxy_keys (api_key, user_id, user_name, enabled)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       api_key = excluded.api_key,
                       user_name = excluded.user_name,
                       enabled = excluded.enabled""",
                (api_key, user_id, user_name, 1 if enabled else 0),
            )
            self._conn.commit()
            await self._reload_cache()

        return {"user_id": user_id, "user_name": user_name or user_id, "enabled": enabled}

    async def set_enabled(self, user_id: str, enabled: bool) -> bool:
        """Enable or disable a user's key. Returns False when the user has none."""
        async with self._lock:
            cur = self._conn.execute(
                "UPDATE proxy_keys SET enabled = ? WHERE user_id = ?",
                (1 if enabled else 0, user_id),
            )
            self._conn.commit()
            await self._reload_cache()
        return cur.rowcount > 0

    async def delete(self, user_id: str) -> bool:
        """Revoke a user's key. Returns False when the user has none."""
        async with self._lock:
            cur = self._conn.execute("DELETE FROM proxy_keys WHERE user_id = ?", (user_id,))
            self._conn.commit()
            await self._reload_cache()
        return cur.rowcount > 0

    async def list_keys(self) -> List[dict]:
        """List all keys, masked. Full key values are never returned."""
        async with self._lock:
            rows = self._conn.execute(
                """SELECT api_key, user_id, user_name, enabled, created_at, last_used_at
                   FROM proxy_keys ORDER BY created_at DESC"""
            ).fetchall()
        return [
            {
                "user_id": r["user_id"],
                "user_name": r["user_name"] or r["user_id"],
                "api_key_masked": _mask(r["api_key"]),
                "enabled": bool(r["enabled"]),
                "created_at": r["created_at"],
                "last_used_at": r["last_used_at"],
            }
            for r in rows
        ]

    async def touch(self, api_key: str) -> None:
        """
        Record that a key was just used.

        Best-effort and non-blocking for the caller: a failure here must never
        turn a working request into an error.
        """
        try:
            async with self._lock:
                self._conn.execute(
                    "UPDATE proxy_keys SET last_used_at = CURRENT_TIMESTAMP WHERE api_key = ?",
                    (api_key,),
                )
                self._conn.commit()
        except Exception as e:
            logger.warning(f"Failed to update last_used_at: {e}")

    async def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None


def _mask(key: str) -> str:
    """Show only the first and last four characters of a key."""
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}...{key[-4:]}"
