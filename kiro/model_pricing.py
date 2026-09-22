# -*- coding: utf-8 -*-
"""
Model pricing configuration for cost estimation.

Prices in USD per 1 million tokens.

Two layers, resolved in order:
1. Overrides stored in the ``model_pricing`` table of ``token_usage.db``,
   editable at runtime through the admin endpoints (``/admin/model-pricing``).
2. ``DEFAULT_PRICING`` hardcoded below, used only when the DB has no entry
   for that model (and partial-match still misses).

``MODEL_PRICING_FILE`` (an off-disk JSON) is still honoured for compatibility
with earlier deploys, but the DB layer takes precedence.
"""

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

# Default pricing (USD per 1M tokens). Kept as a fallback so a fresh install
# with an empty override table still calculates the same numbers it always did.
DEFAULT_PRICING = {
    "claude-opus-4": {"input": 15.0, "output": 75.0},
    "claude-opus-4-20250514": {"input": 15.0, "output": 75.0},
    "claude-sonnet-4": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4-20250514": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4-5-20250514": {"input": 3.0, "output": 15.0},
    "amazon-nova-pro": {"input": 0.8, "output": 3.2},
    "amazon-nova-lite": {"input": 0.06, "output": 0.24},
    "amazon-nova-micro": {"input": 0.035, "output": 0.14},
}


def _load_from_file() -> dict:
    """Legacy escape hatch: reads MODEL_PRICING_FILE if configured."""
    pricing_file = os.getenv("MODEL_PRICING_FILE")
    if pricing_file and os.path.exists(pricing_file):
        try:
            with open(pricing_file, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


# In-memory cache of DB overrides. get_cost() runs on the hot per-request path
# and cannot afford a SQLite hop per call, so writes refresh this dict inline
# and reads see the latest values without touching the file.
_LOCK = threading.RLock()
_OVERRIDES: dict = {}
_DB_PATH: Optional[str] = None

# Kept for callers/tests that expect an eagerly-loaded pricing dict. It reflects
# the file layer only; DB overrides live in _OVERRIDES and are applied on lookup.
MODEL_PRICING = _load_from_file() or DEFAULT_PRICING


def init_pricing_store(db_path: str = "data/token_usage.db") -> None:
    """Create the model_pricing table and warm the override cache.

    Called once at app startup, next to the other stores (proxy_keys,
    request_logger, usage_tracker). Idempotent - safe to call again if the
    process reloads.
    """
    global _DB_PATH, _OVERRIDES
    _DB_PATH = db_path
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS model_pricing (
                model TEXT PRIMARY KEY,
                input_price REAL NOT NULL,
                output_price REAL NOT NULL,
                currency TEXT NOT NULL DEFAULT 'USD',
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
    _reload_overrides()


def _reload_overrides() -> None:
    """Refresh _OVERRIDES from the DB. Called after every write."""
    global _OVERRIDES
    if not _DB_PATH:
        return
    with sqlite3.connect(_DB_PATH) as conn:
        rows = conn.execute(
            "SELECT model, input_price, output_price FROM model_pricing"
        ).fetchall()
    with _LOCK:
        _OVERRIDES = {
            model: {"input": float(inp), "output": float(out)}
            for model, inp, out in rows
        }


def _lookup_pricing(model: str) -> Optional[dict]:
    """Return the rate entry for a model, or None when none is configured.

    Order of precedence:
      1. Exact match in the DB override table.
      2. Exact match in DEFAULT_PRICING / MODEL_PRICING_FILE.
      3. Partial substring match (either direction) against the union of both,
         DB overrides first. Kept so ``claude-opus-4.6`` still finds the
         ``claude-opus-4`` rate the way the pre-DB implementation did.

    Callers can tell "this model costs nothing" apart from "we have no rate
    for this model" via has_pricing(): get_cost() returns 0.0 for both, and
    a report that renders the second as $0.00 tells the reader the traffic
    was free. That's the hole users close by adding an entry through the
    admin page.
    """
    with _LOCK:
        overrides = dict(_OVERRIDES)
    if model in overrides:
        return overrides[model]
    if model in MODEL_PRICING:
        return MODEL_PRICING[model]
    for key, val in overrides.items():
        if key in model or model in key:
            return val
    for key, val in MODEL_PRICING.items():
        if key in model or model in key:
            return val
    return None


def has_pricing(model: str) -> bool:
    """Whether a rate is configured for this model (directly or by partial match)."""
    return _lookup_pricing(model) is not None


def get_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Calculate cost in USD for given token usage."""
    pricing = _lookup_pricing(model)
    if not pricing:
        return 0.0
    input_cost = (prompt_tokens / 1_000_000) * pricing["input"]
    output_cost = (completion_tokens / 1_000_000) * pricing["output"]
    return input_cost + output_cost


# ─── Admin CRUD helpers ─────────────────────────────────────────────────────
# These are the only entry points the admin route needs. They keep the write
# path narrow so we can guarantee _OVERRIDES stays in sync with the DB.

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def list_pricing() -> list:
    """Return every override in the DB, plus the effective built-in default.

    The response marks each row's source (``db``/``default``) so the UI can
    grey out fallbacks. Built-ins are only listed when there is no DB row
    shadowing the same model name.
    """
    with _LOCK:
        overrides = dict(_OVERRIDES)
    rows_by_model: dict = {}
    if _DB_PATH:
        with sqlite3.connect(_DB_PATH) as conn:
            for model, inp, out, cur, upd in conn.execute(
                "SELECT model, input_price, output_price, currency, updated_at "
                "FROM model_pricing ORDER BY model ASC"
            ).fetchall():
                rows_by_model[model] = {
                    "model": model,
                    "input_price": float(inp),
                    "output_price": float(out),
                    "currency": cur or "USD",
                    "updated_at": upd,
                    "source": "db",
                }
    for model, val in DEFAULT_PRICING.items():
        if model not in rows_by_model:
            rows_by_model[model] = {
                "model": model,
                "input_price": float(val["input"]),
                "output_price": float(val["output"]),
                "currency": "USD",
                "updated_at": None,
                "source": "default",
            }
    return sorted(rows_by_model.values(),
                  key=lambda r: (r["source"] != "db", r["model"]))


def upsert_pricing(model: str, input_price: float, output_price: float,
                   currency: str = "USD") -> dict:
    """Insert or replace one model's rate.

    Same-second consistency: the DB is written inside a transaction and
    _OVERRIDES is refreshed before the call returns, so the next request
    that hits get_cost picks up the new number.
    """
    if not _DB_PATH:
        raise RuntimeError("pricing store not initialized")
    if not model or not model.strip():
        raise ValueError("model must not be empty")
    if input_price < 0 or output_price < 0:
        raise ValueError("prices must be non-negative")
    model = model.strip()
    currency = (currency or "USD").strip() or "USD"
    updated_at = _now_utc()
    with sqlite3.connect(_DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO model_pricing (model, input_price, output_price, currency, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(model) DO UPDATE SET
                input_price = excluded.input_price,
                output_price = excluded.output_price,
                currency = excluded.currency,
                updated_at = excluded.updated_at
            """,
            (model, float(input_price), float(output_price), currency, updated_at),
        )
        conn.commit()
    _reload_overrides()
    return {
        "model": model,
        "input_price": float(input_price),
        "output_price": float(output_price),
        "currency": currency,
        "updated_at": updated_at,
    }


def delete_pricing(model: str) -> bool:
    """Remove one override; get_cost then falls back to DEFAULT_PRICING for it."""
    if not _DB_PATH:
        raise RuntimeError("pricing store not initialized")
    with sqlite3.connect(_DB_PATH) as conn:
        cur = conn.execute("DELETE FROM model_pricing WHERE model = ?", (model,))
        conn.commit()
        removed = cur.rowcount > 0
    if removed:
        _reload_overrides()
    return removed
