import os

from strands import Agent
from strands.models.ollama import OllamaModel


class StrandsAnalysisProvider:
    """Analysis provider backed by Strands Agents and a local Ollama model."""

    def __init__(
        self,
        host: str | None = None,
        model_id: str | None = None,
    ) -> None:
        self.host = host or os.getenv(
            "OLLAMA_HOST",
            "http://localhost:11434",
        )
        self.model_id = model_id or os.getenv(
            "OLLAMA_MODEL",
            "llama3.1",
        )

        model = OllamaModel(
            host=self.host,
            model_id=self.model_id,
        )

        self._agent = Agent(
            model=model,
        )

    async def generate(self, prompt: str) -> str:
        """Generate an analysis response from the local Ollama model."""
        response = await self._agent.invoke_async(prompt)
        result = str(response).strip()

        if not result:
            raise ValueError("Analysis provider returned an empty response")

        return result