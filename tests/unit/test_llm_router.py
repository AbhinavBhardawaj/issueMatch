import os
import pytest
import asyncio
import httpx
from unittest.mock import AsyncMock, patch

from app.verifier.schemas import LLMInvocationError
from app.infrastructure.llm_router import (
    load_providers, MultiLLMProvider,
    GroqProvider, GeminiProvider, MistralProvider, CohereProvider, NvidiaNimProvider
)

class DummyProvider:
    def __init__(self, name):
        self.name = name
        self.complete = AsyncMock()

def make_http_error(status_code):
    resp = httpx.Response(status_code, request=httpx.Request("POST", "http://test"))
    return httpx.HTTPStatusError("error", request=resp.request, response=resp)

@pytest.mark.asyncio
async def test_lr1_single_provider_success():
    p = DummyProvider("P1")
    p.complete.return_value = "success_text"
    router = MultiLLMProvider([p])
    
    res = await router.complete("sys", "user")
    assert res == "success_text"
    p.complete.assert_called_once_with("sys", "user")

@pytest.mark.asyncio
async def test_lr2_provider_1_returns_429_first_pass():
    p1 = DummyProvider("P1")
    p2 = DummyProvider("P2")
    p1.complete.side_effect = make_http_error(429)
    p2.complete.return_value = "p2_success"
    
    router = MultiLLMProvider([p1, p2])
    res = await router.complete("sys", "user")
    
    assert res == "p2_success"
    assert p1.complete.call_count == 1
    assert p2.complete.call_count == 1

@pytest.mark.asyncio
async def test_lr3_all_providers_429_first_pass():
    p1 = DummyProvider("P1")
    p2 = DummyProvider("P2")
    
    # p1 gets 429 on pass 1, then success on pass 2
    p1.complete.side_effect = [make_http_error(429), "p1_success_pass2"]
    p2.complete.side_effect = make_http_error(429)
    
    router = MultiLLMProvider([p1, p2])
    res = await router.complete("sys", "user")
        
    assert res == "p1_success_pass2"
    assert p1.complete.call_count == 2
    assert p2.complete.call_count == 1

@pytest.mark.asyncio
async def test_lr4_second_pass_429_sleeps_1():
    p1 = DummyProvider("P1")
    # pass 1: 429
    # pass 2: 429 -> sleeps 1
    # pass 3: success
    p1.complete.side_effect = [make_http_error(429), make_http_error(429), "success_p3"]
    
    router = MultiLLMProvider([p1])
    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        res = await router.complete("sys", "user")
        
    assert res == "success_p3"
    assert p1.complete.call_count == 3
    mock_sleep.assert_called_once_with(1)

@pytest.mark.asyncio
async def test_lr5_second_pass_429_twice_then_exhausts():
    p1 = DummyProvider("P1")
    # p1 gets 429 four times -> exhaust
    p1.complete.side_effect = [make_http_error(429)] * 4
    
    router = MultiLLMProvider([p1])
    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with pytest.raises(LLMInvocationError) as exc:
            await router.complete("sys", "user")
            
    assert "all providers exhausted" in str(exc.value)
    assert p1.complete.call_count == 4
    # sleeps should be 1 then 2
    mock_sleep.assert_any_call(1)
    mock_sleep.assert_any_call(2)
    assert mock_sleep.call_count == 2

@pytest.mark.asyncio
async def test_lr6_401_on_provider_1_skip_immediately():
    p1 = DummyProvider("P1")
    p1.complete.side_effect = make_http_error(401)
    
    router = MultiLLMProvider([p1])
    with pytest.raises(LLMInvocationError):
        await router.complete("sys", "user")
        
    # Should only be called once because 401 is NOT retryable
    assert p1.complete.call_count == 1

@pytest.mark.asyncio
async def test_lr7_provider_2_success_after_provider_1_fails():
    p1 = DummyProvider("P1")
    p2 = DummyProvider("P2")
    
    p1.complete.side_effect = make_http_error(500) # skip
    p2.complete.return_value = "p2_win"
    
    router = MultiLLMProvider([p1, p2])
    res = await router.complete("sys", "user")
    
    assert res == "p2_win"
    assert p1.complete.call_count == 1
    assert p2.complete.call_count == 1

@pytest.mark.asyncio
async def test_lr8_all_providers_exhausted_both_passes():
    p1 = DummyProvider("P1")
    p2 = DummyProvider("P2")
    
    # 429 for all
    p1.complete.side_effect = make_http_error(429)
    p2.complete.side_effect = make_http_error(429)
    
    router = MultiLLMProvider([p1, p2])
    with patch("asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(LLMInvocationError):
            await router.complete("sys", "user")

@pytest.mark.asyncio
async def test_lr9_json_parse_error_no_retry():
    p1 = DummyProvider("P1")
    p1.complete.side_effect = KeyError("bad json")
    
    router = MultiLLMProvider([p1])
    with pytest.raises(LLMInvocationError):
        await router.complete("sys", "user")
        
    assert p1.complete.call_count == 1

def test_lr10_load_providers_zero_keys():
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(LLMInvocationError) as exc:
            load_providers()
        assert "No providers loaded" in str(exc.value)

def test_lr11_load_providers_skips_empty():
    with patch.dict(os.environ, {"GROQ_API_KEYS": " ,  , key1, "}, clear=True):
        providers = load_providers()
        assert len(providers) == 1
        assert isinstance(providers[0], GroqProvider)
        assert providers[0].api_key == "key1"

def test_lr12_load_providers_order():
    env = {
        "NVIDIA_API_KEYS": "nv1",
        "COHERE_API_KEYS": "c1",
        "MISTRAL_API_KEYS": "m1,m2",
        "GEMINI_API_KEYS": "g1",
        "GROQ_API_KEYS": "q1,q2"
    }
    with patch.dict(os.environ, env, clear=True):
        providers = load_providers()
        
    assert len(providers) == 7
    assert isinstance(providers[0], GroqProvider)
    assert isinstance(providers[1], GroqProvider)
    assert isinstance(providers[2], GeminiProvider)
    assert isinstance(providers[3], MistralProvider)
    assert isinstance(providers[4], MistralProvider)
    assert isinstance(providers[5], CohereProvider)
    assert isinstance(providers[6], NvidiaNimProvider)
    assert providers[6].api_key == "nv1"

