"""Acceptance tests for process crash and restart recovery across all write-intent states.

Covers:
- Crash A: RESERVED before WRITE_PENDING -> lease expiration -> safe reclaim by new finding
- Crash B: WRITE_PENDING + PREPARED -> process restart -> same finding/intent resumes; competing finding blocked
- Crash C: POSTING unknown (remote error/timeout) -> restart -> reconciliation-only mode -> create_issue NEVER called again
- Crash D: Remote POST succeeded -> process dies before local COMMITTED -> restart -> reconciliation finds issue -> journal + dedup COMMITTED -> no second POST
"""
import asyncio
import tempfile
import pathlib
import pytest

from app.domain.models import Finding, Verification, RepoContext, RepoContextFile, EvidenceItem, VerifierEvidence
from app.domain.states import FindingStatus, VerificationStatus, EvidenceStatus
from app.verifier.evidence import EvidenceValidationResult, EvidenceItemResult
from app.services.issue_gate import SQLiteDedupStore, GateResult, GateDecision
from app.services.write_journal import SQLiteIssueWriteJournal, IntentState, UnresolvedWriteIntentError
from app.services.issue_creator import (
    create_issue_if_authorized,
    IssueCreationResult,
    Unauthorized,
    IssueCreationError,
)
from app.github.client import GitHubAmbiguousWriteError


REPO_OWNER = "acme-corp"
REPO_NAME = "secure-vault"
REPO_ID = 98765


class RestartTestGitHubClient:
    def __init__(self):
        self.created_issues = []
        self.create_issue_calls = 0
        self.should_fail_create = None
        self.search_returns = []

    async def get_repository(self, owner: str, repo: str) -> dict:
        return {"id": REPO_ID, "owner": {"login": owner}, "name": repo}

    async def create_issue(self, owner: str, repo: str, title: str, body: str, labels: list[str] | None = None) -> dict:
        self.create_issue_calls += 1
        if self.should_fail_create:
            exc = self.should_fail_create
            if isinstance(exc, Exception):
                raise exc
            raise GitHubAmbiguousWriteError("Connection reset after sending request")

        issue_number = len(self.created_issues) + 1
        issue = {
            "number": issue_number,
            "title": title,
            "body": body,
            "labels": labels or [],
        }
        self.created_issues.append(issue)
        return issue

    async def search_issues(self, owner: str, repo: str, query: str) -> list[dict]:
        matching = []
        for issue in self.created_issues:
            if "signature:" in query:
                sig = query.split("signature:")[-1].strip()
                if f"opencontrib:signature:{sig}" in issue.get("body", ""):
                    matching.append(issue)
            elif "finding:" in query:
                fid = query.split("finding:")[-1].strip()
                if f"opencontrib:finding:{fid}" in issue.get("body", ""):
                    matching.append(issue)
        return matching

    async def get_issues(self, owner: str, repo: str, state: str = "all", per_page: int = 30) -> list[dict]:
        return list(self.created_issues)


def _make_sample_finding(finding_id: str = "F-test-001") -> Finding:
    return Finding(
        finding_id=finding_id,
        repository_id=str(REPO_ID),
        installation_id="112233",
        commit_sha="1111111111111111111111111111111111111111",
        category="behavioral_bug",
        title="Authentication bypass on missing token",
        severity="high",
        file="app/auth.py",
        function="authenticate",
        line=2,
        description="Returns None instead of raising AuthenticationRequired",
        expected_behavior="Should raise AuthenticationRequired exception",
        evidence=[
            EvidenceItem(
                file="app/auth.py",
                line=2,
                snippet="if not token:\n        return None",
            )
        ],
        confidence=0.95,
        status=FindingStatus.DISCOVERED,
    )


def _make_sample_verification(finding_id: str = "F-test-001") -> Verification:
    return Verification(
        verification_id="V-test-001",
        finding_id=finding_id,
        installation_id="112233",
        repository_id=str(REPO_ID),
        commit_sha="1111111111111111111111111111111111111111",
        status=VerificationStatus.VERIFIED,
        confidence=0.95,
        reason="Confirmed bypass occurs",
        supporting_evidence=[
            VerifierEvidence(
                file="app/auth.py",
                line=2,
                snippet="if not token:\n        return None",
            )
        ],
        counter_evidence=[],
        duplicate_issue=False,
    )


def _make_sample_repo_context() -> RepoContext:
    return RepoContext(
        owner=REPO_OWNER,
        name=REPO_NAME,
        repository_id=str(REPO_ID),
        installation_id="112233",
        commit_sha="1111111111111111111111111111111111111111",
        files=[
            RepoContextFile(
                path="app/auth.py",
                content="def authenticate(token: str):\n    if not token:\n        return None\n    return verify(token)\n",
                size_bytes=100,
            )
        ],
    )


def _make_sample_evidence_result(finding: Finding) -> EvidenceValidationResult:
    return EvidenceValidationResult(
        finding_id=finding.finding_id,
        commit_sha=finding.commit_sha,
        results=[
            EvidenceItemResult(
                file="app/auth.py",
                line=2,
                snippet="if not token:\n        return None",
                file_exists=True,
                line_exists=True,
                snippet_found=True,
                function_exists=True,
                status=EvidenceStatus.SUPPORTED,
            )
        ],
        overall=EvidenceStatus.SUPPORTED,
    )


# ===========================================================================
# CRASH A: RESERVED before WRITE_PENDING -> lease expiration -> safe reclaim
# ===========================================================================

@pytest.mark.asyncio
async def test_crash_a_lease_expiration_allows_safe_reclaim():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        db_path = str(pathlib.Path(tmp_dir) / "test_crash_a.db")

        # Process 1: Reserves dedup key with very short lease (1 second)
        store1 = SQLiteDedupStore(db_path=db_path, reservation_ttl=1)

        finding1 = _make_sample_finding("F-crash-a-1")
        dedup_res1 = await store1.check_and_reserve(finding1)
        assert dedup_res1.is_duplicate is False
        assert dedup_res1.reservation_token != ""

        # Verify reservation is active
        assert await store1.validate_reservation(finding1, dedup_res1) is True

        # Simulate Crash of Process 1: lease expires
        await asyncio.sleep(1.2)

        # Process 2 starts up after restart with new store instance pointing to same SQLite DB
        store2 = SQLiteDedupStore(db_path=db_path, reservation_ttl=60)

        # Old reservation is expired and invalid
        assert await store2.validate_reservation(finding1, dedup_res1) is False

        # Process 2 (or another finding for same defect) can safely reclaim the reservation
        finding2 = _make_sample_finding("F-crash-a-2")
        dedup_res2 = await store2.check_and_reserve(finding2)
        assert dedup_res2.is_duplicate is False
        assert dedup_res2.reservation_token != ""
        assert await store2.validate_reservation(finding2, dedup_res2) is True


# ===========================================================================
# CRASH B: WRITE_PENDING + PREPARED -> restart -> same intent can resume; competing blocked
# ===========================================================================

@pytest.mark.asyncio
async def test_crash_b_write_pending_prepared_recovery():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        db_path = str(pathlib.Path(tmp_dir) / "test_crash_b.db")

        # Process 1 initializes stores and sets up WRITE_PENDING + PREPARED intent
        store1 = SQLiteDedupStore(db_path=db_path, reservation_ttl=60)
        journal1 = SQLiteIssueWriteJournal(db_path=db_path)

        finding = _make_sample_finding("F-crash-b-1")
        verification = _make_sample_verification("F-crash-b-1")
        repo_context = _make_sample_repo_context()
        evidence_result = _make_sample_evidence_result(finding)

        dedup_res = await store1.check_and_reserve(finding)
        assert dedup_res.reservation_token != ""

        # Create write intent: transitions dedup to WRITE_PENDING, journal to PREPARED
        intent_id = await journal1.create_write_intent(
            finding=finding,
            verification=verification,
            repo_context=repo_context,
            signature=dedup_res.signature,
            reservation_token=dedup_res.reservation_token,
        )
        assert intent_id is not None

        # Simulate Crash of Process 1 before POSTING!
        # Process 2 starts after restart
        store2 = SQLiteDedupStore(db_path=db_path, reservation_ttl=60)
        journal2 = SQLiteIssueWriteJournal(db_path=db_path)

        # Competing finding with different finding_id cannot steal WRITE_PENDING
        competing_finding = _make_sample_finding("F-competing-002")
        competing_dedup = await store2.check_and_reserve(competing_finding)
        assert competing_dedup.is_duplicate is True
        assert competing_dedup.reservation_token == ""

        # Trying to create write intent with wrong finding/token fails closed
        with pytest.raises(Exception):
            await journal2.create_write_intent(
                finding=competing_finding,
                verification=verification,
                repo_context=repo_context,
                signature=competing_dedup.signature,
                reservation_token="fake_token",
            )

        # But the original intent owner can safely complete the POST
        github_client = RestartTestGitHubClient()
        gate_res = GateResult(decision=GateDecision.ALLOW, reason="Authorized")
        result = await create_issue_if_authorized(
            gate_result=gate_res,
            dedup_result=dedup_res,
            finding=finding,
            verification=verification,
            github_client=github_client,
            owner=REPO_OWNER,
            repo_name=REPO_NAME,
            evidence_result=evidence_result,
            repo_context=repo_context,
            dedup_store=store2,
            write_journal=journal2,
        )
        assert result.issue_number == 1
        assert github_client.create_issue_calls == 1

        # State is now COMMITTED in both journal and dedup
        intent = await journal2.get_intent(dedup_res.signature)
        assert intent["state"] == IntentState.COMMITTED.value
        assert intent["issue_number"] == 1


# ===========================================================================
# CRASH C: POSTING unknown -> restart -> reconciliation-only -> NO blind re-POST
# ===========================================================================

@pytest.mark.asyncio
async def test_crash_c_posting_unknown_never_reposts_blindly():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        db_path = str(pathlib.Path(tmp_dir) / "test_crash_c.db")

        store1 = SQLiteDedupStore(db_path=db_path, reservation_ttl=60)
        journal1 = SQLiteIssueWriteJournal(db_path=db_path)

        finding = _make_sample_finding("F-crash-c-1")
        verification = _make_sample_verification("F-crash-c-1")
        repo_context = _make_sample_repo_context()
        evidence_result = _make_sample_evidence_result(finding)

        dedup_res = await store1.check_and_reserve(finding)

        github_client1 = RestartTestGitHubClient()
        # Make the remote POST fail ambiguously (timeout / network disconnect)
        github_client1.should_fail_create = GitHubAmbiguousWriteError("Timeout waiting for response")

        gate_res = GateResult(decision=GateDecision.ALLOW, reason="Authorized")
        with pytest.raises(UnresolvedWriteIntentError):
            await create_issue_if_authorized(
                gate_result=gate_res,
                dedup_result=dedup_res,
                finding=finding,
                verification=verification,
                github_client=github_client1,
                owner=REPO_OWNER,
                repo_name=REPO_NAME,
                evidence_result=evidence_result,
                repo_context=repo_context,
                dedup_store=store1,
                write_journal=journal1,
            )

        # 1 POST attempt was made
        assert github_client1.create_issue_calls == 1

        # Check that intent state in SQLite is left in POSTING (not reverted to PREPARED or ABORTED)
        intent = await journal1.get_intent(dedup_res.signature)
        assert intent["state"] == IntentState.POSTING.value

        # Simulate Crash and Process Restart
        store2 = SQLiteDedupStore(db_path=db_path, reservation_ttl=60)
        journal2 = SQLiteIssueWriteJournal(db_path=db_path)

        # Subsequent run after restart with new client
        github_client2 = RestartTestGitHubClient()

        # Because intent is in POSTING and reconciliation cannot find issue on GitHub:
        # It must refuse to call create_issue() again!
        with pytest.raises(UnresolvedWriteIntentError):
            await create_issue_if_authorized(
                gate_result=gate_res,
                dedup_result=dedup_res,
                finding=finding,
                verification=verification,
                github_client=github_client2,
                owner=REPO_OWNER,
                repo_name=REPO_NAME,
                evidence_result=evidence_result,
                repo_context=repo_context,
                dedup_store=store2,
                write_journal=journal2,
            )

        # INVARIANT CHECK: create_issue was NEVER called again!
        assert github_client2.create_issue_calls == 0


# ===========================================================================
# CRASH D: Remote POST succeeded -> process dies -> restart -> reconciliation succeeds
# ===========================================================================

@pytest.mark.asyncio
async def test_crash_d_remote_post_succeeded_local_dies_reconciliation_commits():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        db_path = str(pathlib.Path(tmp_dir) / "test_crash_d.db")

        store1 = SQLiteDedupStore(db_path=db_path, reservation_ttl=60)
        journal1 = SQLiteIssueWriteJournal(db_path=db_path)

        finding = _make_sample_finding("F-crash-d-1")
        verification = _make_sample_verification("F-crash-d-1")
        repo_context = _make_sample_repo_context()
        evidence_result = _make_sample_evidence_result(finding)

        dedup_res = await store1.check_and_reserve(finding)

        # Simulate remote POST that succeeded remotely, but local process crashed before committing!
        intent_id = await journal1.create_write_intent(
            finding=finding,
            verification=verification,
            repo_context=repo_context,
            signature=dedup_res.signature,
            reservation_token=dedup_res.reservation_token,
        )
        await journal1.transition_to_posting(dedup_res.signature, intent_id)

        # GitHub actually has the issue created remotely
        github_client = RestartTestGitHubClient()
        sig_marker = f"<!-- opencontrib:signature:{dedup_res.signature} -->"
        finding_marker = f"<!-- opencontrib:finding:{finding.finding_id}:v:{verification.verification_id} -->"
        github_client.created_issues.append({
            "number": 42,
            "title": finding.title,
            "body": f"Some body text\n{finding_marker}\n{sig_marker}",
            "labels": ["bug"],
        })

        # Process dies here. Now restart with new store instances
        store2 = SQLiteDedupStore(db_path=db_path, reservation_ttl=60)
        journal2 = SQLiteIssueWriteJournal(db_path=db_path)

        # create_issue_if_authorized resumes: detects POSTING state -> reconciles with GitHub
        gate_res = GateResult(decision=GateDecision.ALLOW, reason="Authorized")
        result = await create_issue_if_authorized(
            gate_result=gate_res,
            dedup_result=dedup_res,
            finding=finding,
            verification=verification,
            github_client=github_client,
            owner=REPO_OWNER,
            repo_name=REPO_NAME,
            evidence_result=evidence_result,
            repo_context=repo_context,
            dedup_store=store2,
            write_journal=journal2,
        )

        assert result.issue_number == 42
        assert result.was_existing is True
        # Remote create_issue was NEVER called
        assert github_client.create_issue_calls == 0

        # Both journal and dedup are now COMMITTED
        committed_intent = await journal2.get_intent(dedup_res.signature)
        assert committed_intent["state"] == IntentState.COMMITTED.value
        assert committed_intent["issue_number"] == 42
