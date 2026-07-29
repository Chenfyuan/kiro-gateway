# -*- coding: utf-8 -*-
"""Storage-layer interfaces for kiro-gateway.

Purpose of this module (Phase 1.2 of the stateless-refactor plan):

    Today kiro-gateway keeps all persistent state on the local filesystem:
        - Kiro auth tokens in per-account JSON files or in the kiro-cli SQLite
        - The gateway's own admin key in ``data/api_key.txt``
        - Account registry in ``data/credentials.json``
        - Runtime state (round-robin cursor, per-account counters, health) in
          ``data/state.json``
        - Token usage + request logs in ``data/token_usage.db`` (SQLite, WAL)

    That works for a single-process deployment but breaks the moment we run
    two gateway instances: refresh tokens are one-time-use, and both instances
    would happily refresh the same token in parallel, burning the account.

    Phase 1.2 introduces the abstractions below WITHOUT changing any behaviour.
    The existing filesystem-based logic will move behind these interfaces as
    ``FileTokenStore`` / ``JsonAccountRegistry`` / ``SqliteUsageStore`` /
    ``FileAdminKeyStore`` implementations that keep the current wire behaviour
    byte-identical (so ``STORAGE_BACKEND=file`` is a no-op safety switch during
    rollout).

    Phase 1.4 will then add ``RedisTokenStore`` / ``PostgresAccountRegistry`` /
    ``PostgresUsageStore`` / ``SecretsManagerAdminKeyStore`` so multiple
    gateway instances can share state safely (Redis distributed locks in
    particular protect the refresh-token race).

Everything here is intentionally free of I/O side effects: instantiating an
interface subclass is expected to be cheap and safe to do inside FastAPI's
lifespan startup hook, before any request is served.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any, AsyncContextManager, Awaitable, Callable, Iterable, Mapping, Optional


# ---------------------------------------------------------------------------
# Value objects that flow through the interfaces.
#
# We deliberately use plain dataclasses instead of the existing runtime objects
# (KiroAuthManager / AccountManager.Account / raw dicts) so the storage layer
# has no dependency on the auth/account modules. That way we can move the
# storage layer around, unit-test it in isolation, and swap the file impl for a
# Redis one without pulling half the codebase along.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KiroToken:
    """A refreshable Kiro credential set for one account.

    Fields mirror what the existing JSON credential file stores today
    (`accessToken`, `refreshToken`, `expiresAt`, optional `profileArn` / region
    / `clientId` / `clientSecret` for AWS SSO OIDC). We keep everything in one
    dataclass because they are always read and written together — you never
    want to write ``refresh_token`` without the matching ``expires_at``.

    ``expires_at`` is stored as epoch seconds (float) rather than an ISO
    string. The existing code has parsed it both ways depending on which
    codepath wrote it, and epoch-seconds is what every consumer converts to
    anyway — normalising here saves one class of "why does it say NaN" bugs.
    """

    access_token: str
    refresh_token: str
    expires_at: float  # epoch seconds
    profile_arn: Optional[str] = None
    region: Optional[str] = None
    # AWS SSO OIDC only:
    client_id: Optional[str] = None
    client_secret: Optional[str] = None


@dataclass(frozen=True)
class AccountRecord:
    """One row in the account registry (the moral equivalent of one entry in
    ``credentials.json`` today).

    ``id`` is the stable primary key (currently the ``kiro-auth-token-<uuid8>``
    filename stem for on-disk accounts). ``auth_type`` picks the refresh
    codepath — this is currently a Python ``AuthType`` enum in
    ``kiro/auth.py``; we keep it as a plain string here to avoid a circular
    import.

    Everything else (metadata about *how* to refresh) lives here so the runtime
    doesn't need to open a separate credential file per account — the
    ``KiroToken`` alone is enough for a refresh once we know the auth_type.
    """

    id: str
    auth_type: str  # "kiro_desktop" | "aws_sso_oidc"
    display_name: Optional[str] = None
    disabled: bool = False


# ---------------------------------------------------------------------------
# TokenStore — the highest-risk part of the migration.
#
# Every gateway process today runs one ``KiroAuthManager`` per account and
# refreshes tokens under an ``asyncio.Lock`` that is scoped to that process.
# Two processes = two locks = two concurrent refreshes = the loser's refresh
# token becomes permanently invalid until restart. This interface has to hide
# a *distributed* lock so that only one process at a time can call the upstream
# refresh endpoint for a given account, regardless of how many gateway pods
# exist.
# ---------------------------------------------------------------------------


class TokenStore(abc.ABC):
    """Persists and refreshes ``KiroToken``s across all gateway instances."""

    @abc.abstractmethod
    async def get(self, account_id: str) -> Optional[KiroToken]:
        """Return the current token for ``account_id``, or ``None`` if none is
        stored yet.

        Implementations MUST return the latest persisted value (no stale
        in-memory copies from a previous process). Callers rely on this to
        detect "another instance already refreshed for me" fast-paths.
        """

    @abc.abstractmethod
    async def save(self, account_id: str, token: KiroToken) -> None:
        """Persist ``token`` for ``account_id``, replacing any prior value.

        Implementations MUST be atomic against concurrent readers (no torn
        writes). For file-backed impls that means tmp+rename; for Redis it's
        free since ``HSET`` is atomic.
        """

    @abc.abstractmethod
    def refresh_lock(self, account_id: str) -> AsyncContextManager[bool]:
        """Return an async context manager that yields ``True`` iff the caller
        acquired the distributed refresh lock for ``account_id``.

        Contract:
            async with store.refresh_lock(aid) as acquired:
                if acquired:
                    # exclusive: we are the only process refreshing this
                    # account. Re-read the latest token first (a peer may
                    # have refreshed while we were waiting for the lock),
                    # decide if refresh is still needed, call upstream,
                    # then save().
                    ...
                else:
                    # someone else is refreshing. Sleep briefly, re-read the
                    # token, and hope it's fresh now. If it's still stale,
                    # loop.
                    ...

        The lock MUST auto-expire (recommended: 30s) so a crashed process can
        never permanently block refreshes.

        For the file backend this collapses to an ``asyncio.Lock`` per
        account_id (equivalent to today's behaviour). For the Redis backend
        this becomes ``SET NX PX 30000`` + Lua-based delete-if-mine on release.
        """


# ---------------------------------------------------------------------------
# AccountRegistry — replaces credentials.json plus the "which accounts exist"
# half of state.json.
# ---------------------------------------------------------------------------


class AccountRegistry(abc.ABC):
    """The authoritative list of Kiro accounts the gateway knows about.

    Today this lives in ``data/credentials.json`` with each entry pointing at a
    per-account file that owns the actual refresh token. Phase 1 flattens that
    indirection: an ``AccountRecord`` is metadata only, and the token lives
    inside the ``TokenStore`` above.

    Admin add/remove endpoints (``account_manager.add_account`` and
    ``remove_account``) call into these methods. They currently do read-then-
    write on the JSON file — the Postgres impl will replace that with proper
    ``INSERT`` / ``DELETE`` under a transaction, ending the concurrent-admin
    race condition described in the Phase 1.1 report.
    """

    @abc.abstractmethod
    async def list_accounts(self) -> list[AccountRecord]:
        """Return every account, in a stable order."""

    @abc.abstractmethod
    async def get_account(self, account_id: str) -> Optional[AccountRecord]:
        """Fetch one account by id, or ``None``."""

    @abc.abstractmethod
    async def add_account(self, record: AccountRecord, initial_token: KiroToken) -> None:
        """Atomically add ``record`` and store its initial token.

        MUST be atomic: either both the metadata and the token are persisted,
        or neither is. This prevents the current failure mode where an admin
        request writes a token file, then crashes before appending to
        credentials.json, leaving an orphan file.
        """

    @abc.abstractmethod
    async def remove_account(self, account_id: str) -> None:
        """Atomically remove ``record`` and delete its stored token."""

    @abc.abstractmethod
    async def set_disabled(self, account_id: str, disabled: bool) -> None:
        """Toggle the ``disabled`` flag. Persists immediately (no periodic
        flush) so a disable takes effect on every peer instance right away."""


# ---------------------------------------------------------------------------
# UsageStore — replaces token_usage.db + request_logs.
#
# The two tables currently share a file but are otherwise independent, so we
# keep them under one facade for now. Read/write patterns are: writes on the
# request hot path (fire-and-forget in the non-stream case, awaited in the
# stream case), reads only from the admin dashboard.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UsageRecord:
    """One row for the ``token_usage`` table."""

    model: str
    api_type: str  # "openai" | "anthropic"
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    account_id: Optional[str]
    request_id: Optional[str]


@dataclass(frozen=True)
class RequestLogEntry:
    """One row for the ``request_logs`` table."""

    model: str
    api_type: str
    streaming: bool
    status: str  # "success" | "error"
    status_code: int
    duration_ms: int
    prompt_tokens: int
    completion_tokens: int
    account_id: Optional[str]
    error_message: Optional[str]
    request_id: Optional[str]
    request_body: Optional[str]
    response_body: Optional[str]


class UsageStore(abc.ABC):
    """Records per-request token usage and full request/response logs.

    Reads are only used by the admin dashboard; writes are on the hot path.
    Implementations MUST make writes cheap (single INSERT, no transactions
    spanning multiple rows) because they run inside every completed request.
    """

    # --- writes (hot path) ---

    @abc.abstractmethod
    async def record_usage(self, entry: UsageRecord) -> None: ...

    @abc.abstractmethod
    async def record_request(self, entry: RequestLogEntry) -> None: ...

    # --- reads (admin dashboard only) ---

    @abc.abstractmethod
    async def usage_summary(self, days: int = 30) -> dict[str, Any]:
        """Aggregate: total tokens/requests over the last ``days`` days."""

    @abc.abstractmethod
    async def usage_daily(self, days: int = 30) -> list[dict[str, Any]]:
        """Per-day breakdown of tokens for the last ``days`` days."""

    @abc.abstractmethod
    async def usage_by_model(self, days: int = 30) -> list[dict[str, Any]]:
        """Per-model breakdown of tokens for the last ``days`` days."""

    @abc.abstractmethod
    async def request_history(
        self,
        page: int = 1,
        page_size: int = 50,
        model: str = "",
        status: str = "",
        days: int = 7,
    ) -> dict[str, Any]:
        """Paginated request logs with optional model/status filter over the
        last ``days`` days. Signature mirrors ``RequestLogger.query`` so admin
        routes can call through without changing their pagination code."""

    @abc.abstractmethod
    async def request_by_id(self, log_id: int) -> Optional[dict[str, Any]]:
        """Fetch one request log row by its primary-key ``id`` (int).

        Note: this is the SQLite/Postgres auto-increment id, not the caller-
        supplied ``request_id`` string — that's a separate indexed field.
        Matches the current ``RequestLogger.get_by_id`` semantics.
        """

    @abc.abstractmethod
    async def request_stats(self, days: int = 7) -> dict[str, Any]:
        """Aggregate: success/error counts, average latency, etc. Default
        window is 7 days to match the pre-refactor behaviour."""


# ---------------------------------------------------------------------------
# AdminKeyStore — replaces api_key.txt.
#
# Small but important: the current process-local cache means rotating the key
# via the admin API only affects the process that served that request. Every
# other worker keeps accepting the previous key until it's restarted. The
# Secrets Manager impl fixes that by having each worker read (with a small
# in-memory TTL) from a shared source of truth.
# ---------------------------------------------------------------------------


class AdminKeyStore(abc.ABC):
    """Holds the gateway's own admin API key (the one clients send in
    ``Authorization``, not the Kiro upstream tokens)."""

    @abc.abstractmethod
    async def get(self) -> str:
        """Return the current admin API key. Callers cache this behind a
        short-lived (e.g. 30s) in-memory memoisation, so a rotation propagates
        to every worker within one memoisation window."""

    @abc.abstractmethod
    async def rotate(self, new_key: str) -> None:
        """Persist ``new_key`` as the new admin key. Every process reads the
        new value on its next ``get()`` after the cache TTL."""


# ---------------------------------------------------------------------------
# Convenience: a container that holds all four stores.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Storage:
    """Bundle of all storage-layer components.

    The application constructs one of these in the FastAPI lifespan startup
    hook (based on ``STORAGE_BACKEND``) and passes it into whatever needs it
    (``AccountManager``, ``UsageTracker``, ``RequestLogger``, ``routes_admin``).
    Wiring the four separately would be equally fine, but bundling them here
    keeps the "swap all four at once" story easy to reason about.
    """

    tokens: TokenStore
    accounts: AccountRegistry
    usage: UsageStore
    admin_key: AdminKeyStore
