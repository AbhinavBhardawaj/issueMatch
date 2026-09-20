import logging
import re
import time
from pydantic import ValidationError
from app.verifier.agent import LLMProvider
from app.github.events import GitHubPushEvent
from app.scout.models import ScoutContext
from app.scout.schemas import ScoutResponse
from app.scout.prompts import (
    SYSTEM_PROMPT,
    build_scout_prompt,
    estimate_serialized_request_size,
    resolve_max_scout_request_bytes,
    MAX_SCOUT_REQUEST_BYTES,
)


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
        batch_index: int = 1,
        total_batches: int = 1,
    ) -> ScoutResponse:
        target_budget = resolve_max_scout_request_bytes()
        raw_provider = getattr(self.llm_provider, "provider_name", "")
        raw_model = getattr(self.llm_provider, "model_name", "")
        provider_name = raw_provider.upper() if isinstance(raw_provider, str) else ""
        model_name = raw_model.lower() if isinstance(raw_model, str) else ""
        if "GROQ" in provider_name or "qwen" in model_name:
            target_budget = min(target_budget, 9_200)

        user_prompt = build_scout_prompt(event, context, max_request_bytes=target_budget)
        request_bytes = estimate_serialized_request_size(user_prompt, SYSTEM_PROMPT)

        # Layer 2 Guard: Never knowingly send an outbound request exceeding our internal safety budget
        if request_bytes > target_budget:
            raise MalformedScoutResponse(
                f"Scout outbound request envelope ({request_bytes} bytes) exceeds internal safety budget ({target_budget} bytes)"
            )

        last_error = None
        for attempt in range(2):
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
                break
            except ValidationError as exc:
                last_error = MalformedScoutResponse(f"Scout response failed schema validation: {str(exc)}")
            except Exception as exc:
                last_error = MalformedScoutResponse(f"Failed to parse Scout JSON: {str(exc)}")

            if attempt == 0:
                logger.warning(
                    "scout_malformed_response_retrying",
                    extra={"error": str(last_error), "attempt": attempt + 1},
                )
                continue
        else:
            if last_error:
                raise last_error

        # Strictly enforce maximum AI drafts limit (max 8 per batch)
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
                "scout_batch_count": total_batches,
                "scout_batch_index": batch_index,
                "source_file_count": len(context.files),
                "test_file_count": len(context.test_files),
                "changed_file_count": len(context.changed_paths),
                "raw_context_bytes": context.total_context_bytes,
                "user_prompt_bytes": len(user_prompt.encode("utf-8")),
                "system_prompt_bytes": len(SYSTEM_PROMPT.encode("utf-8")),
                "request_bytes": request_bytes,
                "prompt_budget_bytes": target_budget,
                "context_completeness": context.context_completeness.value,
                "partial_reasons": context.partial_reasons,
            },
        )

        return response
