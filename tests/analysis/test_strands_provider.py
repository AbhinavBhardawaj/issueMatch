import pytest

from app.analysis.strands_provider import StrandsAnalysisProvider


class FakeAgent:
    async def invoke_async(self, prompt: str) -> str:
        assert prompt == "test prompt"
        return '{"decision":"PASS"}'


@pytest.mark.asyncio
async def test_strands_provider_returns_agent_response():
    provider = StrandsAnalysisProvider(
        host="http://localhost:11434",
        model_id="test-model",
    )

    provider._agent = FakeAgent()

    result = await provider.generate("test prompt")

    assert result == '{"decision":"PASS"}'

@pytest.mark.asyncio
async def test_strands_provider_rejects_empty_response():
    provider = StrandsAnalysisProvider(
        host="http://localhost:11434",
        model_id="test-model",
    )

    class EmptyAgent:
        async def invoke_async(self, prompt: str) -> str:
            return ""

    provider._agent = EmptyAgent()

    with pytest.raises(ValueError, match="empty response"):
        await provider.generate("test prompt")