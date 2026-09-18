from typing import Protocol

from app.models.candidate import CandidateSubmission
from app.models.context import RepositoryAnalysisContext


class AnalysisProvider(Protocol):
    async def analyze(
        self,
        candidate: CandidateSubmission,
        context: RepositoryAnalysisContext,
    ) -> dict:
        ...