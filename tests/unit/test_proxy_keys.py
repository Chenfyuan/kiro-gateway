# -*- coding: utf-8 -*-
"""Tests for per-user proxy keys and per-user usage attribution."""

import pytest

from kiro.proxy_keys import ProxyKeyStore, UNASSIGNED_USER_ID, _mask
from kiro.request_logger import RequestLogger


async def _store(tmp_path) -> ProxyKeyStore:
    s = ProxyKeyStore(db_path=str(tmp_path / "keys.db"))
    await s.init_db()
    return s


async def _rlogger(tmp_path) -> RequestLogger:
    rl = RequestLogger(db_path=str(tmp_path / "logs.db"))
    await rl.init_db()
    return rl


class TestProxyKeyStore:
    @pytest.mark.asyncio
    async def test_upsert_then_resolve(self, tmp_path):
        store = await _store(tmp_path)
        await store.upsert("key-alice", "u1", "Alice")
        assert store.resolve("key-alice") == {"user_id": "u1", "user_name": "Alice"}

    @pytest.mark.asyncio
    async def test_unknown_key_does_not_resolve(self, tmp_path):
        store = await _store(tmp_path)
        assert store.resolve("never-issued") is None
        assert store.resolve("") is None

    @pytest.mark.asyncio
    async def test_disabled_key_does_not_authenticate(self, tmp_path):
        store = await _store(tmp_path)
        await store.upsert("key-bob", "u2", "Bob")
        assert await store.set_enabled("u2", False) is True
        # A disabled key must behave like an unknown one, not merely be flagged.
        assert store.resolve("key-bob") is None

    @pytest.mark.asyncio
    async def test_reissue_rotates_instead_of_adding_second_key(self, tmp_path):
        """One key per user: re-issuing must invalidate the previous value."""
        store = await _store(tmp_path)
        await store.upsert("key-old", "u3", "Carol")
        await store.upsert("key-new", "u3", "Carol")

        assert store.resolve("key-old") is None
        assert store.resolve("key-new")["user_id"] == "u3"
        assert len(await store.list_keys()) == 1

    @pytest.mark.asyncio
    async def test_reserved_user_id_rejected(self, tmp_path):
        """The unassigned bucket is not a real user and cannot own a key."""
        store = await _store(tmp_path)
        with pytest.raises(ValueError):
            await store.upsert("key-x", UNASSIGNED_USER_ID, "nope")

    @pytest.mark.asyncio
    async def test_delete_revokes(self, tmp_path):
        store = await _store(tmp_path)
        await store.upsert("key-dave", "u4", "Dave")
        assert await store.delete("u4") is True
        assert store.resolve("key-dave") is None
        assert await store.delete("u4") is False

    @pytest.mark.asyncio
    async def test_list_never_exposes_full_key(self, tmp_path):
        store = await _store(tmp_path)
        await store.upsert("supersecretkeyvalue", "u5", "Eve")
        rows = await store.list_keys()
        assert rows[0]["api_key_masked"] == "supe...alue"
        assert "api_key" not in rows[0]
        assert all("supersecretkeyvalue" not in str(v) for v in rows[0].values())

    def test_mask_short_key_reveals_nothing(self):
        assert _mask("abc") == "***"
        assert _mask("") == ""


class TestPerUserUsage:
    @pytest.mark.asyncio
    async def test_usage_grouped_by_user(self, tmp_path):
        rlogger = await _rlogger(tmp_path)
        await rlogger.record(model="m1", prompt_tokens=100, completion_tokens=50,
                             user_id="u1", user_name="Alice")
        await rlogger.record(model="m1", prompt_tokens=10, completion_tokens=5,
                             user_id="u1", user_name="Alice")
        await rlogger.record(model="m2", prompt_tokens=1000, completion_tokens=500,
                             user_id="u2", user_name="Bob")

        rows = await rlogger.get_user_usage(days=30)
        by_user = {r["user_id"]: r for r in rows}

        assert by_user["u1"]["total_tokens"] == 165
        assert by_user["u1"]["requests"] == 2
        assert by_user["u2"]["total_tokens"] == 1500
        # Ordered by consumption, so the heaviest user is actionable at a glance.
        assert rows[0]["user_id"] == "u2"

    @pytest.mark.asyncio
    async def test_rows_without_identity_are_reported_as_unknown(self, tmp_path):
        """
        Requests logged before per-user keys existed carry no user_id. They must
        surface as a distinct unknown bucket rather than being attributed to
        someone or silently dropped from totals.
        """
        rlogger = await _rlogger(tmp_path)
        await rlogger.record(model="m1", prompt_tokens=7, completion_tokens=3)

        rows = await rlogger.get_user_usage(days=30)
        assert len(rows) == 1
        assert rows[0]["user_id"] == "__unknown__"
        assert rows[0]["total_tokens"] == 10

    @pytest.mark.asyncio
    async def test_daily_breakdown(self, tmp_path):
        rlogger = await _rlogger(tmp_path)
        await rlogger.record(model="m1", prompt_tokens=100, completion_tokens=0,
                             user_id="u1", user_name="Alice")
        rows = await rlogger.get_user_daily_usage(days=30)
        assert len(rows) == 1
        assert rows[0]["date"]
        assert rows[0]["total_tokens"] == 100

    @pytest.mark.asyncio
    async def test_model_breakdown_and_user_filter(self, tmp_path):
        rlogger = await _rlogger(tmp_path)
        await rlogger.record(model="opus", prompt_tokens=100, completion_tokens=0,
                             user_id="u1", user_name="Alice")
        await rlogger.record(model="haiku", prompt_tokens=20, completion_tokens=0,
                             user_id="u1", user_name="Alice")
        await rlogger.record(model="opus", prompt_tokens=999, completion_tokens=0,
                             user_id="u2", user_name="Bob")

        all_rows = await rlogger.get_user_model_usage(days=30)
        assert len(all_rows) == 3

        only_u1 = await rlogger.get_user_model_usage(days=30, user_id="u1")
        assert {r["model"] for r in only_u1} == {"opus", "haiku"}
        assert all(r["user_id"] == "u1" for r in only_u1)


class TestCallerIdentity:
    """
    Legacy global-key traffic must stay authenticated and land in its own bucket
    rather than being rejected or attributed to a real user.
    """

    @pytest.mark.asyncio
    async def test_global_key_resolves_to_unassigned(self, tmp_path, monkeypatch):
        from kiro import caller_identity

        monkeypatch.setattr(caller_identity, "get_proxy_api_key", lambda: "global-key")
        identity = caller_identity.resolve_caller(None, "global-key")
        assert identity["user_id"] == UNASSIGNED_USER_ID

    @pytest.mark.asyncio
    async def test_unknown_credential_is_rejected(self, tmp_path, monkeypatch):
        from kiro import caller_identity

        monkeypatch.setattr(caller_identity, "get_proxy_api_key", lambda: "global-key")
        assert caller_identity.resolve_caller(None, "bogus") is None
        assert caller_identity.resolve_caller(None, None) is None


class TestPerUserCost:
    """cost_usd on the per-user reports.

    Tokens alone cannot answer "what did this user cost"; only the gateway knows
    the per-model rates, so the number has to be produced here rather than in the
    console UI. The cases below pin the one thing that is easy to get wrong:
    pricing has to happen per model, before any summing across models.
    """

    # claude-opus-4: $15/1M in, $75/1M out. claude-sonnet-4: $3/1M in, $15/1M out.
    OPUS = "claude-opus-4"
    SONNET = "claude-sonnet-4"

    @pytest.mark.asyncio
    async def test_single_model_cost(self, tmp_path):
        rlogger = await _rlogger(tmp_path)
        await rlogger.record(model=self.OPUS, prompt_tokens=1_000_000,
                             completion_tokens=1_000_000, user_id="u1", user_name="Alice")

        rows = await rlogger.get_user_usage(days=30)
        assert rows[0]["cost_usd"] == pytest.approx(90.0)

    @pytest.mark.asyncio
    async def test_mixed_models_are_priced_separately(self, tmp_path):
        """The whole reason cost is computed per (user, model) and then summed.

        Summing tokens first and pricing the total would bill all 2M input tokens
        at whichever model name matched first - $30 or $15 instead of the real $18.
        """
        rlogger = await _rlogger(tmp_path)
        await rlogger.record(model=self.OPUS, prompt_tokens=1_000_000, completion_tokens=0,
                             user_id="u1", user_name="Alice")
        await rlogger.record(model=self.SONNET, prompt_tokens=1_000_000, completion_tokens=0,
                             user_id="u1", user_name="Alice")

        rows = await rlogger.get_user_usage(days=30)
        assert len(rows) == 1
        assert rows[0]["cost_usd"] == pytest.approx(18.0)  # 15 + 3, not 30 and not 15

    @pytest.mark.asyncio
    async def test_cost_is_attributed_to_the_right_user(self, tmp_path):
        rlogger = await _rlogger(tmp_path)
        await rlogger.record(model=self.OPUS, prompt_tokens=1_000_000, completion_tokens=0,
                             user_id="u1", user_name="Alice")
        await rlogger.record(model=self.SONNET, prompt_tokens=1_000_000, completion_tokens=0,
                             user_id="u2", user_name="Bob")

        by_user = {r["user_id"]: r for r in await rlogger.get_user_usage(days=30)}
        assert by_user["u1"]["cost_usd"] == pytest.approx(15.0)
        assert by_user["u2"]["cost_usd"] == pytest.approx(3.0)

    @pytest.mark.asyncio
    async def test_daily_cost_is_per_day_and_per_model(self, tmp_path):
        rlogger = await _rlogger(tmp_path)
        await rlogger.record(model=self.OPUS, prompt_tokens=1_000_000, completion_tokens=0,
                             user_id="u1", user_name="Alice")
        await rlogger.record(model=self.SONNET, prompt_tokens=1_000_000, completion_tokens=0,
                             user_id="u1", user_name="Alice")

        rows = await rlogger.get_user_daily_usage(days=30)
        assert len(rows) == 1  # same day
        assert rows[0]["cost_usd"] == pytest.approx(18.0)

    @pytest.mark.asyncio
    async def test_model_rows_carry_their_own_cost(self, tmp_path):
        rlogger = await _rlogger(tmp_path)
        await rlogger.record(model=self.OPUS, prompt_tokens=1_000_000, completion_tokens=0,
                             user_id="u1", user_name="Alice")
        await rlogger.record(model=self.SONNET, prompt_tokens=1_000_000, completion_tokens=0,
                             user_id="u1", user_name="Alice")

        by_model = {r["model"]: r for r in await rlogger.get_user_model_usage(days=30)}
        assert by_model[self.OPUS]["cost_usd"] == pytest.approx(15.0)
        assert by_model[self.SONNET]["cost_usd"] == pytest.approx(3.0)

    @pytest.mark.asyncio
    async def test_daily_cost_filtered_to_one_user(self, tmp_path):
        """The user_id filter must narrow the cost too, not just the token columns."""
        rlogger = await _rlogger(tmp_path)
        await rlogger.record(model=self.OPUS, prompt_tokens=1_000_000, completion_tokens=0,
                             user_id="u1", user_name="Alice")
        await rlogger.record(model=self.OPUS, prompt_tokens=1_000_000, completion_tokens=0,
                             user_id="u2", user_name="Bob")

        rows = await rlogger.get_user_daily_usage(days=30, user_id="u1")
        assert len(rows) == 1
        assert rows[0]["user_id"] == "u1"
        assert rows[0]["cost_usd"] == pytest.approx(15.0)

    @pytest.mark.asyncio
    async def test_unpriced_model_costs_zero_rather_than_failing(self, tmp_path):
        """An unknown model name must not break the report or invent a price."""
        rlogger = await _rlogger(tmp_path)
        await rlogger.record(model="some-model-we-have-no-rate-for",
                             prompt_tokens=5000, completion_tokens=5000,
                             user_id="u1", user_name="Alice")

        rows = await rlogger.get_user_usage(days=30)
        assert rows[0]["total_tokens"] == 10000
        assert rows[0]["cost_usd"] == 0.0

    @pytest.mark.asyncio
    async def test_unknown_bucket_still_gets_a_cost(self, tmp_path):
        """Unattributed traffic costs real money; the bucket must show it."""
        rlogger = await _rlogger(tmp_path)
        await rlogger.record(model=self.OPUS, prompt_tokens=1_000_000, completion_tokens=0)

        rows = await rlogger.get_user_usage(days=30)
        assert rows[0]["user_id"] == "__unknown__"
        assert rows[0]["cost_usd"] == pytest.approx(15.0)

    @pytest.mark.asyncio
    async def test_no_traffic_yields_no_rows(self, tmp_path):
        rlogger = await _rlogger(tmp_path)
        assert await rlogger.get_user_usage(days=30) == []
        assert await rlogger.get_user_daily_usage(days=30) == []
        assert await rlogger.get_user_model_usage(days=30) == []


class TestUnpricedModels:
    """Distinguishing "costs nothing" from "we have no rate for this".

    get_cost returns 0.0 for an unknown model, so a cost column alone renders
    real traffic as free. Newly released names (claude-opus-5, claude-haiku-4.5)
    sit in that hole until the pricing table is updated, which is exactly when a
    usage report is most likely to be trusted and wrong.
    """

    UNPRICED = "claude-opus-5"
    PRICED = "claude-opus-4"

    @pytest.mark.asyncio
    async def test_unpriced_tokens_are_reported(self, tmp_path):
        rlogger = await _rlogger(tmp_path)
        await rlogger.record(model=self.UNPRICED, prompt_tokens=500_000,
                             completion_tokens=100_000, user_id="u1", user_name="Alice")

        row = (await rlogger.get_user_usage(days=30))[0]
        assert row["cost_usd"] == 0.0
        # The cost is 0 only because no rate exists - say so instead of implying free
        assert row["unpriced_tokens"] == 600_000

    @pytest.mark.asyncio
    async def test_priced_traffic_reports_no_unpriced_tokens(self, tmp_path):
        rlogger = await _rlogger(tmp_path)
        await rlogger.record(model=self.PRICED, prompt_tokens=1_000_000, completion_tokens=0,
                             user_id="u1", user_name="Alice")

        row = (await rlogger.get_user_usage(days=30))[0]
        assert row["cost_usd"] == pytest.approx(15.0)
        assert row["unpriced_tokens"] == 0

    @pytest.mark.asyncio
    async def test_mixed_priced_and_unpriced_keeps_both_figures(self, tmp_path):
        """A partially-priced user must keep the cost it does have, plus the gap."""
        rlogger = await _rlogger(tmp_path)
        await rlogger.record(model=self.PRICED, prompt_tokens=1_000_000, completion_tokens=0,
                             user_id="u1", user_name="Alice")
        await rlogger.record(model=self.UNPRICED, prompt_tokens=7_000, completion_tokens=3_000,
                             user_id="u1", user_name="Alice")

        row = (await rlogger.get_user_usage(days=30))[0]
        assert row["cost_usd"] == pytest.approx(15.0)
        assert row["unpriced_tokens"] == 10_000

    @pytest.mark.asyncio
    async def test_daily_rows_carry_unpriced_tokens(self, tmp_path):
        rlogger = await _rlogger(tmp_path)
        await rlogger.record(model=self.UNPRICED, prompt_tokens=4_000, completion_tokens=1_000,
                             user_id="u1", user_name="Alice")

        row = (await rlogger.get_user_daily_usage(days=30))[0]
        assert row["unpriced_tokens"] == 5_000

    @pytest.mark.asyncio
    async def test_model_rows_flag_whether_they_are_priced(self, tmp_path):
        rlogger = await _rlogger(tmp_path)
        await rlogger.record(model=self.PRICED, prompt_tokens=1_000, completion_tokens=0,
                             user_id="u1", user_name="Alice")
        await rlogger.record(model=self.UNPRICED, prompt_tokens=1_000, completion_tokens=0,
                             user_id="u1", user_name="Alice")

        by_model = {r["model"]: r for r in await rlogger.get_user_model_usage(days=30)}
        assert by_model[self.PRICED]["priced"] is True
        assert by_model[self.UNPRICED]["priced"] is False

    def test_partial_match_still_counts_as_priced(self):
        """Dotted variants resolve to the base rate and must not be flagged unpriced."""
        from kiro.model_pricing import has_pricing

        assert has_pricing("claude-opus-4.6") is True
        assert has_pricing("claude-sonnet-4-6") is True
        assert has_pricing("claude-opus-5") is False
        assert has_pricing("totally-made-up-model") is False
