import os

from .service import DefaultAnalysisService


def create_analysis_service() -> DefaultAnalysisService:
    if os.getenv("NVIDIA_API_KEYS"):
        from .nvidia_provider import NvidiaAnalysisProvider
        provider = NvidiaAnalysisProvider(
            api_key=os.getenv("NVIDIA_API_KEYS").split(",")[0].strip(),
            model_id=os.getenv("NVIDIA_MODEL"),
        )
    else:
        from .strands_provider import StrandsAnalysisProvider
        provider = StrandsAnalysisProvider(
            host=os.getenv("OLLAMA_HOST"),
            model_id=os.getenv("OLLAMA_MODEL"),
        )

    return DefaultAnalysisService(provider)