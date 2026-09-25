"""Configuration for the local HSSE chatbot."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ModelProfile:
    """One selectable OpenAI-compatible model backend."""

    key: str
    label: str
    model_id: str
    base_url: str


MODEL_PROFILES = (
    ModelProfile(
        "llama31",
        "Meta Llama 3.1 8B Instruct",
        "bartowski/Meta-Llama-3.1-8B-Instruct-GGUF:Q4_K_M",
        "http://127.0.0.1:8080/v1",
    ),
    ModelProfile(
        "mistral",
        "Mistral 7B Instruct v0.3",
        "bartowski/Mistral-7B-Instruct-v0.3-GGUF:Q4_K_M",
        "http://127.0.0.1:8082/v1",
    ),
)
DEFAULT_MODEL_KEY = "llama31"
DEFAULT_MODEL_ID = MODEL_PROFILES[0].model_id
DEFAULT_TRANSFORMERS_MODEL_ID = "meta-llama/Meta-Llama-3.1-8B-Instruct"


def _environment_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ChatbotConfig:
    """Runtime settings with local-first defaults."""

    project_root: Path
    database_path: Path
    user_database_path: Path
    model_key: str = DEFAULT_MODEL_KEY
    model_id: str = DEFAULT_MODEL_ID
    backend: str = "llama-cpp"
    model_base_url: str = "http://127.0.0.1:8080/v1"
    top_k: int = 6
    max_new_tokens: int = 512
    temperature: float = 0.2
    enable_thinking: bool = False
    local_files_only: bool = False

    def __post_init__(self) -> None:
        if self.backend not in {"llama-cpp", "transformers"}:
            raise ValueError("backend must be 'llama-cpp' or 'transformers'")
        if self.top_k <= 0:
            raise ValueError("top_k must be positive")
        if self.max_new_tokens <= 0:
            raise ValueError("max_new_tokens must be positive")
        if self.temperature < 0:
            raise ValueError("temperature cannot be negative")

    @classmethod
    def from_project_root(
        cls,
        project_root: Path,
        *,
        database_path: Path | None = None,
        user_database_path: Path | None = None,
        model_id: str | None = None,
        backend: str | None = None,
        model_base_url: str | None = None,
        top_k: int | None = None,
        max_new_tokens: int | None = None,
        temperature: float | None = None,
        enable_thinking: bool | None = None,
        local_files_only: bool | None = None,
    ) -> "ChatbotConfig":
        root = project_root.resolve()
        database_value = database_path or Path(
            os.environ.get(
                "HSSE_DATABASE_PATH", "data/processed/hsse_incidents.sqlite"
            )
        )
        user_database_value = user_database_path or Path(
            os.environ.get(
                "HSSE_USER_DATABASE_PATH", "data/local/user_cases.sqlite"
            )
        )
        resolved_database = (
            database_value
            if database_value.is_absolute()
            else root / database_value
        )
        resolved_user_database = (
            user_database_value
            if user_database_value.is_absolute()
            else root / user_database_value
        )
        resolved_backend = backend or os.environ.get(
            "HSSE_MODEL_BACKEND", "llama-cpp"
        )
        default_model_id = (
            DEFAULT_TRANSFORMERS_MODEL_ID
            if resolved_backend == "transformers"
            else DEFAULT_MODEL_ID
        )
        return cls(
            project_root=root,
            database_path=resolved_database.resolve(),
            user_database_path=resolved_user_database.resolve(),
            model_id=model_id or os.environ.get("HSSE_MODEL_ID", default_model_id),
            backend=resolved_backend,
            model_base_url=(
                model_base_url
                or os.environ.get(
                    "HSSE_MODEL_BASE_URL", "http://127.0.0.1:8080/v1"
                )
            ).rstrip("/"),
            top_k=(top_k if top_k is not None else int(os.environ.get("HSSE_TOP_K", "6"))),
            max_new_tokens=(
                max_new_tokens
                if max_new_tokens is not None
                else int(os.environ.get("HSSE_MAX_NEW_TOKENS", "512"))
            ),
            temperature=(
                temperature
                if temperature is not None
                else float(os.environ.get("HSSE_TEMPERATURE", "0.2"))
            ),
            enable_thinking=(
                enable_thinking
                if enable_thinking is not None
                else _environment_flag("HSSE_ENABLE_THINKING")
            ),
            local_files_only=(
                local_files_only
                if local_files_only is not None
                else _environment_flag("HSSE_MODEL_LOCAL_ONLY")
            ),
        )
