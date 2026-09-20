"""
Multi-Provider LLM Router module.

Supports:
- Legacy single-provider execution (via SCOUT_PROVIDER / VERIFIER_PROVIDER)
- Ordered failover (MultiLLMProvider)
- Deterministic health-aware round-robin multi-provider execution (HealthAwareProviderPool via SCOUT_PROVIDERS / VERIFIER_PROVIDERS)
"""

import asyncio
import enum
import json
import logging
import os
import re
import time
from collections import deque
from typing import Any, Callable, List

import httpx

from app.verifier.agent import LLMProvider
from app.verifier.schemas import LLMInvocationError

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER_MODELS = {
    "GROQ": "qwen/qwen3.6-27b",
    "GEMINI": "gemini-2.5-flash",
    "MISTRAL": "mistral-large-latest",
    "COHERE": "command-a-plus-05-2026",
    "NVIDIA": "nvidia/nemotron-3-super-120b-a12b",
    "NVIDIA_NIM": "nvidia/nemotron-3-super-120b-a12b",
}


class CircuitState(str, enum.Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class ProviderEndpoint:
    """Encapsulates runtime health and metadata for a single provider endpoint."""

    def __init__(
        self,
        provider: LLMProvider,
        provider_name: str,
        model_name: str,
    ) -> None:
        self.provider = provider
        self.provider_name = provider_name
        self.model_name = model_name
        self.consecutive_failures: int = 0
        self.circuit_state: CircuitState = CircuitState.CLOSED
        self.cooldown_until: float = 0.0
        self.last_failure_type: str | None = None
        self.half_open_probe_in_flight: bool = False
        self.is_quarantined: bool = False

    def quarantine(self, failure_type: str) -> None:
        """Immediately and permanently removes endpoint from normal scheduling."""
        self.is_quarantined = True
        self.circuit_state = CircuitState.OPEN
        self.cooldown_until = float("inf")
        self.consecutive_failures = max(self.consecutive_failures, 1)
        self.last_failure_type = failure_type
        self.half_open_probe_in_flight = False

    def is_available(self, current_time: float) -> bool:
        if self.is_quarantined or self.cooldown_until == float("inf"):
            return False
        if self.circuit_state == CircuitState.CLOSED:
            return True
        if self.circuit_state == CircuitState.OPEN:
            if current_time >= self.cooldown_until:
                self.circuit_state = CircuitState.HALF_OPEN
                self.half_open_probe_in_flight = False
                return True
            return False
        if self.circuit_state == CircuitState.HALF_OPEN:
            return not self.half_open_probe_in_flight
        return False


def sanitize_error(text: str) -> str:
    """Sanitizes error messages to ensure no API keys, tokens, or auth headers leak."""
    if not text:
        return ""
    text = re.sub(r"(?i)bearer\s+[a-zA-Z0-9_\-\.]+", "Bearer [REDACTED]", text)
    text = re.sub(
        r"(?i)(?:key|api_key|token|secret|password|auth|authorization)(?:[:=\s]+|[a-z_\s]+[:=\s]+)([a-zA-Z0-9_\-\.]{8,})",
        "[REDACTED_SECRET]",
        text,
    )
    text = re.sub(r"\b(?:ghp|gho|ghu|ghs|ghr|glpat|sk|nva)_[a-zA-Z0-9_\-\.]{10,}\b", "[REDACTED_SECRET]", text)
    text = re.sub(r"(?i)x-goog-api-key:\s*[^\s,;]+", "x-goog-api-key: [REDACTED]", text)
    if len(text) > 400:
        text = text[:400] + "... [truncated]"
    return text


def classify_error(exc: Exception) -> tuple[str, bool]:
    """
    Classifies provider errors for circuit breaking and failover.
    Returns (failure_class, is_retryable).
    Classes:
    - PAYLOAD_TOO_LARGE: HTTP 413 (not retryable across providers, immediately raise)
    - AUTH: HTTP 401/403 (permanent credential failure; immediately quarantined)
    - CONFIGURATION_ERROR: HTTP 404 (model endpoint not found), 410 (model retired/gone)
      (permanent configuration failure; immediately quarantined)
    - TRANSIENT: HTTP 408, 429, 5xx, timeouts, connect errors
      (transient failure; normal circuit-breaker threshold/cooldown behavior)
    - BAD_REQUEST: HTTP 400, 422 (failover allowed)
    - MALFORMED: unparseable output (failover allowed)
    - UNKNOWN: other (failover allowed)
    """
    if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError)):
        return "TRANSIENT", True

    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return "TRANSIENT", True

    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 413:
            return "PAYLOAD_TOO_LARGE", False
        if status in (401, 403):
            return "AUTH", True
        if status in (404, 410):
            return "CONFIGURATION_ERROR", True
        if status in (408, 429) or (500 <= status <= 599):
            return "TRANSIENT", True
        if 400 <= status < 500:
            return "BAD_REQUEST", True

    exc_str = str(exc).lower()
    if "413" in exc_str or "too large" in exc_str or "payload_too_large" in exc_str:
        return "PAYLOAD_TOO_LARGE", False

    if "401" in exc_str or "403" in exc_str or "unauthorized" in exc_str or "forbidden" in exc_str:
        return "AUTH", True

    if "404" in exc_str or "not found" in exc_str or "410" in exc_str or "retired" in exc_str or "gone" in exc_str:
        return "CONFIGURATION_ERROR", True

    if "429" in exc_str or "rate limit" in exc_str or "too many requests" in exc_str or "timeout" in exc_str:
        return "TRANSIENT", True

    if isinstance(exc, (KeyError, ValueError, IndexError, TypeError)):
        return "MALFORMED", True

    return "UNKNOWN", True


DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_COOLDOWN_SECONDS = 60.0


class HealthAwareProviderPool:
    """
    Deterministic health-aware round-robin load balancer with circuit breaking and automatic failover.
    Dispatches normal requests to exactly ONE healthy provider.
    Fails over to other healthy providers only upon eligible operational failures.
    Preserves strict HTTP 413 non-spray policy.
    """

    def __init__(
        self,
        endpoints: list[ProviderEndpoint | LLMProvider],
        role: str = "llm",
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not endpoints:
            raise LLMInvocationError(f"No endpoints configured for {role} provider pool")

        wrapped_endpoints: list[ProviderEndpoint] = []
        for ep in endpoints:
            if isinstance(ep, ProviderEndpoint):
                wrapped_endpoints.append(ep)
            else:
                p_name = getattr(ep, "provider_name", type(ep).__name__)
                m_name = getattr(ep, "model_name", getattr(ep, "model", "unknown"))
                wrapped_endpoints.append(
                    ProviderEndpoint(provider=ep, provider_name=p_name, model_name=m_name)
                )

        self.endpoints = wrapped_endpoints
        self.role = role
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.clock = clock
        self._cursor: int = 0
        self._lock = asyncio.Lock()
        self.last_selected_provider_name: str | None = None
        self.last_selected_model_name: str | None = None

    @property
    def provider_name(self) -> str:
        return self.last_selected_provider_name or "POOL"

    @property
    def model_name(self) -> str:
        return self.last_selected_model_name or "pool"

    async def complete(self, system: str, user: str) -> str:
        async with self._lock:
            now = self.clock()
            # 1. Inspect endpoints and update eligible status
            eligible_indices: list[int] = []
            for idx, ep in enumerate(self.endpoints):
                if ep.is_available(now):
                    eligible_indices.append(idx)

            if not eligible_indices:
                raise LLMInvocationError(
                    f"All {len(self.endpoints)} providers in {self.role} pool are unhealthy or in cooldown"
                )

            # 2. Deterministic round-robin selection starting from current cursor
            chosen_pos = 0
            for pos, idx in enumerate(eligible_indices):
                if idx >= self._cursor:
                    chosen_pos = pos
                    break

            # Re-order candidates starting with selected index
            ordered_indices = eligible_indices[chosen_pos:] + eligible_indices[:chosen_pos]

            # Advance cursor past the selected index for the next request
            selected_idx = ordered_indices[0]
            self._cursor = (selected_idx + 1) % len(self.endpoints)

            # If first candidate is HALF_OPEN, reserve its single probe slot
            selected_ep = self.endpoints[selected_idx]
            if selected_ep.circuit_state == CircuitState.HALF_OPEN:
                selected_ep.half_open_probe_in_flight = True

        # 3. Failover execution loop (Lock is NOT held during network invocation)
        failover_count = 0
        last_error = None

        for attempt, idx in enumerate(ordered_indices):
            ep = self.endpoints[idx]
            self.last_selected_provider_name = ep.provider_name
            self.last_selected_model_name = ep.model_name

            logger.info(
                "llm_pool_request_attempt",
                extra={
                    "role": self.role,
                    "selected_provider": ep.provider_name,
                    "selected_model": ep.model_name,
                    "attempt_number": attempt + 1,
                    "failover_count": failover_count,
                    "circuit_state": ep.circuit_state.value,
                },
            )

            t0 = time.perf_counter()
            try:
                result = await ep.provider.complete(system, user)
                latency_ms = round((time.perf_counter() - t0) * 1000, 2)

                # Reset health on success
                async with self._lock:
                    ep.consecutive_failures = 0
                    ep.circuit_state = CircuitState.CLOSED
                    ep.half_open_probe_in_flight = False

                logger.info(
                    "llm_pool_request_success",
                    extra={
                        "role": self.role,
                        "selected_provider": ep.provider_name,
                        "selected_model": ep.model_name,
                        "latency_ms": latency_ms,
                        "attempt_number": attempt + 1,
                        "failover_count": failover_count,
                    },
                )
                return result

            except Exception as exc:
                latency_ms = round((time.perf_counter() - t0) * 1000, 2)
                last_error = exc
                fail_class, _ = classify_error(exc)

                async with self._lock:
                    now = self.clock()
                    if fail_class == "PAYLOAD_TOO_LARGE":
                        # HTTP 413 Special Rule: Never spray across providers!
                        logger.error(
                            "llm_pool_payload_too_large",
                            extra={
                                "role": self.role,
                                "failed_provider": ep.provider_name,
                                "failed_model": ep.model_name,
                                "latency_ms": latency_ms,
                            },
                        )
                        raise LLMInvocationError(
                            f"Provider '{ep.provider_name}' rejected request as too large (HTTP 413). "
                            "Refusing to fail over to other providers for oversized request."
                        ) from exc

                    elif fail_class in ("AUTH", "CONFIGURATION_ERROR"):
                        # Permanent credential or configuration failure:
                        # Immediately quarantine endpoint so it is never retried on future normal requests
                        ep.quarantine(fail_class)

                    else:
                        ep.consecutive_failures += 1
                        ep.last_failure_type = fail_class
                        if (
                            ep.circuit_state == CircuitState.HALF_OPEN
                            or ep.consecutive_failures >= self.failure_threshold
                        ):
                            ep.circuit_state = CircuitState.OPEN
                            ep.cooldown_until = now + self.cooldown_seconds
                            ep.half_open_probe_in_flight = False

                can_failover = (attempt + 1) < len(ordered_indices)
                logger.warning(
                    "llm_pool_provider_failed",
                    extra={
                        "role": self.role,
                        "failed_provider": ep.provider_name,
                        "failed_model": ep.model_name,
                        "failure_class": fail_class,
                        "consecutive_failures": ep.consecutive_failures,
                        "circuit_state": ep.circuit_state.value,
                        "is_quarantined": ep.is_quarantined,
                        "will_failover": can_failover,
                        "latency_ms": latency_ms,
                    },
                )

                if can_failover:
                    failover_count += 1
                    # If next candidate is HALF_OPEN, reserve probe slot
                    next_ep = self.endpoints[ordered_indices[attempt + 1]]
                    async with self._lock:
                        if next_ep.circuit_state == CircuitState.HALF_OPEN:
                            next_ep.half_open_probe_in_flight = True
                    continue

        sanitized_err = sanitize_error(str(last_error))
        raise LLMInvocationError(
            f"All eligible providers in {self.role} pool exhausted without success. Last error: {sanitized_err}"
        ) from None


class GroqProvider:
    provider_name = "GROQ"

    def __init__(self, api_key: str, model: str | None = None, timeout: float = 10.0):
        self.api_key = api_key
        self.model = model or os.environ.get("GROQ_MODEL", DEFAULT_PROVIDER_MODELS["GROQ"])
        self.model_name = self.model
        self.timeout = timeout

    async def complete(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 413:
                    outbound_bytes = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                    raise LLMInvocationError(
                        f"Groq rejected payload as too large (HTTP 413): request size was {outbound_bytes} bytes. "
                        "Context must be partitioned or truncated before submission."
                    ) from None
                raise
            data = resp.json()
            return data["choices"][0]["message"]["content"]


class GeminiProvider:
    provider_name = "GEMINI"

    def __init__(self, api_key: str, model: str | None = None, timeout: float = 10.0):
        self.api_key = api_key
        self.model = model or os.environ.get("GEMINI_MODEL", DEFAULT_PROVIDER_MODELS["GEMINI"])
        self.model_name = self.model
        self.timeout = timeout

    async def complete(self, system: str, user: str) -> str:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"parts": [{"text": user}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                url,
                headers={"x-goog-api-key": self.api_key},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]


class MistralProvider:
    provider_name = "MISTRAL"

    def __init__(self, api_key: str, model: str | None = None, timeout: float = 10.0):
        self.api_key = api_key
        self.model = model or os.environ.get("MISTRAL_MODEL", DEFAULT_PROVIDER_MODELS["MISTRAL"])
        self.model_name = self.model
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
                        {"role": "user", "content": user},
                    ],
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]


class CohereProvider:
    provider_name = "COHERE"

    def __init__(self, api_key: str, model: str | None = None, timeout: float = 10.0):
        self.api_key = api_key
        self.model = model or os.environ.get("COHERE_MODEL", DEFAULT_PROVIDER_MODELS["COHERE"])
        self.model_name = self.model
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
                        {"role": "user", "content": user},
                    ],
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["message"]["content"][0]["text"]


class NvidiaNimProvider:
    provider_name = "NVIDIA"

    def __init__(self, api_key: str, model: str | None = None, timeout: float = 20.0):
        self.api_key = api_key
        self.model = model or os.environ.get("NVIDIA_MODEL", DEFAULT_PROVIDER_MODELS["NVIDIA"])
        self.model_name = self.model
        self.timeout = timeout

    async def complete(self, system: str, user: str) -> str:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                "https://integrate.api.nvidia.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.1,
                },
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
    """Raised when an explicitly configured provider lacks a required API key or is invalid."""
    pass


def get_provider_key(provider_name: str, role: str) -> str:
    """Resolves provider key for legacy single-provider mode."""
    role_upper = role.upper()
    role_key = os.environ.get(f"{role_upper}_API_KEY", "").strip()
    if role_key:
        return role_key

    norm_name = provider_name.upper().replace("-", "_")
    if norm_name == "NVIDIA_NIM":
        norm_name = "NVIDIA"

    key_envs = {
        "GROQ": ["GROQ_API_KEYS", "GROQ_API_KEY"],
        "GEMINI": ["GEMINI_API_KEYS", "GEMINI_API_KEY"],
        "MISTRAL": ["MISTRAL_API_KEYS", "MISTRAL_API_KEY"],
        "COHERE": ["COHERE_API_KEYS", "COHERE_API_KEY"],
        "NVIDIA": ["NVIDIA_API_KEYS", "NVIDIA_API_KEY"],
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


def get_pool_provider_key(provider_name: str, role: str) -> str:
    """
    Resolves provider key strictly for multi-provider pool mode.
    Does NOT borrow generic {ROLE}_API_KEY across distinct pool providers.
    Fails closed if the specific provider lacks an API key.
    """
    norm_name = provider_name.upper().replace("-", "_")
    if norm_name == "NVIDIA_NIM":
        norm_name = "NVIDIA"

    key_envs = {
        "GROQ": ["GROQ_API_KEYS", "GROQ_API_KEY"],
        "GEMINI": ["GEMINI_API_KEYS", "GEMINI_API_KEY"],
        "MISTRAL": ["MISTRAL_API_KEYS", "MISTRAL_API_KEY"],
        "COHERE": ["COHERE_API_KEYS", "COHERE_API_KEY"],
        "NVIDIA": ["NVIDIA_API_KEYS", "NVIDIA_API_KEY"],
    }.get(norm_name, [])

    for env_var in key_envs:
        val = os.environ.get(env_var, "").strip()
        if val:
            first_key = val.split(",")[0].strip()
            if first_key:
                return first_key

    role_upper = role.upper()
    raise ProviderConfigurationError(
        f"{role_upper}_PROVIDERS explicitly includes '{provider_name}' but no API key is configured in {key_envs}."
    )


def resolve_pool_model_id(role: str, provider_name: str) -> str:
    """
    Per-provider model resolution for multi-provider pool mode.
    Precedence:
    1. {ROLE}_{PROVIDER}_MODEL_ID (e.g. SCOUT_GROQ_MODEL_ID, VERIFIER_GEMINI_MODEL_ID)
    2. {PROVIDER}_MODEL (e.g. GROQ_MODEL, GEMINI_MODEL)
    3. DEFAULT_PROVIDER_MODELS[provider]
    Never applies generic {ROLE}_MODEL_ID to all pool providers.
    """
    role_upper = role.upper()
    norm_name = provider_name.upper().replace("-", "_")
    if norm_name == "NVIDIA_NIM":
        norm_name = "NVIDIA"

    # 1. {ROLE}_{PROVIDER}_MODEL_ID
    role_prov_model = os.environ.get(f"{role_upper}_{norm_name}_MODEL_ID", "").strip()
    if role_prov_model:
        return role_prov_model

    # 2. {PROVIDER}_MODEL
    prov_model = os.environ.get(f"{norm_name}_MODEL", "").strip()
    if prov_model:
        return prov_model

    # 3. Default model
    return DEFAULT_PROVIDER_MODELS.get(norm_name, DEFAULT_PROVIDER_MODELS.get("GROQ", "qwen/qwen3.6-27b"))


def resolve_role_timeout(role: str, explicit_timeout: float | None = None) -> float | None:
    """
    Timeout precedence rule:
    1. explicit function argument
    2. SCOUT_TIMEOUT / VERIFIER_TIMEOUT
    3. LLM_TIMEOUT_SECONDS (shared fallback)
    4. None (defer to provider class default)
    """
    if explicit_timeout is not None:
        return explicit_timeout

    role_upper = role.upper()
    role_timeout_env = os.environ.get(f"{role_upper}_TIMEOUT", "").strip()
    if role_timeout_env:
        try:
            return float(role_timeout_env)
        except ValueError:
            pass

    global_timeout_env = os.environ.get("LLM_TIMEOUT_SECONDS", "").strip()
    if global_timeout_env:
        try:
            return float(global_timeout_env)
        except ValueError:
            pass

    return None


def create_role_provider(
    role: str,
    provider_name: str | None = None,
    model_id: str | None = None,
    timeout: float | None = None,
) -> LLMProvider:
    """
    Creates an independent provider instance for a specific role (scout or verifier).
    Precedence:
    1. If both {ROLE}_PROVIDERS and {ROLE}_PROVIDER are set -> ProviderConfigurationError (fail closed)
    2. {ROLE}_PROVIDERS -> HealthAwareProviderPool with per-provider model/key resolution
    3. {ROLE}_PROVIDER or explicit provider_name -> single concrete provider
    4. Fallback -> MultiLLMProvider with configured providers
    """
    role_upper = role.upper()
    role_timeout = resolve_role_timeout(role, timeout)

    env_pool = os.environ.get(f"{role_upper}_PROVIDERS", "").strip()
    env_single = (provider_name or os.environ.get(f"{role_upper}_PROVIDER", "")).strip()

    # Rule: If both are configured, fail explicitly to avoid concealing configuration ambiguity
    if env_pool and env_single:
        raise ProviderConfigurationError(
            f"Both {role_upper}_PROVIDERS and {role_upper}_PROVIDER are configured. "
            f"Specify either {role_upper}_PROVIDERS for pool mode or {role_upper}_PROVIDER for single-provider mode, not both."
        )

    # 1. Multi-provider pool mode
    if env_pool:
        raw_names = [p.strip().upper() for p in env_pool.split(",") if p.strip()]
        if not raw_names:
            raise ProviderConfigurationError(f"{role_upper}_PROVIDERS was specified but is empty.")

        pool_endpoints: list[ProviderEndpoint] = []
        for p_name in raw_names:
            norm_name = p_name.replace("-", "_")
            cls = PROVIDER_CLASSES.get(norm_name)
            if not cls:
                raise ProviderConfigurationError(f"Unknown provider '{p_name}' in {role_upper}_PROVIDERS")

            api_key = get_pool_provider_key(norm_name, role)
            resolved_model = resolve_pool_model_id(role, norm_name)
            kwargs: dict[str, Any] = {"api_key": api_key, "model": resolved_model}
            if role_timeout is not None:
                kwargs["timeout"] = role_timeout

            instance = cls(**kwargs)
            pool_endpoints.append(
                ProviderEndpoint(
                    provider=instance,
                    provider_name=getattr(instance, "provider_name", norm_name),
                    model_name=getattr(instance, "model_name", resolved_model),
                )
            )

        return HealthAwareProviderPool(pool_endpoints, role=role)

    # 2. Legacy single-provider mode
    resolved_model = model_id or os.environ.get(f"{role_upper}_MODEL_ID")

    if env_single:
        prov_key = env_single.upper().replace("-", "_")
        cls = PROVIDER_CLASSES.get(prov_key)
        if not cls:
            raise ProviderConfigurationError(f"Unknown provider '{env_single}' for role '{role}'")
        api_key = get_provider_key(env_single, role)
        kwargs = {"api_key": api_key, "model": resolved_model}
        if role_timeout is not None:
            kwargs["timeout"] = role_timeout
        return cls(**kwargs)

    # 3. Fallback: load providers independently for this role (never share instances across roles)
    providers = load_providers(timeout=role_timeout)
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


def load_providers(timeout: float | None = None) -> List[LLMProvider]:
    providers: List[LLMProvider] = []

    def add_keys(env_var: str, provider_cls):
        keys_str = os.environ.get(env_var, "")
        if keys_str:
            for key in keys_str.split(","):
                key = key.strip()
                if key:
                    kwargs = {"api_key": key}
                    if timeout is not None:
                        kwargs["timeout"] = timeout
                    providers.append(provider_cls(**kwargs))

    add_keys("GROQ_API_KEYS", GroqProvider)
    add_keys("GEMINI_API_KEYS", GeminiProvider)
    add_keys("MISTRAL_API_KEYS", MistralProvider)
    add_keys("COHERE_API_KEYS", CohereProvider)
    add_keys("NVIDIA_API_KEYS", NvidiaNimProvider)

    if not providers:
        raise LLMInvocationError("No providers loaded")

    return providers


class MultiLLMProvider:
    """Ordered failover router preserved for backward compatibility with legacy tests."""

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
