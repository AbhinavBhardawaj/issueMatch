import json
import os
import httpx
from typing import Optional
from pydantic import ValidationError

from .prompts import ANALYSIS_SYSTEM_PROMPT
from .provider import AnalysisProviderResult


class NvidiaAnalysisProvider:
    """
    Analysis provider backed by NVIDIA NIM cloud API.
    Returns the same validated provider model as the Ollama adapter.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model_id: Optional[str] = None,
    ) -> None:
        self.api_key = api_key or os.getenv("ISSUEMATCH_NVIDIA_API_KEY", "").strip()
        self.model_id = model_id or os.getenv(
            "ISSUEMATCH_NVIDIA_MODEL",
            "nvidia/nemotron-3-super-120b-a12b",
        )
        if not self.api_key:
            raise ValueError("ISSUEMATCH_NVIDIA_API_KEY not configured")

    async def generate(self, prompt: str) -> AnalysisProviderResult:
        """Validate NIM JSON at the provider boundary, with one schema retry."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            for attempt in range(2):
                request = prompt if attempt == 0 else (
                    prompt + "\nReturn one JSON object matching this exact schema, with no prose: "
                    + json.dumps(AnalysisProviderResult.model_json_schema(), separators=(",", ":"))
                )
                resp = await client.post(
                    "https://integrate.api.nvidia.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={
                        "model": self.model_id,
                        "messages": [
                            {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
                            {"role": "user", "content": request},
                        ],
                        "temperature": 0.1,
                    },
                )
                resp.raise_for_status()
                try:
                    result = resp.json()["choices"][0]["message"]["content"]
                    if not isinstance(result, str) or not result.strip():
                        raise ValueError("NVIDIA NIM provider returned an empty response")
                    return AnalysisProviderResult.model_validate_json(result)
                except ValueError as exc:
                    if "empty response" in str(exc):
                        raise
                    if attempt == 1:
                        raise ValueError("Analysis provider returned invalid structured output") from exc
                except (KeyError, IndexError, TypeError, ValidationError) as exc:
                    if attempt == 1:
                        raise ValueError("Analysis provider returned invalid structured output") from exc
        raise AssertionError("unreachable")
