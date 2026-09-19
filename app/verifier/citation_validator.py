import logging
from pydantic import BaseModel, ConfigDict, Field
from app.domain.models import Verification, RepoContext, RepoContextFile, VerifierEvidence
from app.domain.states import VerificationStatus
from app.domain.normalization import normalize_code, normalize_path

logger = logging.getLogger(__name__)


class VerifierCitationValidationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    valid: bool
    reason: str
    contradicted_citations: list[dict] = Field(default_factory=list)


def build_repo_context_file_pool(repo_context: RepoContext) -> tuple[dict[str, RepoContextFile] | None, str | None]:
    """
    Build a unified pool of all addressable context files.
    Preserves case sensitivity while normalizing slashes.
    Detects conflicting copies of the same path with different content and fails closed.
    """
    pool: dict[str, RepoContextFile] = {}

    all_files = list(repo_context.files) + list(repo_context.caller_files) + list(repo_context.test_files)

    if repo_context.readme:
        readme_file = RepoContextFile(
            path="README.md",
            content=repo_context.readme,
            size_bytes=len(repo_context.readme.encode("utf-8")),
        )
        all_files.append(readme_file)

    for f in all_files:
        norm_p = normalize_path(f.path)
        if norm_p in pool:
            if pool[norm_p].content != f.content:
                return None, f"CONFLICTING_CONTENT_FOR_PATH: {norm_p}"
            continue
        pool[norm_p] = f

    return pool, None


def _validate_single_citation(
    evidence: VerifierEvidence,
    pool: dict[str, RepoContextFile],
) -> tuple[bool, str]:
    norm_p = normalize_path(evidence.file)
    if norm_p not in pool:
        return False, f"FILE_NOT_IN_CONTEXT: {evidence.file}"

    target_file = pool[norm_p]
    lines = target_file.content.split("\n")

    if evidence.line < 1 or evidence.line > len(lines):
        return False, f"LINE_OUT_OF_BOUNDS: {evidence.file}:{evidence.line} (total lines: {len(lines)})"

    line_idx = evidence.line - 1
    start = max(0, line_idx - 5)
    end = min(len(lines), line_idx + 6)
    window = "\n".join(lines[start:end])

    if normalize_code(evidence.snippet) not in normalize_code(window):
        return False, f"SNIPPET_NOT_FOUND_IN_WINDOW: {evidence.file}:{evidence.line}"

    return True, "OK"


def validate_verifier_citations(
    verification: Verification,
    repo_context: RepoContext,
) -> VerifierCitationValidationResult:
    """
    Deterministically validate all citations in verifier supporting and counter evidence.
    For VERIFIED: requires at least one supporting citation and 100% grounded citations.
    For REJECTED / NEEDS_MORE_CONTEXT: checks citations for observability.
    """
    pool, conflict_err = build_repo_context_file_pool(repo_context)
    if pool is None:
        return VerifierCitationValidationResult(
            valid=False,
            reason=conflict_err or "CONTEXT_CONFLICT",
            contradicted_citations=[],
        )

    contradicted = []

    # Check for empty supporting evidence when claiming VERIFIED
    if verification.status == VerificationStatus.VERIFIED and len(verification.supporting_evidence) == 0:
        return VerifierCitationValidationResult(
            valid=False,
            reason="NO_SUPPORTING_EVIDENCE_FOR_VERIFIED",
            contradicted_citations=[],
        )

    for item in verification.supporting_evidence:
        ok, detail = _validate_single_citation(item, pool)
        if not ok:
            contradicted.append({"type": "supporting", "citation": item.model_dump(), "detail": detail})

    for item in verification.counter_evidence:
        ok, detail = _validate_single_citation(item, pool)
        if not ok:
            contradicted.append({"type": "counter", "citation": item.model_dump(), "detail": detail})

    if contradicted:
        reason = "VERIFIER_CITATION_CONTRADICTED"
        if verification.status != VerificationStatus.VERIFIED:
            reason = f"{verification.status.value}_CITATION_CONTRADICTED"
        return VerifierCitationValidationResult(
            valid=False,
            reason=reason,
            contradicted_citations=contradicted,
        )

    return VerifierCitationValidationResult(
        valid=True,
        reason="ALL_CITATIONS_GROUNDED",
        contradicted_citations=[],
    )
