from app.models.analysis import (
    AnalysisDecision,
    ApproachAnalysis,
)
from app.models.candidate import CandidateSubmission
from app.models.context import RepositoryAnalysisContext

from .provider import AnalysisProvider
from .verifier import verify_evidence


class RuleBasedAnalysisProvider:
    """
    Deterministic baseline provider.

    This is intentionally simple. A real AI provider can later implement
    AnalysisProvider without changing AnalysisService.
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
    def __init__(self, provider: AnalysisProvider | None = None) -> None:
        self._provider = provider or RuleBasedAnalysisProvider()

    async def analyze(self,candidate: CandidateSubmission,
        context: RepositoryAnalysisContext) -> ApproachAnalysis:
        
        raw_result = await self._provider.analyze(candidate, context)

        if not isinstance(raw_result, dict):
            raise ValueError("Analysis provider must return a dictionary")

        evidence = raw_result.get("evidence", [])

        if not isinstance(evidence, list):
            raise ValueError("Analysis provider returned invalid evidence")

        verified_evidence = verify_evidence(evidence, context)

        decision = raw_result.get("decision")

        if decision == AnalysisDecision.PASS.value and not verified_evidence:
            decision = AnalysisDecision.REVISION_REQUIRED.value
            raw_result["revision_feedback"] = (
                "The proposed approach did not contain repository-grounded evidence. "
                "Please identify relevant existing files or components."
            )

        result = {
            **raw_result,
            "decision": decision,
            "evidence": verified_evidence,
        }

        return ApproachAnalysis.model_validate(result)