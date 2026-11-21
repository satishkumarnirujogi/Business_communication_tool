"""All settings in one place.

- `.env` (or real environment variables): secrets and deployment settings.
- `config/clinic.yaml`: the clinic profile. Any field can be overridden with a CLINIC_* env var.
- `config/rules.yaml`: business rules, loaded by app.rules.

Call `get_settings()` anywhere; it is loaded once and cached. Invalid settings raise
`ConfigError` with a message written for whoever is setting up the clinic.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

ROOT = Path(__file__).resolve().parents[2]  # repo root: backend/app/config.py -> ../../
ENV_FILE = ROOT / ".env"

ToolMode = Literal["auto", "native", "json"]
DataSource = Literal["sqlite", "postgres", "excel", "google_sheets"]


class ConfigError(RuntimeError):
    """A setting is missing or invalid. The message is meant for humans."""


def _format(err: ValidationError, source: str, env_names: bool = False) -> str:
    lines = [f"Configuration problem in {source}:"]
    for e in err.errors():
        field = ".".join(str(p) for p in e["loc"]) or "(file)"
        if env_names:
            field = field.upper()
        lines.append(f"  - {field}: {e['msg'].removeprefix('Value error, ')}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Clinic profile (config/clinic.yaml + CLINIC_* overrides)
# --------------------------------------------------------------------------
class Branding(BaseModel):
    logo_url: str = ""
    primary_color: str = "#0f9d94"


class ClinicProfile(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CLINIC_", env_nested_delimiter="__", env_file=ENV_FILE, env_ignore_empty=True, extra="ignore"
    )

    name: str = Field(min_length=1)
    phone: str = Field(min_length=3)
    email: str = ""
    address: str = ""
    website: str = ""
    timezone: str = "UTC"
    languages: list[str] = ["en"]
    currency: str = "EUR"
    branding: Branding = Branding()

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"'{v}' is not a known timezone. Use a name like 'Europe/Berlin' or 'UTC'.") from None
        return v

    @field_validator("languages")
    @classmethod
    def _langs(cls, v: list[str]) -> list[str]:
        v = [x.strip().lower() for x in v if x.strip()]
        if not v:
            raise ValueError("list at least one language, e.g. [en]")
        return v

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Priority: explicit args > real env vars > .env > clinic.yaml
        sources: tuple[PydanticBaseSettingsSource, ...] = (init_settings, env_settings, dotenv_settings)
        if settings_cls.model_config.get("yaml_file"):
            sources += (YamlConfigSettingsSource(settings_cls),)
        return sources

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def load_clinic(path: Path) -> ClinicProfile:
    if not path.is_file():
        raise ConfigError(
            f"Clinic profile not found at {path}. Copy config/clinic.yaml from the repository "
            "or set CONFIG_DIR to the folder that contains it."
        )
    # Bind the YAML path on a throwaway subclass so the base class stays path-free.
    bound = type(
        "ClinicProfile",
        (ClinicProfile,),
        {"model_config": {**ClinicProfile.model_config, "yaml_file": path, "yaml_file_encoding": "utf-8"}},
    )
    try:
        return bound()
    except ValidationError as e:
        raise ConfigError(_format(e, str(path))) from None
    except Exception as e:  # malformed YAML
        raise ConfigError(f"Could not read {path}: {e}") from None


# --------------------------------------------------------------------------
# App settings (.env)
# --------------------------------------------------------------------------
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", env_ignore_empty=True, extra="ignore")

    # LLM (any provider LiteLLM supports)
    llm_provider: str = ""
    llm_model: str = ""
    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = None
    llm_tool_mode: ToolMode = "auto"
    llm_timeout_seconds: float = Field(60, gt=0, le=600)
    llm_temperature: float = Field(0.2, ge=0, le=2)
    llm_max_tokens: int = Field(2000, ge=64, le=32000)

    # Data source
    data_source: DataSource = "sqlite"
    sqlite_path: Path = ROOT / "data" / "clinic.db"
    database_url: SecretStr | None = None  # postgres
    supabase_pooler_host: str | None = None  # informational only
    excel_path: Path = ROOT / "data" / "clinic.xlsx"
    google_sheets_id: str = ""
    google_service_account_file: Path | None = None
    sync_interval_seconds: int = Field(120, ge=15, le=3600)

    # Knowledge base
    embedding_provider: Literal["local", "litellm"] = "local"
    embedding_model: str = ""  # local default: paraphrase-multilingual-MiniLM-L12-v2
    embedding_api_key: SecretStr | None = None  # litellm only; falls back to LLM_API_KEY
    embedding_base_url: str | None = None
    kb_vector_store: Literal["auto", "chroma", "pgvector"] = "auto"
    chroma_path: Path = ROOT / "data" / "chroma"
    chroma_host: str = ""  # a Chroma server (Docker: "chroma"); empty = embedded Chroma in CHROMA_PATH
    chroma_port: int = Field(8000, gt=0, lt=65536)
    kb_min_score: float = Field(0.30, ge=0, le=1)  # minimum similarity for a passage to be shown to the model
    kb_allow_private_urls: bool = False

    # Admin
    admin_token: SecretStr | None = None

    # Channels (Phase 7). PUBLIC_BASE_URL is the address the internet reaches this server at, e.g.
    # https://assistant.your-clinic.example; webhooks and Twilio's signature check depend on it.
    public_base_url: str = ""
    whatsapp_provider: Literal["none", "twilio", "meta"] = "none"
    twilio_account_sid: str = ""
    twilio_auth_token: SecretStr | None = None
    twilio_whatsapp_number: str = ""          # the clinic's WhatsApp sender, e.g. +4930123456
    twilio_reminder_content_sid: str = ""     # approved content template for reminders (HX...)
    whatsapp_access_token: SecretStr | None = None  # Meta Cloud API
    whatsapp_phone_number_id: str = ""
    whatsapp_app_secret: SecretStr | None = None
    whatsapp_verify_token: SecretStr | None = None
    whatsapp_graph_version: str = "v23.0"
    whatsapp_reminder_template: str = ""      # approved template name; language = the patient's (en/de)
    email_enabled: bool = False
    email_address: str = ""                   # the clinic inbox the assistant reads and answers from
    email_display_name: str = ""
    imap_host: str = ""
    imap_port: int = Field(993, gt=0, lt=65536)
    imap_user: str = ""
    imap_password: SecretStr | None = None
    imap_folder: str = "INBOX"
    smtp_host: str = ""
    smtp_port: int = Field(587, gt=0, lt=65536)
    smtp_user: str = ""
    smtp_password: SecretStr | None = None
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"
    email_poll_seconds: int = Field(60, ge=15, le=3600)
    reminders_enabled: bool = True
    reminder_send_time: str = "10:00"         # clinic local time, the day before the appointment
    reminder_min_hours: float = Field(3, ge=0, le=48)  # skip reminders for appointments closer than this

    # Demo mode: an empty database is filled with the demo clinic and documents on startup
    demo_mode: bool = False

    # Files and logging
    config_dir: Path = ROOT / "config"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("llm_provider")
    @classmethod
    def _provider(cls, v: str) -> str:
        return v.strip().lower()

    @field_validator("llm_base_url", "llm_api_key", "database_url", "admin_token", "embedding_api_key",
                     "embedding_base_url", "twilio_auth_token", "whatsapp_access_token", "whatsapp_app_secret",
                     "whatsapp_verify_token", "imap_password", "smtp_password", mode="before")
    @classmethod
    def _empty_is_none(cls, v):
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("data_source", mode="before")
    @classmethod
    def _source(cls, v):
        return v.strip().lower().replace("-", "_").replace("googlesheets", "google_sheets") if isinstance(v, str) else v

    @field_validator("reminder_send_time")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        try:
            h, m = (int(x) for x in v.strip().split(":"))
            assert 0 <= h < 24 and 0 <= m < 60
        except (ValueError, AssertionError):
            raise ValueError(f"'{v}' is not a time of day. Use HH:MM, e.g. 10:00.") from None
        return f"{h:02d}:{m:02d}"

    @field_validator("public_base_url")
    @classmethod
    def _base_url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if v and not v.startswith(("http://", "https://")):
            raise ValueError("must start with https:// (for example https://assistant.your-clinic.example)")
        return v

    @field_validator("embedding_provider", "kb_vector_store", "whatsapp_provider", "smtp_security", mode="before")
    @classmethod
    def _lower(cls, v):
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("sqlite_path", "excel_path", "google_service_account_file", "config_dir", "chroma_path")
    @classmethod
    def _relative_to_root(cls, v: Path | None) -> Path | None:
        # Relative paths in .env are relative to the project folder, not wherever the server was started.
        return v if v is None or v.is_absolute() else (ROOT / v).resolve()

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper(cls, v):
        return v.upper() if isinstance(v, str) else v

    @property
    def rules_file(self) -> Path:
        return self.config_dir / "rules.yaml"

    @property
    def clinic_file(self) -> Path:
        return self.config_dir / "clinic.yaml"

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_provider and self.llm_model)

    def secret(self, name: str) -> str | None:
        value = getattr(self, name)
        return value.get_secret_value() if value is not None else None

    # loaded after the env settings, because its location depends on config_dir
    _clinic: ClinicProfile | None = None

    @property
    def clinic(self) -> ClinicProfile:
        if self._clinic is None:
            self._clinic = load_clinic(self.clinic_file)
        return self._clinic


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        s = Settings()
    except ValidationError as e:
        raise ConfigError(_format(e, f"environment / {ENV_FILE.name}", env_names=True)) from None
    s.clinic  # noqa: B018 - load now so problems surface at startup, not mid-chat
    return s


def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()
