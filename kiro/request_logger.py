# -*- coding: utf-8 -*-
"""
Request logger with SQLite storage.

Records every request passing through the gateway for monitoring and debugging.
Shares the same database file as UsageTracker.
"""

import asyncio
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

from .config import (
    LOG_FAILURE_BODY,
    LOG_SUCCESS_BODY,
    MAX_LOGGED_BODY_BYTES,
)


class RequestLogger:
    """Records and queries request logs."""

    def __init__(self, db_path: str = "data/token_usage.db"):
        self._db_path = db_path
        self._lock = asyncio.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    async def init_db(self) -> None:
        """Initialize database and create request_logs table."""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")

        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS request_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                model TEXT,
                api_type TEXT,
                streaming INTEGER DEFAULT 0,
                status TEXT,
                status_code INTEGER DEFAULT 200,
                duration_ms INTEGER DEFAULT 0,
                prompt_tokens INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                account_id TEXT,
                error_message TEXT,
                request_id TEXT,
                request_body TEXT,
                response_body TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_request_logs_timestamp ON request_logs(timestamp);
            CREATE INDEX IF NOT EXISTS idx_request_logs_status ON request_logs(status);
        """)
        self._conn.commit()

        # Migration: add columns if missing
        try:
            self._conn.execute("SELECT request_body FROM request_logs LIMIT 1")
        except sqlite3.OperationalError:
            self._conn.execute("ALTER TABLE request_logs ADD COLUMN request_body TEXT")
            self._conn.execute("ALTER TABLE request_logs ADD COLUMN response_body TEXT")
            self._conn.commit()

        # Migration: caller identity. Rows written before this existed keep NULL
        # rather than being back-filled with a guess - per-user reports show them
        # as unknown, which is the truth.
        try:
            self._conn.execute("SELECT user_id FROM request_logs LIMIT 1")
        except sqlite3.OperationalError:
            self._conn.execute("ALTER TABLE request_logs ADD COLUMN user_id TEXT")
            self._conn.execute("ALTER TABLE request_logs ADD COLUMN user_name TEXT")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_request_logs_user_id ON request_logs(user_id)"
            )
            self._conn.commit()
            logger.info("RequestLogger: added user_id/user_name columns")

        logger.info("RequestLogger initialized")

    @staticmethod
    def _body_for_storage(body: str, status: str) -> str:
        """Decide what of a body actually gets stored.

        Bodies are the only large thing in this table - a stat row is under 100
        bytes, its bodies average 200+ KB. Storing them for every successful call
        is what grows the database into the tens of gigabytes; dropping them costs
        nothing statistically, because every column the per-user/per-day/per-model
        reports read lives outside the body.

        Failures keep their payload: that is the case where the bytes are the
        evidence. What survives is capped either way, so a single pathological
        request cannot write hundreds of megabytes.
        """
        if not body:
            return ""
        keep = LOG_FAILURE_BODY if status != "success" else LOG_SUCCESS_BODY
        if not keep:
            return ""
        if len(body) <= MAX_LOGGED_BODY_BYTES:
            return body
        return body[:MAX_LOGGED_BODY_BYTES] + f"...[truncated, original {len(body)} chars]"

    async def record(
        self,
        model: str = "",
        api_type: str = "openai",
        streaming: bool = False,
        status: str = "success",
        status_code: int = 200,
        duration_ms: int = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        account_id: str = "",
        error_message: str = "",
        request_id: str = "",
        request_body: str = "",
        response_body: str = "",
        user_id: str = "",
        user_name: str = "",
    ) -> None:
        """Record a request log entry.

        Statistics columns are always written. Bodies are filtered by
        :meth:`_body_for_storage` - see LOG_SUCCESS_BODY in config.
        """
        request_body = self._body_for_storage(request_body, status)
        response_body = self._body_for_storage(response_body, status)

        async with self._lock:
            try:
                self._conn.execute(
                    """INSERT INTO request_logs
                       (model, api_type, streaming, status, status_code, duration_ms,
                        prompt_tokens, completion_tokens, account_id, error_message, request_id,
                        request_body, response_body, user_id, user_name)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (model, api_type, 1 if streaming else 0, status, status_code,
                     duration_ms, prompt_tokens, completion_tokens, account_id,
                     error_message, request_id, request_body, response_body,
                     user_id, user_name),
                )
                self._conn.commit()
            except Exception as e:
                logger.error(f"Failed to record request log: {e}")

    async def query(
        self,
        page: int = 1,
        page_size: int = 50,
        model: str = "",
        status: str = "",
        days: int = 7,
    ) -> dict:
        """Query request logs with pagination and filters."""
        since = (datetime.utcnow() - timedelta(days=days)).isoformat()
        conditions = ["timestamp >= ?"]
        params: list = [since]

        if model:
            conditions.append("model = ?")
            params.append(model)
        if status:
            conditions.append("status = ?")
            params.append(status)

        where = " AND ".join(conditions)

        # Count total
        total = self._conn.execute(
            f"SELECT COUNT(*) FROM request_logs WHERE {where}", params
        ).fetchone()[0]

        # Fetch page (exclude large body fields for list view)
        offset = (page - 1) * page_size
        rows = self._conn.execute(
            f"""SELECT id, timestamp, model, api_type, streaming, status, status_code,
                       duration_ms, prompt_tokens, completion_tokens, account_id,
                       error_message, request_id
                FROM request_logs WHERE {where}
                ORDER BY timestamp DESC LIMIT ? OFFSET ?""",
            params + [page_size, offset],
        ).fetchall()

        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "data": [dict(row) for row in rows],
        }

    async def get_by_id(self, log_id: int) -> Optional[dict]:
        """Get a single log entry by ID (includes full body)."""
        row = self._conn.execute(
            "SELECT * FROM request_logs WHERE id = ?", (log_id,)
        ).fetchone()
        if row:
            return dict(row)
        return None

    async def get_stats(self, days: int = 7) -> dict:
        """Get summary stats for recent requests."""
        since = (datetime.utcnow() - timedelta(days=days)).isoformat()
        row = self._conn.execute(
            """SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as success_count,
                AVG(duration_ms) as avg_duration_ms
            FROM request_logs WHERE timestamp >= ?""",
            (since,),
        ).fetchone()

        total = row["total"] or 0
        success = row["success_count"] or 0
        avg_ms = row["avg_duration_ms"] or 0

        return {
            "days": days,
            "total_requests": total,
            "success_count": success,
            "error_count": total - success,
            "success_rate": round(success / total * 100, 1) if total > 0 else 0,
            "avg_duration_ms": round(avg_ms),
        }

    # ── Per-user usage ──────────────────────────────────────────────────────
    #
    # These read request_logs rather than the token_usage table because only
    # request_logs carries the caller's identity. Rows written before the
    # user_id column existed are reported under the "unknown" bucket instead of
    # being attributed to someone.

    async def get_user_usage(self, days: int = 30) -> List[dict]:
        """Total tokens and requests per user, highest usage first."""
        since = (datetime.utcnow() - timedelta(days=days)).isoformat()
        async with self._lock:
            rows = self._conn.execute(
                """SELECT
                    COALESCE(NULLIF(user_id, ''), '__unknown__') AS user_id,
                    COALESCE(NULLIF(MAX(user_name), ''), '未知（无身份记录）') AS user_name,
                    COUNT(*) AS requests,
                    SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success_count,
                    COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                    COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                    COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS total_tokens
                   FROM request_logs
                   WHERE timestamp >= ?
                   GROUP BY COALESCE(NULLIF(user_id, ''), '__unknown__')
                   ORDER BY total_tokens DESC""",
                (since,),
            ).fetchall()
        return [dict(r) for r in rows]

    async def get_user_daily_usage(self, days: int = 30, user_id: str = "") -> List[dict]:
        """Per-day token totals, optionally narrowed to one user."""
        since = (datetime.utcnow() - timedelta(days=days)).isoformat()
        sql = """SELECT
                    DATE(timestamp) AS date,
                    COALESCE(NULLIF(user_id, ''), '__unknown__') AS user_id,
                    COALESCE(NULLIF(MAX(user_name), ''), '未知（无身份记录）') AS user_name,
                    COUNT(*) AS requests,
                    COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                    COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                    COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS total_tokens
                 FROM request_logs
                 WHERE timestamp >= ?"""
        params: list = [since]
        if user_id:
            sql += " AND COALESCE(NULLIF(user_id, ''), '__unknown__') = ?"
            params.append(user_id)
        sql += """ GROUP BY DATE(timestamp), COALESCE(NULLIF(user_id, ''), '__unknown__')
                   ORDER BY date ASC"""

        async with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    async def get_user_model_usage(self, days: int = 30, user_id: str = "") -> List[dict]:
        """Per-model token totals, optionally narrowed to one user."""
        since = (datetime.utcnow() - timedelta(days=days)).isoformat()
        sql = """SELECT
                    COALESCE(NULLIF(user_id, ''), '__unknown__') AS user_id,
                    COALESCE(NULLIF(MAX(user_name), ''), '未知（无身份记录）') AS user_name,
                    COALESCE(NULLIF(model, ''), '(unknown)') AS model,
                    COUNT(*) AS requests,
                    COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                    COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                    COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS total_tokens
                 FROM request_logs
                 WHERE timestamp >= ?"""
        params: list = [since]
        if user_id:
            sql += " AND COALESCE(NULLIF(user_id, ''), '__unknown__') = ?"
            params.append(user_id)
        sql += """ GROUP BY COALESCE(NULLIF(user_id, ''), '__unknown__'),
                            COALESCE(NULLIF(model, ''), '(unknown)')
                   ORDER BY total_tokens DESC"""

        async with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    async def close(self) -> None:
        """Close database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
