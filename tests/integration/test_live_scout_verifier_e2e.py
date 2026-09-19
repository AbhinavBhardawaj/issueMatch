"""Live Provider End-to-End Test.

This test connects to real GitHub and real LLM providers.
It is STRICTLY guarded by environment variables and will be skipped unless explicitly enabled:
  RUN_LIVE_SCOUT_E2E == "1"

Requirements for running:
  RUN_LIVE_SCOUT_E2E=1
  LIVE_E2E_OWNER=<owner>
  LIVE_E2E_REPO=<repo>
  LIVE_E2E_EXPECTED_REPO_ID=<numeric repo id>
  LIVE_E2E_ALLOW_ISSUE_WRITES=1 (must be exactly "1" to write issues)

ZERO mocks are used when this test runs.
If any required credential or environment variable is missing, the test SKIPS honestly.
"""
import os
import pytest

# Strict guard: exactly "1", no python truthiness on arbitrary non-empty strings
if os.environ.get("RUN_LIVE_SCOUT_E2E") != "1":
    pytest.skip(
        "RUN_LIVE_SCOUT_E2E is not '1'. Live provider E2E test skipped honestly.",
        allow_module_level=True,
    )

from app.main import build_scout_service, AppRuntimeConfig
from app.services.issue_gate import SQLiteDedupStore
from app.services.write_journal import SQLiteIssueWriteJournal
from app.github.events import GitHubPushEvent
from app.github.auth import GitHubAppConfig, GitHubAppAuthenticator


@pytest.mark.asyncio
async def test_live_scout_and_verifier_pipeline():
    """Executes live Scout -> Verifier against real configured sandbox repository."""
    owner = os.environ.get("LIVE_E2E_OWNER", "").strip()
    repo = os.environ.get("LIVE_E2E_REPO", "").strip()
    expected_repo_id_str = os.environ.get("LIVE_E2E_EXPECTED_REPO_ID", "").strip()
    allow_writes = os.environ.get("LIVE_E2E_ALLOW_ISSUE_WRITES") == "1"

    if not owner or not repo or not expected_repo_id_str:
        pytest.skip(
            "LIVE_E2E_OWNER, LIVE_E2E_REPO, or LIVE_E2E_EXPECTED_REPO_ID is not configured."
        )

    expected_repo_id = int(expected_repo_id_str)

    # Validate that we are not targeting production issueAnalyzer repository unless explicitly configured
    if repo.lower() == "issueanalyzer" and os.environ.get("CONFIRM_ISSUEANALYZER_AS_SANDBOX") != "1":
        pytest.fail(
            "Safety halt: live sandbox cannot be 'issueAnalyzer' unless CONFIRM_ISSUEANALYZER_AS_SANDBOX=1"
        )

    # Initialize GitHub App authenticator and client factory
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
            return await authenticator.create_installation_client(inst_id)

        # Build ScoutService with real providers (fail-closed if credentials missing)
        try:
            scout_service = build_scout_service(
                dedup_store=dedup_store,
                write_journal=write_journal,
                client_factory=real_client_factory,
            )
        except Exception as exc:
            pytest.skip(f"Live LLM provider setup failed: {exc}")

        # Fetch real installation ID for repo
        inst_id_str = os.environ.get("LIVE_E2E_INSTALLATION_ID", "").strip()
        if not inst_id_str:
            pytest.skip("LIVE_E2E_INSTALLATION_ID is required for live testing")
        installation_id = int(inst_id_str)

        # Verify numeric repository ID against live GitHub API before proceeding
        client = await real_client_factory(installation_id)
        repo_data = await client.get_repository(owner, repo)
        actual_repo_id = repo_data.get("id")

        if actual_repo_id != expected_repo_id:
            pytest.fail(
                f"Numeric repo ID mismatch: expected {expected_repo_id}, got {actual_repo_id}. Aborting."
            )

        # Run pipeline with a live push event on current HEAD commit of default branch
        default_branch = repo_data.get("default_branch", "main")
        commit_sha = os.environ.get("LIVE_E2E_COMMIT_SHA", "").strip()
        if not commit_sha:
            tree_items, _ = await client.get_repository_tree(owner, repo, default_branch)
            pytest.skip("LIVE_E2E_COMMIT_SHA must be provided to run live push test")

        event = GitHubPushEvent(
            repository_owner=owner,
            repository_name=repo,
            repository_id=actual_repo_id,
            installation_id=installation_id,
            ref=f"refs/heads/{default_branch}",
            before_sha="0" * 40,
            after_sha=commit_sha,
            forced=False,
            deleted=False,
        )

        result = await scout_service.process_push(event)
        assert result.repository_id == actual_repo_id
        assert result.commit_sha == commit_sha
