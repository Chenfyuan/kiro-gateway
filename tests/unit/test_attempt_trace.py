# -*- coding: utf-8 -*-

"""
Tests for kiro/attempt_trace.py - the per-request record of the account failover loop.

Why it exists: a request that failed with "No available accounts" after 30 seconds left no
trace of what it had done in those 30 seconds, so nobody could say whether it had been
refused at once or had walked through every account.
"""

import time

import pytest
from loguru import logger

from kiro.attempt_trace import AttemptTrace, short_account_id


@pytest.fixture
def log_lines():
    """The log lines (level, text) written while a test runs."""
    lines = []
    handler = logger.add(lambda m: lines.append((m.record["level"].name, m.record["message"])), level="DEBUG")
    yield lines
    logger.remove(handler)


class TestShortAccountId:
    def test_takes_the_hash_out_of_the_credentials_path(self):
        assert short_account_id("/app/data/kiro-auth-token-8dc7a103.json") == "8dc7a103"

    def test_never_returns_a_whole_path(self):
        # an id that is not one of the usual credential files is cut to its last 12 characters
        assert short_account_id("/home/user/some/dir/account-one.json") == "ccount-one.json"[-12:]
        assert len(short_account_id("/home/user/some/dir/account-one.json")) == 12

    def test_missing_id(self):
        assert short_account_id(None) == "-"
        assert short_account_id("") == "-"


class TestAttemptTrace:
    def test_each_outcome_is_logged_with_account_result_and_timing(self, log_lines):
        trace = AttemptTrace("claude-opus-4.8", 4)
        trace.before_selection()
        trace.selected("/app/data/kiro-auth-token-8dc7a103.json")
        trace.outcome("429 UNKNOWN (recoverable)")

        info = [text for level, text in log_lines if level == "INFO"]
        assert len(info) == 1
        assert "attempt 1/4" in info[0] and "account=8dc7a103" in info[0] and "429 UNKNOWN (recoverable)" in info[0]
        assert "select " in info[0] and "request " in info[0]

    def test_summary_lists_every_account_tried_in_order(self):
        trace = AttemptTrace("m", 10)
        for account, result in (("aaaaaaa1", "429"), ("bbbbbbb2", "402"), ("ccccccc3", "502 network error")):
            trace.before_selection()
            trace.selected(f"/app/data/kiro-auth-token-{account}.json")
            trace.outcome(result)

        summary = trace.summary()
        assert summary.startswith("Tried 3 account(s)")
        assert summary.index("aaaaaaa1=429") < summary.index("bbbbbbb2=402") < summary.index("ccccccc3=502 network error")

    def test_long_paths_are_shortened_so_the_error_message_stays_small(self):
        trace = AttemptTrace("m", 60)
        for i in range(30):
            trace.before_selection()
            trace.selected(f"/app/data/kiro-auth-token-{i:08x}.json")
            trace.outcome("429")
        summary = trace.summary()
        assert "Tried 30 account(s)" in summary and "+18 more" in summary
        assert len(summary) < 800

    def test_summary_without_attempts_says_where_the_time_went(self):
        trace = AttemptTrace("m", 4)
        trace.before_selection()
        trace.selected(None)  # nothing could be picked
        assert trace.summary().startswith("No account was tried; picking one took ")

    def test_a_slow_selection_is_reported_as_a_warning(self, log_lines, monkeypatch):
        import kiro.attempt_trace as module
        monkeypatch.setattr(module, "SLOW_SELECTION_SECONDS", 0.05)
        trace = AttemptTrace("m", 4)
        trace.before_selection()
        time.sleep(0.08)
        trace.selected("/app/data/kiro-auth-token-8dc7a103.json")

        warnings = [text for level, text in log_lines if level == "WARNING"]
        assert len(warnings) == 1 and "picking an account took" in warnings[0] and "8dc7a103" in warnings[0]
        assert trace.total_select_seconds >= 0.05

    def test_a_fast_selection_logs_nothing(self, log_lines):
        trace = AttemptTrace("m", 4)
        trace.before_selection()
        trace.selected("/app/data/kiro-auth-token-8dc7a103.json")
        assert not [1 for level, _ in log_lines if level == "WARNING"]

    def test_exhausted_logs_the_whole_path_once(self, log_lines):
        trace = AttemptTrace("m", 4)
        trace.before_selection()
        trace.selected("/app/data/kiro-auth-token-8dc7a103.json")
        trace.outcome("403 UNKNOWN (recoverable)")
        trace.exhausted()

        warnings = [text for level, text in log_lines if level == "WARNING"]
        assert len(warnings) == 1 and "no account left after 1 attempt(s)" in warnings[0] and "8dc7a103=403" in warnings[0]
