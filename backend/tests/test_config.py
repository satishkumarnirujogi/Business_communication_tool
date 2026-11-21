import pytest

from app.config import ConfigError, get_settings, load_clinic
from app.llm.provider import LLMConfig, LLMError

CLINIC_YAML = """
name: Test Clinic
phone: "+49 30 1234"
timezone: Europe/Berlin
languages: [de, en]
"""


def test_clinic_yaml_loads_and_env_overrides(tmp_path, monkeypatch):
    path = tmp_path / "clinic.yaml"
    path.write_text(CLINIC_YAML, encoding="utf-8")
    c = load_clinic(path)
    assert (c.name, c.timezone, c.languages) == ("Test Clinic", "Europe/Berlin", ["de", "en"])

    monkeypatch.setenv("CLINIC_NAME", "Env Clinic")
    monkeypatch.setenv("CLINIC_BRANDING__PRIMARY_COLOR", "#123456")
    c = load_clinic(path)
    assert c.name == "Env Clinic" and c.branding.primary_color == "#123456"


def test_clinic_yaml_errors_are_readable(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_clinic(tmp_path / "missing.yaml")

    path = tmp_path / "clinic.yaml"
    path.write_text(CLINIC_YAML.replace("Europe/Berlin", "Mars/Olympus"), encoding="utf-8")
    with pytest.raises(ConfigError, match="not a known timezone"):
        load_clinic(path)


def test_invalid_env_setting_is_readable(monkeypatch):
    monkeypatch.setenv("LLM_TOOL_MODE", "sometimes")
    get_settings.cache_clear()
    try:
        with pytest.raises(ConfigError, match="LLM_TOOL_MODE"):
            get_settings()
    finally:
        monkeypatch.delenv("LLM_TOOL_MODE")
        get_settings.cache_clear()


def test_empty_env_values_use_defaults(monkeypatch):
    monkeypatch.setenv("LLM_TOOL_MODE", "")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "")
    get_settings.cache_clear()
    try:
        s = get_settings()
        assert s.llm_tool_mode == "auto" and s.llm_timeout_seconds == 60
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("provider,model,expected", [
    ("openrouter", "anthropic/claude-haiku-5.5", "openrouter/anthropic/claude-haiku-5.5"),
    ("openai", "gpt-4o-mini", "openai/gpt-4o-mini"),
    ("anthropic", "anthropic/claude-haiku-5-5", "anthropic/claude-haiku-5-5"),
    ("ollama", "qwen3", "ollama_chat/qwen3"),
    ("ollama", "ollama_chat/qwen3", "ollama_chat/qwen3"),
    ("azure", "my-deployment", "azure/my-deployment"),
])
def test_litellm_model_id(provider, model, expected):
    assert LLMConfig(provider=provider, model=model).litellm_model == expected


def test_missing_llm_settings_are_readable():
    with pytest.raises(LLMError, match="LLM_PROVIDER"):
        LLMConfig(provider="", model="x").check()
    with pytest.raises(LLMError, match="LLM_API_KEY"):
        LLMConfig(provider="openai", model="gpt-4o-mini").check()
    LLMConfig(provider="ollama", model="qwen3").check()  # local models need no key
