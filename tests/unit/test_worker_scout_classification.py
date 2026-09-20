import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from app.services.worker import DurableWebhookWorker
from app.scout.models import ScoutRunResult


@pytest.mark.asyncio
async def test_worker_fails_job_on_scout_operational_failure():
    """Worker must classify ScoutRunResult.failures as operational failure and call fail_job(retryable=True)."""
    delivery_store = AsyncMock()
    delivery_store.claim_next_available_job.return_value = {
        "delivery_id": "del-1",
        "lease_token": "tok-1",
        "event_type": "push",
        "payload_json": json.dumps({
            "repository": {"owner": {"login": "test-owner"}, "name": "test-repo", "id": 100, "default_branch": "main"},
            "installation": {"id": 200},
            "ref": "refs/heads/main",
            "before": "a" * 40,
            "after": "b" * 40,
        }),
    }

    scout_service = AsyncMock()
    # Simulate Scout operational failure
    scout_service.process_push.return_value = ScoutRunResult(
        repository_id=100,
        commit_sha="b" * 40,
        failures=["SCOUT_AGENT_FAILED: LLM provider timeout after 60s"],
    )

    worker = DurableWebhookWorker(
        delivery_store=delivery_store,
        scout_service=scout_service,
    )

    processed = await worker.process_one()
    assert processed is True
    scout_service.process_push.assert_called_once()
    delivery_store.fail_job.assert_called_once_with(
        "del-1", "tok-1", error_msg="SCOUT_AGENT_FAILED: LLM provider timeout after 60s", retryable=True
    )
    delivery_store.complete_job.assert_not_called()


@pytest.mark.asyncio
async def test_worker_completes_job_on_zero_findings_or_deterministic_rejection():
    """Worker must classify ScoutRunResult with 0 findings and empty failures as valid business completion."""
    delivery_store = AsyncMock()
    delivery_store.claim_next_available_job.return_value = {
        "delivery_id": "del-2",
        "lease_token": "tok-2",
        "event_type": "push",
        "payload_json": json.dumps({
            "repository": {"owner": {"login": "test-owner"}, "name": "test-repo", "id": 100, "default_branch": "main"},
            "installation": {"id": 200},
            "ref": "refs/heads/main",
            "before": "a" * 40,
            "after": "b" * 40,
        }),
    }

    scout_service = AsyncMock()
    # 0 findings, 0 issues created, but NO operational failures
    scout_service.process_push.return_value = ScoutRunResult(
        repository_id=100,
        commit_sha="b" * 40,
        ai_drafts=0,
        escalated_findings=0,
        verifier_rejected=0,
        issues_created=0,
        failures=[],
    )

    worker = DurableWebhookWorker(
        delivery_store=delivery_store,
        scout_service=scout_service,
    )

    processed = await worker.process_one()
    assert processed is True
    scout_service.process_push.assert_called_once()
    delivery_store.complete_job.assert_called_once_with("del-2", "tok-2")
    delivery_store.fail_job.assert_not_called()
