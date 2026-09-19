from unittest.mock import patch

import pytest
pytest.importorskip("strands")

from app.analysis.prompts import ANALYSIS_SYSTEM_PROMPT
from app.analysis.provider import AnalysisProviderResult, EvidenceCitation
from app.analysis.strands_provider import StrandsAnalysisProvider


class FakeAgent:
    def __init__(self, structured_output):
        self.result = structured_output

    async def structured_output_async(self, output_model, prompt):
        assert output_model is AnalysisProviderResult
        assert prompt.startswith("test prompt")
        return self.result


def make_provider():
    with patch("app.analysis.strands_provider.Agent") as agent, patch("app.analysis.strands_provider.OllamaModel") as model:
        provider = StrandsAnalysisProvider(host="http://localhost:11434", model_id="test-model")
    assert model.call_args.kwargs["temperature"] == 0
    assert agent.call_args.kwargs["system_prompt"] == ANALYSIS_SYSTEM_PROMPT
    assert agent.call_args.kwargs["callback_handler"] is None
    assert "structured_output_model" not in agent.call_args.kwargs
    return provider


@pytest.mark.asyncio
async def test_direct_structured_output_returns_validated_model():
    provider = make_provider()
    model = AnalysisProviderResult(decision="PASS", confidence=.9,
                                   evidence=[EvidenceCitation(path="app.py", excerpt="x = 1", claim="sets x")])
    provider._agent = FakeAgent(model)
    result = await provider.generate("test prompt")
    assert result is model


@pytest.mark.asyncio
async def test_missing_structured_output_is_rejected():
    provider = make_provider()
    provider._agent = FakeAgent(None)
    with pytest.raises(RuntimeError, match="no structured output"):
        await provider.generate("test prompt")


@pytest.mark.asyncio
async def test_invalid_evidence_shape_fails_at_provider_boundary():
    provider = make_provider()
    provider._agent = FakeAgent({"decision": "PASS", "confidence": .9,
                                 "evidence": [{"key": "app.py", "value": "Yes"}]})
    with pytest.raises(ValueError, match="invalid structured output"):
        await provider.generate("test prompt")


@pytest.mark.asyncio
async def test_schema_failure_retries_once_without_parsing_model_text():
    provider = make_provider()

    class RetryAgent:
        def __init__(self):
            self.prompts = []

        async def structured_output_async(self, output_model, prompt):
            self.prompts.append(prompt)
            if len(self.prompts) == 1:
                raise ValueError("confidence must be at most 1")
            return AnalysisProviderResult(decision="REVISION_REQUIRED", confidence=.5,
                                          revision_feedback="Add a test")

    agent = RetryAgent()
    provider._agent = agent
    result = await provider.generate("test prompt")
    assert result.confidence == .5
    assert len(agent.prompts) == 2
    assert "Do not use percentages" in agent.prompts[1]
