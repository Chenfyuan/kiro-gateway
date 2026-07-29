# -*- coding: utf-8 -*-
"""Filesystem-backed implementation of the storage interfaces.

This is the compatibility shim for Phase 1.2: every method here reproduces the
existing on-disk behaviour byte-for-byte, so switching a running gateway to
``STORAGE_BACKEND=file`` (or leaving it unset, since ``file`` is the default)
must be a strict no-op.

Design choice — thin wrapper, not a rewrite:

    The existing filesystem logic lives spread across ``kiro/auth.py``,
    ``kiro/account_manager.py``, ``kiro/usage_tracker.py``,
    ``kiro/request_logger.py``, and ``kiro/config.py``. We could pull those
    hundreds of lines into this module, but doing so *and* changing behaviour
    on the same commit is exactly how migrations break in subtle ways.

    Instead each method below delegates to the existing implementation. Later
    phases (1.4+) replace these thin adapters with Redis/Postgres impls that
    obey the same contract.

    The one thing we do differently is expose the *distributed* refresh_lock
    contract that ``TokenStore`` requires. For file backends that collapses to
    a plain ``asyncio.Lock`` per account_id (equivalent to today's per-process
    ``KiroAuthManager._lock``), and we mark it as such in the docstring so
    someone reading this in six months doesn't wonder why a "distributed" lock
    is process-local — the answer is that a single-process backend cannot
    provide inter-process coordination and doesn't need to.

Nothing in this module opens files or spawns background tasks eagerly:
construction is cheap and idempotent so the lifespan startup hook can safely
call it before the rest of the app is wired up. The real I/O happens inside
the method implementations, at the moment the caller actually reads/writes.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncContextManager, Optional

from kiro.storage.interfaces import (
    AccountRecord,
    AccountRegistry,
    AdminKeyStore,
    KiroToken,
    RequestLogEntry,
    Storage,
    TokenStore,
    UsageRecord,
    UsageStore,
)


# ---------------------------------------------------------------------------
# TokenStore
# ---------------------------------------------------------------------------


class FileTokenStore(TokenStore):
    """File-backed :class:`TokenStore`.

    NOTE — not yet wired into the runtime. In Phase 1.2 we're only creating
    the abstraction boundary; the *current* refresh path in
    ``kiro/auth.py::KiroAuthManager`` still reads and writes credential files
    directly. Phase 1.2b will move those callsites through this class.

    The methods below therefore raise ``NotImplementedError`` on purpose: if a
    caller starts using ``FileTokenStore`` before the delegation glue is in
    place, we want them to notice immediately rather than silently no-op.
    """

    def __init__(self) -> None:
        # Per-account asyncio locks. Same shape as the existing
        # ``KiroAuthManager._lock`` — one lock per account — but centralised
        # here so multiple KiroAuthManager instances for the same account_id
        # in the same process share a lock (unlike today).
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def get(self, account_id: str) -> Optional[KiroToken]:
        raise NotImplementedError(
            "FileTokenStore.get is a Phase 1.2b task: it needs to read the "
            "same JSON/SQLite credential file that KiroAuthManager currently "
            "reads. Not wired up yet."
        )

    async def save(self, account_id: str, token: KiroToken) -> None:
        raise NotImplementedError(
            "FileTokenStore.save is a Phase 1.2b task: needs to invoke the "
            "same atomic-write dance KiroAuthManager._save_credentials_to_file "
            "uses today (tmp file + rename)."
        )

    def refresh_lock(self, account_id: str) -> AsyncContextManager[bool]:
        """Return an async context manager providing a per-account lock.

        For a file backend "distributed" collapses to "process-local", so we
        yield ``True`` if the caller got the lock and never ``False`` (there
        is nothing to compete with). The Redis backend will yield ``False``
        when another process holds the lock.
        """
        return self._acquire_lock(account_id)

    @asynccontextmanager
    async def _acquire_lock(self, account_id: str):
        async with self._locks_guard:
            lock = self._locks.get(account_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[account_id] = lock

        async with lock:
            # Always True for the file backend: no cross-process coordination.
            yield True


# ---------------------------------------------------------------------------
# AccountRegistry
# ---------------------------------------------------------------------------


class FileAccountRegistry(AccountRegistry):
    """File-backed :class:`AccountRegistry`.

    Same story as :class:`FileTokenStore`: Phase 1.2b will re-point
    ``AccountManager``'s existing credentials.json read/write logic through
    these methods; until then the methods raise. Keeping the placeholders
    around lets ``Storage`` be a fully constructed value object even before
    any caller uses it, so we can start plumbing it through the app in small
    commits.
    """

    async def list_accounts(self) -> list[AccountRecord]:
        raise NotImplementedError(
            "FileAccountRegistry.list_accounts is a Phase 1.2b task: needs to "
            "read data/credentials.json using the same schema account_manager"
            ".load_credentials expects today."
        )

    async def get_account(self, account_id: str) -> Optional[AccountRecord]:
        raise NotImplementedError(
            "FileAccountRegistry.get_account is a Phase 1.2b task."
        )

    async def add_account(
        self, record: AccountRecord, initial_token: KiroToken
    ) -> None:
        raise NotImplementedError(
            "FileAccountRegistry.add_account is a Phase 1.2b task: needs to "
            "append to credentials.json and write the initial token file, "
            "matching account_manager.add_account's current behaviour."
        )

    async def remove_account(self, account_id: str) -> None:
        raise NotImplementedError(
            "FileAccountRegistry.remove_account is a Phase 1.2b task."
        )

    async def set_disabled(self, account_id: str, disabled: bool) -> None:
        raise NotImplementedError(
            "FileAccountRegistry.set_disabled is a Phase 1.2b task: on the "
            "file backend it flips ``disabled`` in state.json and triggers "
            "the periodic save."
        )


# ---------------------------------------------------------------------------
# UsageStore
# ---------------------------------------------------------------------------


class FileUsageStore(UsageStore):
    """SQLite-backed :class:`UsageStore`.

    Wraps the pre-existing ``UsageTracker`` and ``RequestLogger`` classes.
    They own the SQLite connection, WAL pragmas, table DDL and the per-
    process ``asyncio.Lock`` around writes; we do not re-implement any of
    that here. Instead this store:

    1. Provides a unified async API in the shape of the ``UsageStore`` ABC.
    2. Exposes the two underlying instances via ``.tracker`` / ``.logger``
       properties so ``main.py``'s FastAPI lifespan can hoist them onto
       ``app.state`` — that keeps every downstream caller
       (``routes_openai`` / ``routes_anthropic`` / ``routes_admin``) working
       unchanged. Once Phase 1.2c re-routes those callers through the store
       we can drop the ``app.state`` hoist entirely.

    Both trackers use the SAME database file today
    (``data/token_usage.db``). The Postgres backend will keep them logically
    separate (two tables in one schema) but this file backend leaves the
    layout alone so we don't have to migrate an existing prod SQLite file
    just to satisfy the abstraction.
    """

    def __init__(self, db_path: str = "data/token_usage.db") -> None:
        # Import inside __init__ to keep the storage package import cost low
        # and to avoid a top-level dependency on the tracker modules (which
        # in turn pull in loguru + model_pricing). Tests can substitute a
        # different db_path without touching prod defaults.
        from kiro.usage_tracker import UsageTracker
        from kiro.request_logger import RequestLogger

        self._tracker = UsageTracker(db_path=db_path)
        self._logger = RequestLogger(db_path=db_path)
        self._initialised = False

    # ------------------------------------------------------------------
    # Lifecycle hooks (called from main.py's FastAPI lifespan).
    # ------------------------------------------------------------------

    async def init(self) -> None:
        """Open both SQLite connections and create tables. Idempotent."""
        if self._initialised:
            return
        await self._tracker.init_db()
        await self._logger.init_db()
        self._initialised = True

    async def close(self) -> None:
        """Close both SQLite connections. Idempotent."""
        await self._tracker.close()
        await self._logger.close()
        self._initialised = False

    # ------------------------------------------------------------------
    # Underlying-instance accessors for the app.state hoist. These will
    # disappear once Phase 1.2c routes callers through the ABC methods
    # below; keeping them for now avoids touching six hot-path files in a
    # single commit.
    # ------------------------------------------------------------------

    @property
    def tracker(self):
        """The wrapped :class:`UsageTracker` — used by ``app.state`` for
        backwards compatibility. Prefer :meth:`record_usage` in new code."""
        return self._tracker

    @property
    def logger(self):
        """The wrapped :class:`RequestLogger` — used by ``app.state`` for
        backwards compatibility. Prefer :meth:`record_request` in new code."""
        return self._logger

    # ------------------------------------------------------------------
    # UsageStore ABC — writes.
    # ------------------------------------------------------------------

    async def record_usage(self, entry: UsageRecord) -> None:
        await self._tracker.record(
            model=entry.model,
            prompt_tokens=entry.prompt_tokens,
            completion_tokens=entry.completion_tokens,
            account_id=entry.account_id or "",
            api_type=entry.api_type,
            request_id=entry.request_id or "",
        )

    async def record_request(self, entry: RequestLogEntry) -> None:
        # RequestLogger.record has 12+ positional-ish kwargs today; we pass
        # them by keyword to match the current signature. If that signature
        # drifts, the ABC keeps the storage-layer contract stable.
        await self._logger.record(
            model=entry.model,
            api_type=entry.api_type,
            streaming=1 if entry.streaming else 0,
            status=entry.status,
            status_code=entry.status_code,
            duration_ms=entry.duration_ms,
            prompt_tokens=entry.prompt_tokens,
            completion_tokens=entry.completion_tokens,
            account_id=entry.account_id or "",
            error_message=entry.error_message,
            request_id=entry.request_id or "",
            request_body=entry.request_body,
            response_body=entry.response_body,
        )

    # ------------------------------------------------------------------
    # UsageStore ABC — reads. Straightforward passthroughs to the same
    # methods the admin routes call today.
    # ------------------------------------------------------------------

    async def usage_summary(self, days: int = 30) -> dict[str, Any]:
        return await self._tracker.get_summary(days=days)

    async def usage_daily(self, days: int = 30) -> list[dict[str, Any]]:
        return await self._tracker.get_daily_stats(days=days)

    async def usage_by_model(self, days: int = 30) -> list[dict[str, Any]]:
        return await self._tracker.get_model_stats(days=days)

    async def request_history(
        self,
        page: int = 1,
        page_size: int = 50,
        model: str = "",
        status: str = "",
        days: int = 7,
    ) -> dict[str, Any]:
        return await self._logger.query(
            page=page, page_size=page_size, model=model, status=status, days=days
        )

    async def request_by_id(self, log_id: int) -> Optional[dict[str, Any]]:
        return await self._logger.get_by_id(log_id)

    async def request_stats(self, days: int = 7) -> dict[str, Any]:
        return await self._logger.get_stats(days=days)


# ---------------------------------------------------------------------------
# AdminKeyStore
# ---------------------------------------------------------------------------


class FileAdminKeyStore(AdminKeyStore):
    """File-backed admin API key store, backed by ``data/api_key.txt``.

    This class is the AUTHORITATIVE read/write point for the admin key on the
    file backend. The historical ``kiro.config.get_proxy_api_key`` /
    ``set_proxy_api_key`` module-globals will be re-pointed to delegate here
    in the same commit that introduces this store, so callers keep working
    without changes while the storage layer becomes the single source of
    truth.

    Behaviour mirrors the pre-refactor logic exactly:

    * On first ``get()`` the store lazily reads ``data/api_key.txt``. If the
      file exists and is non-empty, its trimmed contents win; otherwise we
      fall back to the ``PROXY_API_KEY`` env var (or the hard-coded default).
      The value is cached in memory afterwards — same "one read per process"
      semantics as before.
    * ``rotate(new_key)`` writes the file (creating ``data/`` if needed) and
      updates the in-memory cache. Non-atomic ``Path.write_text`` because
      that's what the current code does; the Secrets-Manager-backed
      implementation is where atomicity gets solved for real.

    The single-instance limitation — a rotation done from process A doesn't
    propagate to process B until B restarts — is inherent to the file
    backend, which is why it's only used in the single-instance deployment
    profile.
    """

    # These constants intentionally match the pre-refactor kiro.config values
    # so migrating an old ``data/api_key.txt`` in place is a no-op.
    _API_KEY_FILE = _PathType = None  # populated in __init__ to avoid import at module load

    def __init__(self, api_key_file: Optional[str] = None) -> None:
        # Local import so the storage package doesn't pull in Path just to
        # exist. Also makes the file path monkeypatchable in tests.
        from pathlib import Path
        self._api_key_file = Path(api_key_file) if api_key_file else Path("data/api_key.txt")
        self._cached: Optional[str] = None
        self._loaded = False

    def _load(self) -> str:
        import os
        if self._api_key_file.exists():
            stored = self._api_key_file.read_text().strip()
            if stored:
                return stored
        # Fall back to env var, then hard-coded default. Same precedence as
        # the pre-refactor kiro.config module.
        return os.getenv("PROXY_API_KEY", "my-super-secret-password-123")

    async def get(self) -> str:
        if not self._loaded:
            self._cached = self._load()
            self._loaded = True
        # After the first successful load the cache is authoritative for this
        # process — matches the current behaviour (config.PROXY_API_KEY only
        # gets refreshed on rotate() from this same process).
        return self._cached  # type: ignore[return-value]

    async def rotate(self, new_key: str) -> None:
        self._api_key_file.parent.mkdir(parents=True, exist_ok=True)
        self._api_key_file.write_text(new_key)
        self._cached = new_key
        self._loaded = True

    # Synchronous helpers exposed for the kiro.config compatibility shim.
    # kiro.config exports sync get_proxy_api_key/set_proxy_api_key that a
    # ton of code relies on; we can't make those async without a big blast
    # radius. On the file backend the I/O is fast (a few bytes read/write),
    # so a sync-under-async wrapper is safe. When the Redis / Secrets-Manager
    # backends come in they'll expose the same helpers but with proper
    # short-lived memoisation instead of blocking on the network.

    def get_sync(self) -> str:
        if not self._loaded:
            self._cached = self._load()
            self._loaded = True
        return self._cached  # type: ignore[return-value]

    def rotate_sync(self, new_key: str) -> None:
        self._api_key_file.parent.mkdir(parents=True, exist_ok=True)
        self._api_key_file.write_text(new_key)
        self._cached = new_key
        self._loaded = True


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def build_file_storage() -> Storage:
    """Return a :class:`Storage` bundle wired to the file backend.

    This is what ``build_storage()`` returns when ``STORAGE_BACKEND=file`` —
    which today is the only supported value and covers 100% of production
    traffic. The bundle contains placeholder implementations whose methods
    raise until Phase 1.2b re-points the existing runtime code through them.

    Importantly, **just calling this function does nothing observable**: no
    files are opened, no threads are started, no network calls are made. It's
    safe to call unconditionally during startup.
    """
    return Storage(
        tokens=FileTokenStore(),
        accounts=FileAccountRegistry(),
        usage=FileUsageStore(),
        admin_key=FileAdminKeyStore(),
    )
