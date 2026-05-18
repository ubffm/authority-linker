from __future__ import annotations

from authority_linker.config import get_settings


def _settings():
    return get_settings()


# Schwellenwerte und Defaults für das Scoring
SCORING_THRESHOLD: float = _settings().scoring_threshold
REQUIRED_INSTANCE_OF: set[str] = {"Q5"}  # Mensch
WIKIDATA_LANGS: tuple[str, ...] = _settings().wikidata_langs
