import os
import logging

from .service import DefaultAnalysisService

logger = logging.getLogger(__name__)


def create_analysis_service() -> DefaultAnalysisService:
    """Select the assignment-bot provider without reading Scout's API keys."""
    selected = os.getenv("ISSUEMATCH_ANALYSIS_PROVIDER", "ollama").strip().lower()
    if selected == "nvidia":
        from .nvidia_provider import NvidiaAnalysisProvider
        provider = NvidiaAnalysisProvider(
            api_key=os.getenv("ISSUEMATCH_NVIDIA_API_KEY"),
            model_id=os.getenv("ISSUEMATCH_NVIDIA_MODEL"),
        )
        logger.info("Candidate analysis provider selected: nvidia model=%s", provider.model_id)
        return DefaultAnalysisService(provider)
    if selected != "ollama":
        raise ValueError(
            "ISSUEMATCH_ANALYSIS_PROVIDER must be 'ollama' or 'nvidia'"
        )
    from .strands_provider import StrandsAnalysisProvider
    provider = StrandsAnalysisProvider(
        host=os.getenv("OLLAMA_HOST"),
        model_id=os.getenv("OLLAMA_MODEL"),
    )
    provider.check_ready()
    logger.info("Candidate analysis provider selected: ollama model=%s", provider.model_id)

    return DefaultAnalysisService(provider)
