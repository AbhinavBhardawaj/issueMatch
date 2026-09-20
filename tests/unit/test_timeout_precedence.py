import os
import pytest
from app.infrastructure.llm_router import (
    resolve_role_timeout,
    create_role_provider,
    DEFAULT_PROVIDER_MODELS,
    ProviderConfigurationError,
    GroqProvider,
    GeminiProvider,
    MistralProvider,
    CohereProvider,
    NvidiaNimProvider,
)


def test_timeout_precedence_explicit_argument(monkeypatch):
    """Explicit function argument takes highest precedence."""
    monkeypatch.setenv("SCOUT_TIMEOUT", "25.0")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "60.0")

    t = resolve_role_timeout("scout", explicit_timeout=15.0)
    assert t == 15.0


def test_timeout_precedence_role_env_over_global(monkeypatch):
    """Role-specific env var (SCOUT_TIMEOUT) overrides shared LLM_TIMEOUT_SECONDS."""
    monkeypatch.setenv("SCOUT_TIMEOUT", "35.5")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "60.0")

    t = resolve_role_timeout("scout", explicit_timeout=None)
    assert t == 35.5


def test_timeout_precedence_global_fallback(monkeypatch):
    """LLM_TIMEOUT_SECONDS acts as shared fallback when role-specific is unset."""
    monkeypatch.delenv("VERIFIER_TIMEOUT", raising=False)
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "45.0")

    t = resolve_role_timeout("verifier", explicit_timeout=None)
    assert t == 45.0


def test_timeout_precedence_default_none(monkeypatch):
    """When no timeout env vars are set, resolve_role_timeout returns None (deferring to provider)."""
    monkeypatch.delenv("SCOUT_TIMEOUT", raising=False)
    monkeypatch.delenv("LLM_TIMEOUT_SECONDS", raising=False)

    t = resolve_role_timeout("scout", explicit_timeout=None)
    assert t is None


def test_role_provider_applies_timeout(monkeypatch):
    """create_role_provider wires resolved timeout into provider instance."""
    monkeypatch.setenv("SCOUT_PROVIDER", "GROQ")
    monkeypatch.setenv("SCOUT_API_KEY", "test-key-123")
    monkeypatch.setenv("SCOUT_TIMEOUT", "42.0")

    provider = create_role_provider("scout")
    assert isinstance(provider, GroqProvider)
    assert provider.timeout == 42.0


def test_unknown_provider_raises_provider_configuration_error(monkeypatch):
    """Configuring an unknown provider name fails closed with ProviderConfigurationError."""
    monkeypatch.setenv("SCOUT_PROVIDER", "NON_EXISTENT_PROVIDER")
    monkeypatch.setenv("SCOUT_API_KEY", "test-key-123")

    with pytest.raises(ProviderConfigurationError) as exc_info:
        create_role_provider("scout")

    assert "Unknown provider" in str(exc_info.value)


def test_centralized_default_models():
    """All supported providers have centralized default models."""
    assert DEFAULT_PROVIDER_MODELS["GROQ"] == "llama-3.3-70b-versatile"
    assert DEFAULT_PROVIDER_MODELS["GEMINI"] == "gemini-2.0-flash"
    assert DEFAULT_PROVIDER_MODELS["MISTRAL"] == "mistral-large-latest"
    assert DEFAULT_PROVIDER_MODELS["COHERE"] == "command-r-plus"
    assert "llama" in DEFAULT_PROVIDER_MODELS["NVIDIA"].lower()
