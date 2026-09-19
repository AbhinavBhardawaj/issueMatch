from enum import Enum
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class ChangedFile(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    previous_path: str | None = None
    status: str = "modified"
    additions: int = 0
    deletions: int = 0
    changes: int = 0
    patch: str | None = None


class ScoutFile(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    content: str
    changed: bool = False
    patch: str | None = None
    truncated: bool = False
    size_bytes: int = 0


class ScoutExistingIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    number: int
    title: str
    state: str = "open"
    body_summary: str = ""


from app.domain.models import ContextCompleteness


class ScoutContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    installation_id: int
    repository_id: int
    owner: str
    name: str
    before_sha: str
    commit_sha: str
    default_branch: str = "main"
    files: list[ScoutFile] = Field(default_factory=list)
    test_files: list[ScoutFile] = Field(default_factory=list)
    readme: str = ""
    existing_issues: list[ScoutExistingIssue] = Field(default_factory=list)
    changed_paths: list[str] = Field(default_factory=list)
    total_context_bytes: int = 0
    context_completeness: ContextCompleteness = ContextCompleteness.COMPLETE
    partial_reasons: list[str] = Field(default_factory=list)


class SuppressionReason(str, Enum):
    LOW_IMPACT = "LOW_IMPACT"
    NOT_ACTIONABLE = "NOT_ACTIONABLE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    NO_EXPECTED_BEHAVIOR_BASIS = "NO_EXPECTED_BEHAVIOR_BASIS"
    INVALID_FILE = "INVALID_FILE"
    INVALID_LINE = "INVALID_LINE"
    EMPTY_SNIPPET = "EMPTY_SNIPPET"
    INVALID_SNIPPET = "INVALID_SNIPPET"
    DUPLICATE_FILE_FINDING = "DUPLICATE_FILE_FINDING"
    CAP_EXCEEDED = "CAP_EXCEEDED"
    STYLE_OR_CODE_SMELL = "STYLE_OR_CODE_SMELL"
    FEATURE_REQUEST = "FEATURE_REQUEST"
    TEST_GAP = "TEST_GAP"
    REFACTOR = "REFACTOR"
    PARTIAL_CONTEXT_GLOBAL_ABSENCE = "PARTIAL_CONTEXT_GLOBAL_ABSENCE"


class ScoutRunResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    repository_id: int
    commit_sha: str
    changed_files: list[str] = Field(default_factory=list)
    ai_drafts: int = 0
    suppressed_findings: int = 0
    escalated_findings: int = 0
    verifier_rejected: int = 0
    issues_created: int = 0
    failures: list[str] = Field(default_factory=list)
    suppression_reasons: list[dict[str, Any]] = Field(default_factory=list)
