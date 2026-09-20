import hmac
import hashlib
import json
import pytest
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient
from app.main import create_application, AppRuntimeConfig


def test_push_to_non_default_branch_is_ignored_without_scout_invocation():
    """Webhook must return status: ignored and not invoke Scout when push targets a non-default branch."""
    secret = "test-webhook-secret"
    mock_scout = AsyncMock()

    app = create_application(
        webhook_secret=secret,
        scout_service=mock_scout,
        config=AppRuntimeConfig(auto_start_worker=False),
        start_worker=False,
    )
    client = TestClient(app)

    # Repository default branch is 'main', but push is to 'refs/heads/feature-branch'
    payload = {
        "repository": {
            "owner": {"login": "test-owner"},
            "name": "test-repo",
            "id": 12345,
            "default_branch": "main",
        },
        "installation": {"id": 67890},
        "ref": "refs/heads/feature-branch",
        "before": "1" * 40,
        "after": "2" * 40,
        "forced": False,
        "deleted": False,
    }
    raw_body = json.dumps(payload).encode("utf-8")
    sig = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()

    response = client.post(
        "/webhooks/github",
        content=raw_body,
        headers={
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "del-non-default-branch",
            "X-Hub-Signature-256": sig,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 202
    assert response.json() == {"status": "ignored"}
    mock_scout.process_push.assert_not_called()
