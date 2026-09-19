"""Tests for Prompt 4: Durable Execution, Crash Recovery, Multi-Worker Idempotency & Runtime Hardening."""
import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.domain.models import Finding, Verification, EvidenceItem, RepoContext, RepoContextFile
from app.domain.states import FindingStatus, VerificationStatus, EvidenceStatus
from app.verifier.evidence import EvidenceValidationResult, EvidenceItemResult
from app.services.issue_gate import (
    SQLiteDedupStore,
    GateResult,
    GateDecision,
    compute_finding_signature,
    DedupState,
)
from app.storage.sqlite import init_schema
from app.storage.delivery_store import (
    SQLiteDeliveryStore,
    DeliveryClaimStatus,
    DeliveryState,
)
from app.services.write_journal import (
    SQLiteIssueWriteJournal,
    IntentState,
)
from app.services.issue_creator import (
    create_issue_if_authorized,
    Unauthorized,
    IssueCreationResult,
)
from app.services.worker import DurableWebhookWorker
from app.github.client import GitHubRequestTimeoutError, GitHubAmbiguousWriteError


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_durable.db")


@pytest.mark.asyncio
async def test_sqlite_delivery_store_claim_and_idempotency(db_path):
    store = SQLiteDeliveryStore(db_path)
    body1 = b'{"ref": "refs/heads/main", "after": "abc"}'
    hash1 = "hash-1"
    
    # First claim -> ACCEPTED
    res1 = await store.claim_and_enqueue("del-1", hash1, "push", body1.decode())
    assert res1.status == DeliveryClaimStatus.ACCEPTED
    
    # Duplicate same payload -> DUPLICATE
    res2 = await store.claim_and_enqueue("del-1", hash1, "push", body1.decode())
    assert res2.status == DeliveryClaimStatus.DUPLICATE

    # Same delivery ID, different payload -> MISMATCHED_PAYLOAD
    res3 = await store.claim_and_enqueue("del-1", "hash-2", "push", "different")
    assert res3.status == DeliveryClaimStatus.MISMATCHED_PAYLOAD


@pytest.mark.asyncio
async def test_sqlite_delivery_job_lifecycle_and_lease_renewal(db_path):
    store = SQLiteDeliveryStore(db_path)
    await store.claim_and_enqueue("del-job-1", "hash-1", "push", '{"test": 1}')
    
    # Claim job
    job = await store.claim_next_available_job(lease_seconds=10.0)
    assert job is not None
    assert job["delivery_id"] == "del-job-1"
    assert job["state"] == DeliveryState.PROCESSING.value
    assert job["attempt_count"] == 1
    lease_token = job["lease_token"]
    
    # Renew lease
    renewed = await store.renew_job_lease("del-job-1", lease_token, lease_seconds=20.0)
    assert renewed is True
    
    # Complete job
    completed = await store.complete_job("del-job-1", lease_token)
    assert completed is True
    
    # Cannot claim completed job
    next_job = await store.claim_next_available_job(lease_seconds=10.0)
    assert next_job is None


@pytest.mark.asyncio
async def test_sqlite_delivery_terminalize_expired_max_attempts(db_path):
    store = SQLiteDeliveryStore(db_path)
    # Enqueue with max_attempts = 1
    await store.claim_and_enqueue("del-fail", "hash-fail", "push", "{}", max_attempts=1)
    
    job = await store.claim_next_available_job(lease_seconds=0.1)
    assert job is not None
    
    # Let lease expire
    await asyncio.sleep(0.15)
    
    # When claiming next job, expired job at max attempts is terminalized to FAILED
    next_job = await store.claim_next_available_job(lease_seconds=10.0)
    assert next_job is None
    
    job_record = await store.get_job("del-fail")
    assert job_record["state"] == DeliveryState.FAILED.value


@pytest.mark.asyncio
async def test_sqlite_dedup_store_reservations(db_path, make_finding):
    store = SQLiteDedupStore(db_path, reservation_ttl=5.0)
    f1 = make_finding(finding_id="F-DURABLE-1")
    
    # First reservation
    res1 = await store.check_and_reserve(f1)
    assert not res1.is_duplicate
    assert res1.reason == "New reservation"
    token1 = res1.reservation_token
    assert token1 != ""
    
    # Validate reservation
    assert await store.validate_reservation(f1, res1) is True
    
    # Same finding renews own reservation
    res2 = await store.check_and_reserve(f1)
    assert not res2.is_duplicate
    assert res2.reason == "Own reservation"
    
    # Competing finding with same defect signature
    f2 = make_finding(finding_id="F-DURABLE-2")
    res_comp = await store.check_and_reserve(f2)
    assert res_comp.is_duplicate is True
    assert res_comp.reason == "Active reservation exists"


@pytest.mark.asyncio
async def test_sqlite_write_journal_lifecycle(db_path, make_finding, make_verification, make_repo_context):
    journal = SQLiteIssueWriteJournal(db_path)
    f = make_finding()
    v = make_verification(finding=f)
    rc = make_repo_context()
    dedup_store = SQLiteDedupStore(db_path)
    res = await dedup_store.check_and_reserve(f)
    sig = compute_finding_signature(f)

    # Create intent
    intent_id = await journal.create_write_intent(
        finding=f,
        verification=v,
        repo_context=rc,
        signature=sig,
        reservation_token=res.reservation_token,
    )
    assert intent_id != ""
    
    # Query intent
    record = await journal.get_intent(sig)
    assert record is not None
    assert record["intent_id"] == intent_id
    assert record["state"] == IntentState.PREPARED.value
    
    # Transition to POSTING
    posted = await journal.transition_to_posting(sig, intent_id)
    assert posted is True
    
    # Commit intent
    committed = await journal.commit_intent(sig, intent_id, issue_number=123)
    assert committed is True
    
    record_committed = await journal.get_intent(sig)
    assert record_committed["state"] == IntentState.COMMITTED.value
    assert record_committed["issue_number"] == 123


@pytest.mark.asyncio
async def test_issue_creator_with_sqlite_stores_and_reconciliation(db_path, make_finding, make_verification, make_evidence_result, make_repo_context):
    dedup_store = SQLiteDedupStore(db_path)
    write_journal = SQLiteIssueWriteJournal(db_path)
    
    f = make_finding()
    v = make_verification(finding=f)
    er = make_evidence_result(finding=f)
    rc = make_repo_context()
    gate = GateResult(decision=GateDecision.ALLOW, reason="ALL_CONDITIONS_MET")
    
    dedup_res = await dedup_store.check_and_reserve(f)
    assert not dedup_res.is_duplicate
    
    client = AsyncMock()
    client.get_repository.return_value = {"id": f.repository_id, "name": rc.name}
    client.search_issues.return_value = []
    client.get_issues.return_value = []
    client.create_issue.return_value = {"number": 888, "body": "Created issue body"}
    
    res = await create_issue_if_authorized(
        gate,
        dedup_res,
        f,
        v,
        client,
        "test-owner",
        "test-repo",
        evidence_result=er,
        repo_context=rc,
        dedup_store=dedup_store,
        write_journal=write_journal,
    )
    
    assert res.issue_number == 888
    assert res.was_existing is False
    assert client.create_issue.call_count == 1
    
    # Verify committed state in both dedup store and write journal
    sig = compute_finding_signature(f)
    reservation = await dedup_store.get_reservation(sig)
    assert reservation["state"] == DedupState.COMMITTED.value
    assert reservation["issue_number"] == 888
    
    journal_record = await write_journal.get_intent(sig)
    assert journal_record is not None
    assert journal_record["state"] == IntentState.COMMITTED.value
    assert journal_record["issue_number"] == 888


@pytest.mark.asyncio
async def test_ambiguous_timeout_reconciliation_commits_without_second_post(
    db_path, make_finding, make_verification, make_evidence_result, make_repo_context
):
    dedup_store = SQLiteDedupStore(db_path)
    write_journal = SQLiteIssueWriteJournal(db_path)
    
    f = make_finding()
    v = make_verification(finding=f)
    er = make_evidence_result(finding=f)
    rc = make_repo_context()
    gate = GateResult(decision=GateDecision.ALLOW, reason="ALL_CONDITIONS_MET")
    
    dedup_res = await dedup_store.check_and_reserve(f)
    sig = compute_finding_signature(f)
    
    client = AsyncMock()
    client.get_repository.return_value = {"id": f.repository_id, "name": rc.name}
    # Pre-write search: nothing
    client.search_issues.side_effect = [
        [],  # Pre-write search
        [{"number": 999, "body": f"Issue body\n<!-- opencontrib:signature:{sig} -->"}],  # Post-timeout reconciliation search
    ]
    client.get_issues.return_value = []
    # POST creates issue on GitHub but response times out
    client.create_issue.side_effect = GitHubRequestTimeoutError("POST /repos/owner/repo/issues timed out")
    
    res = await create_issue_if_authorized(
        gate,
        dedup_res,
        f,
        v,
        client,
        "test-owner",
        "test-repo",
        evidence_result=er,
        repo_context=rc,
        dedup_store=dedup_store,
        write_journal=write_journal,
    )
    
    # Reconciled without duplicate POST
    assert res.issue_number == 999
    assert res.was_existing is True
    assert client.create_issue.call_count == 1
    
    # Reservation and journal committed to 999
    reservation = await dedup_store.get_reservation(sig)
    assert reservation["state"] == DedupState.COMMITTED.value
    assert reservation["issue_number"] == 999


@pytest.mark.asyncio
async def test_durable_webhook_worker_execution(db_path):
    delivery_store = SQLiteDeliveryStore(db_path)
    scout_service = MagicMock()
    scout_service.process_push = AsyncMock(return_value=None)
    
    payload = {
        "ref": "refs/heads/main",
        "before": "0" * 40,
        "after": "1" * 40,
        "forced": False,
        "deleted": False,
        "repository": {"id": 1, "name": "repo", "owner": {"login": "owner"}, "default_branch": "main"},
        "installation": {"id": 1},
    }
    
    await delivery_store.claim_and_enqueue(
        delivery_id="del-worker-test",
        body_hash="hash-worker-1",
        event_type="push",
        payload_json=json.dumps(payload),
    )
    
    worker = DurableWebhookWorker(
        delivery_store=delivery_store,
        scout_service=scout_service,
        lease_seconds=10.0,
        poll_interval=0.05,
    )
    
    processed = await worker.process_one()
    assert processed is True
    assert scout_service.process_push.call_count == 1
    
    # Job is now completed
    job = await delivery_store.get_job("del-worker-test")
    assert job["state"] == DeliveryState.COMPLETED.value
