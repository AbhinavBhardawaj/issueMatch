import uuid
from typing import List, Tuple
from app.domain.models import Finding, EvidenceItem
from app.domain.states import FindingStatus
from app.github.events import GitHubPushEvent
from app.scout.models import ScoutContext, ContextCompleteness, SuppressionReason
from app.scout.schemas import ScoutFindingDraft

ALWAYS_SUPPRESSED_CATEGORIES = {
    "style",
    "refactor",
    "feature_request",
    "test_gap",
}

MAX_ESCALATED_FINDINGS_PER_PUSH = 3
MAX_FINDINGS_PER_FILE = 1


def should_escalate_to_verifier(
    draft: ScoutFindingDraft,
    context: ScoutContext,
    threshold: float = 0.70,
) -> Tuple[bool, SuppressionReason | None]:
    """
    Pure deterministic pre-verifier policy.
    Decides: 'Is this finding worth spending verifier resources on?'
    """
    # 1. Unconditional category suppression
    if draft.category in ALWAYS_SUPPRESSED_CATEGORIES:
        category_map = {
            "style": SuppressionReason.STYLE_OR_CODE_SMELL,
            "refactor": SuppressionReason.REFACTOR,
            "feature_request": SuppressionReason.FEATURE_REQUEST,
            "test_gap": SuppressionReason.TEST_GAP,
        }
        return False, category_map.get(draft.category, SuppressionReason.STYLE_OR_CODE_SMELL)

    # 2. Actionability
    if not draft.actionable:
        return False, SuppressionReason.NOT_ACTIONABLE

    # 3. Meaningful impact
    if draft.impact not in ("meaningful", "major", "critical"):
        return False, SuppressionReason.LOW_IMPACT

    # 4. Confidence threshold
    if draft.confidence < threshold:
        return False, SuppressionReason.LOW_CONFIDENCE

    # 5. Non-empty required fields
    if not draft.evidence:
        return False, SuppressionReason.INSUFFICIENT_EVIDENCE
    if not draft.observable_behavior or not draft.observable_behavior.strip():
        return False, SuppressionReason.NOT_ACTIONABLE
    if not draft.expected_behavior or not draft.expected_behavior.strip():
        return False, SuppressionReason.NO_EXPECTED_BEHAVIOR_BASIS
    if not draft.expected_behavior_evidence or not draft.expected_behavior_evidence.strip():
        return False, SuppressionReason.NO_EXPECTED_BEHAVIOR_BASIS
    if not draft.affected_user_or_system or not draft.affected_user_or_system.strip():
        return False, SuppressionReason.NOT_ACTIONABLE

    # 6. Context completeness & global absence rule
    # When context is PARTIAL, repository-wide claims or claims depending on absence are suppressed
    if context.context_completeness == ContextCompleteness.PARTIAL:
        if draft.claim_scope == "repository_wide" or draft.depends_on_absence:
            return False, SuppressionReason.PARTIAL_CONTEXT_GLOBAL_ABSENCE

    # 7. Cheap Grounding Check against collected ScoutContext
    all_known_files = {f.path: f for f in context.files}
    all_known_files.update({f.path: f for f in context.test_files})

    if draft.file not in all_known_files:
        return False, SuppressionReason.INVALID_FILE

    for ev in draft.evidence:
        if ev.file not in all_known_files:
            return False, SuppressionReason.INVALID_FILE
        target_file = all_known_files[ev.file]
        lines = target_file.content.split("\n")
        if ev.line < 1 or ev.line > len(lines):
            return False, SuppressionReason.INVALID_LINE
        if not ev.snippet or not ev.snippet.strip():
            return False, SuppressionReason.EMPTY_SNIPPET

    return True, None


def rank_and_filter_drafts(
    drafts: List[ScoutFindingDraft],
    context: ScoutContext,
    threshold: float = 0.70,
) -> Tuple[List[ScoutFindingDraft], List[dict]]:
    """
    Filter drafts and rank survivors by:
    1. impact (critical > major > meaningful)
    2. grounding completeness (number of valid evidence items)
    3. confidence
    Applies MAX_FINDINGS_PER_FILE = 1 and MAX_ESCALATED_FINDINGS_PER_PUSH = 3.
    """
    survivors: List[ScoutFindingDraft] = []
    suppression_logs: List[dict] = []

    for draft in drafts:
        escalate, reason = should_escalate_to_verifier(draft, context, threshold)
        if escalate:
            survivors.append(draft)
        else:
            suppression_logs.append({
                "title": draft.title,
                "file": draft.file,
                "reason": reason.value if reason else "UNKNOWN",
            })

    impact_order = {"critical": 3, "major": 2, "meaningful": 1, "minor": 0, "none": -1}

    def sort_key(d: ScoutFindingDraft):
        return (
            impact_order.get(d.impact, 0),
            len(d.evidence),
            d.confidence,
        )

    survivors.sort(key=sort_key, reverse=True)

    escalated: List[ScoutFindingDraft] = []
    seen_files = set()

    for draft in survivors:
        if draft.file in seen_files:
            suppression_logs.append({
                "title": draft.title,
                "file": draft.file,
                "reason": SuppressionReason.DUPLICATE_FILE_FINDING.value,
            })
            continue

        if len(escalated) >= MAX_ESCALATED_FINDINGS_PER_PUSH:
            suppression_logs.append({
                "title": draft.title,
                "file": draft.file,
                "reason": SuppressionReason.CAP_EXCEEDED.value,
            })
            continue

        escalated.append(draft)
        seen_files.add(draft.file)

    return escalated, suppression_logs


def materialize_finding(draft: ScoutFindingDraft, event: GitHubPushEvent) -> Finding:
    """
    Authoritative backend materialization.
    AI never controls finding_id, installation_id, repository_id, commit_sha, or status.
    """
    evidence_items = [
        EvidenceItem(
            file=ev.file,
            line=ev.line,
            snippet=ev.snippet.strip(),
        )
        for ev in draft.evidence
    ]

    return Finding(
        finding_id=f"F-{uuid.uuid4().hex[:12]}",
        installation_id=str(event.installation_id),
        repository_id=str(event.repository_id),
        commit_sha=event.after_sha,
        title=draft.title,
        severity=draft.severity,
        category=draft.category,
        file=draft.file,
        function=draft.function,
        line=draft.line,
        description=draft.description,
        expected_behavior=draft.expected_behavior,
        evidence=evidence_items,
        confidence=draft.confidence,
        status=FindingStatus.DISCOVERED,
    )
