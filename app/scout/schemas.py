from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class ScoutEvidenceDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    file: str
    line: int = Field(ge=1)
    snippet: str

    @field_validator("snippet")
    @classmethod
    def validate_snippet_not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Evidence snippet cannot be empty or whitespace only")
        return v


class ScoutFindingDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    category: Literal[
        "behavioral_bug",
        "security",
        "data_integrity",
        "reliability",
        "performance",
        "style",
        "refactor",
        "feature_request",
        "test_gap",
        "other",
    ]
    title: str
    severity: Literal["low", "medium", "high", "critical"]
    file: str
    function: str | None = None
    line: int | None = None
    description: str
    expected_behavior: str
    evidence: list[ScoutEvidenceDraft]
    confidence: float = Field(ge=0.0, le=1.0)

    # Scout-only triage fields
    impact: Literal["none", "minor", "meaningful", "major", "critical"]
    impact_reason: str
    observable_behavior: str
    affected_user_or_system: str
    actionable: bool
    actionability_reason: str
    regression_likelihood: Literal[
        "unknown", "existing", "possibly_introduced", "introduced_by_push"
    ]
    expected_behavior_basis: Literal[
        "test",
        "documentation",
        "api_contract",
        "language_semantics",
        "repository_invariant",
        "other",
    ]
    expected_behavior_evidence: str

    # Scope & absence claims
    claim_scope: Literal["local", "cross_file", "repository_wide"] = "local"
    depends_on_absence: bool = False


class ScoutResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    findings: list[ScoutFindingDraft] = Field(default_factory=list)
