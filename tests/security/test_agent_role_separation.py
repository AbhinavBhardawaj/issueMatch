import pytest
from unittest.mock import AsyncMock, MagicMock
from app.scout.agent import ScoutAgent
from app.verifier.agent import run_verifier, LLMProvider
from app.services.pipeline import VerifierPipeline
from app.github.repo_fetcher import fetch_repo_context
from app.domain.models import Finding, EvidenceItem, RepoContext, RepoContextFile, VerifierEvidence
from app.domain.states import VerificationStatus


class DummyLLM(LLMProvider):
    async def complete(self, system: str, user: str) -> str:
        return '{"status": "REJECTED", "reason": "none", "confidence": 0.5, "supporting_evidence": [], "counter_evidence": [], "duplicate_issue": false}'


def test_scout_agent_has_no_write_capabilities():
    """ScoutAgent must have zero write client attributes and zero issue creation methods."""
    agent = ScoutAgent(llm_provider=DummyLLM())

    assert not hasattr(agent, "github_client")
    assert not hasattr(agent, "github_write_client")
    assert not hasattr(agent, "create_issue")
    assert not hasattr(agent, "verifier_pipeline")
    assert not hasattr(agent, "pipeline")


def test_pipeline_constructor_rejects_write_client():
    """VerifierPipeline must not hold a persistent write client."""
    pipeline = VerifierPipeline(llm_provider=DummyLLM())
    assert not hasattr(pipeline, "github_client")
    assert not hasattr(pipeline, "github_write_client")
    assert not hasattr(pipeline, "write_client")


@pytest.mark.asyncio
async def test_verifier_receives_no_write_client():
    """run_verifier function signature accepts only Finding, RepoContext, and LLMProvider."""
    import inspect
    sig = inspect.signature(run_verifier)
    params = list(sig.parameters.keys())
    assert params == ["finding", "repo_context", "llm_provider"]
    assert "github_client" not in params
    assert "client" not in params


@pytest.mark.asyncio
async def test_verifier_context_independently_fetched_pinned_to_sha():
    """
    Verifier context is fetched independently from the GitHub read client
    pinned strictly to finding.commit_sha, not reusing Scout's mutable context.
    """
    mock_read_client = AsyncMock()
    mock_read_client.get_repository.return_value = {
        "id": 200,
        "name": "core-repo",
        "owner": {"login": "org"},
        "default_branch": "main",
    }
    mock_read_client.get_file_content.return_value = "def foo(): pass\n"
    mock_read_client.get_repository_tree.return_value = ([{"path": "app/main.py", "type": "blob"}], False)
    mock_read_client.get_issues.return_value = []

    commit_sha = "f" * 40
    finding = Finding(
        finding_id="F-indep",
        installation_id="100",
        repository_id="200",
        commit_sha=commit_sha,
        title="Sample",
        severity="medium",
        file="app/main.py",
        description="test",
        expected_behavior="test",
        evidence=[EvidenceItem(file="app/main.py", line=1, snippet="def foo(): pass")],
        confidence=0.9,
    )

    ctx = await fetch_repo_context(
        client=mock_read_client,
        owner="org",
        repo_name="core-repo",
        finding=finding,
    )

    assert ctx.commit_sha == commit_sha
    assert ctx.repository_id == "200"
    assert ctx.installation_id == "100"
    # Read client was queried with the finding's exact commit_sha
    mock_read_client.get_file_content.assert_any_call("org", "core-repo", "app/main.py", ref=commit_sha)
