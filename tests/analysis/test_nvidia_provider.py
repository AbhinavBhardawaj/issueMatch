import pytest
from unittest.mock import AsyncMock, patch
import httpx
from pydantic import ValidationError

from app.analysis.nvidia_provider import NvidiaAnalysisProvider
from app.analysis.provider import AnalysisProviderResult


# Valid JSON fixture matching AnalysisProviderResult schema
_VALID_RESPONSE_JSON = (
    '{"decision":"PASS","confidence":0.95,'
    '"strengths":["good coverage"],"issues":[],'
    '"missing_requirements":[],"evidence":[],'
    '"revision_feedback":"","recommendation":"merge"}'
)


@pytest.mark.asyncio
async def test_nvidia_analysis_provider_success():
    provider = NvidiaAnalysisProvider(
        api_key="test-key",
        model_id="test-model"
    )

    mock_resp = {
        "choices": [
            {
                "message": {
                    "content": _VALID_RESPONSE_JSON
                }
            }
        ]
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_response = httpx.Response(
            200,
            json=mock_resp,
            request=httpx.Request("POST", "https://integrate.api.nvidia.com/v1/chat/completions")
        )
        mock_post.return_value = mock_response

        res = await provider.generate("test prompt")
        assert isinstance(res, AnalysisProviderResult)
        assert res.decision.value == "PASS"
        assert res.confidence == 0.95
        mock_post.assert_called_once()


@pytest.mark.asyncio
async def test_nvidia_analysis_provider_empty_response():
    provider = NvidiaAnalysisProvider(
        api_key="test-key",
        model_id="test-model"
    )

    mock_resp = {
        "choices": [
            {
                "message": {
                    "content": "   "
                }
            }
        ]
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_response = httpx.Response(
            200,
            json=mock_resp,
            request=httpx.Request("POST", "https://integrate.api.nvidia.com/v1/chat/completions")
        )
        mock_post.return_value = mock_response

        with pytest.raises(ValueError, match="empty response"):
            await provider.generate("test prompt")


@pytest.mark.asyncio
async def test_nvidia_analysis_provider_malformed_json():
    """Regression: model returns non-JSON or schema-violating output."""
    provider = NvidiaAnalysisProvider(
        api_key="test-key",
        model_id="test-model"
    )

    mock_resp = {
        "choices": [
            {
                "message": {
                    "content": "Sure! Here is my analysis: the code looks fine."
                }
            }
        ]
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_response = httpx.Response(
            200,
            json=mock_resp,
            request=httpx.Request("POST", "https://integrate.api.nvidia.com/v1/chat/completions")
        )
        mock_post.return_value = mock_response

        with pytest.raises(ValidationError):
            await provider.generate("test prompt")
