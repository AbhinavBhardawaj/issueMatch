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


def test_explicit_nvidia_uses_candidate_only_credentials():
    environment = {
        "ISSUEMATCH_ANALYSIS_PROVIDER": "nvidia",
        "ISSUEMATCH_NVIDIA_API_KEY": "candidate-key",
        "ISSUEMATCH_NVIDIA_MODEL": "candidate-model",
        "NVIDIA_API_KEYS": "scout-key",
    }
    with patch.dict(os.environ, environment, clear=True), \
         patch("app.analysis.nvidia_provider.NvidiaAnalysisProvider") as provider_class:
        service = create_analysis_service()
    assert service._provider is provider_class.return_value
    provider_class.assert_called_once_with(api_key="candidate-key", model_id="candidate-model")


def test_nvidia_does_not_borrow_scout_credentials():
    with patch.dict(os.environ, {"ISSUEMATCH_ANALYSIS_PROVIDER": "nvidia",
                                  "NVIDIA_API_KEYS": "scout-only-key"}, clear=True):
        with pytest.raises(ValueError, match="ISSUEMATCH_NVIDIA_API_KEY"):
            create_analysis_service()


def test_unsupported_candidate_provider_fails_at_startup():
    with patch.dict(os.environ, {"ISSUEMATCH_ANALYSIS_PROVIDER": "unknown"}, clear=True):
        with pytest.raises(ValueError, match="ollama.*nvidia"):
            create_analysis_service()
