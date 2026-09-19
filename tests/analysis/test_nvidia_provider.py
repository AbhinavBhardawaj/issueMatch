import pytest
from unittest.mock import AsyncMock, patch
import httpx

from app.analysis.nvidia_provider import NvidiaAnalysisProvider


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
                    "content": '{"decision":"PASS","confidence":0.95}'
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
        assert res == '{"decision":"PASS","confidence":0.95}'
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
