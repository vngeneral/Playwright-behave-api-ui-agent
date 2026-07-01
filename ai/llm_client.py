"""
LLM Client — Unified AI Provider Interface
==========================================
Replaces the direct ``ollama`` dependency with a provider-agnostic abstraction
that works with the same providers supported by OpenCode.ai.

Supported providers (set ``AI_PROVIDER`` env var):
┌─────────────┬──────────────────────────────────────────────────────────────┐
│ Provider    │ Notes                                                        │
├─────────────┼──────────────────────────────────────────────────────────────┤
│ anthropic   │ Anthropic Claude — matches OpenCode.ai Zen default           │
│             │ Requires: AI_API_KEY (Anthropic key)                         │
│             │ Models: claude-3-5-haiku-20241022, claude-3-5-sonnet-*       │
├─────────────┼──────────────────────────────────────────────────────────────┤
│ openai      │ OpenAI GPT or any OpenAI-compatible endpoint                 │
│             │ Requires: AI_API_KEY, optional AI_BASE_URL                   │
│             │ Covers: OpenAI, Azure OpenAI, Groq, Mistral, etc.            │
│             │ Also covers local Ollama via http://localhost:11434/v1        │
├─────────────┼──────────────────────────────────────────────────────────────┤
│ stub        │ No-op (returns empty string) — for CI without AI creds       │
└─────────────┴──────────────────────────────────────────────────────────────┘

Migration from Ollama:
  Old: ollama.generate(model=model, prompt=prompt, images=[path])
  New: LLMClient.from_config().generate(prompt=prompt, images=[path])

  To keep using Ollama locally:
    AI_PROVIDER=openai
    AI_BASE_URL=http://localhost:11434/v1
    AI_API_KEY=ollama          # any non-empty string
    AI_MODEL=devstral:24b      # or any model pulled with `ollama pull`

Environment variables:
    AI_PROVIDER    — anthropic | openai | stub   (default: anthropic)
    AI_API_KEY     — API key for the selected provider
    AI_BASE_URL    — Base URL override (openai provider only)
    AI_MODEL       — Model name override (overrides config.yaml ai_model)
"""
from __future__ import annotations

import base64
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from utils.logger import log_info_emoji, log_warning
from utils.misc import load_config

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ENV_PROVIDER = "AI_PROVIDER"
_ENV_API_KEY  = "AI_API_KEY"
_ENV_BASE_URL = "AI_BASE_URL"
_ENV_MODEL    = "AI_MODEL"

_DEFAULT_PROVIDER = "anthropic"

# Model defaults per provider — override with AI_MODEL env var or config.yaml ai_model
_DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-3-5-haiku-20241022",
    "openai":    "gpt-4o",
    "stub":      "stub",
}


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class BaseLLMClient(ABC):
    """Common interface all provider clients must implement."""

    @abstractmethod
    def generate(
        self,
        prompt: str,
        system: str = "",
        images: Optional[list[str]] = None,
        temperature: float = 0.1,
        max_tokens: int = 2048,
    ) -> str:
        """
        Generate a text response.

        Args:
            prompt:      The user-turn message / instruction.
            system:      System prompt (role/persona/instructions).
            images:      List of local file paths to include as image attachments.
                         Supported by anthropic and openai (vision) providers.
            temperature: Sampling temperature (0 = deterministic, 1 = creative).
            max_tokens:  Maximum tokens in the response.

        Returns:
            The model's text response, stripped of leading/trailing whitespace.
        """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Human-readable provider name for log messages."""


# ---------------------------------------------------------------------------
# Anthropic (Claude)
# ---------------------------------------------------------------------------


class AnthropicLLMClient(BaseLLMClient):
    """
    Anthropic Claude client.

    Requires:
        pip install anthropic
        AI_API_KEY=sk-ant-...

    Default model: claude-3-5-haiku-20241022  (fast + cheap, great for QA tasks)
    For vision:    claude-3-5-sonnet-20241022  (if image quality matters)
    """

    provider_name = "anthropic"

    def __init__(self, api_key: str, model: str) -> None:
        try:
            import anthropic as _anthropic
        except ImportError as exc:
            raise ImportError(
                "anthropic package not installed. Run: pip install anthropic"
            ) from exc
        self._client = _anthropic.Anthropic(api_key=api_key)
        self._model  = model

    def generate(
        self,
        prompt: str,
        system: str = "",
        images: Optional[list[str]] = None,
        temperature: float = 0.1,
        max_tokens: int = 2048,
    ) -> str:
        content: list = []

        # Attach images first (Anthropic expects image before text in content)
        for path in (images or []):
            img_block = _encode_image_anthropic(path)
            if img_block:
                content.append(img_block)

        content.append({"type": "text", "text": prompt})

        kwargs: dict = {
            "model":      self._model,
            "max_tokens": max_tokens,
            "messages":   [{"role": "user", "content": content}],
        }
        if system:
            kwargs["system"] = system

        # anthropic doesn't accept temperature in create() directly —
        # it uses the top-level temperature kwarg added in newer SDK versions
        kwargs["temperature"] = temperature

        response = self._client.messages.create(**kwargs)
        return response.content[0].text.strip()


# ---------------------------------------------------------------------------
# OpenAI / OpenAI-compatible (covers Ollama, Groq, Mistral, Azure, etc.)
# ---------------------------------------------------------------------------


class OpenAILLMClient(BaseLLMClient):
    """
    OpenAI-compatible client.

    Works with:
      - OpenAI (api.openai.com)
      - Ollama  (AI_BASE_URL=http://localhost:11434/v1, AI_API_KEY=ollama)
      - Groq    (AI_BASE_URL=https://api.groq.com/openai/v1)
      - Mistral (AI_BASE_URL=https://api.mistral.ai/v1)
      - Azure OpenAI (AI_BASE_URL=https://<resource>.openai.azure.com/openai/deployments/<deploy>)

    Requires:
        pip install openai
        AI_API_KEY=sk-...
        AI_BASE_URL=... (optional — leave unset for api.openai.com)
    """

    provider_name = "openai"

    def __init__(self, api_key: str, model: str, base_url: Optional[str] = None) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ImportError(
                "openai package not installed. Run: pip install openai"
            ) from exc
        self._client = OpenAI(api_key=api_key, base_url=base_url or None)
        self._model  = model

    def generate(
        self,
        prompt: str,
        system: str = "",
        images: Optional[list[str]] = None,
        temperature: float = 0.1,
        max_tokens: int = 2048,
    ) -> str:
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})

        user_content: list = []
        for path in (images or []):
            img_block = _encode_image_openai(path)
            if img_block:
                user_content.append(img_block)
        user_content.append({"type": "text", "text": prompt})

        messages.append({"role": "user", "content": user_content})

        response = self._client.chat.completions.create(
            model=model     if (model := self._model) else "gpt-4o",
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return (response.choices[0].message.content or "").strip()


# ---------------------------------------------------------------------------
# Stub (no-op for CI without AI credentials)
# ---------------------------------------------------------------------------


class StubLLMClient(BaseLLMClient):
    """
    No-op client — returns empty string for all requests.

    Use when AI_PROVIDER=stub or when no API key is available in CI.
    The framework degrades gracefully: selector healing simply returns ""
    and the test retries normally without AI assistance.
    """

    provider_name = "stub"

    def generate(self, prompt: str, system: str = "", images=None,
                 temperature: float = 0.1, max_tokens: int = 2048) -> str:
        log_warning("[LLMClient/stub] AI disabled — returning empty response")
        return ""


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


class LLMClient:
    """
    Factory — construct the right provider from environment + config.

    Usage::

        client = LLMClient.from_config()
        response = client.generate(
            prompt="What selector matches the login button?",
            system="You are a QA engineer. Return only JSON.",
            images=["/path/to/screenshot.png"],
        )

    Selection order for provider:
        1. AI_PROVIDER env var
        2. config.yaml  ai_provider key
        3. Default: anthropic

    Selection order for model:
        1. AI_MODEL env var
        2. config.yaml ai_model key
        3. Provider-specific default (e.g. claude-3-5-haiku-20241022)
    """

    @staticmethod
    def from_config() -> BaseLLMClient:
        cfg      = load_config()
        provider = (
            os.getenv(_ENV_PROVIDER)
            or cfg.get("ai_provider", _DEFAULT_PROVIDER)
        ).lower()

        model = (
            os.getenv(_ENV_MODEL)
            or cfg.get("ai_model")
            or _DEFAULT_MODELS.get(provider, "")
        )

        api_key  = os.getenv(_ENV_API_KEY, "")
        base_url = os.getenv(_ENV_BASE_URL, "")

        log_info_emoji("🤖", f"LLMClient provider={provider} model={model}")

        if provider == "anthropic":
            if not api_key:
                log_warning(
                    "AI_API_KEY not set for anthropic provider — falling back to stub. "
                    "Set AI_API_KEY=sk-ant-... or AI_PROVIDER=stub to suppress this warning."
                )
                return StubLLMClient()
            return AnthropicLLMClient(api_key=api_key, model=model)

        if provider == "openai":
            if not api_key:
                log_warning(
                    "AI_API_KEY not set for openai provider — falling back to stub."
                )
                return StubLLMClient()
            return OpenAILLMClient(api_key=api_key, model=model, base_url=base_url or None)

        if provider == "stub":
            return StubLLMClient()

        log_warning(
            f"Unknown AI_PROVIDER={provider!r}. "
            f"Valid values: anthropic, openai, stub. Falling back to stub."
        )
        return StubLLMClient()


# ---------------------------------------------------------------------------
# Image encoding helpers
# ---------------------------------------------------------------------------


def _encode_image_anthropic(path: str) -> Optional[dict]:
    """Encode a local image file as an Anthropic content block."""
    try:
        data = Path(path).read_bytes()
        b64  = base64.standard_b64encode(data).decode()
        # Infer media type from extension
        ext  = Path(path).suffix.lower()
        media_type = {
            ".png":  "image/png",
            ".jpg":  "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif":  "image/gif",
            ".webp": "image/webp",
        }.get(ext, "image/png")
        return {
            "type": "image",
            "source": {
                "type":       "base64",
                "media_type": media_type,
                "data":       b64,
            },
        }
    except (OSError, Exception) as exc:
        log_warning(f"Could not encode image {path}: {exc}")
        return None


def _encode_image_openai(path: str) -> Optional[dict]:
    """Encode a local image file as an OpenAI vision content block."""
    try:
        data = Path(path).read_bytes()
        b64  = base64.standard_b64encode(data).decode()
        ext  = Path(path).suffix.lower()
        media_type = {
            ".png":  "image/png",
            ".jpg":  "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif":  "image/gif",
            ".webp": "image/webp",
        }.get(ext, "image/png")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{media_type};base64,{b64}"},
        }
    except (OSError, Exception) as exc:
        log_warning(f"Could not encode image {path}: {exc}")
        return None
