from pydantic import BaseModel, ConfigDict, Field, field_validator
from app.domain.states import FindingStatus, VerificationStatus

class EvidenceItem(BaseModel):
    model_config = ConfigDict(frozen=True)
    
    file: str
    line: int = Field(ge=1)
    snippet: str

    @field_validator("snippet")
    @classmethod
    def validate_snippet_not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Evidence snippet cannot be empty or whitespace only")
        return v

class Finding(BaseModel):
    model_config = ConfigDict(frozen=True)
    
    finding_id: str
    installation_id: str
    repository_id: str
    commit_sha: str
    title: str
    severity: str
    category: str = ""
    file: str
    function: str | None = None
    line: int | None = None
    description: str
    expected_behavior: str
    evidence: list[EvidenceItem] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    claim_scope: str = "local"
    depends_on_absence: bool = False
    status: FindingStatus = FindingStatus.DISCOVERED
    created_at: str = ""

class VerifierEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    file: str = Field(min_length=1)
    line: int = Field(ge=1)
    snippet: str = Field(min_length=1)

    @field_validator("file", "snippet")
    @classmethod
    def reject_whitespace_only(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Field cannot be empty or whitespace-only")
        return v.strip()


class Verification(BaseModel):
    model_config = ConfigDict(frozen=True)
    
    verification_id: str
    finding_id: str
    installation_id: str
    repository_id: str
    commit_sha: str
    status: VerificationStatus
    reason: str
    supporting_evidence: list[VerifierEvidence] = Field(default_factory=list)
    counter_evidence: list[VerifierEvidence] = Field(default_factory=list)
    duplicate_issue: bool = False
    confidence: float = Field(ge=0, le=1)
    verified_at: str = ""

class RepoContextFile(BaseModel):
    model_config = ConfigDict(frozen=True)
    
    path: str
    content: str
    size_bytes: int = 0

class ExistingIssue(BaseModel):
    model_config = ConfigDict(frozen=True)
    
    number: int
    title: str
    state: str
    body_summary: str = ""

from enum import Enum

class ContextCompleteness(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"

class RepoContext(BaseModel):
    model_config = ConfigDict(frozen=True)
    
    repository_id: str
    installation_id: str
    owner: str
    name: str
    commit_sha: str
    default_branch: str = "main"
    files: list[RepoContextFile] = Field(default_factory=list)
    readme: str = ""
    existing_issues: list[ExistingIssue] = Field(default_factory=list)
    test_files: list[RepoContextFile] = Field(default_factory=list)
    caller_files: list[RepoContextFile] = Field(default_factory=list)
    total_context_bytes: int = 0
    context_completeness: ContextCompleteness = ContextCompleteness.COMPLETE
    partial_reasons: list[str] = Field(default_factory=list)
    omissions: list[str] = Field(default_factory=list)
