import pytest
from pydantic import ValidationError
from app.scout.schemas import ScoutEvidenceDraft, ScoutFindingDraft, ScoutResponse


def make_valid_draft_dict():
    return {
        "category": "behavioral_bug",
        "title": "Unhandled null pointer on empty input",
        "severity": "high",
        "file": "app/auth.py",
        "function": "parse_token",
        "line": 42,
        "description": "When header is missing, token parser throws null reference error.",
        "expected_behavior": "Should return None or raise InvalidHeaderError.",
        "evidence": [
            {"file": "app/auth.py", "line": 42, "snippet": "token = header.split(' ')[1]"}
        ],
        "confidence": 0.95,
        "impact": "major",
        "impact_reason": "Causes 500 error for unauthenticated users",
        "observable_behavior": "Server crashes with unhandled exception",
        "affected_user_or_system": "All HTTP clients requesting protected routes",
        "actionable": True,
        "actionability_reason": "Clear null check needed",
        "regression_likelihood": "introduced_by_push",
        "expected_behavior_basis": "api_contract",
        "expected_behavior_evidence": "API docs require 401 on missing auth",
        "claim_scope": "local",
        "depends_on_absence": False,
    }


def test_valid_scout_draft():
    draft = ScoutFindingDraft(**make_valid_draft_dict())
    assert draft.title == "Unhandled null pointer on empty input"
    assert draft.confidence == 0.95


def test_blank_snippet_rejected():
    data = make_valid_draft_dict()
    data["evidence"] = [{"file": "app/auth.py", "line": 42, "snippet": "   "}]
    with pytest.raises(ValidationError, match="cannot be empty or whitespace"):
        ScoutFindingDraft(**data)


def test_extra_fields_forbidden():
    data = make_valid_draft_dict()
    data["finding_id"] = "hacked-id"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ScoutFindingDraft(**data)


def test_confidence_bounds():
    data = make_valid_draft_dict()
    data["confidence"] = 1.5
    with pytest.raises(ValidationError):
        ScoutFindingDraft(**data)


def test_zero_findings_response():
    resp = ScoutResponse(findings=[])
    assert len(resp.findings) == 0


def test_scout_response_max_drafts():
    # Valid with 8 drafts
    drafts = [ScoutFindingDraft(**make_valid_draft_dict()) for _ in range(8)]
    resp = ScoutResponse(findings=drafts)
    assert len(resp.findings) == 8
