import pytest
from app.scout.models import ScoutContext, ScoutFile, ContextCompleteness, SuppressionReason
from app.scout.schemas import ScoutFindingDraft
from app.scout.worthiness import should_escalate_to_verifier, rank_and_filter_drafts
from tests.scout.test_schemas import make_valid_draft_dict


def make_context_with_auth_file(completeness=ContextCompleteness.COMPLETE):
    return ScoutContext(
        installation_id=1,
        repository_id=2,
        owner="owner",
        name="repo",
        before_sha="a" * 40,
        commit_sha="b" * 40,
        context_completeness=completeness,
        files=[
            ScoutFile(
                path="app/auth.py",
                content="\n".join([f"line {i}" for i in range(1, 100)]),
                changed=True,
                size_bytes=500,
            )
        ],
    )


def test_unconditional_category_suppression():
    ctx = make_context_with_auth_file()
    for cat in ["style", "refactor", "feature_request", "test_gap"]:
        data = make_valid_draft_dict()
        data["category"] = cat
        data["confidence"] = 1.0
        data["impact"] = "critical"
        draft = ScoutFindingDraft(**data)
        escalate, reason = should_escalate_to_verifier(draft, ctx)
        assert escalate is False
        assert reason is not None


def test_low_impact_suppression():
    ctx = make_context_with_auth_file()
    data = make_valid_draft_dict()
    data["impact"] = "minor"
    draft = ScoutFindingDraft(**data)
    escalate, reason = should_escalate_to_verifier(draft, ctx)
    assert escalate is False
    assert reason == SuppressionReason.LOW_IMPACT


def test_low_confidence_suppression():
    ctx = make_context_with_auth_file()
    data = make_valid_draft_dict()
    data["confidence"] = 0.50
    draft = ScoutFindingDraft(**data)
    escalate, reason = should_escalate_to_verifier(draft, ctx, threshold=0.70)
    assert escalate is False
    assert reason == SuppressionReason.LOW_CONFIDENCE


def test_partial_context_global_absence_suppression():
    ctx = make_context_with_auth_file(completeness=ContextCompleteness.PARTIAL)
    data = make_valid_draft_dict()
    data["claim_scope"] = "repository_wide"
    data["depends_on_absence"] = True
    draft = ScoutFindingDraft(**data)
    escalate, reason = should_escalate_to_verifier(draft, ctx)
    assert escalate is False
    assert reason == SuppressionReason.PARTIAL_CONTEXT_GLOBAL_ABSENCE


def test_real_behavioral_defect_escalates():
    ctx = make_context_with_auth_file()
    draft = ScoutFindingDraft(**make_valid_draft_dict())
    escalate, reason = should_escalate_to_verifier(draft, ctx)
    assert escalate is True
    assert reason is None


def test_ranking_and_caps():
    ctx = make_context_with_auth_file()
    # 2 drafts on app/auth.py (only 1 should survive)
    d1 = ScoutFindingDraft(**make_valid_draft_dict())
    d2 = ScoutFindingDraft(**make_valid_draft_dict())
    escalated, logs = rank_and_filter_drafts([d1, d2], ctx)
    assert len(escalated) == 1
    assert any(log["reason"] == SuppressionReason.DUPLICATE_FILE_FINDING.value for log in logs)
