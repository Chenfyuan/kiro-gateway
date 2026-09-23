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


@router.post("/accounts/{account_id:path}/refresh-quota")
async def refresh_account_quota(request: Request, account_id: str, authorization: str = Header(None)):
    _verify_admin_auth(authorization)
    account_manager = request.app.state.account_manager
    account = account_manager._accounts.get(account_id)
    if not account:
        raise HTTPException(status_code=404, detail=f"Account not found: {account_id}")
    if not account.auth_manager:
        raise HTTPException(status_code=400, detail="Account not initialized")
    await account_manager._fetch_usage_limits(account)
    await account_manager._save_state()
    return {"status": "ok", "account": account_manager.get_account_info(account_id)}


@router.get("/accounts/{account_id:path}/export")
async def export_account(request: Request, account_id: str, authorization: str = Header(None)):
    """Export one account's credentials as a JSON blob.

    The response body is a plain JSON object matching the `type=json` shape of
    POST /accounts — refresh_token, profile_arn, region, expires_at etc — so an
    operator can back a gateway up and reimport into another gateway in one
    click. Only fields present on the auth manager are emitted; keys missing
    from the source are omitted (not written as null).

    ⚠️ The response contains plaintext credentials capable of using the
    account's monthly quota. Bearer auth on this endpoint restricts it to
    operators; the caller is responsible for not leaving the download around.
    """
    _verify_admin_auth(authorization)
    account_manager = request.app.state.account_manager
    account = account_manager._accounts.get(account_id)
    if not account:
        raise HTTPException(status_code=404, detail=f"Account not found: {account_id}")

    if account.auth_manager is None:
        # 兜底：懒加载以拿到 refresh token 等字段。init 失败就 502 —
        # 大概率是这个凭证已经废了、导出也没意义。
        success = await account_manager._initialize_account(account_id)
        if not success or account.auth_manager is None:
            raise HTTPException(status_code=502, detail="Account initialization failed; nothing to export")

    am = account.auth_manager
    payload: dict = {}
    # 属性名是 auth manager 的内部 _foo；导出格式用 camelCase 与 POST /accounts
    # 那份 credentials schema 对齐（见 auth.py: _load_credentials_from_file）。
    if getattr(am, "_refresh_token", None):
        payload["refreshToken"] = am._refresh_token
    if getattr(am, "_access_token", None):
        payload["accessToken"] = am._access_token
    if getattr(am, "_profile_arn", None):
        payload["profileArn"] = am._profile_arn
    region = getattr(am, "_sso_region", None) or getattr(am, "_detected_api_region", None)
    if region:
        payload["region"] = region
    if getattr(am, "_expires_at", None):
        # datetime → ISO-8601（Z 结尾），跟 load 端支持的格式一致
        exp = am._expires_at
        try:
            payload["expiresAt"] = exp.isoformat().replace("+00:00", "Z")
        except Exception:
            pass
    if getattr(am, "_client_id", None):
        payload["clientId"] = am._client_id
    if getattr(am, "_client_secret", None):
        payload["clientSecret"] = am._client_secret
    if getattr(am, "_client_id_hash", None):
        payload["clientIdHash"] = am._client_id_hash

    if "refreshToken" not in payload:
        raise HTTPException(status_code=500, detail="No refresh token available; account cannot be exported")

    return payload


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


class DispatchConfigRequest(BaseModel):
    load_balance_mode: str = Field(description="'round_robin' or 'sticky'")


@router.get("/dispatch-config")
async def get_dispatch_config(request: Request, authorization: str = Header(None)):
    """Get current dispatch configuration."""
    _verify_admin_auth(authorization)
    account_manager = request.app.state.account_manager
    return {"load_balance_mode": account_manager._load_balance_mode}


@router.post("/dispatch-config")
async def set_dispatch_config(request: Request, body: DispatchConfigRequest, authorization: str = Header(None)):
    """Update dispatch configuration."""
    _verify_admin_auth(authorization)
    allowed = {"round_robin", "sticky"}
    if body.load_balance_mode not in allowed:
        raise HTTPException(status_code=400, detail=f"Invalid mode. Allowed: {allowed}")
    account_manager = request.app.state.account_manager
    account_manager._load_balance_mode = body.load_balance_mode
    account_manager._dirty = True
    logger.info(f"Load balance mode changed to: {body.load_balance_mode}")
    return {"load_balance_mode": account_manager._load_balance_mode}


class CircuitConfigRequest(BaseModel):
    recovery_timeout: int = Field(description="Base recovery timeout in seconds (e.g. 60)")
    max_backoff_multiplier: float = Field(description="Max backoff multiplier (e.g. 1440 = 24h cap)")
    probabilistic_retry_chance: float = Field(description="Probabilistic retry chance 0.0-1.0")
    quota_threshold: float = Field(description="Quota exhaustion threshold 0.0-1.0")


@router.get("/circuit-config")
async def get_circuit_config(request: Request, authorization: str = Header(None)):
    """Get current circuit breaker configuration."""
    _verify_admin_auth(authorization)
    am = request.app.state.account_manager
    return {
        "recovery_timeout": am._recovery_timeout,
        "max_backoff_multiplier": am._max_backoff_multiplier,
        "probabilistic_retry_chance": am._probabilistic_retry_chance,
        "quota_threshold": am._quota_threshold,
    }


@router.post("/circuit-config")
async def set_circuit_config(request: Request, body: CircuitConfigRequest, authorization: str = Header(None)):
    """Update circuit breaker configuration at runtime."""
    _verify_admin_auth(authorization)
    if not (0 <= body.probabilistic_retry_chance <= 1):
        raise HTTPException(status_code=400, detail="probabilistic_retry_chance must be 0.0-1.0")
    if not (0 < body.quota_threshold <= 1):
        raise HTTPException(status_code=400, detail="quota_threshold must be 0.0-1.0")
    if body.recovery_timeout <= 0:
        raise HTTPException(status_code=400, detail="recovery_timeout must be > 0")
    am = request.app.state.account_manager
    am._recovery_timeout = body.recovery_timeout
    am._max_backoff_multiplier = body.max_backoff_multiplier
    am._probabilistic_retry_chance = body.probabilistic_retry_chance
    am._quota_threshold = body.quota_threshold
    am._dirty = True
    logger.info(f"Circuit breaker config updated: {body.dict()}")
    return body.dict()


class NicknameRequest(BaseModel):
    account_id: str = Field(description="Account ID")
    nickname: Optional[str] = Field(default=None, description="Custom nickname, null to clear")


@router.post("/accounts/set-nickname")
async def set_account_nickname(request: Request, body: NicknameRequest, authorization: str = Header(None)):
    """Set or clear a custom nickname for an account."""
    _verify_admin_auth(authorization)
    am = request.app.state.account_manager
    account = am._accounts.get(body.account_id)
    if not account:
        raise HTTPException(status_code=404, detail=f"Account not found: {body.account_id}")
    account.nickname = body.nickname or None
    am._dirty = True
    return {"id": body.account_id, "nickname": account.nickname}


class TestCallRequest(BaseModel):
    model: str = Field(default="claude-sonnet-4-5", description="Model name to test")
    prompt: str = Field(default="Hello! Respond in one sentence.", description="Prompt to send")
    max_tokens: int = Field(default=256, description="Max tokens for response")


@router.post("/test-call")
async def test_model_call(request: Request, body: TestCallRequest, authorization: str = Header(None)):
    """Make a test model call through the load balancer."""
    _verify_admin_auth(authorization)

    import time
    start = time.time()

    api_key = get_proxy_api_key()
    payload = {
        "model": body.model,
        "max_tokens": body.max_tokens,
        "messages": [{"role": "user", "content": body.prompt}],
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "http://127.0.0.1:8000/v1/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )

    elapsed = round((time.time() - start) * 1000)
    if resp.status_code != 200:
        try:
            err = resp.json()
            detail = err.get("error", {}).get("message") or resp.text
        except Exception:
            detail = resp.text
        raise HTTPException(status_code=resp.status_code, detail=f"Model call failed: {detail}")

    data = resp.json()
    content = ""
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        content = str(data)

    account_id = data.get("x_account_id") or resp.headers.get("x-account-id") or "unknown"

    return {
        "response": content,
        "model": data.get("model", body.model),
        "account_id": account_id,
        "response_time_ms": elapsed,
        "usage": data.get("usage"),
    }


@router.post("/accounts/{account_id:path}/test-connection")
async def test_account_connection(request: Request, account_id: str, authorization: str = Header(None)):
    """Test if an account's credentials are still valid by calling getUsageLimits."""
    _verify_admin_auth(authorization)
    account_manager = request.app.state.account_manager

    account = account_manager._accounts.get(account_id)
    if not account:
        raise HTTPException(status_code=404, detail=f"Account not found: {account_id}")

    import time
    import httpx
    from kiro.utils import get_kiro_headers

    if not account.auth_manager:
        success = await account_manager._initialize_account(account_id)
        if not success or not account.auth_manager:
            return {"status": "error", "message": "Account initialization failed", "connected": False}

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
        token_payload = {
            "grantType": "authorization_code",
            "code": code,
            "redirectUri": _SSO_REDIRECT_URI,
            "clientId": session["client_id"],
            "codeVerifier": session["code_verifier"],
        }
        if session.get("client_secret"):
            token_payload["clientSecret"] = session["client_secret"]
        resp = await client.post(
            f"https://oidc.{region}.amazonaws.com/token",
            json=token_payload,
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


# ─── Per-User Usage Endpoints ───────────────────────────────────────────────
#
# These read request_logs (not the token_usage table) because only request_logs
# carries the caller's identity. Requests logged before per-user keys existed
# appear under user_id "__unknown__"; traffic using the legacy global
# PROXY_API_KEY appears under "__unassigned__".

def _require_request_logger(request: Request):
    rl = getattr(request.app.state, "request_logger", None)
    if not rl:
        raise HTTPException(status_code=503, detail="Request logger not initialized")
    return rl


@router.get("/usage/by-user")
async def usage_by_user(request: Request, days: int = 30, authorization: str = Header(None)):
    """Token totals per user, highest first."""
    _verify_admin_auth(authorization)
    rl = _require_request_logger(request)
    return {"days": days, "data": await rl.get_user_usage(days)}


@router.get("/usage/by-user/daily")
async def usage_by_user_daily(request: Request, days: int = 30, user_id: str = "",
                             authorization: str = Header(None)):
    """Per-day token totals; pass user_id to narrow to one user."""
    _verify_admin_auth(authorization)
    rl = _require_request_logger(request)
    return {"days": days, "user_id": user_id, "data": await rl.get_user_daily_usage(days, user_id)}


@router.get("/usage/by-user/models")
async def usage_by_user_models(request: Request, days: int = 30, user_id: str = "",
                              authorization: str = Header(None)):
    """Per-model token totals; pass user_id to narrow to one user."""
    _verify_admin_auth(authorization)
    rl = _require_request_logger(request)
    return {"days": days, "user_id": user_id, "data": await rl.get_user_model_usage(days, user_id)}


# ─── Per-User Proxy Key Endpoints ───────────────────────────────────────────
#
# Keys are provisioned by ai-console (which owns the user table) and pushed
# here. One key per user, enforced by a UNIQUE constraint on user_id.

def _require_key_store(request: Request):
    store = getattr(request.app.state, "proxy_key_store", None)
    if not store:
        raise HTTPException(status_code=503, detail="Proxy key store not initialized")
    return store


class UpsertProxyKeyRequest(BaseModel):
    """Payload for issuing or rotating a per-user key.

    ``api_key`` is optional. When omitted, the gateway generates one — this
    is the recommended path from the ai-console UI, because it keeps the
    plaintext-generation logic in exactly one place (here). The response
    carries the plaintext back so the admin can display it once to the user.

    When supplied by the caller (legacy path), it is trusted as-is; the store
    hashes/masks it downstream and the plaintext is never returned by list()
    or resolve(), matching the pre-existing invariant.
    """

    api_key: Optional[str] = Field(
        default=None, min_length=8,
        description="Key value; leave empty to let the server generate one",
    )
    user_id: str = Field(..., min_length=1, description="ai-console user id")
    user_name: str = Field("", description="Display name for reports")
    enabled: bool = Field(True)


@router.get("/proxy-keys")
async def list_proxy_keys(request: Request, authorization: str = Header(None)):
    """List per-user keys. Key values are masked and never returned in full."""
    _verify_admin_auth(authorization)
    store = _require_key_store(request)
    return {"data": await store.list_keys()}


@router.post("/proxy-keys")
async def upsert_proxy_key(request: Request, payload: UpsertProxyKeyRequest,
                          authorization: str = Header(None)):
    """Create or rotate the key for one user.

    Re-posting for an existing user_id replaces that user's key rather than
    adding a second one, so the one-key-per-user rule cannot be bypassed.

    Response includes the plaintext ``api_key`` — the ONLY point it leaves
    the gateway, and only in reply to a successful issue/rotate. list_keys
    and resolve never disclose it. Callers must show it to the human once
    and discard.
    """
    _verify_admin_auth(authorization)
    store = _require_key_store(request)
    # Generate server-side by default. Keeping the algorithm here means
    # ai-console never needs a secure-random dependency and stays pure UI.
    api_key = payload.api_key or _generate_api_key()
    try:
        result = await store.upsert(api_key, payload.user_id,
                                    payload.user_name, payload.enabled)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # A duplicate api_key (same value already bound to another user) lands here.
        logger.warning(f"Failed to upsert proxy key for {payload.user_id}: {e}")
        raise HTTPException(status_code=409, detail="API key already in use by another user")
    return {"status": "ok", "api_key": api_key, **result}


def _generate_api_key() -> str:
    """Return a fresh 43-char urlsafe base64 token (32 bytes of entropy)."""
    import base64, secrets
    return "sk-" + base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


@router.patch("/proxy-keys/{user_id}")
async def set_proxy_key_enabled(request: Request, user_id: str, enabled: bool,
                               authorization: str = Header(None)):
    """Enable or disable a user's key without deleting it."""
    _verify_admin_auth(authorization)
    store = _require_key_store(request)
    if not await store.set_enabled(user_id, enabled):
        raise HTTPException(status_code=404, detail=f"No key for user: {user_id}")
    return {"status": "ok", "user_id": user_id, "enabled": enabled}


@router.delete("/proxy-keys/{user_id}")
async def delete_proxy_key(request: Request, user_id: str, authorization: str = Header(None)):
    """Revoke a user's key."""
    _verify_admin_auth(authorization)
    store = _require_key_store(request)
    if not await store.delete(user_id):
        raise HTTPException(status_code=404, detail=f"No key for user: {user_id}")
    return {"status": "ok", "user_id": user_id}




@router.get("/models")
async def list_available_models(request: Request, authorization: str = Header(None)):
    """List every model the gateway can route to.

    Same source of truth as /v1/models, only authenticated with the admin key
    (proxy-services should be able to see the catalog without also needing a
    per-user key). Useful for admin UIs that want to render a dropdown when
    entering model prices, so nobody has to type ``claude-opus-5`` by hand
    and get it slightly wrong.
    """
    _verify_admin_auth(authorization)
    if request.app.state.account_system:
        models = request.app.state.account_manager.get_all_available_models()
    else:
        account = request.app.state.account_manager.get_first_account()
        models = account.model_resolver.get_available_models()
    return {"data": sorted(models)}


# ─── Model Pricing Endpoints ────────────────────────────────────────────────
# Runtime pricing overrides. Ships with a hardcoded DEFAULT_PRICING covering
# the models that existed at first release; new model names appear as
# ``source: default`` if they hit the partial-match rule, otherwise as $0.00
# in the report. The admin UI on the per-user usage page lets an operator
# add rates for those (and override existing ones). Writes are picked up on
# the next request without a restart - see kiro.model_pricing for details.

class UpsertModelPricingRequest(BaseModel):
    model: str = Field(..., min_length=1, description="Model identifier as it appears in requests")
    input_price: float = Field(..., ge=0, description="USD per 1M input tokens")
    output_price: float = Field(..., ge=0, description="USD per 1M output tokens")
    currency: str = Field("USD", description="Always USD in the current build; the field exists so a future CNY layer can be added without breaking the wire format.")


@router.get("/model-pricing")
async def list_model_pricing(authorization: str = Header(None)):
    """Return every override plus the built-in defaults not shadowed by one."""
    _verify_admin_auth(authorization)
    from kiro.model_pricing import list_pricing
    return {"data": list_pricing()}


@router.post("/model-pricing")
async def upsert_model_pricing(payload: UpsertModelPricingRequest,
                               authorization: str = Header(None)):
    """Create or update the rate for one model.

    Re-posting the same model name replaces the row (there is at most one
    entry per model, matching how the report engine looks it up).
    """
    _verify_admin_auth(authorization)
    from kiro.model_pricing import upsert_pricing
    try:
        result = upsert_pricing(
            payload.model, payload.input_price,
            payload.output_price, payload.currency,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"status": "ok", **result}


@router.delete("/model-pricing/{model:path}")
async def delete_model_pricing(model: str, authorization: str = Header(None)):
    """Remove an override; get_cost falls back to DEFAULT_PRICING for that model.

    ``:path`` converter so model names with slashes (unusual but not forbidden
    by the request path) still route correctly.
    """
    _verify_admin_auth(authorization)
    from kiro.model_pricing import delete_pricing
    try:
        removed = delete_pricing(model)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    if not removed:
        raise HTTPException(status_code=404, detail=f"No override for model: {model}")
    return {"status": "ok", "model": model}


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


@router.post("/accounts/health-check")
async def trigger_health_check(request: Request, authorization: str = Header(None)):
    """Manually trigger health check for all accounts."""
    _verify_admin_auth(authorization)
    account_manager = request.app.state.account_manager
    await account_manager.health_check_all()
    results = []
    async with account_manager._lock:
        for account_id, account in account_manager._accounts.items():
            results.append({
                "id": account_id,
                "status": account.last_health_status,
                "checked_at": account.last_health_check_at,
            })
    return {"checked": len(results), "results": results}
