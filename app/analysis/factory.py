import os

from .service import DefaultAnalysisService
from .strands_provider import StrandsAnalysisProvider


def create_analysis_service() -> DefaultAnalysisService:
    provider = StrandsAnalysisProvider(
        host=os.getenv("OLLAMA_HOST"),
        model_id=os.getenv("OLLAMA_MODEL"),
    )

    return DefaultAnalysisService(provider)