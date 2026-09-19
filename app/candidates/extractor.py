"""Deterministic first-pass contributor-claim extraction."""
import re
from enum import Enum
from pydantic import BaseModel, ConfigDict


class CandidateKind(str, Enum):
    NOT_CANDIDATE = "NOT_CANDIDATE"
    CLAIM_ONLY = "CLAIM_ONLY"
    APPROACH_SUBMITTED = "APPROACH_SUBMITTED"


class CandidateExtraction(BaseModel):
    model_config = ConfigDict(frozen=True)
    is_candidate: bool
    kind: CandidateKind = CandidateKind.NOT_CANDIDATE
    approach_text: str = ""
    reason: str


_CLAIM_PATTERNS = (
    r"\bassign\s+(?:this\s+)?issue\s+to\s+me\b", r"\bplease\s+assign\s+(?:(?:this\s+)?(?:issue|it)|this)\s+to\s+me\b",
    r"\bcan\s+i\s+work\s+on\s+(?:(?:this\s+)?(?:issue|it)|this)\b",
    r"\bi(?:'d|\s+would)\s+like\s+to\s+(?:work\s+on|take)\s+(?:(?:this\s+)?(?:issue|it)|this)\b",
    r"\bi\s+(?:want|would\s+like)\s+to\s+take\s+(?:this\s+)?(?:issue|it|this\s+up)\b",
    r"\bi\s+can\s+(?:fix|take)\s+(?:this\s+)?(?:issue|it)\b",
    r"\bi\s+want\s+to\s+work\s+on\s+(?:(?:this\s+)?(?:issue|it)|this)\b",
    r"\bi\s+will\s+work\s+on\s+(?:this\s+)?(?:issue|it)\b",
    r"\bi\s+want\s+(?:this\s+)?issue\b",
)

_APPROACH_PATTERNS = (
    r"\b(?:i(?:'ll|\s+will)|plan\s+to|intend\s+to)\s+(?:modify|update|change|add|remove|replace|refactor|implement|introduce|adjust|refine|check|validate|calculate|compare|query|parse|call|move|split)\b",
    r"\b(?:first|then|next)\s+(?:check|validate|modify|update|change|add|remove|replace|refactor|implement|return|calculate|compare|query|parse|call|move|split)\b",
    r"\b(?:modify|update|change|add|remove|replace|refactor|implement|introduce|adjust|refine|validate|calculate|compare|query|parse|call|move|split)\b",
    r"\b(?:check|checking|validate|validating|return|returning|handle|handling|calculate|calculating|compare|query|parse|call|calling|move|split)\b",
    r"\b(?:function|method|class|module|algorithm|condition|logic|flow|query|token|test|regression|validation|expiry|expired|timeout|session|behavior)\b",
    r"(?:^|\s)(?:if|else|elif|for|while)\s*[^\n:]+:",
    r"(?:^|\s)return\s+[^\n]+",
    r"\b[\w.-]+/[\w./-]+\.[A-Za-z0-9]+\b",
    r"\b[A-Z][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b",
)


def extract_candidate(comment_body: str | None) -> CandidateExtraction:
    """Classify a comment without invoking the LLM for claim-only messages.

    A claim is eligible only when it also contains technical substance.  The
    substance detector intentionally accepts natural language, pseudocode,
    function/class references, and file paths; file paths are not required.
    """
    normalized = " ".join((comment_body or "").strip().split())
    if not normalized:
        return CandidateExtraction(is_candidate=False, kind=CandidateKind.NOT_CANDIDATE, reason="empty comment")
    comparable = normalized.lower().replace("’", "'")
    has_claim = any(re.search(pattern, comparable) for pattern in _CLAIM_PATTERNS)
    has_approach = any(re.search(pattern, normalized, re.I) for pattern in _APPROACH_PATTERNS)
    if has_claim and not has_approach:
        return CandidateExtraction(is_candidate=False, kind=CandidateKind.CLAIM_ONLY, reason="claim or work intent without an implementation approach")
    # Explicit implementation language is sufficient even if the contributor
    # omitted a standard "assign me" phrase (for example, a pseudocode plan).
    if has_approach:
        return CandidateExtraction(is_candidate=True, kind=CandidateKind.APPROACH_SUBMITTED, approach_text=normalized, reason="technical implementation approach detected")
    return CandidateExtraction(is_candidate=False, kind=CandidateKind.NOT_CANDIDATE, reason="no contributor claim or technical approach")
