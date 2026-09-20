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
from pathlib import Path
from dotenv import load_dotenv

# Explicitly resolve repository root .env; OS environment variables still take precedence (override=False)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
PROJECT_ENV = PROJECT_ROOT / ".env"
if PROJECT_ENV.is_file():
    load_dotenv(dotenv_path=PROJECT_ENV, override=False)
else:
    load_dotenv(override=False)

from app.main import build_scout_service
from app.services.issue_gate import SQLiteDedupStore
from app.services.write_journal import SQLiteIssueWriteJournal
from app.github.events import GitHubPushEvent
from app.github.app_auth import GitHubAppConfig, GitHubAppAuthenticator


class LiveWriteDisabledError(RuntimeError):
    """Retained for backward compatibility in test imports."""
    pass


class GuardedGitHubClient:
    """Wraps GitHubRestClient in live tests to safely intercept create_issue calls when writes are disabled."""

    def __init__(self, inner, allow_writes: bool):
        self._inner = inner
        self._allow_writes = allow_writes
        self.simulated_issue_writes = 0
        self.recorded_calls = []

    async def create_issue(
        self, owner: str, repo: str, title: str, body: str, labels: list[str] | None = None
    ) -> dict:
        if not self._allow_writes:
            self.simulated_issue_writes += 1
            call_data = {
                "owner": owner,
                "repo": repo,
                "title": title,
                "body": body,
                "labels": labels or [],
            }
            self.recorded_calls.append(call_data)
            # Sentinel negative issue number clearly indicates a synthetic test dry-run response
            return {
                "number": -1,
                "html_url": f"https://github.com/{owner}/{repo}/issues/dry-run-sentinel",
                "title": title,
                "state": "open",
            }
        return await self._inner.create_issue(owner, repo, title, body, labels=labels)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.mark.asyncio
async def test_live_scout_and_verifier_pipeline():
    """Executes live Scout -> Verifier against real configured sandbox repository."""
    run_live = os.getenv("RUN_LIVE_SCOUT_E2E", "").strip()
    if run_live != "1":
        pytest.skip(
            "Live Scout/Verifier E2E disabled: RUN_LIVE_SCOUT_E2E must equal exactly '1'"
        )

    owner = os.environ.get("LIVE_E2E_OWNER", "").strip()
    repo = os.environ.get("LIVE_E2E_REPO", "").strip()
    expected_repo_id_str = os.environ.get("LIVE_E2E_EXPECTED_REPO_ID", "").strip()
    inst_id_str = os.environ.get("LIVE_E2E_INSTALLATION_ID", "").strip()
    before_sha = os.environ.get("LIVE_E2E_BEFORE_SHA", "").strip()
    commit_sha = os.environ.get("LIVE_E2E_COMMIT_SHA", "").strip()
    allow_writes = os.environ.get("LIVE_E2E_ALLOW_ISSUE_WRITES") == "1"
    expect_issue_creation = os.environ.get("LIVE_E2E_EXPECT_ISSUE_CREATION") == "1"
    expect_authorized_finding = os.environ.get("LIVE_E2E_EXPECT_AUTHORIZED_FINDING") == "1"
    expected_changed_file = os.environ.get("LIVE_E2E_EXPECTED_CHANGED_FILE", "").strip()

    # STEP 5: Contradictory write flags must fail immediately
    if expect_issue_creation and not allow_writes:
        pytest.fail(
            "LIVE_E2E_EXPECT_ISSUE_CREATION=1 requires LIVE_E2E_ALLOW_ISSUE_WRITES=1"
        )

    # STEP 6: SKIP ONLY WHEN REQUIRED CONFIGURATION IS ABSENT
    if not owner or not repo:
        pytest.skip(
            "LIVE_E2E_OWNER or LIVE_E2E_REPO is not configured."
        )

    if not inst_id_str:
        pytest.skip("LIVE_E2E_INSTALLATION_ID is required for live testing.")

    if not before_sha or not commit_sha:
        pytest.skip(
            "LIVE_E2E_BEFORE_SHA and LIVE_E2E_COMMIT_SHA are required for live testing."
        )

    # Required GitHub App environment variables completely absent -> SKIP
    app_id = os.environ.get("GITHUB_APP_ID", "").strip()
    priv_key_raw = os.environ.get("GITHUB_PRIVATE_KEY", "").strip()
    webhook_secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "").strip()
    if not app_id and not priv_key_raw and not webhook_secret:
        pytest.skip("Required GitHub App environment variables completely absent.")

    # Once configuration has been SUPPLIED, invalid behavior must FAIL.
    sha_pattern = re.compile(r"^[0-9a-fA-F]{40}$")
    if not sha_pattern.match(before_sha) or not sha_pattern.match(commit_sha):
        pytest.fail(
            f"Invalid commit SHA format: before_sha='{before_sha}', commit_sha='{commit_sha}'. "
            "Must be 40-character hexadecimal Git commit SHAs."
        )

    # STEP 7: Validate before_sha != commit_sha before any LLM call
    if before_sha == commit_sha:
        pytest.fail(
            "LIVE_E2E_BEFORE_SHA and LIVE_E2E_COMMIT_SHA must refer to different commits"
        )

    try:
        installation_id = int(inst_id_str)
    except ValueError as exc:
        pytest.fail(f"Numeric conversion failure for LIVE_E2E_INSTALLATION_ID: {exc}")

    # Validate that we are not targeting production issueAnalyzer repository unless explicitly configured
    if repo.lower() == "issueanalyzer" and os.environ.get("CONFIRM_ISSUEANALYZER_AS_SANDBOX") != "1":
        pytest.fail(
            "Safety halt: live sandbox cannot be 'issueAnalyzer' unless CONFIRM_ISSUEANALYZER_AS_SANDBOX=1"
        )

    # Initialize GitHub App authenticator (fail if supplied config is malformed)
    try:
        app_config = GitHubAppConfig.from_environment()
        authenticator = GitHubAppAuthenticator(app_config)
    except Exception as exc:
        pytest.fail(f"Invalid GitHub App configuration: {exc}")

    # Use temporary sqlite DB for dedup and journal
    import tempfile
    import pathlib
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = str(pathlib.Path(tmp_dir) / "live_e2e.db")
        dedup_store = SQLiteDedupStore(db_path=db_path)
        write_journal = SQLiteIssueWriteJournal(db_path=db_path)

        guarded_clients = []

        async def real_client_factory(inst_id: int):
            try:
                raw_client = await authenticator.create_installation_client(inst_id)
            except Exception as exc:
                pytest.fail(f"GitHub installation-token exchange failed for installation {inst_id}: {exc}")
            client_wrapper = GuardedGitHubClient(raw_client, allow_writes=allow_writes)
            guarded_clients.append(client_wrapper)
            return client_wrapper

        # Build ScoutService with real providers (fail-closed on invalid provider setup)
        try:
            scout_service = build_scout_service(
                dedup_store=dedup_store,
                write_journal=write_journal,
                client_factory=real_client_factory,
            )
        except Exception as exc:
            pytest.fail(f"Live LLM provider setup failed: {exc}")

        # Obtain client for pre-flight repository and commit comparison checks
        client = await real_client_factory(installation_id)
        try:
            repo_data = await client.get_repository(owner, repo)
        except Exception as exc:
            pytest.fail(f"Failed to access live repository '{owner}/{repo}': {exc}")

        actual_repo_id = repo_data.get("id")
        if not actual_repo_id:
            pytest.fail(f"Could not retrieve repository ID for '{owner}/{repo}'")

        if expected_repo_id_str:
            try:
                expected_repo_id = int(expected_repo_id_str)
            except ValueError as exc:
                pytest.fail(f"Numeric conversion failure for LIVE_E2E_EXPECTED_REPO_ID: {exc}")
            if actual_repo_id != expected_repo_id:
                pytest.fail(
                    f"Numeric repo ID mismatch: expected {expected_repo_id}, got {actual_repo_id}. Aborting."
                )

        # STEP 7: Compare commits before any LLM call using real client method
        try:
            comparison = await client.compare_commits(owner, repo, before_sha, commit_sha)
        except Exception as exc:
            pytest.fail(
                f"Commit comparison failed between {before_sha} and {commit_sha}: {exc}"
            )

        # STEP 8: Validate expected changed file before LLM call
        if expected_changed_file:
            changed_paths = [
                item["filename"]
                for item in comparison.get("files", [])
                if isinstance(item, dict) and "filename" in item
            ]
            if expected_changed_file not in changed_paths:
                pytest.fail(
                    f"Expected changed file '{expected_changed_file}' not found in comparison files: {changed_paths}"
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

        # STEP 8: Optional dry-run authorized finding assertion (zero real GitHub mutations)
        if expect_authorized_finding and not allow_writes:
            total_simulated = sum(c.simulated_issue_writes for c in guarded_clients)
            assert result.failures == [], f"Pipeline reported operational failures: {result.failures}"
            assert result.ai_drafts >= 1, "Expected Scout to produce at least 1 draft finding"
            assert result.escalated_findings >= 1, "Expected at least 1 finding to pass worthiness filter"
            assert (total_simulated >= 1 or result.issues_created >= 1), (
                "Expected test-only simulated write boundary to be reached without creating a real issue"
            )

        # STEP 4: Remove misleading arithmetic; verify real fields for LIVE_E2E_EXPECT_ISSUE_CREATION=1
        if expect_issue_creation:
            assert result.failures == []
            assert result.ai_drafts >= 1, "Expected Scout to produce at least 1 draft finding"
            assert result.escalated_findings >= 1, "Expected at least 1 finding to pass worthiness filter"
            assert result.issues_created >= 1, "Expected at least 1 GitHub issue to be created in sandbox"
