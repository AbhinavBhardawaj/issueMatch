import json
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
            err_msg = str(exc) or type(exc).__name__
            raise MalformedScoutResponse(f"Scout LLM invocation failed: {err_msg}") from exc
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)

        # Strip markdown fences if present
        cleaned = raw_response.strip()
        match = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL)
        if match:
            cleaned = match.group(1).strip()

        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict) and isinstance(parsed.get("findings"), list):
                cleaned_findings = []
                for f in parsed["findings"]:
                    if isinstance(f, dict):
                        f_copy = dict(f)
                        if not f_copy.get("file"):
                            f_copy["file"] = f_copy.get("file_path") or f_copy.get("path")
                            if not f_copy.get("file") and f_copy.get("evidence"):
                                first_ev = f_copy["evidence"][0]
                                if isinstance(first_ev, dict):
                                    f_copy["file"] = first_ev.get("file")
                        if isinstance(f_copy.get("severity"), str):
                            f_copy["severity"] = f_copy["severity"].lower()
                        elif not f_copy.get("severity"):
                            f_copy["severity"] = "medium"
                        cleaned_findings.append(f_copy)
                    else:
                        cleaned_findings.append(f)
                parsed = {"findings": cleaned_findings}
            response = ScoutResponse.model_validate(parsed)
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
