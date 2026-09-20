"""Candidate analysis must not silently inherit Scout's provider selection."""
import os
from unittest.mock import patch

import pytest

from app.analysis.factory import create_analysis_service
from app.analysis.service import DefaultAnalysisService


def test_nvidia_key_for_scout_does_not_change_candidate_provider():
    environment = {
        "NVIDIA_API_KEYS": "scout-only-key",
        "OLLAMA_MODEL": "local-test-model",
    }
    with patch.dict(os.environ, environment, clear=True), \
         patch("app.analysis.strands_provider.StrandsAnalysisProvider") as provider_class:
        provider_class.return_value.model_id = "local-test-model"
        service = create_analysis_service()
    assert isinstance(service, DefaultAnalysisService)
    assert service._provider is provider_class.return_value
    provider_class.assert_called_once_with(host=None, model_id="local-test-model")
    provider_class.return_value.check_ready.assert_called_once_with()


def test_unsupported_candidate_provider_fails_at_startup():
    with patch.dict(os.environ, {"ISSUEMATCH_ANALYSIS_PROVIDER": "nvidia"}, clear=True):
        with pytest.raises(ValueError, match="legacy NVIDIA text adapter"):
            create_analysis_service()
