from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CandidateRef(BaseModel):
    """Leichtgewichtiger Kandidatenverweis (z. B. aus einer Such-API)."""

    source: Literal["wikidata", "gnd"]
    id: str  # QID oder GND-ID
    label: str | None = None
    lang: str | None = None


class Candidate(BaseModel):
    """Angereicherter Kandidat mit den für das Matching relevanten Fakten."""

    source: Literal["wikidata", "gnd"]
    id: str  # QID oder GND-ID
    uri: str

    labels: dict[str, str] = Field(default_factory=dict)  # lang -> label
    aliases: dict[str, list[str]] = Field(default_factory=dict)  # lang -> alias-liste

    instance_of: list[str] = Field(default_factory=list)  # Wikidata-QIDs (z. B. ["Q5"])
    birth_year: int | None = None
    death_year: int | None = None
    occupations: list[str] = Field(default_factory=list)  # normalisierte Rollen/Berufe
    descriptions: dict[str, list[str]] = Field(default_factory=dict)  # lang -> beschreibungen
    subject_categories: list[str] = Field(default_factory=list)

    # externe IDs (z. B. gnd, viaf)
    external_ids: dict[str, str] = Field(default_factory=dict)
    image_url: str | None = None


class ScoreBreakdown(BaseModel):
    """Detailausgabe der Scoring-Regeln."""

    total: float
    name_match: bool = False
    human_instance: bool = False
    subject_overlap: bool = False
    role_overlap: bool = False
    evidences: list[str] = Field(default_factory=list)


class LLMDecision(BaseModel):
    """Strukturierte Entscheidung eines LLM-Re-Rankings."""

    decision: Literal["accept_best", "choose_alternative", "reject_all"]
    chosen_candidate_id: str | None = None
    confidence: float = 0.0
    reason: str | None = None
    used_evidence: list[str] = Field(default_factory=list)


class MatchResult(BaseModel):
    """Ergebnis eines Link-Vorgangs für eine Person."""

    score: float
    breakdown: ScoreBreakdown | None = None
    candidate: Candidate | None
    evidences: list[str] = Field(default_factory=list)
    reason: str | None = None

    # Transparenz über den Entscheidungsweg
    accepted: bool = False
    decision_path: list[str] = Field(default_factory=list)
    rule_rejections: list[str] = Field(default_factory=list)
    llm_decision: LLMDecision | None = None

    # Debug-/Transparenzdaten für Suche, Scoring und LLM-Auswahl
    debug_search_refs: list[dict[str, Any]] = Field(default_factory=list)
    debug_lobid_search_refs: list[dict[str, Any]] = Field(default_factory=list)
    debug_scored: list[dict[str, Any]] = Field(default_factory=list)
    debug_llm_ranked_items: list[dict[str, Any]] = Field(default_factory=list)
