# -*- coding: utf-8 -*-
"""Tests for request_logs body retention.

Bodies are what grow this table without bound - a stat row is under 100 bytes,
its bodies average 200+ KB. These tests pin the two properties that make dropping
them safe: statistics survive, and failures keep their payload.
"""

import pytest

from kiro import request_logger as rl_module
from kiro.request_logger import RequestLogger

BIG = "x" * 200_000


async def _rlogger(tmp_path) -> RequestLogger:
    rl = RequestLogger(db_path=str(tmp_path / "logs.db"))
    await rl.init_db()
    return rl


def _row(rl: RequestLogger):
    return rl._conn.execute(
        "SELECT status, prompt_tokens, completion_tokens, model, user_id, "
        "COALESCE(request_body,'') rb, COALESCE(response_body,'') resp "
        "FROM request_logs ORDER BY id DESC LIMIT 1"
    ).fetchone()


@pytest.fixture
def drop_success_bodies(monkeypatch):
    """Default production setting: successes keep no body, failures do."""
    monkeypatch.setattr(rl_module, "LOG_SUCCESS_BODY", False)
    monkeypatch.setattr(rl_module, "LOG_FAILURE_BODY", True)
    monkeypatch.setattr(rl_module, "MAX_LOGGED_BODY_BYTES", 65536)


class TestSuccessBodies:
    @pytest.mark.asyncio
    async def test_success_body_is_dropped(self, tmp_path, drop_success_bodies):
        rl = await _rlogger(tmp_path)
        await rl.record(status="success", request_body=BIG, response_body=BIG)

        row = _row(rl)
        assert row["rb"] == ""
        assert row["resp"] == ""

    @pytest.mark.asyncio
    async def test_statistics_survive_dropping_the_body(self, tmp_path, drop_success_bodies):
        """The whole point: per-user reporting must not notice the body is gone."""
        rl = await _rlogger(tmp_path)
        await rl.record(
            status="success",
            model="claude-opus-4.8",
            prompt_tokens=1200,
            completion_tokens=340,
            user_id="zhangsan",
            request_body=BIG,
            response_body=BIG,
        )

        row = _row(rl)
        assert row["model"] == "claude-opus-4.8"
        assert row["prompt_tokens"] == 1200
        assert row["completion_tokens"] == 340
        assert row["user_id"] == "zhangsan"

    @pytest.mark.asyncio
    async def test_success_body_kept_when_explicitly_enabled(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rl_module, "LOG_SUCCESS_BODY", True)
        monkeypatch.setattr(rl_module, "MAX_LOGGED_BODY_BYTES", 65536)
        rl = await _rlogger(tmp_path)
        await rl.record(status="success", request_body="small payload")

        assert _row(rl)["rb"] == "small payload"


class TestFailureBodies:
    @pytest.mark.asyncio
    async def test_failure_keeps_its_body(self, tmp_path, drop_success_bodies):
        """A failed payload is the evidence - IMAGE_DIMENSION_EXCEEDED was found this way."""
        rl = await _rlogger(tmp_path)
        await rl.record(status="error", request_body="oversized image here")

        assert _row(rl)["rb"] == "oversized image here"

    @pytest.mark.asyncio
    async def test_failure_body_dropped_when_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rl_module, "LOG_FAILURE_BODY", False)
        monkeypatch.setattr(rl_module, "LOG_SUCCESS_BODY", False)
        monkeypatch.setattr(rl_module, "MAX_LOGGED_BODY_BYTES", 65536)
        rl = await _rlogger(tmp_path)
        await rl.record(status="error", request_body="something")

        assert _row(rl)["rb"] == ""


class TestTruncation:
    @pytest.mark.asyncio
    async def test_oversized_failure_body_is_capped(self, tmp_path, monkeypatch):
        """One pathological request must not be able to write hundreds of MB."""
        monkeypatch.setattr(rl_module, "LOG_FAILURE_BODY", True)
        monkeypatch.setattr(rl_module, "MAX_LOGGED_BODY_BYTES", 1000)
        rl = await _rlogger(tmp_path)
        await rl.record(status="error", request_body=BIG)

        stored = _row(rl)["rb"]
        assert len(stored) < 1200
        assert stored.startswith("x" * 1000)
        assert "truncated, original 200000 chars" in stored

    @pytest.mark.asyncio
    async def test_body_at_the_limit_is_untouched(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rl_module, "LOG_FAILURE_BODY", True)
        monkeypatch.setattr(rl_module, "MAX_LOGGED_BODY_BYTES", 100)
        rl = await _rlogger(tmp_path)
        exact = "y" * 100
        await rl.record(status="error", request_body=exact)

        assert _row(rl)["rb"] == exact

    @pytest.mark.asyncio
    async def test_empty_body_stays_empty(self, tmp_path, drop_success_bodies):
        rl = await _rlogger(tmp_path)
        await rl.record(status="error", request_body="")

        assert _row(rl)["rb"] == ""
