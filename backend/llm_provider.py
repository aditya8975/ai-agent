"""
llm_provider.py — Multi-provider, multi-model LLM factory with automatic fallback.

Why this exists
----------------
Free LLM APIs rotate and rate-limit constantly (Groq alone deprecated 4 model
IDs between March and June 2026). Hard-coding one model/provider means the
whole agent breaks the day that model is retired or you hit a rate limit.

This module builds a *fallback chain* instead of a single model:

    Groq (quality model) -> Groq (backup model) -> Gemini -> OpenRouter free model

Only GROQ_API_KEY is required. Every other provider is optional — if its key
isn't set, it's silently skipped. Add more keys any time to get more
resilience for free; no code changes needed.

Two "kinds" of model are exposed so the graph can use cheap/fast models for
routing and a stronger model for the actual work:
    - "fast"    -> small model, used by the Router agent to classify intent
    - "quality" -> larger model, used by the specialist agents that do the work

Free keys
---------
Groq        (required)  https://console.groq.com/keys
Google      (optional)  https://aistudio.google.com/apikey
OpenRouter  (optional)  https://openrouter.ai/keys
Cerebras    (optional)  https://cloud.cerebras.ai  (same open models as Groq — good same-weights fallback)
"""

import os
import logging
from typing import Literal, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model catalogue. All overridable via env vars.
#
# NOTE ON GROQ DEPRECATIONS (checked 2026-07-14):
#   llama-3.1-8b-instant / llama-3.3-70b-versatile  -> shut down 2026-08-16
#   qwen/qwen3-32b / llama-4-scout-17b-16e-instruct  -> shut down 2026-07-17
# We default to openai/gpt-oss-20b / openai/gpt-oss-120b, Groq's own
# recommended replacements, which are NOT on the deprecation list.
# ---------------------------------------------------------------------------
GROQ_FAST_MODEL = os.getenv("GROQ_FAST_MODEL", "openai/gpt-oss-20b")
GROQ_QUALITY_MODEL = os.getenv("GROQ_QUALITY_MODEL", "openai/gpt-oss-120b")
# Same-provider backup so one bad model deploy doesn't take the agent down.
GROQ_QUALITY_MODEL_BACKUP = os.getenv("GROQ_QUALITY_MODEL_BACKUP", "llama-3.3-70b-versatile")

CEREBRAS_MODEL = os.getenv("CEREBRAS_MODEL", "gpt-oss-120b")
GOOGLE_MODEL = os.getenv("GOOGLE_MODEL", "gemini-2.0-flash")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.1-8b-instruct:free")


def _groq(model: str, temperature: float):
    key = os.getenv("GROQ_API_KEY")
    if not key:
        return None
    from langchain_groq import ChatGroq
    return ChatGroq(api_key=key, model=model, temperature=temperature, max_retries=2, timeout=60)


def _cerebras(temperature: float):
    """Cerebras hosts the same open-weight gpt-oss models as Groq — a true
    same-model fallback so responses don't change shape mid-incident."""
    key = os.getenv("CEREBRAS_API_KEY")
    if not key:
        return None
    try:
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            api_key=key,
            base_url="https://api.cerebras.ai/v1",
            model=CEREBRAS_MODEL,
            temperature=temperature,
        )
    except ImportError:
        log.warning("langchain-openai not installed; skipping Cerebras fallback.")
        return None


def _gemini(temperature: float):
    key = os.getenv("GOOGLE_API_KEY")
    if not key:
        return None
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(google_api_key=key, model=GOOGLE_MODEL, temperature=temperature)
    except ImportError:
        log.warning("langchain-google-genai not installed; skipping Gemini fallback. "
                     "Run: pip install langchain-google-genai")
        return None


def _openrouter(temperature: float):
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        return None
    try:
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            api_key=key,
            base_url="https://openrouter.ai/api/v1",
            model=OPENROUTER_MODEL,
            temperature=temperature,
        )
    except ImportError:
        log.warning("langchain-openai not installed; skipping OpenRouter fallback.")
        return None


def get_llm(kind: Literal["fast", "quality"] = "quality", tools: Optional[list] = None):
    """Return a chat-model runnable that automatically fails over across
    providers. Order: Groq primary -> Groq backup -> Cerebras -> Gemini -> OpenRouter.
    """
    temperature = 0.1 if kind == "fast" else 0.2
    candidates = []

    if kind == "fast":
        m = _groq(GROQ_FAST_MODEL, temperature)
        if m:
            candidates.append(m)
        m = _groq(GROQ_QUALITY_MODEL, temperature)  # step up rather than fail
        if m:
            candidates.append(m)
    else:
        m = _groq(GROQ_QUALITY_MODEL, temperature)
        if m:
            candidates.append(m)
        m = _groq(GROQ_QUALITY_MODEL_BACKUP, temperature)
        if m:
            candidates.append(m)

    m = _cerebras(temperature)
    if m:
        candidates.append(m)
    m = _gemini(temperature)
    if m:
        candidates.append(m)
    m = _openrouter(temperature)
    if m:
        candidates.append(m)

    if not candidates:
        raise ValueError(
            "No LLM provider configured. Set GROQ_API_KEY at minimum "
            "(free, no card required: https://console.groq.com/keys). "
            "Optionally add GOOGLE_API_KEY, OPENROUTER_API_KEY, or "
            "CEREBRAS_API_KEY for automatic fallback if Groq rate-limits you."
        )

    if tools:
        candidates = [c.bind_tools(tools) for c in candidates]

    primary, *rest = candidates
    return primary.with_fallbacks(rest) if rest else primary


def configured_providers() -> dict:
    """Report which providers are active, for a /status style health check."""
    return {
        "groq": bool(os.getenv("GROQ_API_KEY")),
        "cerebras": bool(os.getenv("CEREBRAS_API_KEY")),
        "google_gemini": bool(os.getenv("GOOGLE_API_KEY")),
        "openrouter": bool(os.getenv("OPENROUTER_API_KEY")),
    }
