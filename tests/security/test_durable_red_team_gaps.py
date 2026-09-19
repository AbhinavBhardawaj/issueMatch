"""Mandatory Red-Team Security and Correctness Invariant Tests.

Covers items A through S:
A. Durable webhook does not schedule FastAPI BackgroundTask processing
B. One durable delivery causes exactly one Scout execution
C. Concurrent duplicate webhook requests still cause one Scout execution
D. Ambiguous POST can never produce create_issue call #2
E. POSTING unknown state remains POSTING after failure
F. reservation_token mismatch cannot create write intent
G. Wrong finding_id cannot transition WRITE_PENDING
H. Wrong intent_id cannot transition POSTING
I. PREPARED cannot be committed as normal POST success without expected state
J. COMMITTED cannot become POSTING
K. COMMITTED cannot become RESERVED
L. POSTING cannot be automatically aborted
M. Caller scan truncation marks context PARTIAL
N. Test scan truncation marks context PARTIAL
O. Tree lookup failure marks Scout context PARTIAL
P. Explicit missing Scout provider key is not swallowed (fail-closed)
Q. Explicit missing Verifier provider key is not swallowed (fail-closed)
R. WEBHOOK_MAX_ATTEMPTS actually reaches persisted webhook_jobs.max_attempts
S. config.auto_start_worker controls runtime consistently
"""
import os
import hmac
import hashlib
import json
import tempfile
import pathlib
import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi.testclient import TestClient

from app.domain.models import Finding, Verification, RepoContext, RepoContextFile, EvidenceItem, VerifierEvidence
from app.domain.states import FindingStatus, VerificationStatus, EvidenceStatus
from app.verifier.evidence import EvidenceValidationResult, EvidenceItemResult
from app.services.issue_gate import SQLiteDedupStore, GateResult, GateDecision
from app.services.write_journal import (
    SQLiteIssueWriteJournal,
    IntentState,
    UnresolvedWriteIntentError,
)
from app.services.issue_creator import (
    create_issue_if_authorized,
    IssueCreationResult,
    Unauthorized,
    IssueCreationError,
)
from app.storage.delivery_store import SQLiteDeliveryStore
from app.services.worker import DurableWebhookWorker
from app.main import create_application, build_scout_service, AppRuntimeConfig
from app.infrastructure.llm_router import ProviderConfigurationError
from app.github.events import GitHubPushEvent
from app.github.client import GitHubAmbiguousWriteError
from app.scout.context import ScoutContextBuilder
from app.github.repo_fetcher import fetch_repo_context
from app.domain.models import ContextCompleteness


REPO_OWNER = "acme-corp"
REPO_NAME = "secure-vault"
REPO_ID = 98765
WEBHOOK_SECRET = "red-team-secret-123"


def _make_push_payload(commit_sha: str = "a" * 40, delivery_id: str = "del-test-01") -> tuple[bytes, dict]:
    body = json.dumps({
        "ref": "refs/heads/main",
        "before": "0" * 40,
        "after": commit_sha,
        "repository": {
            "id": REPO_ID,
            "name": REPO_NAME,
            "default_branch": "main",
            "owner": {"login": REPO_OWNER},
        },
        "installation": {"id": 112233},
        "forced": False,
        "deleted": False,
    }).encode("utf-8")
    sig = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "X-Hub-Signature-256": sig,
        "X-GitHub-Delivery": delivery_id,
        "X-GitHub-Event": "push",
        "Content-Type": "application/json",
    }
    return body, headers


def _make_sample_finding(finding_id: str = "F-rt-001") -> Finding:
    return Finding(
        finding_id=finding_id,
        repository_id=str(REPO_ID),
        installation_id="112233",
        commit_sha="a" * 40,
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


def _make_sample_verification(finding_id: str = "F-rt-001") -> Verification:
    return Verification(
        verification_id="V-rt-001",
        finding_id=finding_id,
        installation_id="112233",
        repository_id=str(REPO_ID),
        commit_sha="a" * 40,
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
        commit_sha="a" * 40,
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
# A & B: Durable webhook does not schedule BackgroundTask; exactly one execution
# ===========================================================================

@pytest.mark.asyncio
async def test_a_and_b_durable_webhook_no_background_task_single_execution():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        db_path = str(pathlib.Path(tmp_dir) / "rt_ab.db")
        del_store = SQLiteDeliveryStore(db_path=db_path)
        dedup_store = SQLiteDedupStore(db_path=db_path)
        journal = SQLiteIssueWriteJournal(db_path=db_path)

        mock_scout_service = MagicMock()
        process_count = 0

        async def tracking_process_push(event):
            nonlocal process_count
            process_count += 1
            return MagicMock(failures=[], repository_id=event.repository_id, commit_sha=event.after_sha)

        mock_scout_service.process_push = tracking_process_push

        app = create_application(
            webhook_secret=WEBHOOK_SECRET,
            delivery_store=del_store,
            dedup_store=dedup_store,
            write_journal=journal,
            scout_service=mock_scout_service,
            config=AppRuntimeConfig(db_path=db_path, auto_start_worker=False),
            start_worker=False,
        )
        worker = DurableWebhookWorker(delivery_store=del_store, scout_service=mock_scout_service)

        client = TestClient(app)
        body, headers = _make_push_payload(delivery_id="del-ab-001")
        resp = client.post("/webhooks/github", content=body, headers=headers)
        assert resp.status_code == 202

        # Invariant A: FastAPI request completion MUST NOT execute process_push
        assert process_count == 0

        # Invariant B: Durable worker processes it exactly once
        processed = await worker.process_one()
        assert processed is True
        assert process_count == 1

        # Subsequent worker poll finds no remaining jobs
        assert await worker.process_one() is False
        assert process_count == 1


# ===========================================================================
# C: Concurrent duplicate webhook requests cause only one execution
# ===========================================================================

@pytest.mark.asyncio
async def test_c_concurrent_duplicate_webhooks_one_execution():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        db_path = str(pathlib.Path(tmp_dir) / "rt_c.db")
        del_store = SQLiteDeliveryStore(db_path=db_path)
        dedup_store = SQLiteDedupStore(db_path=db_path)
        journal = SQLiteIssueWriteJournal(db_path=db_path)

        mock_scout = MagicMock()
        exec_count = 0

        async def tracking_push(event):
            nonlocal exec_count
            exec_count += 1
            return MagicMock(failures=[])

        mock_scout.process_push = tracking_push
        app = create_application(
            webhook_secret=WEBHOOK_SECRET,
            delivery_store=del_store,
            dedup_store=dedup_store,
            write_journal=journal,
            scout_service=mock_scout,
            config=AppRuntimeConfig(db_path=db_path, auto_start_worker=False),
            start_worker=False,
        )
        worker = DurableWebhookWorker(delivery_store=del_store, scout_service=mock_scout)
        client = TestClient(app)

        body, headers = _make_push_payload(delivery_id="del-c-identical")
        resp1 = client.post("/webhooks/github", content=body, headers=headers)
        resp2 = client.post("/webhooks/github", content=body, headers=headers)

        assert resp1.status_code == 202
        assert resp2.status_code == 202

        # Only one job was enqueued in SQLite
        assert await worker.process_one() is True
        assert await worker.process_one() is False
        assert exec_count == 1


# ===========================================================================
# D & E: Ambiguous POST produces 1 create_issue call and remains POSTING
# ===========================================================================

@pytest.mark.asyncio
async def test_d_and_e_ambiguous_post_no_retry_remains_posting():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        db_path = str(pathlib.Path(tmp_dir) / "rt_de.db")
        dedup_store = SQLiteDedupStore(db_path=db_path)
        journal = SQLiteIssueWriteJournal(db_path=db_path)

        finding = _make_sample_finding("F-de-001")
        verification = _make_sample_verification("F-de-001")
        repo_context = _make_sample_repo_context()
        evidence_result = _make_sample_evidence_result(finding)

        dedup_res = await dedup_store.check_and_reserve(finding)

        mock_gh = AsyncMock()
        mock_gh.get_repository.return_value = {"id": REPO_ID, "owner": {"login": REPO_OWNER}, "name": REPO_NAME}
        mock_gh.search_issues.return_value = []
        mock_gh.get_issues.return_value = []
        mock_gh.create_issue.side_effect = GitHubAmbiguousWriteError("Timeout waiting for remote GitHub response")

        gate_res = GateResult(decision=GateDecision.ALLOW, reason="Authorized")

        with pytest.raises(UnresolvedWriteIntentError):
            await create_issue_if_authorized(
                gate_result=gate_res,
                dedup_result=dedup_res,
                finding=finding,
                verification=verification,
                github_client=mock_gh,
                owner=REPO_OWNER,
                repo_name=REPO_NAME,
                evidence_result=evidence_result,
                repo_context=repo_context,
                dedup_store=dedup_store,
                write_journal=journal,
            )

        # Invariant D: exactly 1 create_issue call
        assert mock_gh.create_issue.call_count == 1

        # Invariant E: intent state in SQLite remains POSTING
        intent = await journal.get_intent(dedup_res.signature)
        assert intent["state"] == IntentState.POSTING.value


# ===========================================================================
# F: Reservation token mismatch cannot create write intent
# ===========================================================================

@pytest.mark.asyncio
async def test_f_reservation_token_mismatch_fails_closed():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        db_path = str(pathlib.Path(tmp_dir) / "rt_f.db")
        dedup_store = SQLiteDedupStore(db_path=db_path)
        journal = SQLiteIssueWriteJournal(db_path=db_path)

        finding = _make_sample_finding("F-f-001")
        verification = _make_sample_verification("F-f-001")
        repo_context = _make_sample_repo_context()

        dedup_res = await dedup_store.check_and_reserve(finding)

        # Attempt to create write intent with wrong / forged token
        with pytest.raises(ValueError):
            await journal.create_write_intent(
                finding=finding,
                verification=verification,
                repo_context=repo_context,
                signature=dedup_res.signature,
                reservation_token="forged-token-xyz",
            )


# ===========================================================================
# G: Wrong finding_id cannot transition WRITE_PENDING
# ===========================================================================

@pytest.mark.asyncio
async def test_g_wrong_finding_id_cannot_transition_write_pending():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        db_path = str(pathlib.Path(tmp_dir) / "rt_g.db")
        dedup_store = SQLiteDedupStore(db_path=db_path)
        journal = SQLiteIssueWriteJournal(db_path=db_path)

        finding1 = _make_sample_finding("F-g-legit")
        finding2 = _make_sample_finding("F-g-imposter")
        verification = _make_sample_verification("F-g-legit")
        repo_context = _make_sample_repo_context()

        dedup_res = await dedup_store.check_and_reserve(finding1)

        # Competing finding with same token attempts to claim slot
        with pytest.raises(ValueError):
            await journal.create_write_intent(
                finding=finding2,
                verification=verification,
                repo_context=repo_context,
                signature=dedup_res.signature,
                reservation_token=dedup_res.reservation_token,
            )


# ===========================================================================
# H: Wrong intent_id cannot transition POSTING
# ===========================================================================

@pytest.mark.asyncio
async def test_h_wrong_intent_id_cannot_transition_posting():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        db_path = str(pathlib.Path(tmp_dir) / "rt_h.db")
        dedup_store = SQLiteDedupStore(db_path=db_path)
        journal = SQLiteIssueWriteJournal(db_path=db_path)

        finding = _make_sample_finding("F-h-001")
        verification = _make_sample_verification("F-h-001")
        repo_context = _make_sample_repo_context()

        dedup_res = await dedup_store.check_and_reserve(finding)
        real_intent_id = await journal.create_write_intent(
            finding=finding,
            verification=verification,
            repo_context=repo_context,
            signature=dedup_res.signature,
            reservation_token=dedup_res.reservation_token,
        )

        # Trying to transition with incorrect intent_id returns False
        ok = await journal.transition_to_posting(dedup_res.signature, "wrong-intent-id-999")
        assert ok is False

        # Status remains PREPARED
        intent = await journal.get_intent(dedup_res.signature)
        assert intent["state"] == IntentState.PREPARED.value


# ===========================================================================
# I: PREPARED cannot be committed without expected state
# ===========================================================================

@pytest.mark.asyncio
async def test_i_prepared_cannot_be_committed_without_posting_state():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        db_path = str(pathlib.Path(tmp_dir) / "rt_i.db")
        dedup_store = SQLiteDedupStore(db_path=db_path)
        journal = SQLiteIssueWriteJournal(db_path=db_path)

        finding = _make_sample_finding("F-i-001")
        verification = _make_sample_verification("F-i-001")
        repo_context = _make_sample_repo_context()

        dedup_res = await dedup_store.check_and_reserve(finding)
        intent_id = await journal.create_write_intent(
            finding=finding,
            verification=verification,
            repo_context=repo_context,
            signature=dedup_res.signature,
            reservation_token=dedup_res.reservation_token,
        )

        # While in PREPARED, committing with expected_state="POSTING" must fail
        ok = await journal.commit_intent(dedup_res.signature, intent_id, 42, expected_state="POSTING")
        assert ok is False


# ===========================================================================
# J & K: COMMITTED cannot become POSTING or RESERVED
# ===========================================================================

@pytest.mark.asyncio
async def test_j_and_k_committed_cannot_become_posting_or_reserved():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        db_path = str(pathlib.Path(tmp_dir) / "rt_jk.db")
        dedup_store = SQLiteDedupStore(db_path=db_path)
        journal = SQLiteIssueWriteJournal(db_path=db_path)

        finding = _make_sample_finding("F-jk-001")
        verification = _make_sample_verification("F-jk-001")
        repo_context = _make_sample_repo_context()

        dedup_res = await dedup_store.check_and_reserve(finding)
        intent_id = await journal.create_write_intent(
            finding=finding,
            verification=verification,
            repo_context=repo_context,
            signature=dedup_res.signature,
            reservation_token=dedup_res.reservation_token,
        )
        await journal.transition_to_posting(dedup_res.signature, intent_id)
        await journal.commit_intent(dedup_res.signature, intent_id, 42, expected_state="POSTING")
        await dedup_store.commit_reservation(dedup_res.signature, finding.finding_id, 42)

        # Invariant J: COMMITTED cannot transition to POSTING
        assert await journal.transition_to_posting(dedup_res.signature, intent_id) is False

        # Invariant K: COMMITTED cannot revert to RESERVED
        recheck = await dedup_store.check_and_reserve(finding)
        assert recheck.is_duplicate is True
        assert "Already committed" in recheck.reason


# ===========================================================================
# L: POSTING cannot be automatically aborted
# ===========================================================================

@pytest.mark.asyncio
async def test_l_posting_cannot_be_aborted():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        db_path = str(pathlib.Path(tmp_dir) / "rt_l.db")
        dedup_store = SQLiteDedupStore(db_path=db_path)
        journal = SQLiteIssueWriteJournal(db_path=db_path)

        finding = _make_sample_finding("F-l-001")
        verification = _make_sample_verification("F-l-001")
        repo_context = _make_sample_repo_context()

        dedup_res = await dedup_store.check_and_reserve(finding)
        intent_id = await journal.create_write_intent(
            finding=finding,
            verification=verification,
            repo_context=repo_context,
            signature=dedup_res.signature,
            reservation_token=dedup_res.reservation_token,
        )
        await journal.transition_to_posting(dedup_res.signature, intent_id)

        # Invariant L: abort_intent requires PREPARED; calling abort on POSTING returns False
        ok = await journal.abort_intent(dedup_res.signature, intent_id)
        assert ok is False

        # Still in POSTING
        intent = await journal.get_intent(dedup_res.signature)
        assert intent["state"] == IntentState.POSTING.value


# ===========================================================================
# M & N: Caller and test scan truncation marks context PARTIAL
# ===========================================================================

@pytest.mark.asyncio
async def test_m_caller_scan_truncation_marks_context_partial():
    mock_gh = AsyncMock()
    # 25 candidate caller files (above limit of 10)
    tree_items = [{"path": f"src/caller_{i}.py", "type": "blob"} for i in range(25)]
    tree_items.append({"path": "app/auth.py", "type": "blob"})
    mock_gh.get_repository_tree.return_value = (tree_items, False)
    mock_gh.get_file_content.return_value = "def test(): pass"
    mock_gh.get_issues.return_value = []

    finding = _make_sample_finding("F-m-001")
    repo_ctx = await fetch_repo_context(
        finding=finding,
        client=mock_gh,
        owner=REPO_OWNER,
        repo_name=REPO_NAME,
    )

    # Invariant M: Truncated caller scan MUST mark context PARTIAL
    assert repo_ctx.context_completeness == ContextCompleteness.PARTIAL
    assert "CALLER_SCAN_LIMIT_REACHED" in repo_ctx.partial_reasons


@pytest.mark.asyncio
async def test_n_test_scan_truncation_marks_context_partial():
    mock_gh = AsyncMock()
    # 25 test files (above MAX_VERIFIER_TEST_FILES = 10)
    tree_items = [{"path": f"tests/test_{i}.py", "type": "blob"} for i in range(25)]
    tree_items.append({"path": "app/auth.py", "type": "blob"})
    mock_gh.get_repository_tree.return_value = (tree_items, False)
    mock_gh.get_file_content.return_value = "def test(): pass"
    mock_gh.get_issues.return_value = []

    finding = _make_sample_finding("F-n-001")
    repo_ctx = await fetch_repo_context(
        finding=finding,
        client=mock_gh,
        owner=REPO_OWNER,
        repo_name=REPO_NAME,
    )

    # Invariant N: Truncated test scan MUST mark context PARTIAL
    assert repo_ctx.context_completeness == ContextCompleteness.PARTIAL
    assert "TEST_FILE_LIMIT_REACHED" in repo_ctx.partial_reasons


# ===========================================================================
# O: Tree lookup failure marks Scout context PARTIAL
# ===========================================================================

@pytest.mark.asyncio
async def test_o_tree_lookup_failure_marks_scout_context_partial():
    mock_gh = AsyncMock()
    mock_gh.get_repository_tree.side_effect = RuntimeError("Tree API failure (500)")
    mock_gh.compare_commits.return_value = {"files": []}
    mock_gh.get_file_content.return_value = "content"
    mock_gh.get_issues.return_value = []

    builder = ScoutContextBuilder(mock_gh)
    event = GitHubPushEvent(
        delivery_id="del-o-001",
        repository_owner=REPO_OWNER,
        repository_name=REPO_NAME,
        repository_id=REPO_ID,
        installation_id=112233,
        ref="refs/heads/main",
        default_branch="main",
        before_sha="0" * 40,
        after_sha="a" * 40,
        forced=False,
        deleted=False,
    )

    scout_ctx = await builder.build_context(event=event, changed_files=[])
    # Invariant O: Tree lookup error MUST mark context PARTIAL
    assert scout_ctx.context_completeness == ContextCompleteness.PARTIAL
    assert "TREE_LOOKUP_FAILED" in scout_ctx.partial_reasons


# ===========================================================================
# P & Q: Explicit missing provider key fails closed with ProviderConfigurationError
# ===========================================================================

def test_p_and_q_explicit_missing_provider_key_fails_closed(monkeypatch):
    monkeypatch.setenv("SCOUT_PROVIDER", "GROQ")
    monkeypatch.delenv("SCOUT_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEYS", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    with pytest.raises(ProviderConfigurationError):
        build_scout_service(
            dedup_store=MagicMock(),
            write_journal=MagicMock(),
            client_factory=MagicMock(),
        )

    monkeypatch.delenv("SCOUT_PROVIDER", raising=False)
    monkeypatch.setenv("VERIFIER_PROVIDER", "GROQ")
    monkeypatch.delenv("VERIFIER_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEYS", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    with pytest.raises(ProviderConfigurationError):
        build_scout_service(
            dedup_store=MagicMock(),
            write_journal=MagicMock(),
            client_factory=MagicMock(),
        )


# ===========================================================================
# R: WEBHOOK_MAX_ATTEMPTS reaches persisted webhook_jobs.max_attempts
# ===========================================================================

@pytest.mark.asyncio
async def test_r_webhook_max_attempts_reaches_persisted_storage():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        db_path = str(pathlib.Path(tmp_dir) / "rt_r.db")
        del_store = SQLiteDeliveryStore(db_path=db_path)
        dedup_store = SQLiteDedupStore(db_path=db_path)
        journal = SQLiteIssueWriteJournal(db_path=db_path)

        app = create_application(
            webhook_secret=WEBHOOK_SECRET,
            delivery_store=del_store,
            dedup_store=dedup_store,
            write_journal=journal,
            scout_service=MagicMock(),
            config=AppRuntimeConfig(db_path=db_path, worker_max_attempts=7, auto_start_worker=False),
            start_worker=False,
        )

        client = TestClient(app)
        body, headers = _make_push_payload(delivery_id="del-r-001")
        resp = client.post("/webhooks/github", content=body, headers=headers)
        assert resp.status_code == 202

        job = await del_store.claim_next_available_job()
        assert job is not None
        assert job["max_attempts"] == 7


# ===========================================================================
# S: config.auto_start_worker controls runtime consistently
# ===========================================================================

def test_s_auto_start_worker_controls_runtime():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        db_path = str(pathlib.Path(tmp_dir) / "rt_s.db")
        del_store = SQLiteDeliveryStore(db_path=db_path)
        dedup_store = SQLiteDedupStore(db_path=db_path)
        journal = SQLiteIssueWriteJournal(db_path=db_path)

        # auto_start_worker=False -> worker is not running
        app_no_worker = create_application(
            webhook_secret=WEBHOOK_SECRET,
            delivery_store=del_store,
            dedup_store=dedup_store,
            write_journal=journal,
            config=AppRuntimeConfig(db_path=db_path, auto_start_worker=False),
            start_worker=False,
        )
        assert app_no_worker.state.config.auto_start_worker is False
        with TestClient(app_no_worker):
            assert app_no_worker.state.webhook_worker._running is False

        # auto_start_worker=True (passed via config, even with start_worker=False) -> worker is running
        app_with_worker = create_application(
            webhook_secret=WEBHOOK_SECRET,
            delivery_store=del_store,
            dedup_store=dedup_store,
            write_journal=journal,
            config=AppRuntimeConfig(db_path=db_path, auto_start_worker=True),
            start_worker=False,
        )
        assert app_with_worker.state.config.auto_start_worker is True
        with TestClient(app_with_worker):
            assert app_with_worker.state.webhook_worker._running is True
