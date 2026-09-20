from unittest.mock import patch
import httpx

import pytest
pytest.importorskip("strands")

from app.analysis.prompts import ANALYSIS_SYSTEM_PROMPT
from app.analysis.provider import AnalysisProviderResult, EvidenceCitation
from app.analysis.strands_provider import StrandsAnalysisProvider
from strands.types.exceptions import StructuredOutputException


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


def test_ollama_readiness_checks_configured_model():
    provider = make_provider()
    with patch("app.analysis.strands_provider.httpx.get") as get:
        get.return_value.json.return_value = {"models": [{"name": "test-model:latest"}]}
        provider.check_ready()
    get.assert_called_once_with("http://localhost:11434/api/tags", timeout=3.0)


def test_ollama_readiness_rejects_missing_model():
    provider = make_provider()
    with patch("app.analysis.strands_provider.httpx.get") as get:
        get.return_value.json.return_value = {"models": []}
        with pytest.raises(RuntimeError, match="not installed"):
            provider.check_ready()


def test_ollama_readiness_rejects_unreachable_server():
    provider = make_provider()
    with patch("app.analysis.strands_provider.httpx.get", side_effect=httpx.ConnectError("offline")):
        with pytest.raises(RuntimeError, match="server is unavailable"):
            provider.check_ready()


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


@pytest.mark.asyncio
async def test_strands_structured_output_failure_retries_once():
    provider = make_provider()

    class RetryAgent:
        def __init__(self):
            self.calls = 0

        async def structured_output_async(self, output_model, prompt):
            self.calls += 1
            if self.calls == 1:
                raise StructuredOutputException("invalid structured response")
            return AnalysisProviderResult(
                decision="REVISION_REQUIRED", confidence=.5,
                revision_feedback="Explain the rate-limit test.",
            )

    agent = RetryAgent()
    provider._agent = agent
    result = await provider.generate("test prompt")
    assert result.decision.value == "REVISION_REQUIRED"
    assert agent.calls == 2
