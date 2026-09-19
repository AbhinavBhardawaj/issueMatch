import hashlib
import hmac
import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi.testclient import TestClient

from app.main import create_application, AppRuntimeConfig
from app.storage.delivery_store import SQLiteDeliveryStore, DeliveryState
from app.storage.sqlite import get_db_connection
from app.services.worker import DurableWebhookWorker
from app.github.events import GitHubIssueAssignmentEvent

WEBHOOK_SECRET = "test-secret-issues-123"


def make_signed_headers(body: bytes, delivery_id: str, event: str = "issues") -> dict:
    signature = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {
        "X-Hub-Signature-256": signature,
        "X-GitHub-Delivery": delivery_id,
        "X-GitHub-Event": event,
        "Content-Type": "application/json",
    }


def make_assignment_payload(action: str = "assigned") -> dict:
    return {
        "action": action,
        "repository": {
            "id": 12345,
            "name": "test-repo",
            "owner": {"login": "test-org"},
            "default_branch": "main",
        },
        "issue": {
            "number": 42,
            "title": "Fix memory leak",
            "body": "There is a leak in worker heartbeat",
        },
        "assignee": {
            "login": "octocat",
            "id": 583231,
        },
        "installation": {
            "id": 1001,
        },
    }


@pytest.mark.asyncio
async def test_signed_issues_assigned_durable_queue_worker_exactly_once(tmp_path):
    db_path = str(tmp_path / "test_assigned.db")
    delivery_store = SQLiteDeliveryStore(db_path)

    mock_candidate_service = MagicMock()
    mock_candidate_service.process_issue_assignment = AsyncMock()

    config = AppRuntimeConfig(
        db_path=db_path,
        auto_start_worker=False,
    )
    app = create_application(
        webhook_secret=WEBHOOK_SECRET,
        candidate_service=mock_candidate_service,
        delivery_store=delivery_store,
        config=config,
    )
    client = TestClient(app)

    payload = make_assignment_payload(action="assigned")
    body_bytes = json.dumps(payload).encode()
    headers = make_signed_headers(body_bytes, delivery_id="del-assigned-001")

    # 1. Post to webhook
    resp = client.post("/webhooks/github", content=body_bytes, headers=headers)
    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted"}

    # In durable mode, CandidateService must NOT have been called directly by the HTTP handler
    assert mock_candidate_service.process_issue_assignment.call_count == 0

    # 2. Worker executes job
    worker = DurableWebhookWorker(
        delivery_store=delivery_store,
        candidate_service=mock_candidate_service,
        lease_seconds=10.0,
    )
    processed = await worker.process_one()
    assert processed is True

    # CandidateService called exactly once with normalized event
    assert mock_candidate_service.process_issue_assignment.call_count == 1
    call_arg = mock_candidate_service.process_issue_assignment.call_args[0][0]
    assert isinstance(call_arg, GitHubIssueAssignmentEvent)
    assert call_arg.action == "assigned"
    assert call_arg.issue_number == 42
    assert call_arg.assignee_username == "octocat"
    assert call_arg.delivery_id == "del-assigned-001"

    # Job is marked COMPLETED
    job = await delivery_store.get_job("del-assigned-001")
    assert job["state"] == DeliveryState.COMPLETED.value

    # Subsequent worker poll yields no jobs
    assert await worker.process_one() is False


@pytest.mark.asyncio
async def test_signed_issues_unassigned_durable_queue_worker_exactly_once(tmp_path):
    db_path = str(tmp_path / "test_unassigned.db")
    delivery_store = SQLiteDeliveryStore(db_path)

    mock_candidate_service = MagicMock()
    mock_candidate_service.process_issue_assignment = AsyncMock()

    config = AppRuntimeConfig(
        db_path=db_path,
        auto_start_worker=False,
    )
    app = create_application(
        webhook_secret=WEBHOOK_SECRET,
        candidate_service=mock_candidate_service,
        delivery_store=delivery_store,
        config=config,
    )
    client = TestClient(app)

    payload = make_assignment_payload(action="unassigned")
    body_bytes = json.dumps(payload).encode()
    headers = make_signed_headers(body_bytes, delivery_id="del-unassigned-001")

    # 1. Post to webhook
    resp = client.post("/webhooks/github", content=body_bytes, headers=headers)
    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted"}

    # No immediate call in durable mode
    assert mock_candidate_service.process_issue_assignment.call_count == 0

    # 2. Worker executes job
    worker = DurableWebhookWorker(
        delivery_store=delivery_store,
        candidate_service=mock_candidate_service,
        lease_seconds=10.0,
    )
    processed = await worker.process_one()
    assert processed is True

    # CandidateService called exactly once
    assert mock_candidate_service.process_issue_assignment.call_count == 1
    call_arg = mock_candidate_service.process_issue_assignment.call_args[0][0]
    assert isinstance(call_arg, GitHubIssueAssignmentEvent)
    assert call_arg.action == "unassigned"
    assert call_arg.issue_number == 42
    assert call_arg.assignee_username == "octocat"
    assert call_arg.delivery_id == "del-unassigned-001"

    # Job is completed
    job = await delivery_store.get_job("del-unassigned-001")
    assert job["state"] == DeliveryState.COMPLETED.value


@pytest.mark.asyncio
async def test_duplicate_delivery_no_second_processing(tmp_path):
    db_path = str(tmp_path / "test_dup.db")
    delivery_store = SQLiteDeliveryStore(db_path)

    mock_candidate_service = MagicMock()
    mock_candidate_service.process_issue_assignment = AsyncMock()

    config = AppRuntimeConfig(
        db_path=db_path,
        auto_start_worker=False,
    )
    app = create_application(
        webhook_secret=WEBHOOK_SECRET,
        candidate_service=mock_candidate_service,
        delivery_store=delivery_store,
        config=config,
    )
    client = TestClient(app)

    payload = make_assignment_payload(action="assigned")
    body_bytes = json.dumps(payload).encode()
    headers = make_signed_headers(body_bytes, delivery_id="del-dup-001")

    # First delivery
    resp1 = client.post("/webhooks/github", content=body_bytes, headers=headers)
    assert resp1.status_code == 202
    assert resp1.json() == {"status": "accepted"}

    # Process first delivery
    worker = DurableWebhookWorker(
        delivery_store=delivery_store,
        candidate_service=mock_candidate_service,
        lease_seconds=10.0,
    )
    assert await worker.process_one() is True
    assert mock_candidate_service.process_issue_assignment.call_count == 1

    # Second (duplicate) delivery with same ID and payload
    resp2 = client.post("/webhooks/github", content=body_bytes, headers=headers)
    assert resp2.status_code == 202
    assert resp2.json() == {"status": "duplicate_delivery_ignored"}

    # No second job available to process
    assert await worker.process_one() is False
    # CandidateService call count must remain 1
    assert mock_candidate_service.process_issue_assignment.call_count == 1


@pytest.mark.asyncio
async def test_issues_assignment_failure_uses_fail_job_and_retries(tmp_path):
    db_path = str(tmp_path / "test_failure.db")
    delivery_store = SQLiteDeliveryStore(db_path)

    mock_candidate_service = MagicMock()
    mock_candidate_service.process_issue_assignment = AsyncMock(
        side_effect=RuntimeError("Database connection lost")
    )

    config = AppRuntimeConfig(
        db_path=db_path,
        auto_start_worker=False,
    )
    app = create_application(
        webhook_secret=WEBHOOK_SECRET,
        candidate_service=mock_candidate_service,
        delivery_store=delivery_store,
        config=config,
    )
    client = TestClient(app)

    payload = make_assignment_payload(action="assigned")
    body_bytes = json.dumps(payload).encode()
    headers = make_signed_headers(body_bytes, delivery_id="del-fail-001")

    resp = client.post("/webhooks/github", content=body_bytes, headers=headers)
    assert resp.status_code == 202

    worker = DurableWebhookWorker(
        delivery_store=delivery_store,
        candidate_service=mock_candidate_service,
        lease_seconds=10.0,
    )
    processed = await worker.process_one()
    assert processed is True

    # Check job is reset to PENDING for retry with error recorded
    job = await delivery_store.get_job("del-fail-001")
    assert job["state"] == DeliveryState.PENDING.value
    assert "Database connection lost" in (job["last_error"] or "")
    assert job["attempt_count"] == 1

    # Simulate exhausting remaining attempts (max_attempts = 3)
    # Fast-forward available_at to allow immediate reclaim
    async with await get_db_connection(delivery_store.db_path) as db:
        await db.execute(
            "UPDATE webhook_jobs SET available_at = 0 WHERE delivery_id = 'del-fail-001';"
        )
        await db.commit()

    # Attempt 2
    assert await worker.process_one() is True
    job = await delivery_store.get_job("del-fail-001")
    assert job["state"] == DeliveryState.PENDING.value
    assert job["attempt_count"] == 2

    async with await get_db_connection(delivery_store.db_path) as db:
        await db.execute(
            "UPDATE webhook_jobs SET available_at = 0 WHERE delivery_id = 'del-fail-001';"
        )
        await db.commit()

    # Attempt 3 (exhausts max_attempts=3)
    assert await worker.process_one() is True
    job = await delivery_store.get_job("del-fail-001")
    assert job["state"] == DeliveryState.FAILED.value
    assert job["attempt_count"] == 3
