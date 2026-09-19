import pytest
from unittest.mock import AsyncMock, MagicMock
from app.github.events import GitHubPushEvent
from app.scout.service import ScoutService
from app.scout.models import ScoutRunResult, SuppressionReason
from app.scout.schemas import ScoutResponse, ScoutFindingDraft, ScoutEvidenceDraft
from app.domain.models import Finding
from app.domain.states import FindingStatus

@pytest.fixture
def push_event():
    return GitHubPushEvent(
        delivery_id="del-1",
        installation_id=100,
        repository_id=200,
        repository_owner="test-org",
        repository_name="test-repo",
        default_branch="main",
        before_sha="a" * 40,
        after_sha="b" * 40,
        ref="refs/heads/main",
        forced=False,
        deleted=False,
    )

@pytest.fixture
def mock_read_client():
    client = AsyncMock()
    # Metadata matches repository_id
    client.get_repository.return_value = {"id": 200, "name": "test-repo", "owner": {"login": "test-org"}}
    client.compare_commits.return_value = {
        "files": [
            {"filename": "app/core.py", "status": "modified", "additions": 10, "deletions": 2, "changes": 12}
        ]
    }
    client.get_file_content.return_value = "def compute():\n    return 42\n"
    client.get_issues.return_value = []
    client.get_repository_tree.return_value = ([{"path": "app/core.py", "type": "blob"}], False)
    return client

@pytest.mark.asyncio
async def test_identity_mismatch_fails_closed(push_event, mock_read_client):
    # Metadata returns different repo ID
    mock_read_client.get_repository.return_value = {"id": 999, "name": "test-repo", "owner": {"login": "test-org"}}
    
    scout_agent = MagicMock()
    verifier_pipeline = MagicMock()
    service = ScoutService(
        scout_agent=scout_agent,
        verifier_pipeline=verifier_pipeline,
        read_client_factory=AsyncMock(return_value=mock_read_client),
    )

    result = await service.process_push(push_event)
    assert isinstance(result, ScoutRunResult)
    assert any("IDENTITY_CHECK_FAILED" in f for f in result.failures)
    assert result.ai_drafts == 0
    scout_agent.discover.assert_not_called()
    verifier_pipeline.run.assert_not_called()

@pytest.mark.asyncio
async def test_initial_push_uses_tree_scan(push_event, mock_read_client):
    initial_push = GitHubPushEvent(
        delivery_id="del-init",
        installation_id=100,
        repository_id=200,
        repository_owner="test-org",
        repository_name="test-repo",
        default_branch="main",
        before_sha="0" * 40,
        after_sha="b" * 40,
        ref="refs/heads/main",
        forced=False,
        deleted=False,
    )
    
    scout_agent = AsyncMock()
    scout_agent.discover.return_value = ScoutResponse(findings=[])
    verifier_pipeline = AsyncMock()
    service = ScoutService(
        scout_agent=scout_agent,
        verifier_pipeline=verifier_pipeline,
        read_client_factory=AsyncMock(return_value=mock_read_client),
    )

    result = await service.process_push(initial_push)
    assert mock_read_client.get_repository_tree.call_count >= 1
    mock_read_client.compare_commits.assert_not_called()
    assert result.ai_drafts == 0

@pytest.mark.asyncio
async def test_forced_push_fallback_when_compare_fails(push_event, mock_read_client):
    forced_push = GitHubPushEvent(
        delivery_id="del-forced",
        installation_id=100,
        repository_id=200,
        repository_owner="test-org",
        repository_name="test-repo",
        default_branch="main",
        before_sha="a" * 40,
        after_sha="b" * 40,
        ref="refs/heads/main",
        forced=True,
        deleted=False,
    )
    
    # compare_commits fails for forced push
    mock_read_client.compare_commits.side_effect = RuntimeError("Unrelated histories")
    scout_agent = AsyncMock()
    scout_agent.discover.return_value = ScoutResponse(findings=[])
    verifier_pipeline = AsyncMock()
    service = ScoutService(
        scout_agent=scout_agent,
        verifier_pipeline=verifier_pipeline,
        read_client_factory=AsyncMock(return_value=mock_read_client),
    )

    result = await service.process_push(forced_push)
    assert mock_read_client.get_repository_tree.call_count >= 1
    assert result.ai_drafts == 0

@pytest.mark.asyncio
async def test_agent_failure_handled_gracefully(push_event, mock_read_client):
    scout_agent = AsyncMock()
    scout_agent.discover.side_effect = RuntimeError("LLM rate limit reached")
    verifier_pipeline = AsyncMock()
    service = ScoutService(
        scout_agent=scout_agent,
        verifier_pipeline=verifier_pipeline,
        read_client_factory=AsyncMock(return_value=mock_read_client),
    )

    result = await service.process_push(push_event)
    assert any("SCOUT_AGENT_FAILED" in f for f in result.failures)
    assert result.ai_drafts == 0
    verifier_pipeline.run.assert_not_called()
