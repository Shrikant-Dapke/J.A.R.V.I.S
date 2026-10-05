"""Typed LLM failures (safe messages only, never carry secrets)."""


class LLMError(Exception):
    """Base class for all provider failures."""


class LLMConfigurationError(LLMError):
    """Provider is misconfigured (e.g. missing API key)."""


class LLMProviderError(LLMError):
    """Provider/API call failed."""


class LLMTimeoutError(LLMError):
    """Provider call timed out."""


class LLMInvalidResponseError(LLMError):
    """Provider returned an unusable response."""
