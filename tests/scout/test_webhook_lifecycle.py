import hashlib
import hmac
import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi.testclient import TestClient

from app.main import create_application
from app.storage.delivery_store import InMemoryDeliveryStore, DeliveryState
from app.scout.models import ScoutRunResult


WEBHOOK_SECRET = "test-webhook-secret-123"


def make_signed_headers(body: bytes, delivery_id: str = "del-test-1", event: str = "push") -> dict:
    signature = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {
        "X-Hub-Signature-256": signature,
        "X-GitHub-Delivery": delivery_id,
        "X-GitHub-Event": event,
        "Content-Type": "application/json",
    }


def make_valid_push_dict():
    return {
        "ref": "refs/heads/main",
        "before": "a" * 40,
        "after": "b" * 40,
        "repository": {
            "id": 12345,
            "name": "repo",
            "default_branch": "main",
            "owner": {"login": "org"},
        },
        "installation": {"id": 100},
        "forced": False,
        "deleted": False,
    }


# ===========================================================================
# 1. MALFORMED PAYLOAD DOES NOT POISON DELIVERY ID (Section 11)
# ===========================================================================

def test_malformed_json_does_not_poison_delivery_id():
    """Malformed JSON must not permanently claim the delivery ID."""
    store = InMemoryDeliveryStore()
    mock_scout_service = MagicMock()
    mock_scout_service.process_push = AsyncMock(
        return_value=ScoutRunResult(repository_id=12345, commit_sha="b" * 40)
    )

    app = create_application(
        webhook_secret=WEBHOOK_SECRET,
        delivery_store=store,
        scout_service=mock_scout_service,
    )
    client = TestClient(app)

    # Request 1: Malformed JSON
    malformed_body = b'{"broken json'
    headers_1 = make_signed_headers(malformed_body, delivery_id="del-poison-1")
    resp1 = client.post("/webhooks/github", content=malformed_body, headers=headers_1)
    assert resp1.status_code == 400
    assert "Malformed JSON" in resp1.json()["detail"]

    # Delivery ID should NOT be claimed in store
    assert "del-poison-1" not in store._deliveries

    # Request 2: Valid payload with same delivery ID
    valid_payload = json.dumps(make_valid_push_dict()).encode("utf-8")
    headers_2 = make_signed_headers(valid_payload, delivery_id="del-poison-1")
    resp2 = client.post("/webhooks/github", content=valid_payload, headers=headers_2)
    assert resp2.status_code == 202
    assert resp2.json()["status"] == "accepted"


# ===========================================================================
# 2. MALFORMED PUSH STRUCTURE (Section 14)
# ===========================================================================

def test_malformed_push_structure_rejected_and_does_not_poison():
    """Missing required push fields must be treated as malformed (400), not ignored."""
    store = InMemoryDeliveryStore()
    app = create_application(webhook_secret=WEBHOOK_SECRET, delivery_store=store)
    client = TestClient(app)

    # Missing ref and after
    bad_payload = json.dumps({"repository": {"id": 123}}).encode("utf-8")
    headers = make_signed_headers(bad_payload, delivery_id="del-malformed-push")
    resp = client.post("/webhooks/github", content=bad_payload, headers=headers)
    assert resp.status_code == 400
    assert "del-malformed-push" not in store._deliveries


# ===========================================================================
# 3. FAILED PROCESSING IS RETRYABLE (Section 12)
# ===========================================================================

def test_failed_processing_is_retryable():
    """When processing fails, redelivery of same ID + same hash can retry."""
    store = InMemoryDeliveryStore()
    call_count = 0

    async def dynamic_process(event):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("Temporary infrastructure outage")
        return ScoutRunResult(repository_id=12345, commit_sha="b" * 40)

    mock_scout_service = MagicMock()
    mock_scout_service.process_push = dynamic_process

    app = create_application(
        webhook_secret=WEBHOOK_SECRET,
        delivery_store=store,
        scout_service=mock_scout_service,
    )
    client = TestClient(app)

    payload = json.dumps(make_valid_push_dict()).encode("utf-8")
    headers = make_signed_headers(payload, delivery_id="del-retry-1")

    # Attempt 1: Fails
    # Note: TestClient runs background_tasks before returning!
    with pytest.raises(RuntimeError, match="Temporary infrastructure outage"):
        client.post("/webhooks/github", content=payload, headers=headers)

    # Verify store is FAILED
    record = store._deliveries.get("del-retry-1")
    assert record is not None
    assert record["state"] == DeliveryState.FAILED

    # Attempt 2: Redelivery succeeds
    resp2 = client.post("/webhooks/github", content=payload, headers=headers)
    assert resp2.status_code == 202
    assert resp2.json()["status"] == "accepted"
    assert call_count == 2
    assert store._deliveries["del-retry-1"]["state"] == DeliveryState.COMPLETED


# ===========================================================================
# 4. SAME DELIVERY ID DIFFERENT BODY (Section 13)
# ===========================================================================

def test_same_delivery_id_different_body_fails_closed():
    """Same delivery ID with differing payload hash must fail closed (400) and never dispatch."""
    store = InMemoryDeliveryStore()
    mock_scout = MagicMock()
    mock_scout.process_push = AsyncMock(
        return_value=ScoutRunResult(repository_id=12345, commit_sha="b" * 40)
    )

    app = create_application(
        webhook_secret=WEBHOOK_SECRET,
        delivery_store=store,
        scout_service=mock_scout,
    )
    client = TestClient(app)

    payload1 = json.dumps(make_valid_push_dict()).encode("utf-8")
    headers1 = make_signed_headers(payload1, delivery_id="del-collision")
    resp1 = client.post("/webhooks/github", content=payload1, headers=headers1)
    assert resp1.status_code == 202

    # Second request: same delivery ID, different content
    modified_dict = make_valid_push_dict()
    modified_dict["after"] = "c" * 40
    payload2 = json.dumps(modified_dict).encode("utf-8")
    headers2 = make_signed_headers(payload2, delivery_id="del-collision")

    resp2 = client.post("/webhooks/github", content=payload2, headers=headers2)
    assert resp2.status_code == 400
    assert "payload mismatch" in resp2.json()["detail"].lower()

    # process_push called exactly once (for payload1 only)
    assert mock_scout.process_push.call_count == 1
