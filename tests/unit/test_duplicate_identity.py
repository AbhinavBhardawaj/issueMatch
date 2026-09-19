import pytest
from app.domain.models import Finding, EvidenceItem
from app.domain.states import FindingStatus
from app.services.issue_gate import compute_defect_signature, compute_finding_signature


def test_wording_independent_defect_signature():
    """
    Two findings describing the identical defect with completely different
    titles, descriptions, and expected-behavior wording MUST produce the exact same signature.
    """
    f1 = Finding(
        finding_id="F-1",
        installation_id="100",
        repository_id="200",
        commit_sha="a" * 40,
        title="ZeroDivisionError in calculate_tax",
        severity="high",
        category="behavioral_bug",
        file="app/tax.py",
        function="calculate_tax",
        line=15,
        description="The denominator rate_count can be zero causing a ZeroDivisionError.",
        expected_behavior="Should return 0.0 or check rate_count != 0 before division.",
        evidence=[
            EvidenceItem(
                file="app/tax.py",
                line=15,
                snippet="return total / rate_count",
            )
        ],
        confidence=0.95,
        status=FindingStatus.DISCOVERED,
    )

    f2 = Finding(
        finding_id="F-2",
        installation_id="100",
        repository_id="200",
        commit_sha="b" * 40,
        title="Crash when tax rate count is empty",
        severity="medium",
        category="behavioral_bug",
        file="app/tax.py",
        function="calculate_tax",
        line=15,
        description="divide-by-zero exception triggered during invoice calculation batch.",
        expected_behavior="Guard against empty list when performing tax calculation.",
        evidence=[
            EvidenceItem(
                file="app/tax.py",
                line=15,
                snippet="return total / rate_count",
            )
        ],
        confidence=0.88,
        status=FindingStatus.DISCOVERED,
    )

    sig1 = compute_finding_signature(f1)
    sig2 = compute_finding_signature(f2)

    assert sig1 == sig2, "Different LLM prose must NOT change deterministic defect signature"


def test_signature_differentiates_different_code_locations():
    """Findings in different files or different functions must have different signatures."""
    f1 = Finding(
        finding_id="F-1",
        installation_id="100",
        repository_id="200",
        commit_sha="a" * 40,
        title="Bug A",
        severity="high",
        category="behavioral_bug",
        file="app/tax.py",
        function="fn1",
        line=10,
        description="desc",
        expected_behavior="exp",
        evidence=[EvidenceItem(file="app/tax.py", line=10, snippet="x = 1 / y")],
        confidence=0.9,
    )
    f2 = Finding(
        finding_id="F-2",
        installation_id="100",
        repository_id="200",
        commit_sha="a" * 40,
        title="Bug B",
        severity="high",
        category="behavioral_bug",
        file="app/tax.py",
        function="fn2",
        line=20,
        description="desc",
        expected_behavior="exp",
        evidence=[EvidenceItem(file="app/tax.py", line=20, snippet="x = 1 / y")],
        confidence=0.9,
    )

    assert compute_finding_signature(f1) != compute_finding_signature(f2)
