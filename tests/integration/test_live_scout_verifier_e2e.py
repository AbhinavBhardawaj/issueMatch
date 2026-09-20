"""Live Provider End-to-End Test.

This test connects to real GitHub and real LLM providers.
It is STRICTLY guarded by environment variables and will be skipped unless explicitly enabled:
  RUN_LIVE_SCOUT_E2E == "1"

Requirements for running:
  RUN_LIVE_SCOUT_E2E=1
  LIVE_E2E_OWNER=<owner>
  LIVE_E2E_REPO=<repo>
  LIVE_E2E_EXPECTED_REPO_ID=<numeric repo id>
  LIVE_E2E_INSTALLATION_ID=<installation id>
  LIVE_E2E_BEFORE_SHA=<40-char parent commit SHA>
  LIVE_E2E_COMMIT_SHA=<40-char target commit SHA>
  LIVE_E2E_ALLOW_ISSUE_WRITES=1 (must be exactly "1" to write issues)
  LIVE_E2E_EXPECT_ISSUE_CREATION=1 (optional; assert at least one issue created)
  LIVE_E2E_EXPECTED_CHANGED_FILE=<relative file path> (optional)

ZERO mocks are used when this test runs.
If any required credential or environment variable is missing, the test SKIPS honestly.
"""
import os
import re
import uuid
import pytest
from dotenv import load_dotenv

# Load local .env for developer convenience; environment variables still take precedence
load_dotenv()

# Strict guard: exactly "1", no python truthiness on arbitrary non-empty strings
pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_SCOUT_E2E") != "1",
    reason="RUN_LIVE_SCOUT_E2E is not '1'. Live provider E2E test skipped honestly.",
)

from app.main import build_scout_service
from app.services.issue_gate import SQLiteDedupStore
from app.services.write_journal import SQLiteIssueWriteJournal
from app.github.events import GitHubPushEvent
from app.github.app_auth import GitHubAppConfig, GitHubAppAuthenticator


class LiveWriteDisabledError(RuntimeError):
    """Raised when an issue write is attempted while LIVE_E2E_ALLOW_ISSUE_WRITES != '1'."""
    pass


class GuardedGitHubClient:
    """Wraps GitHubRestClient in live tests to strictly guard create_issue calls."""

    def __init__(self, inner, allow_writes: bool):
        self._inner = inner
        self._allow_writes = allow_writes

    async def create_issue(self, *args, **kwargs):
        if not self._allow_writes:
            raise LiveWriteDisabledError(
                "LIVE_E2E_ALLOW_ISSUE_WRITES != '1'. Issue write blocked by test harness safety guard."
            )
        return await self._inner.create_issue(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.mark.asyncio
async def test_live_scout_and_verifier_pipeline():
    """Executes live Scout -> Verifier against real configured sandbox repository."""
    owner = os.environ.get("LIVE_E2E_OWNER", "").strip()
    repo = os.environ.get("LIVE_E2E_REPO", "").strip()
    expected_repo_id_str = os.environ.get("LIVE_E2E_EXPECTED_REPO_ID", "").strip()
    inst_id_str = os.environ.get("LIVE_E2E_INSTALLATION_ID", "").strip()
    before_sha = os.environ.get("LIVE_E2E_BEFORE_SHA", "").strip()
    commit_sha = os.environ.get("LIVE_E2E_COMMIT_SHA", "").strip()
    allow_writes = os.environ.get("LIVE_E2E_ALLOW_ISSUE_WRITES") == "1"
    expect_issue_creation = os.environ.get("LIVE_E2E_EXPECT_ISSUE_CREATION") == "1"
    expected_changed_file = os.environ.get("LIVE_E2E_EXPECTED_CHANGED_FILE", "").strip()

    if not owner or not repo or not expected_repo_id_str:
        pytest.skip(
            "LIVE_E2E_OWNER, LIVE_E2E_REPO, or LIVE_E2E_EXPECTED_REPO_ID is not configured."
        )

    if not inst_id_str:
        pytest.skip("LIVE_E2E_INSTALLATION_ID is required for live testing.")

    if not before_sha or not commit_sha:
        pytest.skip(
            "LIVE_E2E_BEFORE_SHA and LIVE_E2E_COMMIT_SHA are required for live testing."
        )

    sha_pattern = re.compile(r"^[0-9a-fA-F]{40}$")
    if not sha_pattern.match(before_sha) or not sha_pattern.match(commit_sha):
        pytest.fail(
            f"Invalid commit SHA format: before_sha='{before_sha}', commit_sha='{commit_sha}'. "
            "Must be 40-character hexadecimal Git commit SHAs."
        )

    try:
        expected_repo_id = int(expected_repo_id_str)
        installation_id = int(inst_id_str)
    except ValueError as exc:
        pytest.fail(f"Numeric conversion failure for repo or installation ID: {exc}")

    # Validate that we are not targeting production issueAnalyzer repository unless explicitly configured
    if repo.lower() == "issueanalyzer" and os.environ.get("CONFIRM_ISSUEANALYZER_AS_SANDBOX") != "1":
        pytest.fail(
            "Safety halt: live sandbox cannot be 'issueAnalyzer' unless CONFIRM_ISSUEANALYZER_AS_SANDBOX=1"
        )

    # Initialize GitHub App authenticator
    try:
        app_config = GitHubAppConfig.from_environment()
        authenticator = GitHubAppAuthenticator(app_config)
    except Exception as exc:
        pytest.skip(f"GitHub App credentials not configured: {exc}")

    # Use temporary sqlite DB for dedup and journal
    import tempfile
    import pathlib
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = str(pathlib.Path(tmp_dir) / "live_e2e.db")
        dedup_store = SQLiteDedupStore(db_path=db_path)
        write_journal = SQLiteIssueWriteJournal(db_path=db_path)

        async def real_client_factory(inst_id: int):
            raw_client = await authenticator.create_installation_client(inst_id)
            return GuardedGitHubClient(raw_client, allow_writes=allow_writes)

        # Build ScoutService with real providers (fail-closed if credentials missing)
        try:
            scout_service = build_scout_service(
                dedup_store=dedup_store,
                write_journal=write_journal,
                client_factory=real_client_factory,
            )
        except Exception as exc:
            pytest.skip(f"Live LLM provider setup failed: {exc}")

        # Verify numeric repository ID against live GitHub API before proceeding
        client = await real_client_factory(installation_id)
        try:
            repo_data = await client.get_repository(owner, repo)
        except Exception as exc:
            pytest.skip(f"Failed to access live repository '{owner}/{repo}': {exc}")

        actual_repo_id = repo_data.get("id")
        if actual_repo_id != expected_repo_id:
            pytest.fail(
                f"Numeric repo ID mismatch: expected {expected_repo_id}, got {actual_repo_id}. Aborting."
            )

        default_branch = repo_data.get("default_branch", "main")

        delivery_id = f"live-e2e-{uuid.uuid4().hex[:12]}"
        event = GitHubPushEvent(
            delivery_id=delivery_id,
            repository_owner=owner,
            repository_name=repo,
            repository_id=actual_repo_id,
            installation_id=installation_id,
            default_branch=default_branch,
            ref=f"refs/heads/{default_branch}",
            before_sha=before_sha,
            after_sha=commit_sha,
            forced=False,
            deleted=False,
        )

        result = await scout_service.process_push(event)

        assert result.repository_id == actual_repo_id
        assert result.commit_sha == commit_sha
        assert result.failures == [], f"Pipeline reported operational failures: {result.failures}"

        if expected_changed_file:
            assert expected_changed_file in result.changed_files, (
                f"Expected changed file '{expected_changed_file}' not found in {result.changed_files}"
            )

        if expect_issue_creation:
            assert result.ai_drafts >= 1, "Expected Scout to produce at least 1 draft finding"
            assert result.escalated_findings >= 1, "Expected at least 1 finding to pass worthiness filter"
            assert (result.escalated_findings - result.verifier_rejected) >= 1, (
                "Expected at least 1 finding to survive Verifier evaluation"
            )
            assert result.issues_created >= 1, "Expected at least 1 GitHub issue to be created in sandbox"
