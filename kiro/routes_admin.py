# -*- coding: utf-8 -*-

"""
Admin API for managing Kiro Gateway accounts at runtime.

Provides endpoints to list, add, and remove accounts without restarting the gateway.
Authentication uses the same PROXY_API_KEY as the main API.
"""

import base64
import hashlib
import json
import secrets
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, urlparse, parse_qs

import httpx
from fastapi import APIRouter, HTTPException, Request, Header
from loguru import logger
from pydantic import BaseModel, Field

from kiro.config import get_proxy_api_key, set_proxy_api_key

router = APIRouter(prefix="/admin", tags=["admin"])

# In-memory SSO session store: session_id -> session dict
_sso_sessions: dict = {}


def _verify_admin_auth(authorization: Optional[str]) -> None:
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or parts[1] != get_proxy_api_key():
        raise HTTPException(status_code=403, detail="Invalid API key")


class AddAccountRequest(BaseModel):
    type: str = Field(description="'json' for raw credentials, 'kiro_export' for Kiro IDE export format")
    credentials: Optional[dict] = Field(default=None, description="Raw credentials (for type=json)")
    data: Optional[dict] = Field(default=None, description="Full Kiro IDE export JSON (for type=kiro_export)")


class UpdateAccountRequest(BaseModel):
    disabled: Optional[bool] = Field(default=None, description="Set to true to disable, false to enable")


_SSO_SCOPES = [
    "codewhisperer:completions",
    "codewhisperer:analysis",
    "codewhisperer:conversations",
    "codewhisperer:transformations",
    "codewhisperer:taskassist",
]
_SSO_REDIRECT_URI = "http://127.0.0.1/oauth/callback"


def _pkce_verifier() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b'=').decode()


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b'=').decode()


class SSOInitRequest(BaseModel):
    start_url: str = Field(description="IAM Identity Center start URL, e.g. https://xxx.awsapps.com/start")
    region: str = Field(default="us-east-1", description="AWS region of IAM Identity Center")


class SSOCompleteRequest(BaseModel):
    session_id: str
    callback_url: str = Field(description="The full callback URL after browser redirect, e.g. http://127.0.0.1/oauth/callback?code=...")


@router.get("/accounts")
async def list_accounts(request: Request, authorization: str = Header(None)):
    _verify_admin_auth(authorization)
    account_manager = request.app.state.account_manager
    accounts_info = account_manager.list_accounts_info()
    return {
        "accounts": accounts_info,
        "total": len(accounts_info),
        "account_system": getattr(request.app.state, "account_system", False),
    }


@router.post("/accounts/refresh-quotas")
async def refresh_quotas(request: Request, authorization: str = Header(None)):
    _verify_admin_auth(authorization)
    account_manager = request.app.state.account_manager
    await account_manager.refresh_all_quotas()
    return {"status": "ok"}


@router.get("/accounts/{account_id:path}")
async def get_account(request: Request, account_id: str, authorization: str = Header(None)):
    _verify_admin_auth(authorization)
    account_manager = request.app.state.account_manager
    info = account_manager.get_account_info(account_id)
    if not info:
        raise HTTPException(status_code=404, detail=f"Account not found: {account_id}")
    return info


@router.post("/accounts")
async def add_account(request: Request, body: AddAccountRequest, authorization: str = Header(None)):
    _verify_admin_auth(authorization)
    account_manager = request.app.state.account_manager

    if body.type == "kiro_export":
        if not body.data:
            raise HTTPException(status_code=400, detail="'data' field required for kiro_export type")
        creds = _parse_kiro_export(body.data)
    elif body.type == "json":
        if not body.credentials:
            raise HTTPException(status_code=400, detail="'credentials' field required for json type")
        creds = body.credentials
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported type: {body.type}. Use 'json' or 'kiro_export'")

    if not creds.get("refreshToken"):
        raise HTTPException(status_code=400, detail="credentials must contain 'refreshToken'")

    try:
        account_id = await account_manager.add_account(creds)
        info = account_manager.get_account_info(account_id)
        return {"status": "ok", "account_id": account_id, "account": info}
    except Exception as e:
        logger.error(f"Failed to add account: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/accounts/{account_id:path}")
async def update_account(request: Request, account_id: str, body: UpdateAccountRequest, authorization: str = Header(None)):
    _verify_admin_auth(authorization)
    account_manager = request.app.state.account_manager

    if body.disabled is not None:
        ok = await account_manager.set_account_disabled(account_id, body.disabled)
        if not ok:
            raise HTTPException(status_code=404, detail=f"Account not found: {account_id}")

    info = account_manager.get_account_info(account_id)
    if not info:
        raise HTTPException(status_code=404, detail=f"Account not found: {account_id}")
    return {"status": "ok", "account": info}


@router.post("/accounts/{account_id:path}/reset-circuit")
async def reset_circuit_breaker(request: Request, account_id: str, authorization: str = Header(None)):
    """Reset circuit breaker for an account (clear failures and cooldown)."""
    _verify_admin_auth(authorization)
    account_manager = request.app.state.account_manager

    account = account_manager._accounts.get(account_id)
    if not account:
        raise HTTPException(status_code=404, detail=f"Account not found: {account_id}")

    account.failures = 0
    account.last_failure_time = 0.0
    account_manager._dirty = True
    logger.info(f"Admin API: Reset circuit breaker for {account_id}")

    info = account_manager.get_account_info(account_id)
    return {"status": "ok", "account": info}


@router.post("/accounts/{account_id:path}/set-sticky")
async def set_sticky_account(request: Request, account_id: str, authorization: str = Header(None)):
    """Manually set an account as the sticky (priority) account."""
    _verify_admin_auth(authorization)
    account_manager = request.app.state.account_manager

    all_account_ids = list(account_manager._accounts.keys())
    if account_id not in all_account_ids:
        raise HTTPException(status_code=404, detail=f"Account not found: {account_id}")

    target_index = all_account_ids.index(account_id)
    account_manager._current_account_index = target_index
    account_manager._dirty = True
    logger.info(f"Admin API: Set sticky account to {account_id} (index={target_index})")

    return {"status": "ok", "sticky_account_id": account_id}


@router.post("/accounts/{account_id:path}/test-connection")
async def test_account_connection(request: Request, account_id: str, authorization: str = Header(None)):
    """Test if an account's credentials are still valid by calling getUsageLimits."""
    _verify_admin_auth(authorization)
    account_manager = request.app.state.account_manager

    account = account_manager._accounts.get(account_id)
    if not account:
        raise HTTPException(status_code=404, detail=f"Account not found: {account_id}")

    if not account.auth_manager:
        return {"status": "error", "message": "Account not initialized", "connected": False}

    import time
    import httpx
    from kiro.utils import get_kiro_headers

    start_time = time.time()
    try:
        url = f"https://q.{account.auth_manager.api_region}.amazonaws.com/getUsageLimits?origin=AI_EDITOR&resourceType=AGENTIC_REQUEST&isEmailRequired=true"
        token = await account.auth_manager.get_access_token()
        headers = get_kiro_headers(account.auth_manager, token)

        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url, headers=headers)
            elapsed = round((time.time() - start_time) * 1000)

            if response.status_code == 200:
                return {"status": "ok", "connected": True, "response_time_ms": elapsed, "message": "凭证有效"}
            else:
                return {"status": "error", "connected": False, "response_time_ms": elapsed, "message": f"HTTP {response.status_code}"}
    except Exception as e:
        elapsed = round((time.time() - start_time) * 1000)
        return {"status": "error", "connected": False, "response_time_ms": elapsed, "message": str(e)}


@router.delete("/accounts/{account_id:path}")
async def remove_account(request: Request, account_id: str, authorization: str = Header(None)):
    _verify_admin_auth(authorization)
    account_manager = request.app.state.account_manager

    removed = await account_manager.remove_account(account_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Account not found: {account_id}")
    return {"status": "ok", "removed": account_id}



@router.post("/accounts/sso/start")
async def sso_start(request: Request, body: SSOInitRequest, authorization: str = Header(None)):
    """Start IAM Identity Center OAuth Authorization Code + PKCE flow. Returns auth URL."""
    _verify_admin_auth(authorization)

    region = body.region

    # Register public OIDC client
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"https://oidc.{region}.amazonaws.com/client/register",
            json={
                "clientName": "Kiro",
                "clientType": "public",
                "scopes": _SSO_SCOPES,
                "grantTypes": ["authorization_code", "refresh_token"],
                "redirectUris": [_SSO_REDIRECT_URI],
                "issuerUrl": body.start_url,
            },
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"RegisterClient failed: {resp.text}")
        reg = resp.json()

    client_id = reg["clientId"]
    client_secret = reg.get("clientSecret", "")

    verifier = _pkce_verifier()
    challenge = _pkce_challenge(verifier)
    state = secrets.token_urlsafe(16)

    params = {
        "client_id": client_id,
        "response_type": "code",
        "scopes": ",".join(_SSO_SCOPES),
        "redirect_uri": _SSO_REDIRECT_URI,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    auth_url = f"https://oidc.{region}.amazonaws.com/authorize?" + urlencode(params)

    session_id = uuid.uuid4().hex
    _sso_sessions[session_id] = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code_verifier": verifier,
        "state": state,
        "region": region,
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
    }

    logger.info(f"SSO start: session={session_id}, region={region}")
    return {"session_id": session_id, "auth_url": auth_url}


@router.post("/accounts/sso/complete")
async def sso_complete(request: Request, body: SSOCompleteRequest, authorization: str = Header(None)):
    """Complete IAM Identity Center OAuth flow by exchanging the authorization code."""
    _verify_admin_auth(authorization)

    session = _sso_sessions.get(body.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="SSO session not found or expired")

    if datetime.now(timezone.utc) >= datetime.fromisoformat(session["expires_at"]):
        del _sso_sessions[body.session_id]
        raise HTTPException(status_code=410, detail="SSO session expired")

    parsed = urlparse(body.callback_url)
    qs = parse_qs(parsed.query)
    code = (qs.get("code") or [None])[0]
    if not code:
        raise HTTPException(status_code=400, detail="No authorization code found in callback URL")

    region = session["region"]

    async with httpx.AsyncClient(timeout=30) as client:
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _SSO_REDIRECT_URI,
            "client_id": session["client_id"],
            "code_verifier": session["code_verifier"],
        }

        resp = await client.post(
            f"https://oidc.{region}.amazonaws.com/token",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    if resp.status_code != 200:
        try:
            err = resp.json()
            detail = err.get("error_description") or err.get("message") or err.get("error") or resp.text
        except Exception:
            detail = resp.text
        logger.error(f"Token exchange failed: {resp.status_code} {resp.text}")
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {detail}")

    token = resp.json()
    credentials = {
        "accessToken": token.get("access_token") or token.get("accessToken"),
        "refreshToken": token.get("refresh_token") or token.get("refreshToken"),
        "clientId": session["client_id"],
        "clientSecret": session["client_secret"],
        "region": region,
    }
    del _sso_sessions[body.session_id]

    account_manager = request.app.state.account_manager
    try:
        account_id = await account_manager.add_account(credentials)
        info = account_manager.get_account_info(account_id)
        logger.info(f"SSO completed: account={account_id}")
        return {"status": "completed", "account_id": account_id, "account": info}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create account: {e}")


def _parse_kiro_export(data: dict) -> dict:
    """Parse Kiro IDE export format into gateway credentials."""
    accounts = data.get("accounts", [])
    if not accounts:
        raise HTTPException(status_code=400, detail="No accounts found in export data")

    account = accounts[0]
    creds = account.get("credentials", {})

    result = {}
    if creds.get("accessToken"):
        result["accessToken"] = creds["accessToken"]
    if creds.get("refreshToken"):
        result["refreshToken"] = creds["refreshToken"]
    if creds.get("clientId"):
        result["clientId"] = creds["clientId"]
    if creds.get("clientSecret"):
        result["clientSecret"] = creds["clientSecret"]
    if creds.get("region"):
        result["region"] = creds["region"]

    return result


# ─── Usage Statistics Endpoints ─────────────────────────────────────────────

@router.get("/usage/summary")
async def usage_summary(request: Request, days: int = 30, authorization: str = Header(None)):
    _verify_admin_auth(authorization)
    tracker = getattr(request.app.state, "usage_tracker", None)
    if not tracker:
        raise HTTPException(status_code=503, detail="Usage tracker not initialized")
    return await tracker.get_summary(days)


@router.get("/usage/daily")
async def usage_daily(request: Request, days: int = 30, authorization: str = Header(None)):
    _verify_admin_auth(authorization)
    tracker = getattr(request.app.state, "usage_tracker", None)
    if not tracker:
        raise HTTPException(status_code=503, detail="Usage tracker not initialized")
    return {"days": days, "data": await tracker.get_daily_stats(days)}


@router.get("/usage/by-model")
async def usage_by_model(request: Request, days: int = 30, authorization: str = Header(None)):
    _verify_admin_auth(authorization)
    tracker = getattr(request.app.state, "usage_tracker", None)
    if not tracker:
        raise HTTPException(status_code=503, detail="Usage tracker not initialized")
    return {"days": days, "data": await tracker.get_model_stats(days)}


# ─── Request Logs Endpoints ─────────────────────────────────────────────────

@router.get("/logs")
async def get_logs(
    request: Request,
    page: int = 1,
    page_size: int = 50,
    model: str = "",
    status: str = "",
    days: int = 7,
    authorization: str = Header(None),
):
    _verify_admin_auth(authorization)
    req_logger = getattr(request.app.state, "request_logger", None)
    if not req_logger:
        raise HTTPException(status_code=503, detail="Request logger not initialized")
    return await req_logger.query(page=page, page_size=page_size, model=model, status=status, days=days)


@router.get("/logs/stats")
async def get_logs_stats(request: Request, days: int = 7, authorization: str = Header(None)):
    _verify_admin_auth(authorization)
    req_logger = getattr(request.app.state, "request_logger", None)
    if not req_logger:
        raise HTTPException(status_code=503, detail="Request logger not initialized")
    return await req_logger.get_stats(days)


@router.get("/logs/{log_id}")
async def get_log_detail(request: Request, log_id: int, authorization: str = Header(None)):
    _verify_admin_auth(authorization)
    req_logger = getattr(request.app.state, "request_logger", None)
    if not req_logger:
        raise HTTPException(status_code=503, detail="Request logger not initialized")
    entry = await req_logger.get_by_id(log_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Log entry not found")
    return entry


@router.get("/dispatch-status")
async def dispatch_status(request: Request, authorization: str = Header(None)):
    """Get current dispatch/scheduling status for visualization."""
    _verify_admin_auth(authorization)
    account_manager = request.app.state.account_manager

    all_account_ids = list(account_manager._accounts.keys())
    current_index = account_manager._current_account_index
    sticky_account_id = all_account_ids[current_index] if all_account_ids and current_index < len(all_account_ids) else None

    import time
    from kiro.config import ACCOUNT_RECOVERY_TIMEOUT, ACCOUNT_MAX_BACKOFF_MULTIPLIER

    accounts_status = []
    for account_id, account in account_manager._accounts.items():
        cooldown_remaining = 0
        if account.failures > 0:
            backoff_multiplier = min(2 ** (account.failures - 1), ACCOUNT_MAX_BACKOFF_MULTIPLIER)
            effective_timeout = ACCOUNT_RECOVERY_TIMEOUT * backoff_multiplier
            elapsed = time.time() - account.last_failure_time
            cooldown_remaining = max(0, effective_timeout - elapsed)

        if account.disabled:
            status = "disabled"
        elif account.failures > 0:
            status = "circuit_open"
        elif account_manager._is_quota_exhausted(account):
            status = "quota_low"
        else:
            status = "healthy"

        accounts_status.append({
            "id": account_id,
            "email": account.email,
            "is_sticky": account_id == sticky_account_id,
            "status": status,
            "failures": account.failures,
            "cooldown_remaining_seconds": round(cooldown_remaining),
            "last_failure_time": account.last_failure_time,
            "current_usage": account.current_usage,
            "usage_limit": account.usage_limit,
            "stats": {
                "total": account.stats.total_requests,
                "success": account.stats.successful_requests,
                "failed": account.stats.failed_requests,
            },
        })

    healthy_count = sum(1 for a in accounts_status if a["status"] == "healthy")
    circuit_open_count = sum(1 for a in accounts_status if a["status"] == "circuit_open")
    disabled_count = sum(1 for a in accounts_status if a["status"] == "disabled")
    quota_low_count = sum(1 for a in accounts_status if a["status"] == "quota_low")

    return {
        "total_accounts": len(all_account_ids),
        "healthy": healthy_count,
        "circuit_open": circuit_open_count,
        "disabled": disabled_count,
        "quota_low": quota_low_count,
        "sticky_account_id": sticky_account_id,
        "accounts": accounts_status,
    }


class UpdateApiKeyRequest(BaseModel):
    new_key: str = Field(min_length=8, description="New API key (min 8 characters)")


@router.put("/config/api-key")
async def update_api_key(body: UpdateApiKeyRequest, authorization: str = Header(None)):
    _verify_admin_auth(authorization)
    set_proxy_api_key(body.new_key)
    logger.info("API key updated successfully")
    return {"status": "ok", "message": "API key updated. Use the new key for subsequent requests."}
