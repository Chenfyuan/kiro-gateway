# -*- coding: utf-8 -*-
"""Storage layer for kiro-gateway.

See ``kiro.storage.interfaces`` for the design rationale. This ``__init__``
just re-exports the public surface so callers can do:

    from kiro.storage import Storage, KiroToken, build_storage

instead of digging into the submodules.
"""

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
from kiro.storage.factory import build_storage

__all__ = [
    "AccountRecord",
    "AccountRegistry",
    "AdminKeyStore",
    "KiroToken",
    "RequestLogEntry",
    "Storage",
    "TokenStore",
    "UsageRecord",
    "UsageStore",
    "build_storage",
]
