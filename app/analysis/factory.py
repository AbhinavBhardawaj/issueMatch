import os
import logging

from .service import DefaultAnalysisService

logger = logging.getLogger(__name__)


def create_analysis_service() -> DefaultAnalysisService:
    """Build candidate analysis independently of Scout's provider API keys.

    The legacy NVIDIA adapter returns free-form text, not the validated
    AnalysisProviderResult contract. It must not be selected implicitly just
    because Scout has NVIDIA_API_KEYS configured.
    """
    selected = os.getenv("ISSUEMATCH_ANALYSIS_PROVIDER", "ollama").strip().lower()
    if selected != "ollama":
        raise ValueError(
            "ISSUEMATCH_ANALYSIS_PROVIDER must be 'ollama'; the candidate "
            "analysis contract does not support the legacy NVIDIA text adapter"
        )
    from .strands_provider import StrandsAnalysisProvider
    provider = StrandsAnalysisProvider(
        host=os.getenv("OLLAMA_HOST"),
        model_id=os.getenv("OLLAMA_MODEL"),
    )
    provider.check_ready()
    logger.info("Candidate analysis provider selected: ollama model=%s", provider.model_id)

    return DefaultAnalysisService(provider)
