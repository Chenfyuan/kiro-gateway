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
        logger.info("RequestLogger initialized")

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
    ) -> None:
        """Record a request log entry."""
        async with self._lock:
            try:
                self._conn.execute(
                    """INSERT INTO request_logs
                       (model, api_type, streaming, status, status_code, duration_ms,
                        prompt_tokens, completion_tokens, account_id, error_message, request_id,
                        request_body, response_body)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (model, api_type, 1 if streaming else 0, status, status_code,
                     duration_ms, prompt_tokens, completion_tokens, account_id,
                     error_message, request_id, request_body, response_body),
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

        # Fetch page
        offset = (page - 1) * page_size
        rows = self._conn.execute(
            f"""SELECT * FROM request_logs WHERE {where}
                ORDER BY timestamp DESC LIMIT ? OFFSET ?""",
            params + [page_size, offset],
        ).fetchall()

        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "data": [dict(row) for row in rows],
        }

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

    async def close(self) -> None:
        """Close database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
