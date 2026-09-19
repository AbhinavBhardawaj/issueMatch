import pytest
from app.scout.models import ScoutContext, ScoutFile, ContextCompleteness, SuppressionReason
from app.scout.schemas import ScoutFindingDraft, ScoutEvidenceDraft
from app.scout.worthiness import should_escalate_to_verifier
from app.domain.models import Finding, Verification, EvidenceItem, RepoContext, RepoContextFile, VerifierEvidence
from app.domain.states import FindingStatus, VerificationStatus, EvidenceStatus
from app.verifier.citation_validator import validate_verifier_citations
from app.verifier.deterministic import post_verifier_recheck, RecheckStatus
from app.verifier.evidence import EvidenceValidationResult, EvidenceItemResult
from app.services.issue_gate import evaluate_issue_authorization, GateDecision, DedupResult, compute_finding_signature


def _make_scout_context():
    content = "\n".join([
        "def authenticate_user(username, password):",  # line 1
        "    if not username or not password:",          # line 2
        "        return None",                           # line 3
        "    user = db.get_user(username)",              # line 4
        "    if not user.verify_password(password):",     # line 5
        "        return None",                           # line 6
        "    return user",                               # line 7
    ])
    return ScoutContext(
        installation_id=1,
        repository_id=2,
        owner="org",
        name="repo",
        before_sha="a" * 40,
        commit_sha="b" * 40,
        context_completeness=ContextCompleteness.COMPLETE,
        files=[ScoutFile(path="app/auth.py", content=content, size_bytes=len(content))],
        readme="# Auth Service\n\nMust require cryptographic validation.\n",
    )


def test_scout_grounding_fabricated_file_suppressed():
    ctx = _make_scout_context()
    draft = ScoutFindingDraft(
        category="behavioral_bug",
        title="Bug in phantom file",
        severity="high",
        file="app/phantom.py",
        line=1,
        description="Phantom defect",
        expected_behavior="Normal",
        evidence=[ScoutEvidenceDraft(file="app/phantom.py", line=1, snippet="x = 1")],
        confidence=0.9,
        impact="major",
        impact_reason="Crash",
        observable_behavior="Error",
        affected_user_or_system="Users",
        actionable=True,
        actionability_reason="Fix",
        regression_likelihood="introduced_by_push",
        expected_behavior_basis="language_semantics",
        expected_behavior_evidence="Rules",
        claim_scope="local",
        depends_on_absence=False,
    )
    escalate, reason = should_escalate_to_verifier(draft, ctx)
    assert escalate is False
    assert reason == SuppressionReason.INVALID_FILE


def test_scout_grounding_fabricated_line_suppressed():
    ctx = _make_scout_context()
    draft = ScoutFindingDraft(
        category="behavioral_bug",
        title="Bug at line 9999",
        severity="high",
        file="app/auth.py",
        line=9999,
        description="Out of bounds defect",
        expected_behavior="Normal",
        evidence=[ScoutEvidenceDraft(file="app/auth.py", line=9999, snippet="def authenticate_user")],
        confidence=0.9,
        impact="major",
        impact_reason="Crash",
        observable_behavior="Error",
        affected_user_or_system="Users",
        actionable=True,
        actionability_reason="Fix",
        regression_likelihood="introduced_by_push",
        expected_behavior_basis="language_semantics",
        expected_behavior_evidence="Rules",
        claim_scope="local",
        depends_on_absence=False,
    )
    escalate, reason = should_escalate_to_verifier(draft, ctx)
    assert escalate is False
    assert reason == SuppressionReason.INVALID_LINE


def test_scout_grounding_fabricated_snippet_suppressed():
    ctx = _make_scout_context()
    draft = ScoutFindingDraft(
        category="behavioral_bug",
        title="Hallucinated snippet",
        severity="high",
        file="app/auth.py",
        line=4,
        description="Fabricated snippet at line 4",
        expected_behavior="Normal",
        evidence=[ScoutEvidenceDraft(file="app/auth.py", line=4, snippet="totally_fabricated_code_does_not_exist()")],
        confidence=0.9,
        impact="major",
        impact_reason="Crash",
        observable_behavior="Error",
        affected_user_or_system="Users",
        actionable=True,
        actionability_reason="Fix",
        regression_likelihood="introduced_by_push",
        expected_behavior_basis="language_semantics",
        expected_behavior_evidence="Rules",
        claim_scope="local",
        depends_on_absence=False,
    )
    escalate, reason = should_escalate_to_verifier(draft, ctx)
    assert escalate is False
    assert reason == SuppressionReason.INVALID_SNIPPET


def test_scout_grounding_correct_nearby_snippet_allowed():
    ctx = _make_scout_context()
    draft = ScoutFindingDraft(
        category="behavioral_bug",
        title="Grounded defect",
        severity="high",
        file="app/auth.py",
        line=4,
        description="Legitimate snippet nearby",
        expected_behavior="Normal",
        evidence=[ScoutEvidenceDraft(file="app/auth.py", line=4, snippet="user = db.get_user(username)")],
        confidence=0.9,
        impact="major",
        impact_reason="Crash",
        observable_behavior="Error",
        affected_user_or_system="Users",
        actionable=True,
        actionability_reason="Fix",
        regression_likelihood="introduced_by_push",
        expected_behavior_basis="language_semantics",
        expected_behavior_evidence="Rules",
        claim_scope="local",
        depends_on_absence=False,
    )
    escalate, reason = should_escalate_to_verifier(draft, ctx)
    assert escalate is True
    assert reason is None


# ===========================================================================
# VERIFIER CITATION GROUNDING RED-TEAM
# ===========================================================================

def _make_verifier_setup():
    file_content = (
        "def process(x):\n"
        "    y = x + 1\n"
        "    return y\n"
    )
    rc = RepoContext(
        repository_id="200",
        installation_id="100",
        owner="org",
        name="repo",
        commit_sha="b" * 40,
        files=[RepoContextFile(path="src/calc.py", content=file_content, size_bytes=len(file_content))],
        readme="Specification: x must be an integer\n",
    )
    f = Finding(
        finding_id="F-1",
        installation_id="100",
        repository_id="200",
        commit_sha="b" * 40,
        title="Bug",
        severity="high",
        category="behavioral_bug",
        file="src/calc.py",
        line=2,
        description="Desc",
        expected_behavior="Expected",
        evidence=[EvidenceItem(file="src/calc.py", line=2, snippet="y = x + 1")],
        confidence=0.9,
    )
    er = EvidenceValidationResult(
        finding_id="F-1",
        commit_sha="b" * 40,
        results=[
            EvidenceItemResult(
                file="src/calc.py",
                line=2,
                snippet="y = x + 1",
                file_exists=True,
                line_exists=True,
                snippet_found=True,
                function_exists=True,
                status=EvidenceStatus.SUPPORTED,
            )
        ],
        overall=EvidenceStatus.SUPPORTED,
    )
    dr = DedupResult(
        signature=compute_finding_signature(f),
        finding_id="F-1",
        is_duplicate=False,
        reason="new",
        reservation_token="tok1",
    )
    return f, rc, er, dr


def test_verifier_fabricated_file_citation_fails_closed():
    f, rc, er, dr = _make_verifier_setup()
    v = Verification(
        verification_id="v1",
        finding_id=f.finding_id,
        installation_id=f.installation_id,
        repository_id=f.repository_id,
        commit_sha=f.commit_sha,
        status=VerificationStatus.VERIFIED,
        reason="Fabricated file citation",
        confidence=0.9,
        supporting_evidence=[
            VerifierEvidence(file="nonexistent.py", line=1, snippet="foo()")
        ],
    )
    res = post_verifier_recheck(f, v, er, rc)
    assert res.status == RecheckStatus.FAIL

    gate_res = evaluate_issue_authorization(f, v, er, rc, dr)
    assert gate_res.decision == GateDecision.DENY


def test_verifier_fabricated_line_citation_fails_closed():
    f, rc, er, dr = _make_verifier_setup()
    v = Verification(
        verification_id="v1",
        finding_id=f.finding_id,
        installation_id=f.installation_id,
        repository_id=f.repository_id,
        commit_sha=f.commit_sha,
        status=VerificationStatus.VERIFIED,
        reason="Fabricated line citation",
        confidence=0.9,
        supporting_evidence=[
            VerifierEvidence(file="src/calc.py", line=999, snippet="y = x + 1")
        ],
    )
    res = post_verifier_recheck(f, v, er, rc)
    assert res.status == RecheckStatus.FAIL


def test_verifier_fabricated_snippet_citation_fails_closed():
    f, rc, er, dr = _make_verifier_setup()
    v = Verification(
        verification_id="v1",
        finding_id=f.finding_id,
        installation_id=f.installation_id,
        repository_id=f.repository_id,
        commit_sha=f.commit_sha,
        status=VerificationStatus.VERIFIED,
        reason="Fabricated snippet",
        confidence=0.9,
        supporting_evidence=[
            VerifierEvidence(file="src/calc.py", line=2, snippet="nonexistent_code_snippet()")
        ],
    )
    res = post_verifier_recheck(f, v, er, rc)
    assert res.status == RecheckStatus.FAIL


def test_verifier_snippet_outside_window_fails_closed():
    f, rc, er, dr = _make_verifier_setup()
    # Snippet exists on line 1, but verifier cites line 10 (window ±5 cannot reach line 1)
    file_long = "\n".join([f"# line {i}" for i in range(1, 20)])
    file_long = file_long + "\nfound_snippet = 42\n"  # line 20
    rc_long = rc.model_copy(update={"files": [RepoContextFile(path="src/calc.py", content=file_long, size_bytes=len(file_long))]})

    v = Verification(
        verification_id="v1",
        finding_id=f.finding_id,
        installation_id=f.installation_id,
        repository_id=f.repository_id,
        commit_sha=f.commit_sha,
        status=VerificationStatus.VERIFIED,
        reason="Wrong line window",
        confidence=0.9,
        supporting_evidence=[
            VerifierEvidence(file="src/calc.py", line=5, snippet="found_snippet = 42")
        ],
    )
    res = post_verifier_recheck(f, v, er, rc_long)
    assert res.status == RecheckStatus.FAIL


def test_verifier_grounded_readme_citation_accepted():
    f, rc, er, dr = _make_verifier_setup()
    v = Verification(
        verification_id="v1",
        finding_id=f.finding_id,
        installation_id=f.installation_id,
        repository_id=f.repository_id,
        commit_sha=f.commit_sha,
        status=VerificationStatus.VERIFIED,
        reason="Contract confirmed in README",
        confidence=0.9,
        supporting_evidence=[
            VerifierEvidence(file="README.md", line=1, snippet="Specification: x must be an integer")
        ],
    )
    res = post_verifier_recheck(f, v, er, rc)
    assert res.status == RecheckStatus.PASS

    gate_res = evaluate_issue_authorization(f, v, er, rc, dr)
    assert gate_res.decision == GateDecision.ALLOW


def test_verifier_conflicting_context_paths_fails_closed():
    f, rc, er, dr = _make_verifier_setup()
    # Conflicting copies of src/calc.py with different content
    rc_conflict = rc.model_copy(
        update={
            "caller_files": [
                RepoContextFile(path="src/calc.py", content="DIFFERENT CONTENT", size_bytes=17)
            ]
        }
    )
    v = Verification(
        verification_id="v1",
        finding_id=f.finding_id,
        installation_id=f.installation_id,
        repository_id=f.repository_id,
        commit_sha=f.commit_sha,
        status=VerificationStatus.VERIFIED,
        reason="ok",
        confidence=0.9,
        supporting_evidence=[
            VerifierEvidence(file="src/calc.py", line=2, snippet="y = x + 1")
        ],
    )
    res = post_verifier_recheck(f, v, er, rc_conflict)
    assert res.status == RecheckStatus.FAIL


def test_counter_evidence_error_on_rejected_remains_safe_rejection():
    f, rc, er, dr = _make_verifier_setup()
    # Verifier rejected finding, but supplied an invalid counter-evidence citation
    v = Verification(
        verification_id="v1",
        finding_id=f.finding_id,
        installation_id=f.installation_id,
        repository_id=f.repository_id,
        commit_sha=f.commit_sha,
        status=VerificationStatus.REJECTED,
        reason="Caller handles error",
        confidence=0.9,
        supporting_evidence=[],
        counter_evidence=[
            VerifierEvidence(file="ghost_file.py", line=1, snippet="ghost()")
        ],
    )
    cit_res = validate_verifier_citations(v, rc)
    assert cit_res.valid is False

    # Gate still denies because status is REJECTED
    gate_res = evaluate_issue_authorization(f, v, er, rc, dr)
    assert gate_res.decision == GateDecision.DENY
    assert gate_res.reason == "VERIFIER_NOT_VERIFIED"
