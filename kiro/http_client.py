# -*- coding: utf-8 -*-

# Kiro Gateway
# https://github.com/jwadow/kiro-gateway
# Copyright (C) 2025 Jwadow
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""
HTTP client for Kiro API with retry logic support.

Handles:
- 403: automatic token refresh and retry
- 429: exponential backoff
- 5xx: exponential backoff
- Timeouts: exponential backoff

Supports both per-request clients and shared application-level client
with connection pooling for better resource management.
"""

import asyncio
import json
from typing import Optional

import httpx
from fastapi import HTTPException
from loguru import logger

from kiro.config import MAX_RETRIES, BASE_RETRY_DELAY, FIRST_TOKEN_MAX_RETRIES, STREAMING_READ_TIMEOUT
from kiro.auth import KiroAuthManager
from kiro.utils import get_kiro_headers
from kiro.network_errors import classify_network_error, get_short_error_message, NetworkErrorInfo


class KiroHttpClient:
    """
    HTTP client for Kiro API with retry logic support.
    
    Automatically handles errors and retries requests:
    - 403: refreshes token and retries
    - 429: waits with exponential backoff
    - 5xx: waits with exponential backoff
    - Timeouts: waits with exponential backoff
    
    Supports two modes of operation:
    1. Per-request client: Creates and owns its own httpx.AsyncClient
    2. Shared client: Uses an application-level shared client (recommended)
    
    Using a shared client reduces memory usage and enables connection pooling,
    which is especially important for handling concurrent requests.
    
    Attributes:
        auth_manager: Authentication manager for obtaining tokens
        client: httpx HTTP client (owned or shared)
    
    Example:
        >>> # Per-request client (legacy mode)
        >>> client = KiroHttpClient(auth_manager)
        >>> response = await client.request_with_retry(...)
        
        >>> # Shared client (recommended)
        >>> shared = httpx.AsyncClient(limits=httpx.Limits(...))
        >>> client = KiroHttpClient(auth_manager, shared_client=shared)
        >>> response = await client.request_with_retry(...)
    """
    
    def __init__(
        self,
        auth_manager: KiroAuthManager,
        shared_client: Optional[httpx.AsyncClient] = None
    ):
        """
        Initializes the HTTP client.
        
        Args:
            auth_manager: Authentication manager
            shared_client: Optional shared httpx.AsyncClient for connection pooling.
                          If provided, this client will be used instead of creating
                          a new one. The shared client will NOT be closed by close().
        """
        self.auth_manager = auth_manager
        self._shared_client = shared_client
        self._owns_client = shared_client is None
        self.client: Optional[httpx.AsyncClient] = shared_client
    
    async def _get_client(self, stream: bool = False) -> httpx.AsyncClient:
        """
        Returns or creates an HTTP client with proper timeouts.
        
        If a shared client was provided at initialization, it is returned as-is.
        Otherwise, creates a new client with appropriate timeout configuration.
        
        httpx timeouts:
        - connect: TCP handshake (DNS + TCP SYN/ACK)
        - read: waiting for data from server between chunks
        - write: sending data to server
        - pool: waiting for free connection from pool
        
        IMPORTANT: FIRST_TOKEN_TIMEOUT is NOT used here!
        It is applied in streaming_openai.py via asyncio.wait_for() to control
        the wait time for the first token from the model (retry business logic).
        
        Args:
            stream: If True, uses STREAMING_READ_TIMEOUT for read (only for new clients)
        
        Returns:
            Active HTTP client
        """
        # If using shared client, return it directly
        # Shared client should be pre-configured with appropriate timeouts
        if self._shared_client is not None:
            return self._shared_client
        
        # Create new client if needed (per-request mode)
        if self.client is None or self.client.is_closed:
            if stream:
                # For streaming:
                # - connect: 30 sec (TCP connection, usually < 1 sec)
                # - read: STREAMING_READ_TIMEOUT (300 sec) - model may "think" between chunks
                # - write/pool: standard values
                timeout_config = httpx.Timeout(
                    connect=30.0,
                    read=STREAMING_READ_TIMEOUT,
                    write=30.0,
                    pool=30.0
                )
                logger.debug(f"Creating streaming HTTP client (read_timeout={STREAMING_READ_TIMEOUT}s)")
            else:
                # For regular requests: single timeout of 300 sec
                timeout_config = httpx.Timeout(timeout=300.0)
                logger.debug("Creating non-streaming HTTP client (timeout=300s)")
            
            self.client = httpx.AsyncClient(timeout=timeout_config, follow_redirects=True)
        return self.client
    
    async def close(self) -> None:
        """
        Closes the HTTP client if this instance owns it.
        
        If using a shared client, this method does nothing - the shared client
        should be closed by the application lifecycle manager.
        
        Uses graceful exception handling to prevent errors during cleanup
        from masking the original exception in finally blocks.
        """
        # Don't close shared clients - they're managed by the application
        if not self._owns_client:
            return
        
        if self.client and not self.client.is_closed:
            try:
                await self.client.aclose()
            except Exception as e:
                # Log but don't propagate - we're in cleanup code
                # Propagating here could mask the original exception
                logger.warning(f"Error closing HTTP client: {e}")

    @staticmethod
    async def _release_response(response: Optional[httpx.Response]) -> None:
        """
        Returns a response's connection to the pool. Safe to call with None.

        A streamed response (stream=True) keeps its connection checked out of the
        pool until it is closed - httpx cannot know the caller is done reading. So
        every response we abandon mid-retry has to be released explicitly, or its
        pool slot is gone for the lifetime of the process.

        This is what took xiaomei down on 2026-09-21: retries on 403 (expired
        tokens) discarded their responses without closing them, and because
        request_with_retry sets "Connection: close" for streams, the server had
        already sent FIN - so each leaked socket sat in CLOSE_WAIT holding a slot.
        Exactly 100 of them accumulated against max_connections=100, after which
        every new request blocked on pool acquisition and failed with PoolTimeout
        after 30s. The pool never recovers on its own; only a restart clears it.

        Failures here are swallowed deliberately: this runs on error paths where
        the real error must not be masked by a cleanup problem.
        """
        if response is None:
            return
        try:
            await response.aclose()
        except Exception as e:
            logger.debug(f"Error releasing response connection: {e}")

    @staticmethod
    async def _buffer_and_release(response: httpx.Response) -> None:
        """
        Reads a response's body into memory, then frees its pool connection.

        For the 429/5xx responses handed back to the caller after retries are
        exhausted. Those need a readable body, but a streamed response that is still
        open owns a pool slot - so the stored response was competing for the pool
        against the very attempts meant to supersede it. On a tight pool that is a
        self-inflicted PoolTimeout: the retry cannot get a connection because the
        previous failure is still holding one.

        aread() buffers the content, after which aclose() releases the connection and
        .text / .json() keep working. If the read fails the connection is released
        anyway - a missing error body is a far smaller problem than a lost slot.
        """
        try:
            await response.aread()
        except Exception as e:
            logger.debug(f"Could not buffer response body before release: {e}")
        await KiroHttpClient._release_response(response)

    async def request_with_retry(
        self,
        method: str,
        url: str,
        json_data: Optional[dict] = None,
        params: Optional[dict] = None,
        stream: bool = False
    ) -> httpx.Response:
        """
        Executes an HTTP request with retry logic.
        
        Automatically handles various error types:
        - 403: refreshes token via auth_manager.force_refresh() and retries
        - 429: waits with exponential backoff (1s, 2s, 4s)
        - 5xx: waits with exponential backoff
        - Timeouts: waits with exponential backoff
        
        For streaming, STREAMING_READ_TIMEOUT is used for waiting between chunks.
        First token timeout is controlled separately in streaming_openai.py via asyncio.wait_for().
        
        Args:
            method: HTTP method (GET, POST, etc.)
            url: Request URL
            json_data: Optional JSON body (for POST/PUT/PATCH)
            params: Optional query parameters (for GET)
            stream: Use streaming (default False)
        
        Returns:
            httpx.Response with successful response
        
        Raises:
            HTTPException: On failure after all attempts (502/504)
        """
        # Determine the number of retry attempts
        # FIRST_TOKEN_TIMEOUT is used in streaming_openai.py, not here
        max_retries = FIRST_TOKEN_MAX_RETRIES if stream else MAX_RETRIES
        
        client = await self._get_client(stream=stream)
        last_error = None
        last_error_info: Optional[NetworkErrorInfo] = None
        last_response: Optional[httpx.Response] = None  # Для сохранения последнего 429/5xx
        
        for attempt in range(max_retries):
            try:
                # Get current token
                token = await self.auth_manager.get_access_token()
                headers = get_kiro_headers(self.auth_manager, token)
                
                # Build request kwargs based on parameters
                request_kwargs = {"headers": headers}
                
                if json_data is not None:
                    request_kwargs["content"] = json.dumps(json_data).encode()
                
                if params is not None:
                    request_kwargs["params"] = params
                
                if stream:
                    # Prevent CLOSE_WAIT connection leak (issue #38)
                    headers["Connection"] = "close"
                    req = client.build_request(method, url, **request_kwargs)
                    logger.debug("Sending request to Kiro API...")
                    response = await client.send(req, stream=True)
                else:
                    logger.debug("Sending request to Kiro API...")
                    response = await client.request(method, url, **request_kwargs)
                
                # Check status
                if response.status_code == 200:
                    return response

                # 403 - token expired, refresh and retry
                if response.status_code == 403:
                    logger.warning(f"Received 403, refreshing token (attempt {attempt + 1}/{MAX_RETRIES})")
                    # Release before retrying: with stream=True the connection stays
                    # checked out of the pool until the response is closed, so a
                    # discarded response holds its slot forever (see _release_response)
                    await self._release_response(response)
                    await self.auth_manager.force_refresh()
                    continue

                # 429 - rate limit, wait and retry
                if response.status_code == 429:
                    # The caller still needs this response - it comes back after
                    # exhaustion so the real status and error body stay visible - but
                    # while it is open it owns a pool slot, competing against the very
                    # retries meant to supersede it. Buffer the body, free the socket.
                    await self._buffer_and_release(response)
                    # Also release the one this replaces: repeated 429s would
                    # otherwise strand every response but the last
                    await self._release_response(last_response)
                    last_response = response  # Сохраняем для возврата после exhaustion
                    delay = BASE_RETRY_DELAY * (2 ** attempt)
                    logger.warning(f"Received 429, waiting {delay}s (attempt {attempt + 1}/{max_retries})")
                    await asyncio.sleep(delay)
                    continue

                # 5xx - server error, wait and retry
                if 500 <= response.status_code < 600:
                    await self._buffer_and_release(response)
                    await self._release_response(last_response)
                    last_response = response  # Сохраняем для возврата после exhaustion
                    delay = BASE_RETRY_DELAY * (2 ** attempt)
                    logger.warning(f"Received {response.status_code}, waiting {delay}s (attempt {attempt + 1}/{max_retries})")
                    await asyncio.sleep(delay)
                    continue

                # Other errors - return as is
                return response
                
            except httpx.TimeoutException as e:
                last_error = e
                
                # Classify timeout error for user-friendly messaging
                error_info = classify_network_error(e)
                last_error_info = error_info
                
                # Log with user-friendly message
                short_msg = get_short_error_message(error_info)
                
                if error_info.is_retryable and attempt < max_retries - 1:
                    delay = BASE_RETRY_DELAY * (2 ** attempt)
                    logger.warning(f"{short_msg} - waiting {delay}s (attempt {attempt + 1}/{max_retries})")
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"{short_msg} - no more retries (attempt {attempt + 1}/{max_retries})")
                    if not error_info.is_retryable:
                        break  # Don't retry non-retryable errors
                
            except httpx.RequestError as e:
                last_error = e
                
                # Classify the error for user-friendly messaging
                error_info = classify_network_error(e)
                last_error_info = error_info
                
                # Log with user-friendly message
                short_msg = get_short_error_message(error_info)
                
                if error_info.is_retryable and attempt < max_retries - 1:
                    delay = BASE_RETRY_DELAY * (2 ** attempt)
                    logger.warning(f"{short_msg} - waiting {delay}s (attempt {attempt + 1}/{max_retries})")
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"{short_msg} - no more retries (attempt {attempt + 1}/{max_retries})")
                    if not error_info.is_retryable:
                        break  # Don't retry non-retryable errors
        
        # If we have a last_response (429/5xx retry exhausted), return it
        # This allows the caller to see the real status code and error body
        if last_response is not None:
            logger.warning(
                f"Retries exhausted for HTTP {last_response.status_code}, "
                f"returning response to caller for classification"
            )
            return last_response
        
        # All attempts exhausted - provide detailed, user-friendly error message
        if last_error_info:
            # Use classified error information
            error_message = last_error_info.user_message
            
            # Add troubleshooting steps
            if last_error_info.troubleshooting_steps:
                error_message += "\n\nTroubleshooting:\n"
                for i, step in enumerate(last_error_info.troubleshooting_steps, 1):
                    error_message += f"{i}. {step}\n"
            
            # Add technical details for debugging
            error_message += f"\nTechnical details: {last_error_info.technical_details}"
            
            raise HTTPException(
                status_code=last_error_info.suggested_http_code,
                detail=error_message.strip()
            )
        else:
            # Fallback if no error was captured (shouldn't happen)
            if stream:
                raise HTTPException(
                    status_code=504,
                    detail=f"Streaming failed after {max_retries} attempts. Unknown error."
                )
            else:
                raise HTTPException(
                    status_code=502,
                    detail=f"Request failed after {max_retries} attempts. Unknown error."
                )
    
    async def __aenter__(self) -> "KiroHttpClient":
        """Async context manager support."""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Closes the client when exiting context."""
        await self.close()