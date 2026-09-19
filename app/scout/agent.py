import logging
import re
import time
from pydantic import ValidationError
from app.verifier.agent import LLMProvider
from app.github.events import GitHubPushEvent
from app.scout.models import ScoutContext
from app.scout.schemas import ScoutResponse
from app.scout.prompts import SYSTEM_PROMPT, build_scout_prompt

logger = logging.getLogger(__name__)


class MalformedScoutResponse(Exception):
    """Raised when Scout model output is malformed, unparseable, or exceeds bounds."""
    pass


class ScoutAgent:
    """
    Scout discovery agent.
    Consumes LLMProvider protocol.
    Has ZERO GitHub client reference and ZERO issue write capability.
    Never calls VerifierPipeline.
    """

    def __init__(self, llm_provider: LLMProvider) -> None:
        self.llm_provider = llm_provider

    async def discover(
        self,
        event: GitHubPushEvent,
        context: ScoutContext,
    ) -> ScoutResponse:
        user_prompt = build_scout_prompt(event, context)

        t0 = time.perf_counter()
        try:
            raw_response = await self.llm_provider.complete(SYSTEM_PROMPT, user_prompt)
        except Exception as exc:
            raise MalformedScoutResponse(f"Scout LLM invocation failed: {str(exc)}") from exc
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)

        # Strip markdown fences if present
        cleaned = raw_response.strip()
        match = re.search(r"```json\s*(.*?)\s*```", cleaned, re.DOTALL)
        if match:
            cleaned = match.group(1).strip()

        try:
            response = ScoutResponse.model_validate_json(cleaned)
        except ValidationError as exc:
            raise MalformedScoutResponse(f"Scout response failed schema validation: {str(exc)}") from exc
        except Exception as exc:
            raise MalformedScoutResponse(f"Failed to parse Scout JSON: {str(exc)}") from exc

        # Strictly enforce maximum AI drafts limit (max 8)
        if len(response.findings) > 8:
            raise MalformedScoutResponse(
                f"Scout returned {len(response.findings)} drafts, exceeding the maximum limit of 8"
            )

        provider_name = getattr(self.llm_provider, "provider_name", type(self.llm_provider).__name__)
        model_name = getattr(self.llm_provider, "model_name", "unknown")
        logger.info(
            "scout_agent_invoked",
            extra={
                "role": "scout",
                "provider": provider_name,
                "model": model_name,
                "latency_ms": latency_ms,
                "commit_sha": event.after_sha,
                "draft_count": len(response.findings),
                "result_status": "SUCCESS",
            },
        )

        return response
