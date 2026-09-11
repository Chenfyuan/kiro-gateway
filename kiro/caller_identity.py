# -*- coding: utf-8 -*-
"""
Caller identity resolution shared by the OpenAI and Anthropic auth paths.

Both endpoints accept the same two kinds of credential, so the matching rules
live here rather than being duplicated (and drifting) in each router:

  1. a per-user key from ProxyKeyStore -> attributed to that user
  2. the legacy global PROXY_API_KEY   -> attributed to the unassigned bucket

Order matters: per-user keys are checked first so that a user key which happens
to equal the global key still resolves to its owner.
"""

from typing import Optional

from fastapi import Request

from kiro.config import get_proxy_api_key
from kiro.proxy_keys import UNASSIGNED_USER_ID, UNASSIGNED_USER_NAME


def resolve_caller(request: Optional[Request], *candidates: Optional[str]) -> Optional[dict]:
    """
    Resolve the first credential that authenticates, and stash it on the request.

    Returns the identity dict, or None when no candidate is valid - callers turn
    that into their own 401 shape (OpenAI and Anthropic differ).

    The identity is written to request.state so the logging code downstream can
    read it without threading an extra argument through every function.
    """
    store = None
    if request is not None:
        store = getattr(request.app.state, "proxy_key_store", None)

    global_key = get_proxy_api_key()

    for cred in candidates:
        if not cred:
            continue

        if store is not None:
            identity = store.resolve(cred)
            if identity:
                _attach(request, identity, cred)
                return identity

        if cred == global_key:
            identity = {"user_id": UNASSIGNED_USER_ID, "user_name": UNASSIGNED_USER_NAME}
            _attach(request, identity, cred)
            return identity

    return None


def _attach(request: Optional[Request], identity: dict, api_key: str) -> None:
    if request is None:
        return
    request.state.caller_user_id = identity["user_id"]
    request.state.caller_user_name = identity["user_name"]
    request.state.caller_api_key = api_key


def caller_of(request: Optional[Request]) -> dict:
    """
    Read the identity attached during authentication.

    Falls back to empty strings so a code path that was never authenticated
    (or a unit test constructing a bare request) logs an unknown caller instead
    of raising.
    """
    if request is None:
        return {"user_id": "", "user_name": ""}
    return {
        "user_id": getattr(request.state, "caller_user_id", "") or "",
        "user_name": getattr(request.state, "caller_user_name", "") or "",
    }
