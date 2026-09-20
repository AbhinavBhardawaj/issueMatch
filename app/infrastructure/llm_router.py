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
    def __init__(self, api_key: str, model: str | None = None, timeout: float = 10.0):
        self.api_key = api_key
        self.model = model or "qwen/qwen3.8-27b"
        self.timeout = timeout
        
    async def complete(self, system: str, user: str) -> str:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
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
    def __init__(self, api_key: str, model: str | None = None, timeout: float = 10.0):
        self.api_key = api_key
        self.model = model or "gemini-1.5-flash"
        self.timeout = timeout
        
    async def complete(self, system: str, user: str) -> str:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                url,
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
    def __init__(self, api_key: str, model: str | None = None, timeout: float = 10.0):
        self.api_key = api_key
        self.model = model or "mistral-large-latest"
        self.timeout = timeout
        
    async def complete(self, system: str, user: str) -> str:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                "https://api.mistral.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
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
    def __init__(self, api_key: str, model: str | None = None, timeout: float = 10.0):
        self.api_key = api_key
        self.model = model or "command-r-plus"
        self.timeout = timeout
        
    async def complete(self, system: str, user: str) -> str:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                "https://api.cohere.ai/v2/chat",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user}
                    ]
                }
            )
            resp.raise_for_status()
            data = resp.json()
            return data["message"]["content"][0]["text"]

class NvidiaNimProvider:
    def __init__(self, api_key: str, model: str | None = None, timeout: float | None = None):
        self.api_key = api_key
        self.model = model or os.environ.get("NVIDIA_MODEL", "nvidia/nemotron-3-super-120b-a12b")
        env_timeout = os.environ.get("LLM_TIMEOUT_SECONDS")
        self.timeout = timeout if timeout is not None else (float(env_timeout) if env_timeout else 90.0)
        
    async def complete(self, system: str, user: str) -> str:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                "https://integrate.api.nvidia.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user}
                    ],
                    "temperature": 0.1
                }
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]


PROVIDER_CLASSES = {
    "GROQ": GroqProvider,
    "GEMINI": GeminiProvider,
    "MISTRAL": MistralProvider,
    "COHERE": CohereProvider,
    "NVIDIA": NvidiaNimProvider,
    "NVIDIA_NIM": NvidiaNimProvider,
}


class ProviderConfigurationError(ValueError):
    """Raised when an explicitly configured provider lacks a required API key."""
    pass


def get_provider_key(provider_name: str, role: str) -> str:
    role_upper = role.upper()
    role_key = os.environ.get(f"{role_upper}_API_KEY", "").strip()
    if role_key:
        return role_key

    norm_name = provider_name.upper().replace("-", "_")
    key_envs = {
        "GROQ": ["GROQ_API_KEYS", "GROQ_API_KEY"],
        "GEMINI": ["GEMINI_API_KEYS", "GEMINI_API_KEY"],
        "MISTRAL": ["MISTRAL_API_KEYS", "MISTRAL_API_KEY"],
        "COHERE": ["COHERE_API_KEYS", "COHERE_API_KEY"],
        "NVIDIA": ["NVIDIA_API_KEYS", "NVIDIA_API_KEY"],
        "NVIDIA_NIM": ["NVIDIA_API_KEYS", "NVIDIA_API_KEY"],
    }.get(norm_name, [])

    for env_var in key_envs:
        val = os.environ.get(env_var, "").strip()
        if val:
            first_key = val.split(",")[0].strip()
            if first_key:
                return first_key

    raise ProviderConfigurationError(
        f"Explicit provider '{provider_name}' configured for role '{role}', but no API key was found in {role_upper}_API_KEY or {key_envs}."
    )


def create_role_provider(
    role: str,
    provider_name: str | None = None,
    model_id: str | None = None,
    timeout: float | None = None,
) -> LLMProvider:
    role_upper = role.upper()
    resolved_provider = provider_name or os.environ.get(f"{role_upper}_PROVIDER")
    resolved_model = model_id or os.environ.get(f"{role_upper}_MODEL_ID")

    role_timeout_env = os.environ.get(f"{role_upper}_TIMEOUT") or os.environ.get("LLM_TIMEOUT_SECONDS")
    role_timeout = timeout if timeout is not None else (float(role_timeout_env) if role_timeout_env else 90.0)

    if resolved_provider:
        prov_key = resolved_provider.upper().replace("-", "_")
        cls = PROVIDER_CLASSES.get(prov_key)
        if not cls:
            raise ValueError(f"Unknown provider '{resolved_provider}' for role '{role}'")
        api_key = get_provider_key(resolved_provider, role)
        kwargs = {"api_key": api_key, "model": resolved_model}
        if role_timeout is not None:
            kwargs["timeout"] = role_timeout
        return cls(**kwargs)

    # Fallback to loading providers independently for this role (never share instances)
    providers = load_providers()
    return MultiLLMProvider(providers)


def create_scout_provider(
    provider_name: str | None = None,
    model_id: str | None = None,
    timeout: float | None = None,
) -> LLMProvider:
    return create_role_provider("scout", provider_name, model_id, timeout)


def create_verifier_provider(
    provider_name: str | None = None,
    model_id: str | None = None,
    timeout: float | None = None,
) -> LLMProvider:
    return create_role_provider("verifier", provider_name, model_id, timeout)


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
    add_keys("NVIDIA_API_KEYS", NvidiaNimProvider)
    
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
