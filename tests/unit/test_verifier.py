import pytest
from app.domain.states import VerificationStatus
from app.verifier.schemas import LLMInvocationError, MalformedVerifierResponse
from app.verifier.agent import run_verifier

class MockLLMProvider:
    def __init__(self, return_value=None, exception=None, callback=None):
        self.return_value = return_value
        self.exception = exception
        self.callback = callback

    async def complete(self, system: str, user: str) -> str:
        if self.callback:
            self.callback(system, user)
        if self.exception:
            raise self.exception
        return self.return_value

# V1
@pytest.mark.asyncio
async def test_verifier_returns_verified(make_finding, make_repo_context):
    f = make_finding()
    rc = make_repo_context()
    
    mock_llm = MockLLMProvider(return_value='{"status": "VERIFIED", "reason": "Looks good", "confidence": 0.95}')
        
    res = await run_verifier(f, rc, mock_llm)
    assert res.status == VerificationStatus.VERIFIED
    assert res.reason == "Looks good"
    assert res.confidence == 0.95
    assert res.finding_id == f.finding_id

# V2
@pytest.mark.asyncio
async def test_verifier_returns_rejected(make_finding, make_repo_context):
    f = make_finding()
    rc = make_repo_context()
    
    mock_llm = MockLLMProvider(return_value='{"status": "REJECTED", "reason": "False positive", "confidence": 0.8}')
        
    res = await run_verifier(f, rc, mock_llm)
    assert res.status == VerificationStatus.REJECTED

# V3
@pytest.mark.asyncio
async def test_verifier_llm_exception(make_finding, make_repo_context):
    f = make_finding()
    rc = make_repo_context()
    
    mock_llm = MockLLMProvider(exception=ValueError("API Timeout"))
        
    with pytest.raises(LLMInvocationError) as exc:
        await run_verifier(f, rc, mock_llm)
    assert "API Timeout" in str(exc.value)

# V4
@pytest.mark.asyncio
async def test_verifier_missing_status(make_finding, make_repo_context):
    f = make_finding()
    rc = make_repo_context()
    
    mock_llm = MockLLMProvider(return_value='{"reason": "Looks good", "confidence": 0.95}')
        
    with pytest.raises(MalformedVerifierResponse):
        await run_verifier(f, rc, mock_llm)

# V5
@pytest.mark.asyncio
async def test_verifier_status_not_in_enum(make_finding, make_repo_context):
    f = make_finding()
    rc = make_repo_context()
    
    mock_llm = MockLLMProvider(return_value='{"status": "MAYBE", "reason": "Looks good", "confidence": 0.95}')
        
    with pytest.raises(MalformedVerifierResponse):
        await run_verifier(f, rc, mock_llm)

# V6
@pytest.mark.asyncio
async def test_verifier_confidence_too_high(make_finding, make_repo_context):
    f = make_finding()
    rc = make_repo_context()
    
    mock_llm = MockLLMProvider(return_value='{"status": "VERIFIED", "reason": "Looks good", "confidence": 1.1}')
        
    with pytest.raises(MalformedVerifierResponse):
        await run_verifier(f, rc, mock_llm)

# V7
@pytest.mark.asyncio
async def test_verifier_confidence_too_low(make_finding, make_repo_context):
    f = make_finding()
    rc = make_repo_context()
    
    mock_llm = MockLLMProvider(return_value='{"status": "VERIFIED", "reason": "Looks good", "confidence": -0.1}')
        
    with pytest.raises(MalformedVerifierResponse):
        await run_verifier(f, rc, mock_llm)

# V8
@pytest.mark.asyncio
async def test_verifier_missing_reason(make_finding, make_repo_context):
    f = make_finding()
    rc = make_repo_context()
    
    mock_llm = MockLLMProvider(return_value='{"status": "VERIFIED", "confidence": 0.95}')
        
    with pytest.raises(MalformedVerifierResponse):
        await run_verifier(f, rc, mock_llm)

# V9
@pytest.mark.asyncio
async def test_verifier_raw_text(make_finding, make_repo_context):
    f = make_finding()
    rc = make_repo_context()
    
    mock_llm = MockLLMProvider(return_value="I think this is verified because it looks right.")
        
    with pytest.raises(MalformedVerifierResponse):
        await run_verifier(f, rc, mock_llm)

# V10
@pytest.mark.asyncio
async def test_verifier_markdown_wrapped_json(make_finding, make_repo_context):
    f = make_finding()
    rc = make_repo_context()
    
    mock_llm = MockLLMProvider(return_value='''```json
{
  "status": "VERIFIED",
  "reason": "It's good",
  "confidence": 0.88
}
```''')
        
    res = await run_verifier(f, rc, mock_llm)
    assert res.status == VerificationStatus.VERIFIED
    assert res.reason == "It's good"
    assert res.confidence == 0.88

# V11
@pytest.mark.asyncio
async def test_verifier_finding_no_function(make_finding, make_repo_context):
    f = make_finding(function=None)
    rc = make_repo_context()
    
    captured_prompt = ""
    def cb(sys_prompt, user_prompt):
        nonlocal captured_prompt
        captured_prompt = user_prompt
        
    mock_llm = MockLLMProvider(
        return_value='{"status": "VERIFIED", "reason": "x", "confidence": 0.9}',
        callback=cb
    )
        
    await run_verifier(f, rc, mock_llm)
    assert "Function:" not in captured_prompt
    assert "Line: 82" in captured_prompt

# V12
@pytest.mark.asyncio
async def test_verifier_large_repo_context(make_finding, make_repo_context):
    f = make_finding()
    rc = make_repo_context()
    
    captured_prompt = ""
    def cb(sys_prompt, user_prompt):
        nonlocal captured_prompt
        captured_prompt = user_prompt
        
    mock_llm = MockLLMProvider(
        return_value='{"status": "VERIFIED", "reason": "x", "confidence": 0.9}',
        callback=cb
    )
        
    await run_verifier(f, rc, mock_llm)
    assert "--- src/auth.py ---" in captured_prompt
    assert "decoded = jwt.decode(...)" in captured_prompt

# V13
@pytest.mark.asyncio
async def test_verifier_zero_confidence(make_finding, make_repo_context):
    f = make_finding()
    rc = make_repo_context()
    
    mock_llm = MockLLMProvider(return_value='{"status": "VERIFIED", "reason": "Not sure at all", "confidence": 0.0}')
        
    res = await run_verifier(f, rc, mock_llm)
    assert res.status == VerificationStatus.VERIFIED
    assert res.confidence == 0.0
