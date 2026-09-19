import os
import time
import jwt
import httpx
import pytest
from dotenv import load_dotenv
load_dotenv()
from app.domain.models import Finding, EvidenceItem
from app.domain.states import FindingStatus, EvidenceStatus
from app.github.client import GitHubRestClient
from app.github.repo_fetcher import fetch_repo_context
from app.verifier.evidence import validate_evidence
from app.infrastructure.llm_router import load_providers, MultiLLMProvider
from app.services.pipeline import VerifierPipeline

def get_installation_token():
    """Helper to dynamically generate a token using the private key."""
    if os.environ.get("RUN_LIVE_GITHUB_TESTS") != "1":
        return None
    token = os.environ.get("GH_INSTALLATION_TOKEN")
    if token:
        return token
        
    try:
        app_id = "4991882"
        installation_id = "162794652"
        private_key = open("keys/verifier-bot.pem").read()
        
        payload = {"iat": int(time.time()), "exp": int(time.time()) + 600, "iss": app_id}
        encoded = jwt.encode(payload, private_key, algorithm="RS256")
        resp = httpx.post(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {encoded}", "Accept": "application/vnd.github+json"}
        )
        return resp.json()["token"]
    except Exception as e:
        print(f"Failed to generate token: {e}")
        return None

def get_sample_finding():
    return dict(
        finding_id="F-nitiflow-e2e-001",
        installation_id=os.environ.get("GH_INSTALLATION_ID") if os.environ.get("GH_INSTALLATION_ID") else None,
        repository_id=os.environ.get("GH_REPO_ID", "0"),
        commit_sha=os.environ.get("GH_COMMIT_SHA", ""),    # latest SHA on default branch
        title="Global error handler registered after app.listen() — never executes",
        severity="medium",
        file="backend/server.js",
        function=None,
        line=40,
        description=(
            "The global error handler middleware is mounted after app.listen() "
            "on line 40. In Express, middleware registered after the server starts "
            "listening is never invoked. Any unhandled errors will bypass this handler entirely."
        ),
        expected_behavior="Error handler middleware should be registered before app.listen().",
        evidence=[
            EvidenceItem(
                file="backend/server.js",
                line=40,
                snippet="app.use((err, req, res, next) => {",
            )
        ],
        confidence=0.87,
        status=FindingStatus.DISCOVERED,
    )

@pytest.mark.asyncio
@pytest.mark.skipif(
    not get_installation_token(),
    reason="Integration test requires real GitHub credentials (keys/verifier-bot.pem missing)"
)
async def test_e2e_creates_issue_in_nitiflow():
    owner = "koushiksuresh27"
    repo_name = "NitiFlow"
    token = get_installation_token()

    finding = Finding(**get_sample_finding())
    client = GitHubRestClient(token=token)
    repo_context = await fetch_repo_context(finding, client, owner, repo_name)

    ev = validate_evidence(finding, repo_context)
    assert ev.overall == EvidenceStatus.SUPPORTED, f"Evidence failed: {ev}"

    llm = MultiLLMProvider(load_providers())
    pipeline = VerifierPipeline(llm, client)

    result = await pipeline.run(finding, repo_context, owner, repo_name)

    assert result is not None, "Pipeline returned None — issue was not created"
    assert result.issue_number > 0
    print(f"\nSUCCESS: Issue created: https://github.com/{owner}/{repo_name}/issues/{result.issue_number}")

def get_false_positive_finding():
    return dict(
        finding_id="F-nitiflow-e2e-false",
        installation_id=os.environ.get("GH_INSTALLATION_ID") if os.environ.get("GH_INSTALLATION_ID") else None,
        repository_id=os.environ.get("GH_REPO_ID", "0"),
        commit_sha=os.environ.get("GH_COMMIT_SHA", ""),
        title="Missing /api/health healthcheck endpoint",
        severity="low",
        file="backend/server.js",
        function=None,
        line=27,
        description=(
            "The backend server does not implement any healthcheck endpoint. "
            "A healthcheck route at /api/health is missing, making it impossible "
            "to monitor uptime."
        ),
        expected_behavior="Implement a GET /api/health route.",
        evidence=[
            EvidenceItem(
                file="backend/server.js",
                line=27,
                snippet="app.get('/api/health', (req, res) => {",
            )
        ],
        confidence=0.87,
        status=FindingStatus.DISCOVERED,
    )

@pytest.mark.asyncio
@pytest.mark.skipif(
    not get_installation_token(),
    reason="Integration test requires real GitHub credentials (keys/verifier-bot.pem missing)"
)
async def test_e2e_rejects_false_positive():
    owner = "koushiksuresh27"
    repo_name = "NitiFlow"
    token = get_installation_token()

    finding = Finding(**get_false_positive_finding())
    client = GitHubRestClient(token=token)
    repo_context = await fetch_repo_context(finding, client, owner, repo_name)

    # Evidence validator still passes because the snippet text exists
    ev = validate_evidence(finding, repo_context)
    assert ev.overall == EvidenceStatus.SUPPORTED

    llm = MultiLLMProvider(load_providers())
    pipeline = VerifierPipeline(llm, client)

    result = await pipeline.run(finding, repo_context, owner, repo_name)
    
    # The LLM should reject it, meaning the pipeline returns None!
    assert result is None, "Pipeline created an issue for a false positive!"
    print("\nSUCCESS: LLM successfully caught the false positive and rejected the issue!")
