from __future__ import annotations

import json
import time
from collections.abc import Iterable
from typing import Any

import httpx

from authority_linker.config import get_settings
from authority_linker.context import PersonContext
from authority_linker.models import Candidate, LLMDecision, ScoreBreakdown


class LLMMatcher:
    """Kleiner Adapter für LLM-basiertes Re-Ranking in Grenzfällen.

    Unterstützt derzeit OpenAI-kompatible Chat-Completions-Endpunkte.
    """

    def __init__(self, *, min_confidence: float = 0.75) -> None:
        """Initialisiere den Matcher mit einer Mindest-Confidence für positive Entscheidungen."""
        self.min_confidence = min_confidence

    def decide(
        self,
        *,
        ctx: PersonContext,
        ranked_items: list[tuple[ScoreBreakdown, Candidate, str]],
    ) -> LLMDecision:
        """Treffe eine strukturierte LLM-Entscheidung für die Top-Kandidaten."""
        if not ranked_items:
            return LLMDecision(
                decision="reject_all",
                confidence=0.0,
                reason="keine-kandidaten",
                used_evidence=[],
            )

        payload = self._build_payload(ctx=ctx, ranked_items=ranked_items[:3])
        raw = self._complete(payload)
        decision = self._parse_decision(raw)

        if decision.confidence < self.min_confidence and decision.decision != "reject_all":
            return LLMDecision(
                decision="reject_all",
                confidence=decision.confidence,
                reason="llm-confidence-zu-niedrig",
                used_evidence=decision.used_evidence,
            )

        if decision.decision == "choose_alternative":
            ids = {cand.id for _, cand, _ in ranked_items[:3]}
            if not decision.chosen_candidate_id or decision.chosen_candidate_id not in ids:
                return LLMDecision(
                    decision="reject_all",
                    confidence=decision.confidence,
                    reason="llm-kandidat-ungueltig",
                    used_evidence=decision.used_evidence,
                )

        return decision

    def _build_payload(
        self,
        *,
        ctx: PersonContext,
        ranked_items: Iterable[tuple[ScoreBreakdown, Candidate, str]],
    ) -> dict[str, Any]:
        """Erzeuge das strukturierte Prompt-Payload für den LLM-Aufruf."""
        items = []
        for breakdown, candidate, source in ranked_items:
            items.append(
                {
                    "source": source,
                    "candidate_id": candidate.id,
                    "candidate_uri": candidate.uri,
                    "labels": candidate.labels,
                    "aliases": candidate.aliases,
                    "instance_of": candidate.instance_of,
                    "birth_year": candidate.birth_year,
                    "death_year": candidate.death_year,
                    "occupations": candidate.occupations,
                    "score_total": breakdown.total,
                    "score_flags": {
                        "name_match": breakdown.name_match,
                        "human_instance": breakdown.human_instance,
                        "subject_overlap": breakdown.subject_overlap,
                        "role_overlap": breakdown.role_overlap,
                    },
                    "evidences": breakdown.evidences,
                }
            )

        return {
            "instruction": (
                "Entscheide nur auf Basis der gelieferten Daten. "
                "Erlaube nur: accept_best, choose_alternative, reject_all. "
                "Keine neuen Fakten erfinden."
            ),
            "context": {
                "record_id": ctx.record_id,
                "name_pref": ctx.name_pref,
                "name_display": ctx.name_display,
                "name_fuller": ctx.name_fuller,
                "work_year": ctx.work_year,
                "birth_year": ctx.birth_year,
                "death_year": ctx.death_year,
                "roles": sorted(ctx.roles),
                "subjects": ctx.subjects,
                "cast_raw": ctx.cast_raw,
                "external_ids": ctx.existing_ids,
            },
            "candidates": items,
            "output_schema": {
                "decision": "accept_best|choose_alternative|reject_all",
                "chosen_candidate_id": "string|null",
                "confidence": "float_0_to_1",
                "reason": "string|null",
                "used_evidence": "list[string]",
            },
        }

    def _complete(self, payload: dict[str, Any]) -> str:
        """Führe den LLM-Call aus und liefere den rohen JSON-String zurück."""
        settings = get_settings()

        if not settings.llm_enabled:
            return json.dumps(
                {
                    "decision": "reject_all",
                    "chosen_candidate_id": None,
                    "confidence": 0.0,
                    "reason": "llm-deaktiviert",
                    "used_evidence": [],
                }
            )

        if settings.llm_provider != "openai":
            return json.dumps(
                {
                    "decision": "reject_all",
                    "chosen_candidate_id": None,
                    "confidence": 0.0,
                    "reason": f"llm-provider-nicht-unterstuetzt:{settings.llm_provider}",
                    "used_evidence": [],
                }
            )

        if not settings.llm_api_key:
            return json.dumps(
                {
                    "decision": "reject_all",
                    "chosen_candidate_id": None,
                    "confidence": 0.0,
                    "reason": "llm-api-key-fehlt",
                    "used_evidence": [],
                }
            )

        base = settings.llm_api_base or "https://api.openai.com/v1"
        endpoint = f"{base.rstrip('/')}/chat/completions"

        messages = [
            {
                "role": "system",
                "content": (
                    "Du bist ein strenger Normdaten-Resolver. "
                    "Nutze ausschließlich bereitgestellte Daten. "
                    "Antworte ausschließlich als gültiges JSON-Objekt."
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]

        request_payload = {
            "model": settings.llm_model,
            "temperature": settings.llm_temperature,
            "max_tokens": settings.llm_max_tokens,
            "response_format": {"type": "json_object"},
            "messages": messages,
        }

        max_attempts = max(1, settings.llm_max_retries + 1)
        backoff = max(0.0, settings.llm_backoff_initial_s)
        backoff_max = max(backoff, settings.llm_backoff_max_s)

        with httpx.Client(timeout=settings.llm_timeout_s) as client:
            for attempt in range(1, max_attempts + 1):
                try:
                    resp = client.post(
                        endpoint,
                        headers={
                            "Authorization": f"Bearer {settings.llm_api_key}",
                            "Content-Type": "application/json",
                        },
                        json=request_payload,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    try:
                        content = data["choices"][0]["message"]["content"]
                        if isinstance(content, str):
                            return content
                    except Exception:
                        return json.dumps(
                            {
                                "decision": "reject_all",
                                "chosen_candidate_id": None,
                                "confidence": 0.0,
                                "reason": "llm-antwort-ohne-content",
                                "used_evidence": [],
                            }
                        )
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    retriable = status == 429 or 500 <= status < 600
                    if retriable and attempt < max_attempts:
                        if backoff > 0:
                            time.sleep(min(backoff, backoff_max))
                            backoff = min(backoff * 2 if backoff > 0 else 0.0, backoff_max)
                        continue
                    return json.dumps(
                        {
                            "decision": "reject_all",
                            "chosen_candidate_id": None,
                            "confidence": 0.0,
                            "reason": f"llm-request-fehlgeschlagen:HTTPStatusError:{status}:{exc}",
                            "used_evidence": [],
                        }
                    )
                except Exception as exc:
                    return json.dumps(
                        {
                            "decision": "reject_all",
                            "chosen_candidate_id": None,
                            "confidence": 0.0,
                            "reason": f"llm-request-fehlgeschlagen:{type(exc).__name__}:{exc}",
                            "used_evidence": [],
                        }
                    )

        return json.dumps(
            {
                "decision": "reject_all",
                "chosen_candidate_id": None,
                "confidence": 0.0,
                "reason": "llm-request-fehlgeschlagen:unbekannt",
                "used_evidence": [],
            }
        )

    def _parse_decision(self, raw: str) -> LLMDecision:
        """Parse und validiere die LLM-Antwort robust zu einer LLMDecision."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return LLMDecision(
                decision="reject_all",
                confidence=0.0,
                reason="llm-antwort-kein-json",
                used_evidence=[],
            )
        try:
            return LLMDecision.model_validate(data)
        except Exception:
            return LLMDecision(
                decision="reject_all",
                confidence=0.0,
                reason="llm-antwort-ungueltig",
                used_evidence=[],
            )
