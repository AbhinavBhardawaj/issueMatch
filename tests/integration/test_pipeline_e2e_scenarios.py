"""Automated Full-Stack E2E Scenarios starting from signed GitHub webhook delivery.

Scenarios tested:
1. True Positive: Webhook -> Enqueue -> Worker -> Scout -> Verifier -> Gate -> 1 GitHub issue created
2. False Positive: Webhook -> Enqueue -> Worker -> Scout -> Verifier (REJECTED) -> 0 issues created
3. Security: Webhook -> Enqueue -> Worker -> Scout -> Verifier -> Gate (SECURITY_MANUAL_REVIEW_REQUIRED) -> 0 public issues
4. Duplicate: Two separate webhook runs with identical defect signature -> exactly 1 issue created total
5. Single Execution Invariant: Scout pipeline runs exactly once per accepted durable delivery
"""
import asyncio
import hashlib
import hmac
import json
import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

from app.main import create_application, AppRuntimeConfig
from app.storage.delivery_store import SQLiteDeliveryStore
from app.services.issue_gate import SQLiteDedupStore
from app.services.write_journal import SQLiteIssueWriteJournal
from app.services.worker import DurableWebhookWorker
from app.services.pipeline import VerifierPipeline
from app.scout.agent import ScoutAgent
from app.scout.service import ScoutService
from app.infrastructure.llm_router import LLMProvider


WEBHOOK_SECRET = "integration-test-secret-42"
REPO_OWNER = "acme-corp"
REPO_NAME = "secure-vault"
REPO_ID = 98765
INSTALLATION_ID = 112233


def make_signed_headers(body: bytes, delivery_id: str, event: str = "push") -> dict:
    signature = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {
        "X-Hub-Signature-256": signature,
        "X-GitHub-Delivery": delivery_id,
        "X-GitHub-Event": event,
        "Content-Type": "application/json",
    }


def make_push_payload(commit_sha: str = "1111111111111111111111111111111111111111") -> bytes:
    return json.dumps({
        "ref": "refs/heads/main",
        "before": "0" * 40,
        "after": commit_sha,
        "repository": {
            "id": REPO_ID,
            "name": REPO_NAME,
            "default_branch": "main",
            "owner": {"login": REPO_OWNER},
        },
        "installation": {"id": INSTALLATION_ID},
        "forced": False,
        "deleted": False,
    }).encode("utf-8")


class ControlledMockGitHubClient:
    """Mock GitHub client returning realistic repository structure and tracking created issues."""

    def __init__(self):
        self.created_issues = []
        self.files = {
            "app/auth.py": (
                "def authenticate(token: str):\n"
                "    if not token:\n"
                "        return None\n"
                "    return verify_jwt(token)\n"
            ),
            "app/crypto.py": (
                "import os\n"
                "def generate_key():\n"
                "    return os.urandom(32)\n"
            ),
            "README.md": "# Secure Vault\nDocumentation for secure vault service.\n",
        }

    async def get_repository(self, owner: str, repo: str) -> dict:
        return {"id": REPO_ID, "owner": {"login": owner}, "name": repo}

    async def compare_commits(self, owner: str, repo: str, base: str, head: str) -> dict:
        return {
            "files": [
                {
                    "filename": "app/auth.py",
                    "status": "modified",
                    "additions": 4,
                    "deletions": 0,
                    "changes": 4,
                    "patch": "@@ -1,4 +1,4 @@",
                }
            ]
        }

    async def get_repository_tree(self, owner: str, repo: str, ref: str) -> tuple[list[dict], bool]:
        items = [{"path": p, "type": "blob"} for p in self.files.keys()]
        return items, False

    async def get_file_content(self, owner: str, repo: str, path: str, ref: str = "") -> str:
        if path in self.files:
            return self.files[path]
        raise ValueError(f"File {path} not found in mock repository")

    async def get_issues(self, owner: str, repo: str, state: str = "open", per_page: int = 50) -> list[dict]:
        return []

    async def search_issues(self, owner: str, repo: str, query: str) -> list[dict]:
        return []

    async def create_issue(self, owner: str, repo: str, title: str, body: str, labels: list[str] | None = None) -> dict:
        issue_number = len(self.created_issues) + 1
        issue = {
            "number": issue_number,
            "title": title,
            "body": body,
            "labels": labels or [],
        }
        self.created_issues.append(issue)
        return issue


class ControlledMockLLM(LLMProvider):
    """Deterministic LLM Provider returning preset responses."""

    def __init__(self, response_text: str):
        self.response_text = response_text
        self.invocations = []

    async def complete(self, system: str, user: str) -> str:
        self.invocations.append({"system": system, "user": user})
        return self.response_text


@pytest.fixture
def e2e_env(tmp_path):
    db_file = str(tmp_path / "e2e_durable.db")
    delivery_store = SQLiteDeliveryStore(db_file)
    dedup_store = SQLiteDedupStore(db_file)
    write_journal = SQLiteIssueWriteJournal(db_file)
    github_client = ControlledMockGitHubClient()

    config = AppRuntimeConfig(
        db_path=db_file,
        worker_lease_seconds=30.0,
        worker_max_attempts=3,
        auto_start_worker=False,
    )

    return {
        "db_file": db_file,
        "delivery_store": delivery_store,
        "dedup_store": dedup_store,
        "write_journal": write_journal,
        "github_client": github_client,
        "config": config,
    }


def _build_app_with_mock_llms(e2e_env, scout_llm: LLMProvider, verifier_llm: LLMProvider):
    async def client_factory(inst_id: int):
        return e2e_env["github_client"]

    scout_agent = ScoutAgent(llm_provider=scout_llm)
    verifier_pipeline = VerifierPipeline(
        llm_provider=verifier_llm,
        dedup_store=e2e_env["dedup_store"],
        write_journal=e2e_env["write_journal"],
    )
    scout_service = ScoutService(
        scout_agent=scout_agent,
        verifier_pipeline=verifier_pipeline,
        read_client_factory=client_factory,
        downstream_write_client_factory=client_factory,
    )

    app = create_application(
        webhook_secret=WEBHOOK_SECRET,
        delivery_store=e2e_env["delivery_store"],
        dedup_store=e2e_env["dedup_store"],
        write_journal=e2e_env["write_journal"],
        scout_service=scout_service,
        config=e2e_env["config"],
        start_worker=False,
    )

    worker = DurableWebhookWorker(
        delivery_store=e2e_env["delivery_store"],
        scout_service=scout_service,
        candidate_service=None,
    )
    app.state.webhook_worker = worker

    return app, worker, scout_service


# ===========================================================================
# 1. TRUE POSITIVE SCENARIO
# ===========================================================================

@pytest.mark.asyncio
async def test_scenario_true_positive_creates_one_issue(e2e_env):
    """
    True Positive:
    1. Webhook arrives -> HTTP 202 accepted, enqueued to durable SQLite queue
    2. DurableWebhookWorker processes delivery
    3. Scout discovers valid grounded defect
    4. Verifier confirms with valid grounded citation
    5. Issue Gate authorizes
    6. Exactly ONE GitHub issue is created
    """
    scout_json = json.dumps({
        "findings": [
            {
                "category": "behavioral_bug",
                "title": "Authentication bypass on missing token",
                "severity": "high",
                "file": "app/auth.py",
                "function": "authenticate",
                "line": 2,
                "description": "Returns None instead of raising an AuthenticationRequired error",
                "expected_behavior": "Should raise an AuthenticationRequired exception",
                "evidence": [
                    {
                        "file": "app/auth.py",
                        "line": 2,
                        "snippet": "if not token:\n        return None",
                    }
                ],
                "confidence": 0.95,
                "impact": "critical",
                "impact_reason": "Unauthenticated callers may proceed",
                "observable_behavior": "Returns None",
                "affected_user_or_system": "Auth caller",
                "actionable": True,
                "actionability_reason": "Clear bug fix needed",
                "regression_likelihood": "introduced_by_push",
                "expected_behavior_basis": "api_contract",
                "expected_behavior_evidence": "Expected auth failure",
            }
        ]
    })

    verifier_json = json.dumps({
        "status": "VERIFIED",
        "confidence": 0.95,
        "reason": "Confirmed returning None bypasses auth exceptions in callers",
        "supporting_evidence": [
            {
                "file": "app/auth.py",
                "line": 2,
                "snippet": "if not token:\n        return None",
            }
        ],
        "counter_evidence": [],
        "duplicate_issue": False,
    })

    scout_llm = ControlledMockLLM(scout_json)
    verifier_llm = ControlledMockLLM(verifier_json)
    app, worker, _ = _build_app_with_mock_llms(e2e_env, scout_llm, verifier_llm)

    client = TestClient(app)
    payload = make_push_payload()
    headers = make_signed_headers(payload, delivery_id="del-tp-001")

    # Step 1: Webhook ingestion
    resp = client.post("/webhooks/github", content=payload, headers=headers)
    assert resp.status_code == 202
    assert resp.json()["status"] == "accepted"

    # Step 2: Worker processes the job
    processed = await worker.process_one()
    assert processed is True

    # Step 3: Verify exactly 1 issue was created
    assert len(e2e_env["github_client"].created_issues) == 1
    issue = e2e_env["github_client"].created_issues[0]
    assert issue["number"] == 1
    assert "Authentication bypass on missing token" in issue["title"]


# ===========================================================================
# 2. FALSE POSITIVE SCENARIO
# ===========================================================================

@pytest.mark.asyncio
async def test_scenario_false_positive_creates_zero_issues(e2e_env):
    """
    False Positive:
    1. Scout flags a false positive
    2. Verifier investigates independent context and REJECTS it
    3. Gate denies authorization
    4. Exactly ZERO issues created
    """
    scout_json = json.dumps({
        "findings": [
            {
                "category": "behavioral_bug",
                "title": "Flawed random key generation",
                "severity": "medium",
                "file": "app/crypto.py",
                "function": "generate_key",
                "line": 3,
                "description": "os.urandom might not be cryptographically strong",
                "expected_behavior": "Use secrets module",
                "evidence": [
                    {
                        "file": "app/crypto.py",
                        "line": 3,
                        "snippet": "return os.urandom(32)",
                    }
                ],
                "confidence": 0.80,
                "impact": "meaningful",
                "impact_reason": "Security weakness",
                "observable_behavior": "Key generated",
                "affected_user_or_system": "crypto subsystem",
                "actionable": True,
                "actionability_reason": "Replace with secrets.token_bytes",
                "regression_likelihood": "possibly_introduced",
                "expected_behavior_basis": "api_contract",
                "expected_behavior_evidence": "Documentation",
            }
        ]
    })

    verifier_json = json.dumps({
        "status": "REJECTED",
        "confidence": 0.99,
        "reason": "os.urandom(32) is cryptographically secure on all modern supported platforms",
        "supporting_evidence": [],
        "counter_evidence": [
            {
                "file": "app/crypto.py",
                "line": 3,
                "snippet": "return os.urandom(32)",
            }
        ],
        "duplicate_issue": False,
    })

    scout_llm = ControlledMockLLM(scout_json)
    verifier_llm = ControlledMockLLM(verifier_json)
    app, worker, _ = _build_app_with_mock_llms(e2e_env, scout_llm, verifier_llm)

    client = TestClient(app)
    payload = make_push_payload()
    headers = make_signed_headers(payload, delivery_id="del-fp-002")

    resp = client.post("/webhooks/github", content=payload, headers=headers)
    assert resp.status_code == 202

    processed = await worker.process_one()
    assert processed is True

    # Zero issues created
    assert len(e2e_env["github_client"].created_issues) == 0


# ===========================================================================
# 3. SECURITY FINDING SCENARIO
# ===========================================================================

@pytest.mark.asyncio
async def test_scenario_security_category_requires_manual_review(e2e_env):
    """
    Security Finding:
    Security-category defects must NEVER be posted as public issues.
    Gate must deny with SECURITY_MANUAL_REVIEW_REQUIRED.
    """
    scout_json = json.dumps({
        "findings": [
            {
                "category": "security",
                "title": "Hardcoded authentication bypass token in source",
                "severity": "critical",
                "file": "app/auth.py",
                "function": "authenticate",
                "line": 2,
                "description": "Auth check allows bypass token",
                "expected_behavior": "Never allow backdoor bypass",
                "evidence": [
                    {
                        "file": "app/auth.py",
                        "line": 2,
                        "snippet": "if not token:\n        return None",
                    }
                ],
                "confidence": 0.99,
                "impact": "critical",
                "impact_reason": "Critical vulnerability",
                "observable_behavior": "Auth bypass",
                "affected_user_or_system": "Entire system",
                "actionable": True,
                "actionability_reason": "Remove backdoor immediately",
                "regression_likelihood": "introduced_by_push",
                "expected_behavior_basis": "api_contract",
                "expected_behavior_evidence": "Security policy",
            }
        ]
    })

    verifier_json = json.dumps({
        "status": "VERIFIED",
        "confidence": 0.99,
        "reason": "Confirmed backdoor vulnerability",
        "supporting_evidence": [
            {
                "file": "app/auth.py",
                "line": 2,
                "snippet": "if not token:\n        return None",
            }
        ],
        "counter_evidence": [],
        "duplicate_issue": False,
    })

    scout_llm = ControlledMockLLM(scout_json)
    verifier_llm = ControlledMockLLM(verifier_json)
    app, worker, _ = _build_app_with_mock_llms(e2e_env, scout_llm, verifier_llm)

    client = TestClient(app)
    payload = make_push_payload()
    headers = make_signed_headers(payload, delivery_id="del-sec-003")

    resp = client.post("/webhooks/github", content=payload, headers=headers)
    assert resp.status_code == 202

    processed = await worker.process_one()
    assert processed is True

    # Zero public issues created!
    assert len(e2e_env["github_client"].created_issues) == 0


# ===========================================================================
# 4. DUPLICATE DEFECT SCENARIO
# ===========================================================================

@pytest.mark.asyncio
async def test_scenario_duplicate_findings_produce_one_issue_total(e2e_env):
    """
    Duplicate Scenario:
    Delivery 1: Identifies defect and creates issue #1
    Delivery 2: Subsequent push/run re-identifies same defect
    Result: DedupStore identifies existing reservation/committed issue; 0 new issues created. Total issues == 1.
    """
    scout_json = json.dumps({
        "findings": [
            {
                "category": "behavioral_bug",
                "title": "Authentication bypass on missing token",
                "severity": "high",
                "file": "app/auth.py",
                "function": "authenticate",
                "line": 2,
                "description": "Returns None instead of raising an AuthenticationRequired error",
                "expected_behavior": "Should raise an AuthenticationRequired exception",
                "evidence": [
                    {
                        "file": "app/auth.py",
                        "line": 2,
                        "snippet": "if not token:\n        return None",
                    }
                ],
                "confidence": 0.95,
                "impact": "critical",
                "impact_reason": "Unauthenticated callers may proceed",
                "observable_behavior": "Returns None",
                "affected_user_or_system": "Auth caller",
                "actionable": True,
                "actionability_reason": "Clear bug fix needed",
                "regression_likelihood": "introduced_by_push",
                "expected_behavior_basis": "api_contract",
                "expected_behavior_evidence": "Expected auth failure",
            }
        ]
    })

    verifier_json = json.dumps({
        "status": "VERIFIED",
        "confidence": 0.95,
        "reason": "Confirmed returning None bypasses auth exceptions in callers",
        "supporting_evidence": [
            {
                "file": "app/auth.py",
                "line": 2,
                "snippet": "if not token:\n        return None",
            }
        ],
        "counter_evidence": [],
        "duplicate_issue": False,
    })

    scout_llm = ControlledMockLLM(scout_json)
    verifier_llm = ControlledMockLLM(verifier_json)
    app, worker, _ = _build_app_with_mock_llms(e2e_env, scout_llm, verifier_llm)
    client = TestClient(app)

    # First delivery
    payload1 = make_push_payload(commit_sha="a" * 40)
    headers1 = make_signed_headers(payload1, delivery_id="del-dup-001")
    resp1 = client.post("/webhooks/github", content=payload1, headers=headers1)
    assert resp1.status_code == 202

    await worker.process_one()
    assert len(e2e_env["github_client"].created_issues) == 1

    # Second delivery with identical defect on another commit
    payload2 = make_push_payload(commit_sha="b" * 40)
    headers2 = make_signed_headers(payload2, delivery_id="del-dup-002")
    resp2 = client.post("/webhooks/github", content=payload2, headers=headers2)
    assert resp2.status_code == 202

    await worker.process_one()
    # Total issues created remains 1!
    assert len(e2e_env["github_client"].created_issues) == 1


# ===========================================================================
# 5. SINGLE EXECUTION INVARIANT
# ===========================================================================

@pytest.mark.asyncio
async def test_scout_pipeline_runs_exactly_once_per_accepted_durable_delivery(e2e_env):
    """
    Verify that in durable mode:
    1. Webhook enqueue does NOT trigger background execution
    2. Exactly one worker pass runs ScoutService.process_push
    """
    scout_json = json.dumps({"findings": []})
    scout_llm = ControlledMockLLM(scout_json)
    verifier_llm = ControlledMockLLM(json.dumps({"status": "REJECTED"}))
    app, worker, scout_service = _build_app_with_mock_llms(e2e_env, scout_llm, verifier_llm)

    # Wrap process_push to track calls
    original_process_push = scout_service.process_push
    call_count = 0

    async def tracking_process_push(event):
        nonlocal call_count
        call_count += 1
        return await original_process_push(event)

    scout_service.process_push = tracking_process_push

    client = TestClient(app)
    payload = make_push_payload()
    headers = make_signed_headers(payload, delivery_id="del-single-exec-001")

    resp = client.post("/webhooks/github", content=payload, headers=headers)
    assert resp.status_code == 202

    # Verify that TestClient exiting did NOT execute process_push (No BackgroundTask!)
    assert call_count == 0

    # Worker executes exactly once
    processed = await worker.process_one()
    assert processed is True
    assert call_count == 1

    # Second worker poll: no remaining jobs
    processed_again = await worker.process_one()
    assert processed_again is False
    assert call_count == 1
