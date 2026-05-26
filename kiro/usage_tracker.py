# -*- coding: utf-8 -*-
"""
Token usage tracker with SQLite storage.

Records per-request token usage and provides aggregation queries
for the admin dashboard.
"""

import asyncio
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

from kiro.model_pricing import get_cost, MODEL_PRICING


class UsageTracker:
    """
    Tracks token usage in SQLite database.

    Thread-safe via asyncio lock. Uses WAL mode for concurrent reads.
    """

    def __init__(self, db_path: str = "data/token_usage.db"):
        self._db_path = db_path
        self._lock = asyncio.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    async def init_db(self) -> None:
        """Initialize database and create tables."""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")

        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS token_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                model TEXT NOT NULL,
                prompt_tokens INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                account_id TEXT,
                api_type TEXT,
                request_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_token_usage_timestamp ON token_usage(timestamp);
            CREATE INDEX IF NOT EXISTS idx_token_usage_model ON token_usage(model);
        """)
        self._conn.commit()
        logger.info(f"UsageTracker initialized: {self._db_path}")

    async def record(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        account_id: str = "",
        api_type: str = "openai",
        request_id: str = "",
    ) -> None:
        """Record a single request's token usage."""
        total = prompt_tokens + completion_tokens
        if total == 0:
            return

        async with self._lock:
            try:
                self._conn.execute(
                    """INSERT INTO token_usage
                       (model, prompt_tokens, completion_tokens, total_tokens, account_id, api_type, request_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (model, prompt_tokens, completion_tokens, total, account_id, api_type, request_id),
                )
                self._conn.commit()
            except Exception as e:
                logger.error(f"Failed to record usage: {e}")

    async def get_summary(self, days: int = 30) -> dict:
        """Get total usage summary for the last N days."""
        since = (datetime.utcnow() - timedelta(days=days)).isoformat()
        row = self._conn.execute(
            """SELECT
                COUNT(*) as total_requests,
                COALESCE(SUM(prompt_tokens), 0) as total_prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) as total_completion_tokens,
                COALESCE(SUM(total_tokens), 0) as total_tokens
            FROM token_usage WHERE timestamp >= ?""",
            (since,),
        ).fetchone()

        total_cost = self._calculate_total_cost(since)

        return {
            "days": days,
            "total_requests": row["total_requests"],
            "total_prompt_tokens": row["total_prompt_tokens"],
            "total_completion_tokens": row["total_completion_tokens"],
            "total_tokens": row["total_tokens"],
            "total_cost_usd": round(total_cost, 4),
        }

    async def get_daily_stats(self, days: int = 30) -> List[dict]:
        """Get daily token usage breakdown."""
        since = (datetime.utcnow() - timedelta(days=days)).isoformat()
        rows = self._conn.execute(
            """SELECT
                DATE(timestamp) as date,
                COUNT(*) as requests,
                SUM(prompt_tokens) as prompt_tokens,
                SUM(completion_tokens) as completion_tokens,
                SUM(total_tokens) as total_tokens
            FROM token_usage
            WHERE timestamp >= ?
            GROUP BY DATE(timestamp)
            ORDER BY date""",
            (since,),
        ).fetchall()

        result = []
        for row in rows:
            # Estimate daily cost
            day_rows = self._conn.execute(
                """SELECT model, SUM(prompt_tokens) as pt, SUM(completion_tokens) as ct
                FROM token_usage WHERE DATE(timestamp) = ? GROUP BY model""",
                (row["date"],),
            ).fetchall()
            day_cost = sum(get_cost(r["model"], r["pt"], r["ct"]) for r in day_rows)

            result.append({
                "date": row["date"],
                "requests": row["requests"],
                "prompt_tokens": row["prompt_tokens"],
                "completion_tokens": row["completion_tokens"],
                "total_tokens": row["total_tokens"],
                "cost_usd": round(day_cost, 4),
            })
        return result

    async def get_model_stats(self, days: int = 30) -> List[dict]:
        """Get per-model token usage breakdown."""
        since = (datetime.utcnow() - timedelta(days=days)).isoformat()
        rows = self._conn.execute(
            """SELECT
                model,
                COUNT(*) as requests,
                SUM(prompt_tokens) as prompt_tokens,
                SUM(completion_tokens) as completion_tokens,
                SUM(total_tokens) as total_tokens
            FROM token_usage
            WHERE timestamp >= ?
            GROUP BY model
            ORDER BY total_tokens DESC""",
            (since,),
        ).fetchall()

        return [
            {
                "model": row["model"],
                "requests": row["requests"],
                "prompt_tokens": row["prompt_tokens"],
                "completion_tokens": row["completion_tokens"],
                "total_tokens": row["total_tokens"],
                "cost_usd": round(get_cost(row["model"], row["prompt_tokens"], row["completion_tokens"]), 4),
            }
            for row in rows
        ]

    def _calculate_total_cost(self, since: str) -> float:
        """Calculate total cost by model since timestamp."""
        rows = self._conn.execute(
            """SELECT model, SUM(prompt_tokens) as pt, SUM(completion_tokens) as ct
            FROM token_usage WHERE timestamp >= ? GROUP BY model""",
            (since,),
        ).fetchall()
        return sum(get_cost(r["model"], r["pt"], r["ct"]) for r in rows)

    async def close(self) -> None:
        """Close database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
