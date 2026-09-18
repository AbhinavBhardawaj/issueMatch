from typing import Protocol


class AnalysisProvider(Protocol):
    async def generate(self, prompt: str) -> str:
        """Generate an AI response for the given analysis prompt."""
        ...