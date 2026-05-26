# -*- coding: utf-8 -*-

"""
Admin API for managing Kiro Gateway accounts at runtime.

Provides endpoints to list, add, and remove accounts without restarting the gateway.
Authentication uses the same PROXY_API_KEY as the main API.
"""

import json
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Header
from loguru import logger
from pydantic import BaseModel, Field

from kiro.config import PROXY_API_KEY

router = APIRouter(prefix="/admin", tags=["admin"])


def _verify_admin_auth(authorization: Optional[str]) -> None:
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or parts[1] != PROXY_API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")


class AddAccountRequest(BaseModel):
    type: str = Field(description="'json' for raw credentials, 'kiro_export' for Kiro IDE export format")
    credentials: Optional[dict] = Field(default=None, description="Raw credentials (for type=json)")
    data: Optional[dict] = Field(default=None, description="Full Kiro IDE export JSON (for type=kiro_export)")


class UpdateAccountRequest(BaseModel):
    disabled: Optional[bool] = Field(default=None, description="Set to true to disable, false to enable")


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


@router.delete("/accounts/{account_id:path}")
async def remove_account(request: Request, account_id: str, authorization: str = Header(None)):
    _verify_admin_auth(authorization)
    account_manager = request.app.state.account_manager

    removed = await account_manager.remove_account(account_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Account not found: {account_id}")
    return {"status": "ok", "removed": account_id}


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
