"""
Multi-Provider LLM Router module.

Inject into verifier:
from app.infrastructure.llm_router import MultiLLMProvider, load_providers
from app.verifier.agent import VerifierAgent

providers = load_providers()
router = MultiLLMProvider(providers)
agent = VerifierAgent(llm_provider=router)
"""

import os
import asyncio
import httpx
from collections import deque
from typing import List

from app.verifier.agent import LLMProvider
from app.verifier.schemas import LLMInvocationError

class GroqProvider:
    def __init__(self, api_key: str):
        self.api_key = api_key
        
    async def complete(self, system: str, user: str) -> str:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": "qwen/qwen3.8-27b",
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user}
                    ],
                    "response_format": {"type": "json_object"}
                }
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

class GeminiProvider:
    def __init__(self, api_key: str):
        self.api_key = api_key
        
    async def complete(self, system: str, user: str) -> str:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent",
                headers={"x-goog-api-key": self.api_key},
                json={
                    "systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"parts": [{"text": user}]}]
                }
            )
            resp.raise_for_status()
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]

class MistralProvider:
    def __init__(self, api_key: str):
        self.api_key = api_key
        
    async def complete(self, system: str, user: str) -> str:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.mistral.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": "mistral-large-latest",
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user}
                    ]
                }
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

class CohereProvider:
    def __init__(self, api_key: str):
        self.api_key = api_key
        
    async def complete(self, system: str, user: str) -> str:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.cohere.ai/v2/chat",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": "command-r-plus",
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user}
                    ]
                }
            )
            resp.raise_for_status()
            data = resp.json()
            return data["message"]["content"][0]["text"]

def load_providers() -> List[LLMProvider]:
    providers: List[LLMProvider] = []
    
    def add_keys(env_var: str, provider_cls):
        keys_str = os.environ.get(env_var, "")
        if keys_str:
            for key in keys_str.split(","):
                key = key.strip()
                if key:
                    providers.append(provider_cls(key))
                    
    add_keys("GROQ_API_KEYS", GroqProvider)
    add_keys("GEMINI_API_KEYS", GeminiProvider)
    add_keys("MISTRAL_API_KEYS", MistralProvider)
    add_keys("COHERE_API_KEYS", CohereProvider)
    
    if not providers:
        raise LLMInvocationError("No providers loaded")
        
    return providers

class MultiLLMProvider:
    def __init__(self, providers: List[LLMProvider]):
        self.providers = providers
        
    async def complete(self, system: str, user: str) -> str:
        if not self.providers:
            raise LLMInvocationError("no providers available")
            
        queue = deque(self.providers)
        visit_counts = {id(p): 0 for p in self.providers}
        
        while queue:
            provider = queue.popleft()
            visit_counts[id(provider)] += 1
            count = visit_counts[id(provider)]
            
            try:
                return await provider.complete(system, user)
            except Exception as e:
                err_type = "bad"
                if isinstance(e, httpx.HTTPStatusError):
                    status = e.response.status_code
                    if status in (429, 408):
                        err_type = "retryable"
                    elif status in (401, 403):
                        err_type = "auth"
                    elif status in (500, 502, 503):
                        err_type = "down"
                elif isinstance(e, (httpx.TimeoutException, asyncio.TimeoutError)):
                    err_type = "retryable"
                elif isinstance(e, (KeyError, ValueError, IndexError, TypeError)):
                    err_type = "bad"
                    
                if count == 1:
                    if err_type == "retryable":
                        queue.append(provider)
                elif count == 2:
                    if err_type == "retryable":
                        await asyncio.sleep(1)
                        queue.appendleft(provider)
                elif count == 3:
                    if err_type == "retryable":
                        await asyncio.sleep(2)
                        queue.appendleft(provider)
                        
        raise LLMInvocationError("all providers exhausted")
