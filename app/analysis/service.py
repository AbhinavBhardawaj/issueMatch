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
    def __init__(self, provider: AnalysisProvider) -> None:
        self._provider = provider

    async def analyze(
        self,
        candidate: CandidateSubmission,
        context: RepositoryAnalysisContext,
    ) -> ApproachAnalysis:

        prompt = self._build_prompt(candidate, context)

        raw_response = await self._provider.generate(prompt)

        if not isinstance(raw_response, str):
            raise ValueError("Analysis provider must return a string")

        import json

        try:
            raw_result = json.loads(raw_response)
        except json.JSONDecodeError as exc:
            raise ValueError("Analysis provider returned invalid JSON") from exc

        if not isinstance(raw_result, dict):
            raise ValueError("Analysis provider JSON must be an object")

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
            f"FILE: {file.path}\n"
            f"CONTENT:\n{file.content}"
            for file in code_context.file_contents
        ) or "None"

        test_files = "\n\n".join(
            f"TEST FILE: {file.path}\n"
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
    {candidate.issue_title}

    Body:
    {candidate.issue_body}


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

    Return ONLY valid JSON compatible with the ApproachAnalysis schema.
    Do not add markdown fences.
    Do not add any extra text outside the JSON.
    """