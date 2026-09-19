import pytest
from app.github.events import normalize_push_event, GitHubPushEvent, MalformedGitHubEvent

VALID_PAYLOAD = {
    "ref": "refs/heads/main",
    "before": "a" * 40,
    "after": "b" * 40,
    "repository": {
        "id": 12345,
        "name": "my-repo",
        "default_branch": "main",
        "owner": {"login": "my-org"}
    },
    "installation": {"id": 67890},
    "forced": False,
    "deleted": False
}


def test_valid_default_branch_push():
    event = normalize_push_event(VALID_PAYLOAD, "del-123")
    assert isinstance(event, GitHubPushEvent)
    assert event.delivery_id == "del-123"
    assert event.installation_id == 67890
    assert event.repository_id == 12345
    assert event.repository_owner == "my-org"
    assert event.repository_name == "my-repo"
    assert event.default_branch == "main"
    assert event.before_sha == "a" * 40
    assert event.after_sha == "b" * 40
    assert event.ref == "refs/heads/main"
    assert event.forced is False
    assert event.deleted is False


def test_initial_push_all_zero_before():
    payload = dict(VALID_PAYLOAD)
    payload["before"] = "0" * 40
    event = normalize_push_event(payload, "del-initial")
    assert isinstance(event, GitHubPushEvent)
    assert event.before_sha == "0" * 40


def test_deleted_branch_ignored():
    payload = dict(VALID_PAYLOAD)
    payload["deleted"] = True
    event = normalize_push_event(payload, "del-del")
    assert event is None


def test_all_zero_after_sha_ignored():
    payload = dict(VALID_PAYLOAD)
    payload["after"] = "0" * 40
    event = normalize_push_event(payload, "del-zero-after")
    assert event is None


def test_non_default_branch_ignored():
    payload = dict(VALID_PAYLOAD)
    payload["ref"] = "refs/heads/feature-branch"
    event = normalize_push_event(payload, "del-feat")
    assert event is None


def test_missing_delivery_id_rejected():
    with pytest.raises(MalformedGitHubEvent, match="Missing or empty"):
        normalize_push_event(VALID_PAYLOAD, "")


def test_invalid_repo_id():
    payload = dict(VALID_PAYLOAD)
    payload["repository"] = {"id": -5, "name": "r", "owner": {"login": "o"}}
    with pytest.raises(MalformedGitHubEvent, match="positive integer"):
        normalize_push_event(payload, "del-id")


def test_invalid_sha_format():
    payload = dict(VALID_PAYLOAD)
    payload["after"] = "not-a-valid-sha"
    with pytest.raises(MalformedGitHubEvent, match="Invalid after SHA"):
        normalize_push_event(payload, "del-sha")


def test_malformed_payload_type():
    with pytest.raises(MalformedGitHubEvent, match="must be a dictionary"):
        normalize_push_event("string-payload", "del-type")
