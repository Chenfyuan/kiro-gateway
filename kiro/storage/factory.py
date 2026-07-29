# -*- coding: utf-8 -*-
"""Factory for the storage backend.

Reads ``STORAGE_BACKEND`` from the environment and wires up one of:

    STORAGE_BACKEND=file            (default) — current filesystem behaviour,
                                    the migration safety net. This is what
                                    prod runs today, and what we fall back to
                                    if the shared Redis/Postgres go sideways.

    STORAGE_BACKEND=redis+postgres  Phase 1.4 target. Multiple gateway
                                    instances can safely share state.

    STORAGE_BACKEND=file,dual-write=redis+postgres
                                    Phase 1.6 shadow-write mode: reads still
                                    come from the file backend (so behaviour
                                    is unchanged), but every write is
                                    additionally sent to Redis/Postgres. Lets
                                    us verify the new backend against a live
                                    workload before flipping reads.

We only implement ``file`` in this commit. The other branches raise a clear
error so somebody who prematurely flips the env var gets an actionable message
instead of a confusing import time crash.
"""

from __future__ import annotations

import os
from typing import Optional

from kiro.storage.interfaces import Storage


def _parse_backend(raw: str) -> tuple[str, Optional[str]]:
    """Split ``STORAGE_BACKEND`` into ``(primary, dual_write_target)``.

    Format::

        primary[,dual-write=target]

    Whitespace around tokens is tolerated. Anything else is a config error.
    """
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise ValueError("STORAGE_BACKEND is empty")
    primary = parts[0]
    dual: Optional[str] = None
    for extra in parts[1:]:
        if extra.startswith("dual-write="):
            dual = extra[len("dual-write=") :].strip()
        else:
            raise ValueError(
                f"Unknown STORAGE_BACKEND fragment: {extra!r} "
                "(expected 'dual-write=<backend>')"
            )
    return primary, dual


def build_storage(backend: Optional[str] = None) -> Storage:
    """Return a fully-wired :class:`Storage` bundle for the requested backend.

    The default value tracked in the environment is ``file``, which reproduces
    the current single-process behaviour. Callers should invoke this exactly
    once during FastAPI startup and hand the result out to whichever
    components need it.
    """
    raw = backend if backend is not None else os.environ.get("STORAGE_BACKEND", "file")
    primary, dual = _parse_backend(raw)

    if dual is not None:
        # Phase 1.6 dual-write is not yet implemented; erroring out here keeps
        # us from silently accepting a config that "seems to work" but doesn't
        # actually shadow-write anything.
        raise NotImplementedError(
            "STORAGE_BACKEND dual-write mode is not implemented yet "
            "(planned for Phase 1.6). Use STORAGE_BACKEND=file for now."
        )

    if primary == "file":
        # Local import so an environment that never touches the file backend
        # doesn't pay the SQLite/filesystem import cost.
        from kiro.storage.file_backend import build_file_storage
        return build_file_storage()

    if primary in ("redis+postgres", "postgres+redis"):
        raise NotImplementedError(
            "STORAGE_BACKEND=redis+postgres is planned for Phase 1.4 and is "
            "not implemented yet. Use STORAGE_BACKEND=file until then."
        )

    raise ValueError(
        f"Unknown STORAGE_BACKEND {primary!r}. "
        "Expected one of: file, redis+postgres."
    )
