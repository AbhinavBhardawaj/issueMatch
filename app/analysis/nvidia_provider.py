import os
import httpx
from typing import Optional

from app.analysis.provider import AnalysisProviderResult


class NvidiaAnalysisProvider:
    """
    Analysis provider backed by NVIDIA NIM cloud API.
    Conforms to AnalysisProvider protocol (async def generate(self, prompt: str) -> AnalysisProviderResult).
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model_id: Optional[str] = None,
    ) -> None:
        self.api_key = api_key or os.getenv("NVIDIA_API_KEYS", "").split(",")[0].strip()
        self.model_id = model_id or os.getenv(
            "NVIDIA_MODEL",
            "nvidia/nemotron-3-super-120b-a12b",
        )
        if not self.api_key:
            raise ValueError("NVIDIA_API_KEYS not configured")

    async def generate(self, prompt: str) -> AnalysisProviderResult:
        """Generate an analysis response from NVIDIA NIM API."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://integrate.api.nvidia.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model_id,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You are a repository issue analysis assistant. "
                                "Output strictly valid JSON conforming to the requested schema. "
                                "Do not enclose your output with extra commentary."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.1,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            result = data["choices"][0]["message"]["content"].strip()

            if not result:
                raise ValueError("NVIDIA NIM provider returned an empty response")

            return AnalysisProviderResult.model_validate_json(result)
