# -*- coding: utf-8 -*-

"""
Per-request trace of the account failover loop.

The loop in routes_openai / routes_anthropic picks an account, sends the request,
and moves on to the next account when the attempt fails in a recoverable way.
Until now a failed request left almost no trace of that path: only a few branches
logged anything, so a 503 "No available accounts" after thirty seconds could not be
told apart from an instant refusal, and nobody could say where the time went.

One AttemptTrace lives for one client request. It writes one INFO line per attempt
(which account, what came back, how long picking the account and sending the request
took) and can summarise the whole path in a sentence for the final error message,
which also lands in request_logs.error_message.
"""

import re
import time
from typing import List, Optional, Tuple

from loguru import logger

# Picking an account normally takes microseconds. Longer than this means something in
# get_next_account() (waiting for its lock, a token / model-list refresh) is slow.
SLOW_SELECTION_SECONDS = 1.0

# How many attempts the one-line summary spells out before saying "+N more".
SUMMARY_MAX_ENTRIES = 12

_ACCOUNT_FILE_ID = re.compile(r"([0-9a-fA-F]{6,})\.json$")


def short_account_id(account_id: Optional[str]) -> str:
    """'/app/data/kiro-auth-token-8dc7a103.json' -> '8dc7a103' (never the whole path, never a secret)."""
    if not account_id:
        return "-"
    match = _ACCOUNT_FILE_ID.search(account_id)
    return match.group(1) if match else account_id[-12:]


class AttemptTrace:
    """What one client request did across the accounts, in order."""

    def __init__(self, model: str, max_attempts: int, label: str = "") -> None:
        self.model = model
        self.max_attempts = max_attempts
        self.label = label
        self._started = time.monotonic()
        self._select_started = self._started
        self._request_started = self._started
        self._current: Optional[str] = None
        self._select_seconds = 0.0
        self.total_select_seconds = 0.0
        # (account, outcome, request seconds)
        self.attempts: List[Tuple[str, str, float]] = []

    def before_selection(self) -> None:
        """Call right before account_manager.get_next_account()."""
        self._select_started = time.monotonic()

    def selected(self, account_id: Optional[str]) -> None:
        """Call right after get_next_account() returned (account_id is None when nothing is left)."""
        now = time.monotonic()
        self._select_seconds = now - self._select_started
        self.total_select_seconds += self._select_seconds
        self._request_started = now
        self._current = short_account_id(account_id) if account_id else None
        if self._select_seconds >= SLOW_SELECTION_SECONDS:
            logger.warning(
                f"{self._prefix()}picking an account took {self._select_seconds:.1f}s "
                f"(chose {self._current or 'none'}); slow selection means waiting on the account lock or a token/model refresh"
            )

    def outcome(self, result: str) -> None:
        """Record how the attempt on the currently selected account ended and log it."""
        elapsed = time.monotonic() - self._request_started
        account = self._current or "-"
        self.attempts.append((account, result, elapsed))
        logger.info(
            f"{self._prefix()}attempt {len(self.attempts)}/{self.max_attempts} account={account} -> {result} "
            f"(select {self._select_seconds:.1f}s, request {elapsed:.1f}s)"
        )

    def exhausted(self) -> None:
        """Nothing left to try: log the path in one line."""
        logger.warning(f"{self._prefix()}no account left after {len(self.attempts)} attempt(s): {self.summary()}")

    def summary(self) -> str:
        """One sentence for the final error message: how many accounts were tried, what each returned, where the time went."""
        total = time.monotonic() - self._started
        if not self.attempts:
            return (f"No account was tried; picking one took {self.total_select_seconds:.1f}s "
                    f"of {total:.1f}s")
        shown = [f"{account}={result}({secs:.1f}s)" for account, result, secs in self.attempts[:SUMMARY_MAX_ENTRIES]]
        more = len(self.attempts) - len(shown)
        tail = f", +{more} more" if more > 0 else ""
        return (f"Tried {len(self.attempts)} account(s) in {total:.1f}s "
                f"(picking accounts {self.total_select_seconds:.1f}s): {', '.join(shown)}{tail}")

    def _prefix(self) -> str:
        return f"[{self.label or self.model}] " if (self.label or self.model) else ""
