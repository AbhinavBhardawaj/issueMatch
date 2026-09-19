import os
import logging

from pydantic import ValidationError
from strands import Agent
from strands.models.ollama import OllamaModel

from .prompts import ANALYSIS_SYSTEM_PROMPT
from .provider import AnalysisProviderResult

logger = logging.getLogger(__name__)


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
            temperature=0,
        )

        self._agent = Agent(
            model=model,
            system_prompt=ANALYSIS_SYSTEM_PROMPT,
            callback_handler=None,
        )

    async def generate(self, prompt: str) -> AnalysisProviderResult:
        """Use Ollama's native JSON-schema path through Strands 1.56.

        Invocation-level ``structured_output_model`` requires a tool call that
        Ollama cannot force. Strands' direct structured-output method instead
        passes this Pydantic schema to Ollama's ``format`` parameter. Strands
        1.56 marks this method deprecated, so upgrades need a compatibility
        check until invocation-level Ollama tool choice is reliable.
        """
        for attempt in range(2):
            request = prompt if attempt == 0 else (
                prompt + "\nYour previous result failed schema validation. Return the exact schema: "
                "confidence must be a decimal between 0.0 and 1.0, evidence items "
                "must each contain path, excerpt, and claim. Do not use percentages."
            )
            try:
                response = await self._agent.structured_output_async(AnalysisProviderResult, request)
                if response is None:
                    raise RuntimeError("Analysis provider returned no structured output")
                return AnalysisProviderResult.model_validate(response)
            except (ValueError, ValidationError) as exc:
                logger.warning("Ollama structured output validation failed: model=%s attempt=%s failure_type=%s",
                               self.model_id, attempt + 1, type(exc).__name__)
                if attempt == 1:
                    raise ValueError("Analysis provider returned invalid structured output") from exc
        raise AssertionError("unreachable")
