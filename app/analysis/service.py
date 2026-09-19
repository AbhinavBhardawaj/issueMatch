import logging

from app.models.analysis import (
    AnalysisDecision,
    ApproachAnalysis,
)
from app.models.candidate import CandidateSubmission
from app.models.context import RepositoryAnalysisContext

from .criteria import check_acceptance_criteria
from .provider import AnalysisProvider, AnalysisProviderResult
from .verifier import derive_issue_grounded_evidence, verify_evidence

logger = logging.getLogger(__name__)


class RuleBasedAnalysisProvider:
    """
    Legacy deterministic example, not wired into the production analysis path.
    """

    async def analyze(self,candidate: CandidateSubmission,
        context: RepositoryAnalysisContext) -> dict:
        available_paths = {
            file.path
            for file in context.code_context.file_contents
        }

        available_paths.update(
            file.path
            for file in context.code_context.test_files
        )

        approach = candidate.approach

        referenced_paths = [
            path for path in available_paths if path in approach
        ]

        if not referenced_paths:
            return {
                "decision": AnalysisDecision.REVISION_REQUIRED.value,
                "confidence": 0.4,
                "issues": [
                    "The proposed approach does not reference any relevant repository files."
                ],
                "missing_requirements": [
                    "Identify the repository component that should be changed."
                ],
                "revision_feedback": (
                    "Please identify the relevant repository files or components "
                    "and explain how they will address the issue."
                ),
            }

        evidence = [
            f"{path} is referenced by the contributor and exists in the repository context."
            for path in referenced_paths
        ]

        return {
            "decision": AnalysisDecision.PASS.value,
            "confidence": 0.8,
            "strengths": [
                "The approach identifies repository components relevant to the implementation."
            ],
            "evidence": evidence,
            "recommendation": (
                "The approach is consistent with the available repository context. "
                "Maintainer may review and assign the issue."
            ),
        }


class DefaultAnalysisService:
    def __init__(self, provider: AnalysisProvider) -> None:
        self._provider = provider

    async def analyze(
        self,
        candidate: CandidateSubmission,
        context: RepositoryAnalysisContext,
    ) -> ApproachAnalysis:
        coverage = check_acceptance_criteria(context.issue_context.body, candidate.approach)
        if coverage and (coverage.missing or coverage.detail_gaps):
            gaps = [*coverage.missing, *coverage.detail_gaps]
            if coverage.covered:
                analysis = ApproachAnalysis(
                    decision=AnalysisDecision.REVISION_REQUIRED,
                    confidence=0.5,
                    missing_requirements=gaps,
                    revision_feedback=(
                    "Your plan addresses part of the issue, but please also cover: "
                    + "; ".join(gaps[:3])
                    ),
                )
            else:
                analysis = ApproachAnalysis(
                    decision=AnalysisDecision.REJECT,
                    confidence=0.5,
                    issues=["The proposed work does not address the issue's acceptance criteria."],
                    recommendation="This approach is not recommended for this issue.",
                )
            logger.info("Checklist cross-check: candidate=%s covered=%s total=%s decision=%s",
                        candidate.candidate_id, len(coverage.covered), len(coverage.criteria), analysis.decision)
            return analysis

        prompt = self._build_prompt(candidate, context)
        provider_result = AnalysisProviderResult.model_validate(await self._provider.generate(prompt))
        verified_evidence = verify_evidence(provider_result.evidence, context)
        supplied_paths = {file.path for file in (*context.code_context.file_contents,
                                                 *context.code_context.test_files)}
        absent_citations = [item.path for item in provider_result.evidence if item.path not in supplied_paths]
        result = provider_result.model_dump()

        repo_identifiers = {
            context.repository_context.name,
            f"{context.repository_context.owner}/{context.repository_context.name}",
        }
        invented_citations = [path for path in absent_citations if path not in repo_identifiers]

        # A tiny local model can contradict a complete, explicitly checked
        # proposal with zero confidence, a reasonless rejection, or a bogus
        # excerpt from a real file. In that narrow case independently ground
        # the checklist in real source locations. Never accept invented paths.
        invalid_rejection = (provider_result.decision is AnalysisDecision.REJECT
                             and not provider_result.issues and not provider_result.recommendation)
        inconclusive_zero = (provider_result.confidence == 0 and (
            provider_result.decision is AnalysisDecision.PASS or
            (provider_result.decision is AnalysisDecision.REVISION_REQUIRED
             and not provider_result.issues and not provider_result.missing_requirements)
        ))
        invalid_pass_citation = (provider_result.decision is AnalysisDecision.PASS
                                 and (bool(absent_citations)
                                      or (bool(provider_result.evidence) and not verified_evidence)))
        if (coverage and not coverage.missing
                and (inconclusive_zero or invalid_rejection or invalid_pass_citation)):
            if provider_result.evidence and (absent_citations or not verified_evidence):
                if invented_citations and not any(item.path in supplied_paths for item in provider_result.evidence):
                    raise ValueError("Provider cited a repository file absent from supplied context")
                logger.warning("Discarding unverified inconclusive citations: candidate=%s",
                               candidate.candidate_id)
            grounded = derive_issue_grounded_evidence(candidate, context) or verified_evidence
            if not grounded:
                raise ValueError("Complete checklist lacked relevant repository grounding")
            logger.info("Resolved inconclusive checklist contradiction: candidate=%s criteria=%s",
                        candidate.candidate_id, len(coverage.criteria))
            result.update(
                decision=AnalysisDecision.PASS,
                confidence=0.5,
                evidence=grounded,
                issues=[],
                missing_requirements=[],
                revision_feedback="",
                recommendation="The approach covers the issue's explicit acceptance criteria; the maintainer decides assignment.",
            )
            return ApproachAnalysis.model_validate(result)

        if provider_result.decision is AnalysisDecision.PASS:
            if absent_citations:
                if invented_citations:
                    raise ValueError("Provider PASS cited a repository file absent from supplied context")
                if not verified_evidence:
                    verified_evidence = derive_issue_grounded_evidence(candidate, context)
            if provider_result.confidence <= 0:
                raise ValueError("Provider PASS had zero confidence")
            if provider_result.evidence and not verified_evidence:
                if invented_citations or not any(item.path in repo_identifiers for item in provider_result.evidence):
                    raise ValueError("Provider PASS cited no verifiable repository source")
                verified_evidence = derive_issue_grounded_evidence(candidate, context)
            if not provider_result.evidence:
                verified_evidence = derive_issue_grounded_evidence(candidate, context)
                logger.info("Grounded uncited PASS using repository lines: candidate=%s locations=%s",
                            candidate.candidate_id, len(verified_evidence))
            if not verified_evidence:
                raise ValueError("Provider PASS lacked relevant repository grounding")
        result["evidence"] = verified_evidence

        if provider_result.decision is AnalysisDecision.REVISION_REQUIRED and not provider_result.revision_feedback:
            details = provider_result.issues + provider_result.missing_requirements
            if details:
                result["revision_feedback"] = " ".join(details[:3])

        return ApproachAnalysis.model_validate(result)

    def _build_prompt(
        self,
        candidate: CandidateSubmission,
        context: RepositoryAnalysisContext,
    ) -> str:
        code_context = context.code_context

        relevant_files = "\n".join(
            f"- {path}"
            for path in code_context.relevant_files
        ) or "- None"

        file_contents = "\n\n".join(
            f"FILE: {file.path} (truncated: {file.truncated})\n"
            f"CONTENT:\n{file.content}"
            for file in code_context.file_contents
        ) or "None"

        test_files = "\n\n".join(
            f"TEST FILE: {file.path} (truncated: {file.truncated})\n"
            f"CONTENT:\n{file.content}"
            for file in code_context.test_files
        ) or "None"

        missing_paths = "\n".join(
            f"- {path}"
            for path in code_context.missing_paths
        ) or "- None"

        return f"""
    You are analyzing a contributor's proposed approach for a GitHub issue.

    Your analysis MUST be grounded only in the repository information provided below.

    Do not invent:
    - files
    - functions
    - APIs
    - classes
    - repository behavior
    - implementation details

    If the provided repository context is insufficient, do not assume missing information is true.

    ====================
    ISSUE
    ====================

    Repository:
    {candidate.repository_owner}/{candidate.repository_name}

    Issue number:
    {candidate.issue_number}

    Title:
    {context.issue_context.title}

    Body:
    {context.issue_context.body}


    ====================
    CONTRIBUTOR
    ====================

    Username:
    {candidate.contributor_username}


    ====================
    PROPOSED APPROACH
    ====================

    {candidate.approach}


    ====================
    RELEVANT REPOSITORY FILES
    ====================

    {relevant_files}


    ====================
    FILE CONTENT
    ====================

    {file_contents}


    ====================
    TEST FILES
    ====================

    {test_files}


    ====================
    MISSING FILES / PATHS
    ====================

    {missing_paths}


    ====================
    DECISION
    ====================

    Choose exactly one:

    PASS
    REVISION_REQUIRED
    REJECT

    PASS:
    Use when the proposed approach is supported by the available repository evidence.

    REVISION_REQUIRED:
    Use when the approach may be valid but important information, implementation detail,
    or repository-specific reasoning is missing.

    REJECT:
    Use when the approach conflicts with the repository evidence or proposes something
    that clearly does not fit the actual repository.

    For PASS, provide concrete repository evidence.

    For REVISION_REQUIRED, explain what is missing or unclear.

    For REJECT, explain the concrete repository mismatch.

    ====================
    STRUCTURED OUTPUT
    ====================

    The response is constrained by the AnalysisProviderResult schema.
    Confidence is a decimal from 0.0 to 1.0, not a percentage.
    Each evidence item requires path, claim, and a short exact excerpt copied
    from the supplied source or test content. In each evidence item, path must
    be the exact relative file path from supplied files (e.g., "app.py"), NOT
    the repository name. Never cite a tree-only path or content outside the
    supplied excerpt. PASS requires such evidence and positive confidence.
    REVISION_REQUIRED needs actionable revision_feedback.
    REJECT needs concrete issues or a recommendation.
    """
