from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Zentrale Standardkonfiguration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    user_agent: str = (
        "authority-linker/0.1 (+https://performing-arts.eu; contact: j.smendek@ub.uni-frankfurt.de)"
    )
    wikidata_api_endpoint: str = "https://www.wikidata.org/w/api.php"
    wikidata_sparql_endpoint: str = "https://query.wikidata.org/sparql"
    gnd_api_base: str = "https://lobid.org/gnd/"
    gnd_sparql_endpoint: str = "https://sparql.dnb.de/api/gnd"

    wikidata_langs: tuple[str, ...] = ("de", "en")
    wikidata_rate_limit: float = 0.2  # Sekunden pro Request
    scoring_threshold: float = 0.7
    scoring_min_gap: float = Field(default=0.08, alias="AUTHORITY_LINKER_SCORING_MIN_GAP")
    preferred_sources: tuple[str, ...] = ("gnd", "wikidata")
    cache_dir: str = ".cache/authority_linker"

    # Akzeptanzregeln
    require_name_match_evidence: bool = Field(
        default=True,
        alias="AUTHORITY_LINKER_REQUIRE_NAME_MATCH_EVIDENCE",
    )
    require_context_evidence: bool = Field(
        default=True,
        alias="AUTHORITY_LINKER_REQUIRE_CONTEXT_EVIDENCE",
    )
    require_human_instance: bool = Field(
        default=False,
        alias="AUTHORITY_LINKER_REQUIRE_HUMAN_INSTANCE",
    )
    context_evidences_for_acceptance: tuple[str, ...] = Field(
        default=("role_overlap", "subject_overlap"),
        alias="AUTHORITY_LINKER_CONTEXT_EVIDENCES_FOR_ACCEPTANCE",
    )

    # Borderline-Erkennung für LLM
    llm_borderline_margin: float = Field(
        default=0.1,
        alias="AUTHORITY_LINKER_LLM_BORDERLINE_MARGIN",
    )
    llm_borderline_gap: float = Field(
        default=0.08,
        alias="AUTHORITY_LINKER_LLM_BORDERLINE_GAP",
    )

    # Domänen-Hinweise für GND-Sachkategorien (Notation), z. B. 15.1p
    subject_category_hints: tuple[str, ...] = Field(
        default=("14.4p", "15.1p", "15.2p", "15.3p", "6.1p", "12.1p", "12.2p", "13.4p", "12.4p"),
        alias="AUTHORITY_LINKER_SUBJECT_CATEGORY_HINTS",
    )

    # Providers
    use_wikidata_search: bool = Field(default=False, alias="AUTHORITY_LINKER_USE_WIKIDATA_SEARCH")
    use_gnd_uri_resolver: bool = Field(default=True, alias="AUTHORITY_LINKER_USE_GND_URI_RESOLVER")

    # LLM-Optionen (Default auf AI-ToolLab / LiteLLM)
    llm_enabled: bool = Field(default=True, alias="AUTHORITY_LINKER_LLM_ENABLED")
    llm_provider: str = Field(default="openai", alias="AUTHORITY_LINKER_LLM_PROVIDER")
    llm_model: str = Field(default="llama3.1:8b", alias="AUTHORITY_LINKER_LLM_MODEL")
    llm_api_base: str | None = Field(
        default="https://litellm.s.studiumdigitale.uni-frankfurt.de/v1",
        alias="AUTHORITY_LINKER_LLM_API_BASE",
    )
    llm_api_key: str | None = Field(default=None, alias="AUTHORITY_LINKER_LLM_API_KEY")
    llm_timeout_s: float = Field(default=20.0, alias="AUTHORITY_LINKER_LLM_TIMEOUT_S")
    llm_temperature: float = Field(default=0.0, alias="AUTHORITY_LINKER_LLM_TEMPERATURE")
    llm_max_tokens: int = Field(default=400, alias="AUTHORITY_LINKER_LLM_MAX_TOKENS")
    llm_min_confidence: float = Field(default=0.75, alias="AUTHORITY_LINKER_LLM_MIN_CONFIDENCE")
    llm_max_retries: int = Field(default=3, alias="AUTHORITY_LINKER_LLM_MAX_RETRIES")
    llm_backoff_initial_s: float = Field(
        default=0.5,
        alias="AUTHORITY_LINKER_LLM_BACKOFF_INITIAL_S",
    )
    llm_backoff_max_s: float = Field(default=8.0, alias="AUTHORITY_LINKER_LLM_BACKOFF_MAX_S")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Liefere die lazily geladene, gecachte Settings-Instanz."""
    return Settings()


DEFAULTS = get_settings()
