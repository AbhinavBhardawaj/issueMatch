import asyncio
import os
import json
import pytest
import httpx
from unittest.mock import AsyncMock, patch, MagicMock

from app.verifier.schemas import LLMInvocationError
from app.scout.agent import ScoutAgent, MalformedScoutResponse
from app.github.events import GitHubPushEvent
from app.scout.models import ScoutContext
from app.infrastructure.llm_router import (
    CircuitState,
    HealthAwareProviderPool,
    ProviderEndpoint,
    ProviderConfigurationError,
    create_role_provider,
    create_scout_provider,
    create_verifier_provider,
    resolve_pool_model_id,
    get_pool_provider_key,
    GroqProvider,
    GeminiProvider,
    MistralProvider,
    NvidiaNimProvider,
    DEFAULT_PROVIDER_MODELS,
)


class MockProvider:
    def __init__(self, name: str, model: str = "mock-model"):
        self.provider_name = name
        self.model_name = model
        self.model = model
        self.complete = AsyncMock()


def make_http_error(status_code: int):
    req = httpx.Request("POST", "http://test-provider/v1/chat")
    resp = httpx.Response(status_code, request=req, text=f"HTTP {status_code} error")
    return httpx.HTTPStatusError(f"HTTP {status_code}", request=req, response=resp)


class TestHealthAwareProviderPool:
    @pytest.mark.asyncio
    async def test_1_and_2_deterministic_round_robin_and_single_provider_call(self):
        # 1. Four healthy providers receive sequential requests in deterministic round-robin order
        # 2. Request 1 does NOT call all four providers
        p_groq = MockProvider("GROQ")
        p_gemini = MockProvider("GEMINI")
        p_mistral = MockProvider("MISTRAL")
        p_nvidia = MockProvider("NVIDIA")

        p_groq.complete.return_value = '{"findings": []}'
        p_gemini.complete.return_value = '{"findings": []}'
        p_mistral.complete.return_value = '{"findings": []}'
        p_nvidia.complete.return_value = '{"findings": []}'

        pool = HealthAwareProviderPool([p_groq, p_gemini, p_mistral, p_nvidia], role="scout")

        # Request 1 -> GROQ only
        res1 = await pool.complete("sys", "user1")
        assert res1 == '{"findings": []}'
        assert p_groq.complete.call_count == 1
        assert p_gemini.complete.call_count == 0
        assert p_mistral.complete.call_count == 0
        assert p_nvidia.complete.call_count == 0

        # Request 2 -> GEMINI only
        await pool.complete("sys", "user2")
        assert p_groq.complete.call_count == 1
        assert p_gemini.complete.call_count == 1
        assert p_mistral.complete.call_count == 0
        assert p_nvidia.complete.call_count == 0

        # Request 3 -> MISTRAL only
        await pool.complete("sys", "user3")
        assert p_groq.complete.call_count == 1
        assert p_gemini.complete.call_count == 1
        assert p_mistral.complete.call_count == 1
        assert p_nvidia.complete.call_count == 0

        # Request 4 -> NVIDIA only
        await pool.complete("sys", "user4")
        assert p_groq.complete.call_count == 1
        assert p_gemini.complete.call_count == 1
        assert p_mistral.complete.call_count == 1
        assert p_nvidia.complete.call_count == 1

        # Request 5 -> wraps back to GROQ
        await pool.complete("sys", "user5")
        assert p_groq.complete.call_count == 2

    @pytest.mark.asyncio
    async def test_3_ten_calls_distributed_across_providers(self):
        # 3. Ten successful calls are distributed across the configured providers instead of always going to Groq
        providers = [
            MockProvider("GROQ"),
            MockProvider("GEMINI"),
            MockProvider("MISTRAL"),
            MockProvider("NVIDIA"),
        ]
        for p in providers:
            p.complete.return_value = "ok"

        pool = HealthAwareProviderPool(providers, role="scout")

        for i in range(10):
            await pool.complete("sys", f"user{i}")

        # In 10 requests across 4 providers: 10 = 4*2 + 2
        # Providers 0 and 1 receive 3 calls; providers 2 and 3 receive 2 calls
        counts = [p.complete.call_count for p in providers]
        assert counts == [3, 3, 2, 2]
        assert sum(counts) == 10

    @pytest.mark.asyncio
    async def test_4_http_429_causes_failover(self):
        # 4. A 429 from selected provider causes failover to another healthy provider
        p_groq = MockProvider("GROQ")
        p_gemini = MockProvider("GEMINI")

        p_groq.complete.side_effect = make_http_error(429)
        p_gemini.complete.return_value = '{"source": "gemini"}'

        pool = HealthAwareProviderPool([p_groq, p_gemini], role="scout")

        res = await pool.complete("sys", "user")
        assert res == '{"source": "gemini"}'
        assert p_groq.complete.call_count == 1
        assert p_gemini.complete.call_count == 1

    @pytest.mark.asyncio
    async def test_5_timeout_causes_safe_failover(self):
        # 5. A timeout causes safe failover
        p_groq = MockProvider("GROQ")
        p_gemini = MockProvider("GEMINI")

        p_groq.complete.side_effect = asyncio.TimeoutError("timed out")
        p_gemini.complete.return_value = "gemini_ok"

        pool = HealthAwareProviderPool([p_groq, p_gemini], role="scout")

        res = await pool.complete("sys", "user")
        assert res == "gemini_ok"
        assert p_groq.complete.call_count == 1
        assert p_gemini.complete.call_count == 1

    @pytest.mark.asyncio
    async def test_6_and_7_repeated_failures_open_circuit_and_skipped(self):
        # 6. Repeated transient failures open that provider's circuit
        # 7. Open provider is skipped by normal scheduling
        p_groq = MockProvider("GROQ")
        p_gemini = MockProvider("GEMINI")

        # p_groq fails with 503
        p_groq.complete.side_effect = make_http_error(503)
        p_gemini.complete.return_value = "gemini_success"

        fake_time = 1000.0
        pool = HealthAwareProviderPool(
            [p_groq, p_gemini],
            role="scout",
            failure_threshold=3,
            cooldown_seconds=60.0,
            clock=lambda: fake_time,
        )

        # Call 1: selects groq (fails) -> failover to gemini (success). Groq failures: 1
        await pool.complete("sys", "u1")
        assert pool.endpoints[0].consecutive_failures == 1
        assert pool.endpoints[0].circuit_state == CircuitState.CLOSED

        # Call 2: round robin selects gemini (success). Groq cursor not touched.
        await pool.complete("sys", "u2")

        # Call 3: selects groq (fails) -> failover to gemini (success). Groq failures: 2
        await pool.complete("sys", "u3")
        assert pool.endpoints[0].consecutive_failures == 2

        # Call 4: round robin selects gemini (success)
        await pool.complete("sys", "u4")

        # Call 5: selects groq (fails) -> failover to gemini (success). Groq failures: 3 -> CIRCUIT OPEN!
        await pool.complete("sys", "u5")
        assert pool.endpoints[0].circuit_state == CircuitState.OPEN
        assert pool.endpoints[0].cooldown_until == fake_time + 60.0

        # Now Groq is OPEN.
        # Call 6 and Call 7: Groq MUST be skipped without any invocation!
        p_groq_calls_before = p_groq.complete.call_count
        await pool.complete("sys", "u6")
        await pool.complete("sys", "u7")
        assert p_groq.complete.call_count == p_groq_calls_before

    @pytest.mark.asyncio
    async def test_8_9_10_half_open_probe_restore_and_reopen(self):
        # 8. After cooldown, one half-open probe is allowed
        # 9. Successful half-open probe restores provider
        # 10. Failed half-open probe reopens circuit
        p_groq = MockProvider("GROQ")
        p_gemini = MockProvider("GEMINI")

        current_time = 100.0

        pool = HealthAwareProviderPool(
            [p_groq, p_gemini],
            failure_threshold=2,
            cooldown_seconds=30.0,
            clock=lambda: current_time,
        )

        # Force groq into OPEN circuit
        p_groq.complete.side_effect = make_http_error(500)
        p_gemini.complete.return_value = "gemini_ok"
        await pool.complete("sys", "u1")  # groq fails, gemini succeeds -> cursor 1
        await pool.complete("sys", "u2")  # gemini succeeds -> cursor 0
        await pool.complete("sys", "u3")  # groq fails (count 2 -> OPEN), gemini succeeds -> cursor 1
        assert pool.endpoints[0].circuit_state == CircuitState.OPEN

        # Call once more while Groq is OPEN to cycle cursor back to 0
        await pool.complete("sys", "u4")  # starts at 1 (gemini), gemini succeeds -> cursor 0

        # Fast-forward time past cooldown
        current_time = 135.0

        # Groq probe succeeds
        p_groq.complete.side_effect = None
        p_groq.complete.return_value = "groq_restored"

        res = await pool.complete("sys", "probe")  # starts at 0 (groq) in HALF_OPEN
        assert res == "groq_restored"
        assert pool.endpoints[0].circuit_state == CircuitState.CLOSED
        assert pool.endpoints[0].consecutive_failures == 0
        # Cursor is now 1

        # Test 10: Failed probe reopens circuit
        p_groq.complete.side_effect = make_http_error(500)
        await pool.complete("sys", "f1")  # starts at 1 (gemini succeeds) -> cursor 0
        await pool.complete("sys", "f2")  # starts at 0 (groq fails) -> cursor 1
        await pool.complete("sys", "f3")  # starts at 1 (gemini succeeds) -> cursor 0
        await pool.complete("sys", "f4")  # starts at 0 (groq fails 2nd time -> OPEN) -> cursor 1
        assert pool.endpoints[0].circuit_state == CircuitState.OPEN

        # Cycle cursor to 0
        await pool.complete("sys", "f5")  # starts at 1 (gemini succeeds) -> cursor 0

        # Fast forward past cooldown again
        current_time = 170.0
        # Probe fails (starts at 0)
        await pool.complete("sys", "probe_fail")
        assert pool.endpoints[0].circuit_state == CircuitState.OPEN
        assert pool.endpoints[0].cooldown_until == 170.0 + 30.0

    @pytest.mark.asyncio
    async def test_11_auth_failure_does_not_rapid_retry_same_provider(self):
        # 11. 401/403 does not create rapid retry loops on the same provider
        p_groq = MockProvider("GROQ")
        p_gemini = MockProvider("GEMINI")

        p_groq.complete.side_effect = make_http_error(401)
        p_gemini.complete.return_value = "gemini_ok"

        pool = HealthAwareProviderPool([p_groq, p_gemini], cooldown_seconds=60.0)

        # Request 1: Groq fails with 401 -> fails over to Gemini -> Groq circuit opened
        res = await pool.complete("sys", "u1")
        assert res == "gemini_ok"
        assert pool.endpoints[0].circuit_state == CircuitState.OPEN

        # Request 2: Groq is NOT called again
        await pool.complete("sys", "u2")
        assert p_groq.complete.call_count == 1
        assert p_gemini.complete.call_count == 2

    @pytest.mark.asyncio
    async def test_12_http_413_is_not_blindly_sprayed_across_providers(self):
        # 12. 413 is not blindly sprayed across all providers
        p_groq = MockProvider("GROQ")
        p_gemini = MockProvider("GEMINI")
        p_mistral = MockProvider("MISTRAL")

        p_groq.complete.side_effect = make_http_error(413)
        p_gemini.complete.return_value = "ok"
        p_mistral.complete.return_value = "ok"

        pool = HealthAwareProviderPool([p_groq, p_gemini, p_mistral])

        with pytest.raises(LLMInvocationError) as exc_info:
            await pool.complete("sys", "huge_payload")

        assert "HTTP 413" in str(exc_info.value)
        assert "Refusing to fail over" in str(exc_info.value)
        # Groq called once, other providers NEVER called!
        assert p_groq.complete.call_count == 1
        assert p_gemini.complete.call_count == 0
        assert p_mistral.complete.call_count == 0

    @pytest.mark.asyncio
    async def test_13_invalid_malformed_scout_json_fails_validation(self):
        # 13. Invalid/malformed Scout JSON from one provider cannot bypass Scout Pydantic validation
        mock_provider = MockProvider("GROQ")
        mock_provider.complete.return_value = '{"findings": [{"not_a_valid_schema": true}]}'

        agent = ScoutAgent(llm_provider=mock_provider)
        event = GitHubPushEvent(
            delivery_id="d-1",
            installation_id=1,
            repository_id=10,
            repository_owner="owner",
            repository_name="repo",
            default_branch="main",
            before_sha="0" * 40,
            after_sha="1" * 40,
            ref="refs/heads/main",
        )
        ctx = ScoutContext(
            installation_id=1,
            repository_id=10,
            owner="owner",
            name="repo",
            before_sha="0" * 40,
            commit_sha="1" * 40,
        )

        with pytest.raises(MalformedScoutResponse) as exc_info:
            await agent.discover(event, ctx)

        assert "failed schema validation" in str(exc_info.value)

    def test_14_missing_api_key_in_pool_fails_configuration_clearly(self):
        # 14. Explicitly configured pool provider without an API key fails configuration clearly
        env = {
            "SCOUT_PROVIDERS": "GROQ,MISTRAL",
            "GROQ_API_KEYS": "valid-groq-key",
            # MISTRAL_API_KEYS is missing!
        }
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ProviderConfigurationError) as exc_info:
                create_scout_provider()

            assert "SCOUT_PROVIDERS explicitly includes 'MISTRAL'" in str(exc_info.value)

    def test_15_legacy_scout_provider_single_mode_still_works(self):
        # 15. Legacy SCOUT_PROVIDER single-provider configuration still works
        env = {
            "SCOUT_PROVIDER": "GROQ",
            "SCOUT_MODEL_ID": "qwen/custom-model",
            "GROQ_API_KEYS": "my-key",
        }
        with patch.dict(os.environ, env, clear=True):
            prov = create_scout_provider()
            assert isinstance(prov, GroqProvider)
            assert prov.model == "qwen/custom-model"

    def test_16_scout_providers_pool_mode_creates_multiple_providers(self):
        # 16. SCOUT_PROVIDERS pool mode creates more than one provider when configured
        env = {
            "SCOUT_PROVIDERS": "GROQ,GEMINI,MISTRAL,NVIDIA",
            "GROQ_API_KEYS": "k1",
            "GEMINI_API_KEYS": "k2",
            "MISTRAL_API_KEYS": "k3",
            "NVIDIA_API_KEYS": "k4",
        }
        with patch.dict(os.environ, env, clear=True):
            pool = create_scout_provider()
            assert isinstance(pool, HealthAwareProviderPool)
            assert len(pool.endpoints) == 4
            names = [ep.provider_name for ep in pool.endpoints]
            assert names == ["GROQ", "GEMINI", "MISTRAL", "NVIDIA"]

    def test_17_and_18_per_provider_role_model_ids_resolve_correctly(self):
        # 17. Per-provider role model IDs resolve correctly
        # 18. Generic SCOUT_MODEL_ID is not incorrectly applied to every provider in pool mode
        env = {
            "SCOUT_PROVIDERS": "GROQ,GEMINI,MISTRAL,NVIDIA",
            "SCOUT_MODEL_ID": "generic-model-not-for-pool",
            "SCOUT_GROQ_MODEL_ID": "groq-model-x",
            "SCOUT_GEMINI_MODEL_ID": "gemini-model-y",
            "MISTRAL_MODEL": "mistral-default-z",
            # NVIDIA uses DEFAULT_PROVIDER_MODELS
            "GROQ_API_KEYS": "k1",
            "GEMINI_API_KEYS": "k2",
            "MISTRAL_API_KEYS": "k3",
            "NVIDIA_API_KEYS": "k4",
        }
        with patch.dict(os.environ, env, clear=True):
            pool = create_scout_provider()
            models = {ep.provider_name: ep.model_name for ep in pool.endpoints}
            assert models["GROQ"] == "groq-model-x"
            assert models["GEMINI"] == "gemini-model-y"
            assert models["MISTRAL"] == "mistral-default-z"
            assert models["NVIDIA"] == DEFAULT_PROVIDER_MODELS["NVIDIA"]
            # Generic model is never used in pool mode!
            assert "generic-model-not-for-pool" not in models.values()

    def test_19_scout_and_verifier_get_independent_pool_instances(self):
        # 19. Scout and Verifier get independent provider-pool instances
        env = {
            "SCOUT_PROVIDERS": "GROQ,GEMINI",
            "VERIFIER_PROVIDERS": "GROQ,GEMINI",
            "GROQ_API_KEYS": "k1",
            "GEMINI_API_KEYS": "k2",
        }
        with patch.dict(os.environ, env, clear=True):
            scout_pool = create_scout_provider()
            verifier_pool = create_verifier_provider()

            assert scout_pool is not verifier_pool
            assert scout_pool.endpoints[0] is not verifier_pool.endpoints[0]
            assert scout_pool.role == "scout"
            assert verifier_pool.role == "verifier"

    def test_20_no_secrets_in_error_message(self):
        # 20. No API key or prompt body appears in error/log output
        p_groq = MockProvider("GROQ")
        secret_key = "sk-super-secret-password-12345"
        p_groq.complete.side_effect = httpx.HTTPStatusError(
            "401 Unauthorized",
            request=httpx.Request("POST", "http://api.groq.com", headers={"Authorization": f"Bearer {secret_key}"}),
            response=httpx.Response(401, request=httpx.Request("POST", "http://api.groq.com")),
        )
        pool = HealthAwareProviderPool([p_groq])

        with pytest.raises(LLMInvocationError) as exc_info:
            asyncio.run(pool.complete("secret system prompt", "secret user prompt"))

        err_str = str(exc_info.value)
        assert secret_key not in err_str
        assert "secret user prompt" not in err_str
        assert "secret system prompt" not in err_str

    @pytest.mark.asyncio
    async def test_21_concurrent_selection_does_not_corrupt_cursor(self):
        # 21. Concurrent selection does not corrupt the round-robin cursor
        providers = [
            MockProvider("GROQ"),
            MockProvider("GEMINI"),
            MockProvider("MISTRAL"),
            MockProvider("NVIDIA"),
        ]

        async def delayed_complete(sys, user):
            await asyncio.sleep(0.01)
            return "ok"

        for p in providers:
            p.complete.side_effect = delayed_complete

        pool = HealthAwareProviderPool(providers, role="scout")

        # Run 20 concurrent requests
        tasks = [pool.complete("sys", f"u{i}") for i in range(20)]
        results = await asyncio.gather(*tasks)

        assert len(results) == 20
        # Exactly 5 calls to each of the 4 providers
        counts = [p.complete.call_count for p in providers]
        assert counts == [5, 5, 5, 5]

    def test_conflicting_single_and_pool_config_fails_closed(self):
        env = {
            "SCOUT_PROVIDERS": "GROQ,GEMINI",
            "SCOUT_PROVIDER": "GROQ",
            "GROQ_API_KEYS": "k1",
            "GEMINI_API_KEYS": "k2",
        }
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(ProviderConfigurationError) as exc_info:
                create_scout_provider()
            assert "Both SCOUT_PROVIDERS and SCOUT_PROVIDER are configured" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_404_configuration_failure_immediately_quarantines_endpoint(self):
        # 404 configuration failure immediately removes/quarantines endpoint from normal scheduling
        p1 = MockProvider("GEMINI")
        p2 = MockProvider("GROQ")

        p1.complete.side_effect = make_http_error(404)
        p2.complete.return_value = "groq_ok"

        pool = HealthAwareProviderPool([p1, p2], failure_threshold=3)

        # First request: P1 fails with 404 -> fails over to P2 -> P1 is immediately quarantined
        res1 = await pool.complete("sys", "u1")
        assert res1 == "groq_ok"
        assert pool.endpoints[0].is_quarantined is True
        assert pool.endpoints[0].circuit_state == CircuitState.OPEN
        assert pool.endpoints[0].cooldown_until == float("inf")

        # Second request: P1 is NOT called because it is quarantined; goes directly to P2
        res2 = await pool.complete("sys", "u2")
        assert res2 == "groq_ok"
        assert p1.complete.call_count == 1
        assert p2.complete.call_count == 2

    @pytest.mark.asyncio
    async def test_410_retired_model_immediately_quarantines_endpoint(self):
        # 410 retired model immediately removes/quarantines endpoint from normal scheduling
        p1 = MockProvider("NVIDIA")
        p2 = MockProvider("GROQ")

        p1.complete.side_effect = make_http_error(410)
        p2.complete.return_value = "groq_ok"

        pool = HealthAwareProviderPool([p1, p2], failure_threshold=3)

        # First request: P1 fails with 410 -> fails over to P2 -> P1 is immediately quarantined
        res1 = await pool.complete("sys", "u1")
        assert res1 == "groq_ok"
        assert pool.endpoints[0].is_quarantined is True
        assert pool.endpoints[0].circuit_state == CircuitState.OPEN
        assert pool.endpoints[0].cooldown_until == float("inf")

        # Second request: P1 is NOT called because it is quarantined; goes directly to P2
        res2 = await pool.complete("sys", "u2")
        assert res2 == "groq_ok"
        assert p1.complete.call_count == 1
        assert p2.complete.call_count == 2

    @pytest.mark.asyncio
    async def test_429_remains_transient_and_uses_cooldown_half_open_recovery(self):
        # 429 remains transient and uses cooldown/half-open recovery
        p1 = MockProvider("GROQ")
        p2 = MockProvider("GEMINI")

        current_time = 100.0
        pool = HealthAwareProviderPool(
            [p1, p2],
            failure_threshold=2,
            cooldown_seconds=30.0,
            clock=lambda: current_time,
        )

        p1.complete.side_effect = make_http_error(429)
        p2.complete.return_value = "gemini_ok"

        # Call 1: P1 fails once (transient), fails over to P2. P1 is NOT quarantined
        res1 = await pool.complete("sys", "u1")
        assert res1 == "gemini_ok"
        assert pool.endpoints[0].is_quarantined is False
        assert pool.endpoints[0].consecutive_failures == 1
        assert pool.endpoints[0].circuit_state == CircuitState.CLOSED

        # Call 2: Starts on P2 (cursor was 1)
        res2 = await pool.complete("sys", "u2")
        assert res2 == "gemini_ok"

        # Call 3: Starts on P1 (cursor was 0). Fails 2nd time -> circuit opens with standard cooldown (NOT inf)
        res3 = await pool.complete("sys", "u3")
        assert res3 == "gemini_ok"
        assert pool.endpoints[0].is_quarantined is False
        assert pool.endpoints[0].circuit_state == CircuitState.OPEN
        assert pool.endpoints[0].cooldown_until == 130.0  # 100 + 30

        # Fast forward time past cooldown
        current_time = 135.0
        p1.complete.side_effect = None
        p1.complete.return_value = "groq_recovered"

        # Advance cursor so P1 is next
        await pool.complete("sys", "u4")  # P2 called, cursor advances to 0

        # Probe P1: transitions from HALF_OPEN to CLOSED upon success
        res5 = await pool.complete("sys", "u5")
        assert res5 == "groq_recovered"
        assert pool.endpoints[0].circuit_state == CircuitState.CLOSED
        assert pool.endpoints[0].consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_after_permanent_failure_healthy_providers_continue_round_robin(self):
        # After a permanent provider failure, healthy providers continue round-robin
        p1 = MockProvider("GEMINI")
        p2 = MockProvider("GROQ")
        p3 = MockProvider("NVIDIA")

        p1.complete.side_effect = make_http_error(404)
        p2.complete.return_value = "groq_ok"
        p3.complete.return_value = "nvidia_ok"

        pool = HealthAwareProviderPool([p1, p2, p3])

        # Request 1: P1 fails (quarantined), fails over to P2
        res1 = await pool.complete("sys", "u1")
        assert res1 == "groq_ok"
        assert pool.endpoints[0].is_quarantined is True

        # Next 4 requests alternate strictly between healthy providers: P2 -> P3 -> P2 -> P3
        res2 = await pool.complete("sys", "u2")
        assert res2 == "groq_ok"

        res3 = await pool.complete("sys", "u3")
        assert res3 == "nvidia_ok"

        res4 = await pool.complete("sys", "u4")
        assert res4 == "groq_ok"

        res5 = await pool.complete("sys", "u5")
        assert res5 == "nvidia_ok"

        # P1 was called exactly once during failover, then never touched again
        assert p1.complete.call_count == 1
        assert p2.complete.call_count == 3
        assert p3.complete.call_count == 2

    @pytest.mark.asyncio
    async def test_no_secrets_or_prompts_in_logs_or_errors(self, caplog):
        # No secrets/request bodies appear in logs/errors
        import logging
        caplog.set_level(logging.DEBUG)

        secret_token = "ghp_super_secret_token_abcdef123456"
        secret_prompt = "CONFIDENTIAL_SOURCE_CODE_DO_NOT_LEAK"

        p = MockProvider("GROQ")
        p.complete.side_effect = httpx.HTTPStatusError(
            f"401 Unauthorized for token {secret_token}",
            request=httpx.Request("POST", f"http://api.groq.com?token={secret_token}"),
            response=httpx.Response(401, request=httpx.Request("POST", "http://api.groq.com")),
        )

        pool = HealthAwareProviderPool([p])

        with pytest.raises(LLMInvocationError) as exc_info:
            await pool.complete("system", secret_prompt)

        err_text = str(exc_info.value)
        assert secret_token not in err_text
        assert secret_prompt not in err_text

        # Also inspect caplog
        all_logs = caplog.text
        assert secret_token not in all_logs
        assert secret_prompt not in all_logs
