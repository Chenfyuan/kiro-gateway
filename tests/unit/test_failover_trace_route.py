# -*- coding: utf-8 -*-

"""
The real /v1/chat/completions failover loop, with the accounts and Kiro faked, to check what a
failed request leaves behind: one log line per attempt and, in the final error (which also goes
into request_logs.error_message), the path it took.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from loguru import logger

import kiro.routes_openai as routes


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = json.dumps(body).encode()

    async def aread(self):
        return self._body


def make_account(name):
    account = MagicMock()
    account.id = f"/app/data/kiro-auth-token-{name}.json"
    account.auth_manager.profile_arn = "arn:aws:codewhisperer:us-east-1:1:profile/X"
    account.auth_manager.api_host = "https://runtime.example.invalid"
    return account


class FakeManager:
    """Hands out the accounts in order, skipping the ones this request already tried."""

    def __init__(self, names):
        self.accounts = [make_account(n) for n in names]
        self._accounts = {a.id: a for a in self.accounts}
        self.report_failure = AsyncMock()
        self.report_success = AsyncMock()

    async def get_next_account(self, model, exclude_accounts=None):
        for account in self.accounts:
            if not exclude_accounts or account.id not in exclude_accounts:
                return account
        return None


def build_client(manager, responses):
    """A client for the real route with fake accounts. `responses`: what Kiro answers to each attempt, in order."""
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.verify_api_key] = lambda: True
    app.state.account_system = True
    app.state.account_manager = manager
    app.state.http_client = None
    queue = list(responses)

    class FakeHttpClient:
        def __init__(self, *a, **k):
            pass

        async def request_with_retry(self, *a, **k):
            return queue.pop(0)

        async def close(self):
            pass

    patcher = patch.object(routes, "KiroHttpClient", FakeHttpClient)
    patcher.start()
    return TestClient(app), patcher


@pytest.fixture
def client_for():
    return build_client


@pytest.fixture
def log_lines():
    lines = []
    handler = logger.add(lambda m: lines.append((m.record["level"].name, m.record["message"])), level="INFO")
    yield lines
    logger.remove(handler)


REQUEST = {"model": "claude-opus-4.8", "messages": [{"role": "user", "content": "hi"}], "stream": False}


def test_a_request_every_account_refuses_says_which_accounts_were_tried_and_what_they_answered(client_for, log_lines):
    manager = FakeManager(["aaaaaaa1", "bbbbbbb2", "ccccccc3"])
    refusals = [FakeResponse(429, {"message": "Too many requests"}), FakeResponse(402, {"message": "Monthly limit", "reason": "MONTHLY_REQUEST_COUNT"}),
                FakeResponse(403, {"message": "Forbidden"})]
    client, patcher = client_for(manager, refusals)
    try:
        response = client.post("/v1/chat/completions", json=REQUEST)
    finally:
        patcher.stop()

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail.startswith("No available accounts for this model.")
    assert "Tried 3 account(s)" in detail
    assert "aaaaaaa1=429" in detail and "bbbbbbb2=402 MONTHLY_REQUEST_COUNT" in detail and "ccccccc3=403" in detail

    attempts = [t for level, t in log_lines if " attempt " in t]
    assert len(attempts) == 3 and "attempt 1/6 account=aaaaaaa1" in attempts[0]
    assert any("no account left after 3 attempt(s)" in t for _, t in log_lines)


def test_a_success_is_logged_with_the_account_that_served_it(client_for, log_lines):
    manager = FakeManager(["aaaaaaa1", "bbbbbbb2"])
    client, patcher = client_for(manager, [FakeResponse(429, {"message": "slow down"}), FakeResponse(200, {})])
    try:
        # the 200 body is not a real Kiro event stream: the point here is only which attempts were recorded
        client.post("/v1/chat/completions", json=REQUEST)
    except Exception:
        pass  # parsing the fake success body may fail after the attempt was recorded
    finally:
        patcher.stop()

    attempts = [t for level, t in log_lines if " attempt " in t]
    assert "account=aaaaaaa1 -> 429" in attempts[0]
    assert "account=bbbbbbb2 -> 200" in attempts[1]


def test_nothing_to_try_at_all_is_reported_as_such(client_for, log_lines):
    manager = FakeManager([])
    manager._accounts = {"x": object(), "y": object()}  # two known accounts, but none can be picked
    client, patcher = client_for(manager, [])
    try:
        response = client.post("/v1/chat/completions", json=REQUEST)
    finally:
        patcher.stop()

    assert response.status_code == 503
    assert "No account was tried; picking one took" in response.json()["detail"]


# ------------------------------------------------------------------------------------------------
# Regression: the OpenAI route's error handling used to sit inside `if response.status_code == 200:`
# after both of its branches had returned, so it never ran. A Kiro answer other than 200 was
# skipped without a trace: no failure recorded, no error text kept, no circuit breaker count.
# ------------------------------------------------------------------------------------------------

def test_every_refusal_is_reported_to_the_account_manager(client_for):
    manager = FakeManager(["aaaaaaa1", "bbbbbbb2"])
    client, patcher = client_for(manager, [FakeResponse(402, {"message": "Monthly limit", "reason": "MONTHLY_REQUEST_COUNT"}),
                                           FakeResponse(429, {"message": "Too many requests"})])
    try:
        client.post("/v1/chat/completions", json=REQUEST)
    finally:
        patcher.stop()

    assert manager.report_failure.await_count == 2  # without this the circuit breaker never learns that an account is failing
    assert [c.args[3] for c in manager.report_failure.await_args_list] == [402, 429]


def test_the_final_error_carries_the_upstream_message_of_the_last_account(client_for):
    manager = FakeManager(["aaaaaaa1", "bbbbbbb2"])
    client, patcher = client_for(manager, [FakeResponse(402, {"message": "Monthly limit"}), FakeResponse(429, {"message": "Too many requests"})])
    try:
        response = client.post("/v1/chat/completions", json=REQUEST)
    finally:
        patcher.stop()

    assert response.status_code == 503
    assert "Error from last account: Too many requests" in response.json()["detail"]


def test_a_request_level_error_goes_straight_back_to_the_client(client_for):
    """400 CONTENT_LENGTH_EXCEEDS_THRESHOLD is the request's fault: no other account will do better, so it is not retried."""
    manager = FakeManager(["aaaaaaa1", "bbbbbbb2", "ccccccc3"])
    client, patcher = client_for(manager, [FakeResponse(400, {"message": "Input is too long.", "reason": "CONTENT_LENGTH_EXCEEDS_THRESHOLD"})])
    try:
        response = client.post("/v1/chat/completions", json=REQUEST)
    finally:
        patcher.stop()

    assert response.status_code == 400  # not a 503 after walking through every account
    assert manager.report_failure.await_count == 1


# ------------------------------------------------------------------------------------------------
# The Anthropic route has the same loop and gets the same trace.
# ------------------------------------------------------------------------------------------------

def test_the_anthropic_route_records_its_attempts_too(log_lines):
    import kiro.routes_anthropic as anthropic_routes

    manager = FakeManager(["aaaaaaa1", "bbbbbbb2"])
    queue = [FakeResponse(402, {"message": "Monthly limit", "reason": "MONTHLY_REQUEST_COUNT"}), FakeResponse(429, {"message": "Too many requests"})]

    class FakeHttpClient:
        def __init__(self, *a, **k):
            pass

        async def request_with_retry(self, *a, **k):
            return queue.pop(0)

        async def close(self):
            pass

    app = FastAPI()
    app.include_router(anthropic_routes.router)
    app.dependency_overrides[anthropic_routes.verify_anthropic_api_key] = lambda: True
    app.state.account_system = True
    app.state.account_manager = manager
    app.state.http_client = None
    with patch.object(anthropic_routes, "KiroHttpClient", FakeHttpClient):
        response = TestClient(app).post("/v1/messages", json={"model": "claude-opus-4.8", "max_tokens": 50, "messages": [{"role": "user", "content": "hi"}]})

    assert response.status_code == 503
    message = response.json()["error"]["message"]
    assert "Tried 2 account(s)" in message and "aaaaaaa1=402 MONTHLY_REQUEST_COUNT" in message and "bbbbbbb2=429" in message
    assert "Error from last account: Too many requests" in message
    assert manager.report_failure.await_count == 2
