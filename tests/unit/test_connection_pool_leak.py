# -*- coding: utf-8 -*-
"""Regression tests for the connection-pool leak that took xiaomei down on 2026-09-21.

A streamed response holds its pool connection until it is closed. Every retry inside
request_with_retry that abandoned a response without closing it therefore burned a
pool slot permanently; 100 of them filled max_connections=100 and every subsequent
request died on PoolTimeout. Only a restart cleared it.

These tests talk to a real asyncio TCP server on loopback through a real
httpx.AsyncClient with max_connections=1. That detail is the whole point:
httpx.MockTransport does not allocate pool connections at all, so under a
MockTransport you can leak an unlimited number of streamed responses against a pool
of 1 and every request still succeeds - the tests would pass against the unfixed
code and guard nothing. Only a genuine connection forces httpx to account for the
slot, which is what makes a leak observable as the next request timing out.
"""

import asyncio
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from kiro.auth import KiroAuthManager
from kiro.http_client import KiroHttpClient
from kiro.network_errors import ErrorCategory, classify_network_error

# conftest's block_all_network_calls patches httpx.AsyncClient globally (it patches the
# attribute on the shared httpx module, not a per-module alias), so constructing one
# here would hand us the session-wide AsyncMock. Capture the real class at import time,
# before that fixture starts. Traffic stays on 127.0.0.1 against our own server.
_RealAsyncClient = httpx.AsyncClient

POOL_TIMEOUT = 1.0


class ScriptedServer:
    """Minimal HTTP/1.1 server that replies with a scripted sequence of statuses.

    Written by hand rather than with a library because the test needs control over
    the exact bytes and, crucially, over keeping the socket open - a leaked response
    only holds a pool slot while its connection is alive.
    """

    def __init__(self, statuses):
        self._statuses = list(statuses)
        self._index = 0
        self.request_count = 0
        self._server = None

    @property
    def url(self) -> str:
        host, port = self._server.sockets[0].getsockname()[:2]
        return f"http://{host}:{port}/generateAssistantResponse"

    async def __aenter__(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()

    def _next_status(self) -> int:
        if self._index < len(self._statuses):
            status = self._statuses[self._index]
        else:
            status = self._statuses[-1]
        self._index += 1
        return status

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            while True:
                # Read request head
                head = await reader.readuntil(b"\r\n\r\n")
                if not head:
                    break
                self.request_count += 1

                # Drain the body so the connection stays in a clean state
                length = 0
                for line in head.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1].strip())
                if length:
                    await reader.readexactly(length)

                status = self._next_status()
                body = b'{"ok":true}' if status == 200 else b'{"message":"error"}'
                writer.write(
                    f"HTTP/1.1 {status} X\r\n"
                    f"Content-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    f"\r\n".encode()
                    + body
                )
                await writer.drain()
                # Deliberately do NOT close: keep-alive, so an unreleased response
                # keeps occupying its pool slot exactly as in production.
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass


@pytest.fixture
def auth_manager():
    m = Mock(spec=KiroAuthManager)
    m.get_access_token = AsyncMock(return_value="token")
    m.force_refresh = AsyncMock(return_value="token2")
    m.fingerprint = "fp-12345678"
    m._fingerprint = "fp-12345678"
    return m


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    """Retries here are about connection accounting, not timing."""
    monkeypatch.setattr("kiro.http_client.BASE_RETRY_DELAY", 0)


def single_slot_client() -> httpx.AsyncClient:
    """A client whose pool holds exactly one connection.

    With a ceiling of 1, a leaked connection is not gradual degradation - the very
    next acquisition blocks and raises PoolTimeout. That is what turns the bug into
    a failing assertion instead of something only visible under production load.
    """
    return _RealAsyncClient(
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
        timeout=httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=POOL_TIMEOUT),
    )


class TestPoolAccountingHarness:
    """Guards the harness itself: without this, a green suite proves nothing.

    The first version of these tests used httpx.MockTransport and passed against the
    unfixed code, because MockTransport never allocates a pool connection. If someone
    later swaps the real server back out for a mock, this test fails and says why.
    """

    @pytest.mark.asyncio
    async def test_a_leaked_stream_really_does_exhaust_the_pool(self, auth_manager):
        async with ScriptedServer([200]) as server:
            async with single_slot_client() as client:
                req = client.build_request("POST", server.url, content=b"{}")
                leaked = await client.send(req, stream=True)
                assert leaked.status_code == 200

                # The one slot is now held by an unclosed streamed response
                with pytest.raises(httpx.PoolTimeout):
                    await client.request("POST", server.url, content=b"{}")

                await leaked.aclose()
                # Released - the pool works again
                recovered = await client.request("POST", server.url, content=b"{}")
                assert recovered.status_code == 200


class TestRetriesDoNotLeakPoolSlots:
    @pytest.mark.asyncio
    async def test_403_retry_then_success_on_a_single_slot_pool(self, auth_manager):
        """403 -> refresh -> 200 with room for only one connection at a time.

        This is the production sequence: tokens expired on 9-19/9-20, so calls hit
        403 first. Before the fix the 403 response was abandoned unclosed and its
        slot never came back, so the retry could not acquire a connection at all.
        """
        async with ScriptedServer([403, 200]) as server:
            async with single_slot_client() as shared:
                client = KiroHttpClient(auth_manager, shared_client=shared)
                response = await client.request_with_retry(
                    "POST", server.url, {"a": 1}, stream=True
                )
                assert response.status_code == 200
                await response.aclose()

            assert server.request_count == 2
            auth_manager.force_refresh.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_many_sequential_403s_never_exhaust_the_pool(self, auth_manager):
        """The real failure mode: leaks accumulate across requests, not within one.

        xiaomei ran for hours and then collapsed, each request leaking one slot out
        of 100. Here the budget is 1, so 15 requests that each retry once would have
        died at the first retry.
        """
        async with ScriptedServer([403, 200] * 20) as server:
            async with single_slot_client() as shared:
                for i in range(15):
                    client = KiroHttpClient(auth_manager, shared_client=shared)
                    response = await client.request_with_retry(
                        "POST", server.url, {"i": i}, stream=True
                    )
                    assert response.status_code == 200, f"pool died at request {i}"
                    await response.aclose()

    @pytest.mark.asyncio
    async def test_consecutive_429s_release_the_superseded_response(self, auth_manager):
        """Repeated 429s: each is stored in last_response, replacing the previous one.

        Only the newest is handed back to the caller, so the ones it displaces have
        to be released here or nobody ever will.
        """
        async with ScriptedServer([429]) as server:
            async with single_slot_client() as shared:
                client = KiroHttpClient(auth_manager, shared_client=shared)
                response = await client.request_with_retry(
                    "POST", server.url, {"a": 1}, stream=True
                )
                # Retries exhaust and the final 429 comes back for classification
                assert response.status_code == 429
                await response.aclose()

                # Pool still usable: the superseded 429s were not stranded
                probe = await shared.request("POST", server.url, content=b"{}")
                assert probe.status_code == 429

    @pytest.mark.asyncio
    async def test_5xx_retries_leave_the_pool_usable(self, auth_manager):
        async with ScriptedServer([503]) as server:
            async with single_slot_client() as shared:
                client = KiroHttpClient(auth_manager, shared_client=shared)
                response = await client.request_with_retry(
                    "POST", server.url, {"a": 1}, stream=True
                )
                assert response.status_code == 503
                await response.aclose()

                probe = await shared.request("POST", server.url, content=b"{}")
                assert probe.status_code == 503

    @pytest.mark.asyncio
    async def test_mixed_403_and_429_sequence_stays_within_budget(self, auth_manager):
        """403 then 429 then 200: two different release paths in one call."""
        async with ScriptedServer([403, 429, 200]) as server:
            async with single_slot_client() as shared:
                client = KiroHttpClient(auth_manager, shared_client=shared)
                response = await client.request_with_retry(
                    "POST", server.url, {"a": 1}, stream=True
                )
                assert response.status_code == 200
                await response.aclose()


class TestReleaseResponse:
    @pytest.mark.asyncio
    async def test_release_tolerates_none(self):
        """Called with last_response before anything has been stored."""
        await KiroHttpClient._release_response(None)

    @pytest.mark.asyncio
    async def test_release_swallows_close_errors(self):
        """Cleanup failures must not mask the error that triggered cleanup."""
        response = Mock()
        response.aclose = AsyncMock(side_effect=RuntimeError("already detached"))
        await KiroHttpClient._release_response(response)  # must not raise


class TestPoolTimeoutClassification:
    def test_pool_timeout_gets_its_own_category(self):
        """Pool exhaustion used to read as a generic timeout.

        During the outage the surfaced message was "Request timeout - operation took
        too long", pointing at the network and the upstream - neither of which was
        involved, since the upstream was never contacted.
        """
        info = classify_network_error(httpx.PoolTimeout("pool timeout"))

        assert info.category == ErrorCategory.TIMEOUT_POOL
        assert "pool" in info.user_message.lower()
        # 503, not 504: our own capacity limit, not an upstream timeout
        assert info.suggested_http_code == 503
        assert any("CLOSE-WAIT" in step for step in info.troubleshooting_steps)

    def test_connect_and_read_timeouts_keep_their_categories(self):
        """The new branch must not shadow the existing ones."""
        assert classify_network_error(
            httpx.ConnectTimeout("c")
        ).category == ErrorCategory.TIMEOUT_CONNECT
        assert classify_network_error(
            httpx.ReadTimeout("r")
        ).category == ErrorCategory.TIMEOUT_READ

    def test_bare_timeout_still_falls_through_to_generic(self):
        info = classify_network_error(httpx.TimeoutException("generic"))
        assert info.category == ErrorCategory.TIMEOUT_READ
        assert info.suggested_http_code == 504
