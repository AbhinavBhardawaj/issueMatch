"""Deterministic first-pass contributor-claim extraction."""
import re
from pydantic import BaseModel, ConfigDict


class CandidateExtraction(BaseModel):
    model_config = ConfigDict(frozen=True)
    is_candidate: bool
    approach_text: str = ""
    reason: str


_CLAIM_PATTERNS = (
    r"\bassign\s+(?:this\s+)?issue\s+to\s+me\b", r"\bcan\s+i\s+work\s+on\s+(?:this\s+)?(?:issue|it)\b",
    r"\bi(?:'d|\s+would)\s+like\s+to\s+(?:work\s+on|take)\s+(?:(?:this\s+)?(?:issue|it)|this)\b",
    r"\bi\s+(?:want|would\s+like)\s+to\s+take\s+(?:this\s+)?(?:issue|it|this\s+up)\b",
    r"\bi\s+can\s+fix\s+(?:this\s+)?(?:issue|it)\b", r"\bi\s+want\s+to\s+work\s+on\s+(?:this\s+)?(?:issue|it)\b",
)


def extract_candidate(comment_body: str | None) -> CandidateExtraction:
    """Identify explicit issue claims without treating ordinary discussion as a claim."""
    normalized = " ".join((comment_body or "").strip().split())
    if not normalized:
        return CandidateExtraction(is_candidate=False, reason="empty comment")
    comparable = normalized.lower().replace("’", "'")
    if not any(re.search(pattern, comparable) for pattern in _CLAIM_PATTERNS):
        return CandidateExtraction(is_candidate=False, reason="no explicit contributor claim")
    # Keep the source wording: it is useful evidence for a later analyzer.
    approach_markers = re.search(r"\b(i(?:'ll|\s+will)|plan\s+to|by\s+(?:modifying|updating|adding|changing))\b", normalized, re.I)
    approach = normalized[approach_markers.start():] if approach_markers else normalized
    return CandidateExtraction(is_candidate=True, approach_text=approach, reason="explicit contributor claim")
