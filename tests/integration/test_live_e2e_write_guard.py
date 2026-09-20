import pytest
from unittest.mock import AsyncMock
from tests.integration.test_live_scout_verifier_e2e import (
    GuardedGitHubClient,
    LiveWriteDisabledError,
)


@pytest.mark.asyncio
async def test_live_write_guard_blocks_issue_creation_when_disabled():
    """Assert that GuardedGitHubClient raises LiveWriteDisabledError when allow_writes is False."""
    mock_inner = AsyncMock()
    mock_inner.create_issue.return_value = {"number": 123, "html_url": "https://github.com/test/repo/issues/123"}

    guarded_client = GuardedGitHubClient(mock_inner, allow_writes=False)

    with pytest.raises(LiveWriteDisabledError) as exc_info:
        await guarded_client.create_issue("owner", "repo", "Bug Title", "Bug Body")

    assert "LIVE_E2E_ALLOW_ISSUE_WRITES != '1'" in str(exc_info.value)
    mock_inner.create_issue.assert_not_called()


@pytest.mark.asyncio
async def test_live_write_guard_allows_issue_creation_when_enabled():
    """Assert that GuardedGitHubClient passes through create_issue when allow_writes is True."""
    mock_inner = AsyncMock()
    mock_inner.create_issue.return_value = {"number": 456, "html_url": "https://github.com/test/repo/issues/456"}

    guarded_client = GuardedGitHubClient(mock_inner, allow_writes=True)

    result = await guarded_client.create_issue("owner", "repo", "Bug Title", "Bug Body")

    assert result["number"] == 456
    mock_inner.create_issue.assert_called_once_with("owner", "repo", "Bug Title", "Bug Body")
