"""One door to every LLM provider, via LiteLLM.

Configure with LLM_PROVIDER / LLM_MODEL / LLM_API_KEY / LLM_BASE_URL (see .env.example).
Errors are turned into `LLMError`, whose message a receptionist can act on.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

# Use LiteLLM's bundled model list instead of downloading it on import (offline-friendly, no phoning home).
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import litellm  # noqa: E402

from ..config import Settings, ToolMode, get_settings  # noqa: E402

log = logging.getLogger("clinic.llm")

litellm.drop_params = True  # silently skip params a model doesn't accept (e.g. temperature on reasoning models)
litellm.suppress_debug_info = True
for _name in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy", "litellm"):
    logging.getLogger(_name).setLevel(logging.WARNING)

# LiteLLM model prefixes per provider name. Anything not listed is used as-is
# (LiteLLM knows ~100 providers: mistral, groq, gemini, bedrock, ...).
PREFIX = {
    "ollama": "ollama_chat",  # the chat endpoint supports native tool calls
    "azure_openai": "azure",
    "azure-openai": "azure",
}
NO_KEY_PROVIDERS = {"ollama", "ollama_chat", "lm_studio", "vllm", "hosted_vllm"}


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    model: str
    api_key: str | None = None
    base_url: str | None = None
    tool_mode: ToolMode = "auto"
    timeout: float = 60
    temperature: float = 0.2
    max_tokens: int = 2000

    @classmethod
    def from_settings(cls, s: Settings | None = None) -> LLMConfig:
        s = s or get_settings()
        return cls(
            provider=s.llm_provider,
            model=s.llm_model.strip(),
            api_key=s.secret("llm_api_key"),
            base_url=s.llm_base_url,
            tool_mode=s.llm_tool_mode,
            timeout=s.llm_timeout_seconds,
            temperature=s.llm_temperature,
            max_tokens=s.llm_max_tokens,
        )

    @property
    def litellm_model(self) -> str:
        """'openrouter' + 'anthropic/claude-haiku-5.5' -> 'openrouter/anthropic/claude-haiku-5.5'."""
        prefix = PREFIX.get(self.provider, self.provider)
        if not prefix or self.model.startswith(f"{prefix}/") or self.model.startswith(f"{self.provider}/"):
            return self.model
        return f"{prefix}/{self.model}"

    def check(self) -> None:
        if not self.provider:
            raise LLMError("No AI provider is set. Add LLM_PROVIDER to .env (for example openrouter, openai, anthropic or ollama).")
        if not self.model:
            raise LLMError("No AI model is set. Add LLM_MODEL to .env (for example gpt-4o-mini).")
        if not self.api_key and self.provider not in NO_KEY_PROVIDERS:
            raise LLMError(f"No API key for {self.provider}. Add LLM_API_KEY to .env.")


class LLMError(RuntimeError):
    """Readable error for staff. `detail` keeps the raw provider message for the logs."""

    def __init__(self, message: str, detail: str = "", tools_unsupported: bool = False):
        super().__init__(message)
        self.detail = detail
        self.tools_unsupported = tools_unsupported


_PROVIDER_MSG = re.compile(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _provider_message(e: Exception) -> str:
    """The provider's own error text, without LiteLLM prefixes or account ids."""
    m = _PROVIDER_MSG.search(str(e))
    text = m.group(1) if m else str(e).split(" - ", 1)[-1]
    return text.strip()[:200]


def _readable(e: Exception, cfg: LLMConfig) -> LLMError:
    raw = f"{type(e).__name__}: {e}"[:1000]
    where = cfg.base_url or cfg.provider
    text = str(e).lower()
    if isinstance(e, litellm.AuthenticationError):
        msg = f"The API key was rejected by {cfg.provider}. Check LLM_API_KEY."
    elif isinstance(e, litellm.PermissionDeniedError):
        msg = f"{cfg.provider} refused access for this key. Check that the key may use model '{cfg.model}'."
    elif isinstance(e, litellm.NotFoundError):
        msg = f"Model '{cfg.model}' was not found at {cfg.provider}. Check LLM_MODEL."
    elif isinstance(e, litellm.RateLimitError):
        msg = f"{cfg.provider} says the rate limit or credit is exhausted. Wait a minute or check your account balance."
    elif isinstance(e, litellm.Timeout):
        msg = f"{where} did not answer within {cfg.timeout:.0f} seconds. Try again or raise LLM_TIMEOUT_SECONDS."
    elif isinstance(e, litellm.APIConnectionError):
        msg = (f"Can't reach {cfg.base_url}. Check that the server there is running and the address is right."
               if cfg.base_url else f"Can't reach {cfg.provider}. Check the server's internet connection.")
    elif isinstance(e, litellm.ContextWindowExceededError):
        msg = "The conversation is too long for this model. Start a new chat."
    elif isinstance(e, litellm.BadRequestError):
        if any(w in text for w in ("tool", "function")) and any(
            w in text for w in ("support", "not allowed", "unsupported", "invalid")
        ):
            return LLMError(f"Model '{cfg.model}' does not accept tool calls.", raw, tools_unsupported=True)
        if "provider" in text and ("not provided" in text or "llm provider" in text):
            msg = f"LiteLLM doesn't recognise the provider '{cfg.provider}'. Check LLM_PROVIDER."
        elif "model" in text and any(w in text for w in ("not a valid model", "model not found", "does not exist", "unknown model")):
            msg = f"Model '{cfg.model}' was not found at {cfg.provider}. Check LLM_MODEL."
        else:
            msg = f"{cfg.provider} rejected the request: {_provider_message(e)}"
    elif isinstance(e, (litellm.ServiceUnavailableError, litellm.InternalServerError)):
        msg = f"{cfg.provider} is having problems right now. Try again in a few minutes."
    else:
        msg = f"The AI provider returned an unexpected error ({type(e).__name__})."
    return LLMError(msg, raw)


# Models that rejected native tool calls at runtime in "auto" mode; they use the JSON fallback from then on.
_json_models: set[str] = set()
_json_lock = threading.Lock()


def use_native_tools(cfg: LLMConfig) -> bool:
    if cfg.tool_mode == "native":
        return True
    if cfg.tool_mode == "json":
        return False
    with _json_lock:
        return cfg.litellm_model not in _json_models


def mark_tools_unsupported(cfg: LLMConfig) -> None:
    if cfg.tool_mode == "auto":
        with _json_lock:
            _json_models.add(cfg.litellm_model)
        log.warning("model %s rejected tool calls; switching to JSON tool mode", cfg.litellm_model)


def listed_tool_support(cfg: LLMConfig) -> bool | None:
    """What LiteLLM's model list says about native tool calls (None = model not listed)."""
    try:
        if litellm.supports_function_calling(model=cfg.litellm_model):
            return True
        litellm.get_model_info(cfg.litellm_model)  # raises if unknown
        return False
    except Exception:
        return None


def complete(
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    cfg: LLMConfig | None = None,
    **overrides: Any,
):
    """Single chat completion. Returns LiteLLM's OpenAI-shaped ModelResponse. Raises LLMError."""
    cfg = cfg or LLMConfig.from_settings()
    cfg.check()
    kwargs: dict[str, Any] = {
        "model": cfg.litellm_model,
        "messages": messages,
        "timeout": cfg.timeout,
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
        "num_retries": 1,
    }
    if cfg.api_key:
        kwargs["api_key"] = cfg.api_key
    if cfg.base_url:
        kwargs["api_base"] = cfg.base_url
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    if cfg.provider == "openrouter":
        kwargs["extra_headers"] = {"X-Title": "Clinic Assistant"}
    kwargs.update(overrides)
    try:
        return litellm.completion(**kwargs)
    except LLMError:
        raise
    except Exception as e:  # every provider error ends up here
        raise _readable(e, cfg) from e


def test_connection(cfg: LLMConfig | None = None) -> dict:
    """Send a tiny prompt and report success or a readable error. Never raises."""
    cfg = cfg or LLMConfig.from_settings()
    info = {"provider": cfg.provider, "model": cfg.model, "litellm_model": cfg.litellm_model if cfg.provider else ""}
    started = time.perf_counter()
    try:
        resp = complete(
            [{"role": "user", "content": "Reply with the single word: pong"}],
            cfg=cfg, max_tokens=50, timeout=min(cfg.timeout, 20), num_retries=0,
        )
    except LLMError as e:
        log.info("LLM test failed: %s", e.detail or e)
        return {"ok": False, **info, "error": str(e)}
    reply = (resp.choices[0].message.content or "").strip()
    return {
        "ok": True,
        **info,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "reply": reply[:100],
        "tool_mode": cfg.tool_mode,
        "native_tools_listed": listed_tool_support(cfg),
    }
