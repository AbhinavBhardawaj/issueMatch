import pytest
from app.main import create_app
from app.candidates.repository import InMemoryCandidateRepository
from app.candidates.service import CandidateService
from tests.fakes import FakeGitHubClient, FakeAnalysisService
from fastapi.testclient import TestClient


def test_dev_verify_api_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ENABLE_DEV_VERIFY_API", raising=False)
    repo = InMemoryCandidateRepository()
    cand_service = CandidateService(repo, lambda _: FakeGitHubClient(), FakeAnalysisService())
    app = create_app("secret", cand_service)
    client = TestClient(app)

    resp = client.post("/api/verify", json={"owner": "o", "repo_name": "r", "finding": {}})
    assert resp.status_code == 404


def test_dev_scout_api_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ENABLE_DEV_SCOUT_API", raising=False)
    repo = InMemoryCandidateRepository()
    cand_service = CandidateService(repo, lambda _: FakeGitHubClient(), FakeAnalysisService())
    app = create_app("secret", cand_service)
    client = TestClient(app)

    resp = client.post(
        "/api/scout",
        json={
            "installation_id": 1,
            "repository_id": 2,
            "owner": "o",
            "repo_name": "r",
            "before_sha": "a" * 40,
            "after_sha": "b" * 40,
        },
    )
    assert resp.status_code == 404
