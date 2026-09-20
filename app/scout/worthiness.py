import uuid
from typing import List, Tuple
from app.domain.models import Finding, EvidenceItem
from app.domain.states import FindingStatus
from app.github.events import GitHubPushEvent
from app.scout.models import ScoutContext, ContextCompleteness, SuppressionReason, ScoutFile
from app.scout.schemas import ScoutFindingDraft
from app.domain.normalization import normalize_code, normalize_path

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

    # 7. Grounding Check against collected ScoutContext
    all_known_files: dict[str, ScoutFile] = {}
    for f in list(context.files) + list(context.test_files):
        norm_p = normalize_path(f.path)
        if norm_p in all_known_files and all_known_files[norm_p].content != f.content:
            return False, SuppressionReason.INVALID_FILE
        all_known_files[norm_p] = f

    if context.readme and "README.md" not in all_known_files:
        all_known_files["README.md"] = ScoutFile(
            path="README.md",
            content=context.readme,
            size_bytes=len(context.readme.encode()),
        )

    draft_file_norm = normalize_path(draft.file)
    if draft_file_norm not in all_known_files:
        return False, SuppressionReason.INVALID_FILE

    if draft.line is not None:
        target_draft_file = all_known_files[draft_file_norm]
        draft_file_lines = target_draft_file.content.split("\n")
        if draft.line < 1 or draft.line > len(draft_file_lines):
            return False, SuppressionReason.INVALID_LINE

    for ev in draft.evidence:
        ev_file_norm = normalize_path(ev.file)
        if ev_file_norm not in all_known_files:
            return False, SuppressionReason.INVALID_FILE
        target_file = all_known_files[ev_file_norm]
        lines = target_file.content.split("\n")
        if ev.line < 1 or ev.line > len(lines):
            return False, SuppressionReason.INVALID_LINE
        if not ev.snippet or not ev.snippet.strip():
            return False, SuppressionReason.EMPTY_SNIPPET

        line_idx = ev.line - 1
        start = max(0, line_idx - 5)
        end = min(len(lines), line_idx + 6)
        window = "\n".join(lines[start:end])
        if normalize_code(ev.snippet) not in normalize_code(window):
            return False, SuppressionReason.INVALID_SNIPPET

    return True, None


def validate_and_filter_drafts_for_context(
    drafts: List[ScoutFindingDraft],
    context: ScoutContext,
    threshold: float = 0.70,
) -> Tuple[List[ScoutFindingDraft], List[dict]]:
    """
    Validates drafts strictly against the visible files in the given ScoutContext.
    Checks category, actionability, impact, confidence, required fields, and evidence grounding.
    Does NOT apply push-wide caps so multiple batches can be merged before final ranking.
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

    return survivors, suppression_logs


def rank_and_cap_push_findings(
    survivors: List[ScoutFindingDraft],
    max_escalated: int = MAX_ESCALATED_FINDINGS_PER_PUSH,
    max_per_file: int = MAX_FINDINGS_PER_FILE,
) -> Tuple[List[ScoutFindingDraft], List[dict]]:
    """
    Global push-level ranking and capping across all batches.
    Sorts survivors by:
    1. impact (critical > major > meaningful)
    2. grounding completeness (number of valid evidence items)
    3. confidence
    Applies GLOBAL MAX_FINDINGS_PER_FILE = 1 and MAX_ESCALATED_FINDINGS_PER_PUSH = 3.
    """
    suppression_logs: List[dict] = []
    impact_order = {"critical": 3, "major": 2, "meaningful": 1, "minor": 0, "none": -1}

    def sort_key(d: ScoutFindingDraft):
        return (
            impact_order.get(d.impact, 0),
            len(d.evidence),
            d.confidence,
        )

    sorted_survivors = sorted(survivors, key=sort_key, reverse=True)

    escalated: List[ScoutFindingDraft] = []
    seen_files = set()

    for draft in sorted_survivors:
        if draft.file in seen_files:
            suppression_logs.append({
                "title": draft.title,
                "file": draft.file,
                "reason": SuppressionReason.DUPLICATE_FILE_FINDING.value,
            })
            continue

        if len(escalated) >= max_escalated:
            suppression_logs.append({
                "title": draft.title,
                "file": draft.file,
                "reason": SuppressionReason.CAP_EXCEEDED.value,
            })
            continue

        escalated.append(draft)
        seen_files.add(draft.file)

    return escalated, suppression_logs


def rank_and_filter_drafts(
    drafts: List[ScoutFindingDraft],
    context: ScoutContext,
    threshold: float = 0.70,
) -> Tuple[List[ScoutFindingDraft], List[dict]]:
    """
    Backward-compatible convenience wrapper performing per-context validation
    followed by push-wide ranking and capping.
    """
    survivors, sup_v = validate_and_filter_drafts_for_context(drafts, context, threshold)
    escalated, sup_c = rank_and_cap_push_findings(survivors)
    return escalated, sup_v + sup_c


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
        claim_scope=getattr(draft, "claim_scope", "local"),
        depends_on_absence=getattr(draft, "depends_on_absence", False),
        status=FindingStatus.DISCOVERED,
    )
