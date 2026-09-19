import os
import json
import pytest
import httpx
from unittest.mock import AsyncMock, patch, MagicMock

from app.infrastructure.llm_router import (
    create_scout_provider,
    create_verifier_provider,
    NvidiaNimProvider,
    GroqProvider,
)
from app.scout.agent import ScoutAgent, MalformedScoutResponse
from app.verifier.agent import run_verifier, LLMProvider
from app.verifier.schemas import LLMInvocationError, MalformedVerifierResponse
from app.domain.models import Finding, RepoContext, EvidenceItem
from app.domain.states import FindingStatus, VerificationStatus
from app.services.pipeline import VerifierPipeline


@pytest.mark.asyncio
async def test_scout_nvidia_verifier_groq_routing():
    env = {
        "SCOUT_PROVIDER": "NVIDIA",
        "SCOUT_MODEL_ID": "model-A",
        "NVIDIA_API_KEY": "mock-nvidia-key",
        "VERIFIER_PROVIDER": "GROQ",
        "VERIFIER_MODEL_ID": "model-B",
        "GROQ_API_KEY": "mock-groq-key",
    }
    with patch.dict(os.environ, env, clear=True):
        scout_prov = create_scout_provider()
        verifier_prov = create_verifier_provider()

        assert isinstance(scout_prov, NvidiaNimProvider)
        assert scout_prov.model == "model-A"

        assert isinstance(verifier_prov, GroqProvider)
        assert verifier_prov.model == "model-B"

        # Test request payloads
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "ok"}}]
        }
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            # Scout invocation
            res_scout = await scout_prov.complete("sys", "usr")
            assert res_scout == "ok"
            assert mock_post.call_count == 1
            call_url = mock_post.call_args[0][0]
            call_json = mock_post.call_args[1]["json"]
            assert "integrate.api.nvidia.com" in call_url
            assert call_json["model"] == "model-A"

            # Verifier invocation
            mock_post.reset_mock()
            res_verifier = await verifier_prov.complete("sys", "usr")
            assert res_verifier == "ok"
            assert mock_post.call_count == 1
            call_url = mock_post.call_args[0][0]
            call_json = mock_post.call_args[1]["json"]
            assert "api.groq.com" in call_url
            assert call_json["model"] == "model-B"


@pytest.mark.asyncio
async def test_reverse_roles_routing():
    env = {
        "SCOUT_PROVIDER": "GROQ",
        "SCOUT_MODEL_ID": "model-B",
        "GROQ_API_KEY": "mock-groq-key",
        "VERIFIER_PROVIDER": "NVIDIA",
        "VERIFIER_MODEL_ID": "model-A",
        "NVIDIA_API_KEY": "mock-nvidia-key",
    }
    with patch.dict(os.environ, env, clear=True):
        scout_prov = create_scout_provider()
        verifier_prov = create_verifier_provider()

        assert isinstance(scout_prov, GroqProvider)
        assert scout_prov.model == "model-B"

        assert isinstance(verifier_prov, NvidiaNimProvider)
        assert verifier_prov.model == "model-A"

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "ok"}}]
        }
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response

            # Scout invocation
            await scout_prov.complete("sys", "usr")
            call_url = mock_post.call_args[0][0]
            call_json = mock_post.call_args[1]["json"]
            assert "api.groq.com" in call_url
            assert call_json["model"] == "model-B"

            # Verifier invocation
            mock_post.reset_mock()
            await verifier_prov.complete("sys", "usr")
            call_url = mock_post.call_args[0][0]
            call_json = mock_post.call_args[1]["json"]
            assert "integrate.api.nvidia.com" in call_url
            assert call_json["model"] == "model-A"


@pytest.mark.asyncio
async def test_scout_provider_failure_does_not_mutate_verifier_state():
    env = {
        "SCOUT_PROVIDER": "NVIDIA",
        "SCOUT_MODEL_ID": "model-A",
        "NVIDIA_API_KEY": "mock-nvidia-key",
        "VERIFIER_PROVIDER": "GROQ",
        "VERIFIER_MODEL_ID": "model-B",
        "GROQ_API_KEY": "mock-groq-key",
    }
    with patch.dict(os.environ, env, clear=True):
        scout_prov = create_scout_provider()
        verifier_prov = create_verifier_provider()

        # Simulate Scout failure
        with patch("httpx.AsyncClient.post", side_effect=httpx.ConnectError("NVIDIA connection failed")):
            with pytest.raises(httpx.ConnectError):
                await scout_prov.complete("sys", "usr")

        # Verifier must remain unaffected, configured for Groq model-B
        assert isinstance(verifier_prov, GroqProvider)
        assert verifier_prov.model == "model-B"

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "verifier-ok"}}]
        }
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            res = await verifier_prov.complete("sys", "usr")
            assert res == "verifier-ok"
            call_url = mock_post.call_args[0][0]
            call_json = mock_post.call_args[1]["json"]
            assert "api.groq.com" in call_url
            assert call_json["model"] == "model-B"


@pytest.mark.asyncio
async def test_verifier_provider_failure_does_not_alter_scout_routing_state():
    env = {
        "SCOUT_PROVIDER": "NVIDIA",
        "SCOUT_MODEL_ID": "model-A",
        "NVIDIA_API_KEY": "mock-nvidia-key",
        "VERIFIER_PROVIDER": "GROQ",
        "VERIFIER_MODEL_ID": "model-B",
        "GROQ_API_KEY": "mock-groq-key",
    }
    with patch.dict(os.environ, env, clear=True):
        scout_prov = create_scout_provider()
        verifier_prov = create_verifier_provider()

        # Simulate Verifier failure
        with patch("httpx.AsyncClient.post", side_effect=httpx.HTTPStatusError("Rate limited", request=MagicMock(), response=MagicMock(status_code=429))):
            with pytest.raises(httpx.HTTPStatusError):
                await verifier_prov.complete("sys", "usr")

        # Scout must remain unaffected, configured for NVIDIA model-A
        assert isinstance(scout_prov, NvidiaNimProvider)
        assert scout_prov.model == "model-A"

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "scout-ok"}}]
        }
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            res = await scout_prov.complete("sys", "usr")
            assert res == "scout-ok"
            call_url = mock_post.call_args[0][0]
            call_json = mock_post.call_args[1]["json"]
            assert "integrate.api.nvidia.com" in call_url
            assert call_json["model"] == "model-A"


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type,error_payload", [
    ("429", httpx.HTTPStatusError("Rate limit", request=MagicMock(), response=MagicMock(status_code=429))),
    ("timeout", httpx.TimeoutException("Read timeout")),
    ("transport", httpx.NetworkError("Connection reset")),
    ("malformed_json", "this is not json at all {[["),
    ("schema_invalid", json.dumps({"findings": "not-a-list"})),
])
async def test_scout_fail_closed_on_provider_errors(error_type, error_payload):
    mock_llm = MagicMock(spec=LLMProvider)
    if isinstance(error_payload, Exception):
        mock_llm.complete = AsyncMock(side_effect=error_payload)
    else:
        mock_llm.complete = AsyncMock(return_value=error_payload)

    agent = ScoutAgent(llm_provider=mock_llm)

    from app.github.events import GitHubPushEvent
    from app.scout.models import ScoutContext

    event = GitHubPushEvent(
        delivery_id="del-1",
        installation_id=1,
        repository_id=123,
        repository_owner="owner",
        repository_name="repo",
        default_branch="main",
        before_sha="0"*40,
        after_sha="a"*40,
        ref="refs/heads/main",
        forced=False,
        deleted=False,
    )
    context = ScoutContext(
        delivery_id="del-1",
        installation_id=1,
        repository_id=123,
        owner="owner",
        name="repo",
        default_branch="main",
        before_sha="0"*40,
        commit_sha="a"*40,
        ref="refs/heads/main",
        is_initial_push=True,
        is_forced_push=False,
    )

    with pytest.raises(MalformedScoutResponse):
        await agent.discover(event, context)


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type,error_payload", [
    ("429", httpx.HTTPStatusError("Rate limit", request=MagicMock(), response=MagicMock(status_code=429))),
    ("timeout", httpx.TimeoutException("Read timeout")),
    ("transport", httpx.NetworkError("Connection reset")),
    ("malformed_json", "not valid json {{{{"),
    ("schema_invalid", json.dumps({"status": "NOT_A_VALID_STATUS"})),
])
async def test_verifier_fail_closed_on_provider_errors(error_type, error_payload):
    mock_llm = MagicMock(spec=LLMProvider)
    if isinstance(error_payload, Exception):
        mock_llm.complete = AsyncMock(side_effect=error_payload)
    else:
        mock_llm.complete = AsyncMock(return_value=error_payload)

    pipeline = VerifierPipeline(llm_provider=mock_llm)

    finding = Finding(
        finding_id="find-err-1",
        installation_id="1",
        repository_id="123",
        commit_sha="c"*40,
        title="Missing check",
        severity="medium",
        category="security",
        file="app/main.py",
        function="handle",
        line=10,
        description="Missing check",
        expected_behavior="Must check",
        evidence=[EvidenceItem(file="app/main.py", line=10, snippet="def handle(): pass")],
        confidence=0.9,
    )
    repo_context = RepoContext(
        repository_id="123",
        installation_id="1",
        owner="owner",
        name="repo",
        commit_sha="c"*40,
        primary_file_content="def handle(): pass\n",
        evidence_files={"app/main.py": "def handle(): pass\n"},
    )
    mock_write_client = AsyncMock()

    # 1. run_verifier must raise error on provider failure
    with pytest.raises((LLMInvocationError, MalformedVerifierResponse)):
        await run_verifier(finding, repo_context, mock_llm)

    # 2. pipeline.run must fail closed: returns None, zero issues created
    result = await pipeline.run(
        finding,
        repo_context,
        "owner",
        "repo",
        github_write_client=mock_write_client,
    )

    assert result is None
    mock_write_client.create_issue.assert_not_called()
