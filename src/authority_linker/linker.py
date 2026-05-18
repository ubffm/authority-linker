from __future__ import annotations

from contextlib import suppress
from typing import Any

from authority_linker.config import get_settings
from authority_linker.context import PersonContext
from authority_linker.llm_matcher import LLMMatcher
from authority_linker.matching.scorer import score
from authority_linker.models import Candidate, MatchResult, ScoreBreakdown
from authority_linker.providers.lobid_gnd import fetch_gnd, search_gnd
from authority_linker.providers.wikidata import fetch as wd_fetch, search as wd_search

_DEFAULT_MIN_SCORE_GAP = 0.08
_DEFAULT_CONTEXT_EVIDENCES = ("role_overlap", "subject_overlap")

ScoredItem = tuple[ScoreBreakdown, Candidate, str]


def _all_unique_values(values: dict[str, list[str]] | None) -> list[str]:
    if not values:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for entries in values.values():
        if not isinstance(entries, list):
            continue
        for item in entries:
            if not isinstance(item, str):
                continue
            candidate = item.strip()
            if not candidate:
                continue
            key = candidate.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(candidate)
    return out


def _enrich_context_with_candidate(ctx: PersonContext, cand: Candidate) -> PersonContext:
    merged_subjects = list(ctx.subjects)

    for item in _all_unique_values(getattr(cand, "descriptions", {})):
        if item not in merged_subjects:
            merged_subjects.append(item)

    for item in getattr(cand, "subject_categories", []) or []:
        if isinstance(item, str):
            value = item.strip()
            if value and value not in merged_subjects:
                merged_subjects.append(value)

    for item in getattr(cand, "occupations", []) or []:
        if isinstance(item, str):
            value = item.strip()
            if value and value not in merged_subjects:
                merged_subjects.append(value)

    merged_existing_ids = dict(ctx.existing_ids)
    for key, value in (getattr(cand, "external_ids", {}) or {}).items():
        if isinstance(key, str) and isinstance(value, str) and key and value:
            merged_existing_ids[key] = value

    return ctx.model_copy(
        update={
            "subjects": merged_subjects,
            "existing_ids": merged_existing_ids,
        }
    )


def _build_debug_search_ref(ref: Any) -> dict[str, Any]:
    return {
        "source": getattr(ref, "source", None),
        "id": getattr(ref, "id", None),
        "label": getattr(ref, "label", None),
        "lang": getattr(ref, "lang", None),
    }


def _build_debug_scored_item(item: ScoredItem, *, rank: int | None = None) -> dict[str, Any]:
    breakdown, candidate, source = item
    labels = getattr(candidate, "labels", {}) or {}
    primary_label = None
    if isinstance(labels, dict):
        primary_label = labels.get("und") or labels.get("de") or labels.get("en")
        if primary_label is None and labels:
            first_key = next(iter(labels.keys()))
            primary_label = labels.get(first_key)

    payload: dict[str, Any] = {
        "source": source,
        "candidate_id": candidate.id,
        "candidate_uri": candidate.uri,
        "label": primary_label,
        "score_total": breakdown.total,
        "score_flags": {
            "name_match": breakdown.name_match,
            "human_instance": breakdown.human_instance,
            "subject_overlap": breakdown.subject_overlap,
            "role_overlap": breakdown.role_overlap,
        },
        "evidences": breakdown.evidences,
    }
    if rank is not None:
        payload["rank"] = rank
    return payload


def _has_min_context_signal(breakdown: ScoreBreakdown) -> bool:
    return bool(breakdown.subject_overlap or breakdown.role_overlap)


def _passes_llm_accept_guardrail(breakdown: ScoreBreakdown) -> bool:
    """Guardrail: LLM darf nur akzeptieren, wenn zumindest Name oder Kontextsignal passt."""
    return bool(breakdown.name_match or _has_min_context_signal(breakdown))


def _is_borderline(best: ScoredItem, runner_up: ScoredItem | None) -> bool:
    """Ermittle, ob ein Fall unsicher genug für optionales LLM-Re-Ranking ist."""
    settings = get_settings()
    threshold = getattr(settings, "scoring_threshold", 0.7)
    margin = getattr(settings, "llm_borderline_margin", 0.1)
    gap_limit = getattr(settings, "llm_borderline_gap", _DEFAULT_MIN_SCORE_GAP)

    best_score = best[0].total
    near_threshold = (threshold - margin) <= best_score < threshold
    small_gap = False
    if runner_up is not None:
        gap = best_score - runner_up[0].total
        small_gap = gap < gap_limit

    contradictory = best[0].name_match and not best[0].role_overlap
    return near_threshold or small_gap or contradictory


def _decide_acceptance(
    best: ScoredItem,
    runner_up: ScoredItem | None,
) -> tuple[bool, list[str]]:
    """Entscheide regelbasiert über Akzeptanz und sammle Ablehnungsgründe."""
    settings = get_settings()
    threshold = getattr(settings, "scoring_threshold", 0.7)
    min_gap = getattr(settings, "scoring_min_gap", _DEFAULT_MIN_SCORE_GAP)
    require_name = getattr(settings, "require_name_match_evidence", True)
    require_context = getattr(settings, "require_context_evidence", True)
    require_human = getattr(settings, "require_human_instance", False)
    raw_context_evidences = getattr(
        settings,
        "context_evidences_for_acceptance",
        _DEFAULT_CONTEXT_EVIDENCES,
    )
    if raw_context_evidences is None:
        context_evidences: tuple[str, ...] = ()
    elif isinstance(raw_context_evidences, str):
        context_evidences = (raw_context_evidences,)
    else:
        context_evidences = tuple(raw_context_evidences)

    breakdown = best[0]
    reasons: list[str] = []
    accepted = True

    if breakdown.total < threshold:
        accepted = False
        reasons.append("score-unter-schwellwert")

    if runner_up is not None:
        gap = breakdown.total - runner_up[0].total
        if gap < min_gap:
            accepted = False
            reasons.append(f"score-gap-zu-klein ({gap:.2f})")

    if require_name and not breakdown.name_match:
        accepted = False
        reasons.append("kein-namensmatch")

    if require_human and not breakdown.human_instance:
        accepted = False
        reasons.append("kein-menschlicher-kandidat")

    if require_context and context_evidences:
        has_context = any(getattr(breakdown, evidence, False) for evidence in context_evidences)
        if not has_context:
            accepted = False
            reasons.append("keine-kontext-evidenz")

    return accepted, reasons


def link_agent(ctx: PersonContext) -> tuple[MatchResult | None, PersonContext]:
    """Orchestriere Suche, Fetch, Scoring und optionale LLM-Entscheidung für einen Agenten."""
    settings = get_settings()

    candidates_refs = []
    seen_refs: set[tuple[str, str]] = set()

    debug_search_refs: list[dict[str, Any]] = []
    debug_lobid_search_refs: list[dict[str, Any]] = []

    if getattr(settings, "use_wikidata_search", False):
        for lang in settings.wikidata_langs:
            with suppress(Exception):
                for ref in wd_search(name=ctx.name_pref, year=None, lang=lang):
                    key = (ref.source, ref.id)
                    if key in seen_refs:
                        continue
                    seen_refs.add(key)
                    candidates_refs.append(ref)
                    debug_search_refs.append(_build_debug_search_ref(ref))

    with suppress(Exception):
        for ref in search_gnd(name=ctx.name_pref, year=None):
            key = (ref.source, ref.id)
            if key in seen_refs:
                continue
            seen_refs.add(key)
            candidates_refs.append(ref)
            debug_ref = _build_debug_search_ref(ref)
            debug_search_refs.append(debug_ref)
            debug_lobid_search_refs.append(debug_ref)

    scored: list[ScoredItem] = []
    debug_scored: list[dict[str, Any]] = []
    for ref in candidates_refs:
        try:
            cand = wd_fetch(ref.id) if ref.source == "wikidata" else fetch_gnd(ref.id)
        except NotImplementedError:
            continue
        breakdown = score(cand, ctx)
        scored_item = (breakdown, cand, ref.source)
        scored.append(scored_item)
        debug_scored.append(_build_debug_scored_item(scored_item))

    if not scored:
        return None, ctx

    preferred_sources = settings.preferred_sources or ("gnd", "wikidata")
    source_rank = {source: idx for idx, source in enumerate(preferred_sources)}
    scored_sorted = sorted(
        scored,
        key=lambda item: (
            -item[0].total,
            source_rank.get(item[2], len(source_rank)),
        ),
    )
    best = scored_sorted[0]
    runner_up = scored_sorted[1] if len(scored_sorted) > 1 else None

    llm_ranked_items = scored_sorted[:3]
    debug_llm_ranked_items = [
        _build_debug_scored_item(item, rank=idx)
        for idx, item in enumerate(llm_ranked_items, start=1)
    ]

    decision_path: list[str] = ["rules:evaluated"]
    accepted, rejection_reasons = _decide_acceptance(best, runner_up)
    best_breakdown, cand_best, _source = best

    if accepted:
        decision_path.append("rules:accepted")
        enriched_ctx = _enrich_context_with_candidate(ctx, cand_best)
        return (
            MatchResult(
                score=best_breakdown.total,
                breakdown=best_breakdown,
                candidate=cand_best,
                evidences=best_breakdown.evidences,
                reason=None,
                accepted=True,
                decision_path=decision_path,
                rule_rejections=[],
                llm_decision=None,
                debug_search_refs=debug_search_refs,
                debug_lobid_search_refs=debug_lobid_search_refs,
                debug_scored=debug_scored,
                debug_llm_ranked_items=debug_llm_ranked_items,
            ),
            enriched_ctx,
        )

    decision_path.append("rules:rejected")

    if _is_borderline(best, runner_up):
        # Nur LLM aufrufen, wenn unter Top-K wenigstens ein Kandidat minimale Evidenz hat.
        if not any(_passes_llm_accept_guardrail(item[0]) for item in llm_ranked_items):
            decision_path.append("llm:skipped-no-min-evidence")
            rejection_reasons.append("llm-kein-kandidat-mit-mindest-evidenz")
            reason_text = "; ".join(rejection_reasons) if rejection_reasons else "nicht akzeptiert"
            return (
                MatchResult(
                    score=best_breakdown.total,
                    breakdown=best_breakdown,
                    candidate=None,
                    evidences=best_breakdown.evidences,
                    reason=reason_text,
                    accepted=False,
                    decision_path=decision_path,
                    rule_rejections=rejection_reasons,
                    llm_decision=None,
                    debug_search_refs=debug_search_refs,
                    debug_lobid_search_refs=debug_lobid_search_refs,
                    debug_scored=debug_scored,
                    debug_llm_ranked_items=debug_llm_ranked_items,
                ),
                ctx,
            )

        decision_path.append("llm:invoked")
        llm_matcher = LLMMatcher(min_confidence=getattr(settings, "llm_min_confidence", 0.75))
        decision = llm_matcher.decide(ctx=ctx, ranked_items=llm_ranked_items)

        if decision.decision == "accept_best":
            if not _passes_llm_accept_guardrail(best_breakdown):
                decision_path.append("llm:rejected-guardrail")
                rejection_reasons.append("llm-guardrail-verhindert-accept_best")
                reason_text = (
                    "; ".join(rejection_reasons) if rejection_reasons else "nicht akzeptiert"
                )
                return (
                    MatchResult(
                        score=best_breakdown.total,
                        breakdown=best_breakdown,
                        candidate=None,
                        evidences=best_breakdown.evidences,
                        reason=reason_text,
                        accepted=False,
                        decision_path=decision_path,
                        rule_rejections=rejection_reasons,
                        llm_decision=decision,
                        debug_search_refs=debug_search_refs,
                        debug_lobid_search_refs=debug_lobid_search_refs,
                        debug_scored=debug_scored,
                        debug_llm_ranked_items=debug_llm_ranked_items,
                    ),
                    ctx,
                )
            decision_path.append("llm:accept_best")
            enriched_ctx = _enrich_context_with_candidate(ctx, cand_best)
            return (
                MatchResult(
                    score=best_breakdown.total,
                    breakdown=best_breakdown,
                    candidate=cand_best,
                    evidences=best_breakdown.evidences
                    + [f"llm:accept_best:{decision.confidence:.2f}"],
                    reason=decision.reason,
                    accepted=True,
                    decision_path=decision_path,
                    rule_rejections=rejection_reasons,
                    llm_decision=decision,
                    debug_search_refs=debug_search_refs,
                    debug_lobid_search_refs=debug_lobid_search_refs,
                    debug_scored=debug_scored,
                    debug_llm_ranked_items=debug_llm_ranked_items,
                ),
                enriched_ctx,
            )

        if decision.decision == "choose_alternative" and decision.chosen_candidate_id:
            for alt_breakdown, alt_candidate, _ in llm_ranked_items:
                if alt_candidate.id == decision.chosen_candidate_id:
                    if not _passes_llm_accept_guardrail(alt_breakdown):
                        decision_path.append("llm:rejected-guardrail")
                        rejection_reasons.append("llm-guardrail-verhindert-choose_alternative")
                        reason_text = (
                            "; ".join(rejection_reasons)
                            if rejection_reasons
                            else "nicht akzeptiert"
                        )
                        return (
                            MatchResult(
                                score=best_breakdown.total,
                                breakdown=best_breakdown,
                                candidate=None,
                                evidences=best_breakdown.evidences,
                                reason=reason_text,
                                accepted=False,
                                decision_path=decision_path,
                                rule_rejections=rejection_reasons,
                                llm_decision=decision,
                                debug_search_refs=debug_search_refs,
                                debug_lobid_search_refs=debug_lobid_search_refs,
                                debug_scored=debug_scored,
                                debug_llm_ranked_items=debug_llm_ranked_items,
                            ),
                            ctx,
                        )
                    decision_path.append("llm:choose_alternative")
                    enriched_ctx = _enrich_context_with_candidate(ctx, alt_candidate)
                    return (
                        MatchResult(
                            score=alt_breakdown.total,
                            breakdown=alt_breakdown,
                            candidate=alt_candidate,
                            evidences=alt_breakdown.evidences
                            + [f"llm:choose_alternative:{decision.confidence:.2f}"],
                            reason=decision.reason,
                            accepted=True,
                            decision_path=decision_path,
                            rule_rejections=rejection_reasons,
                            llm_decision=decision,
                            debug_search_refs=debug_search_refs,
                            debug_lobid_search_refs=debug_lobid_search_refs,
                            debug_scored=debug_scored,
                            debug_llm_ranked_items=debug_llm_ranked_items,
                        ),
                        enriched_ctx,
                    )

        decision_path.append("llm:rejected")
        rejection_reasons.append(f"llm:{decision.reason or 'reject_all'}")
        reason_text = "; ".join(rejection_reasons) if rejection_reasons else "nicht akzeptiert"
        return (
            MatchResult(
                score=best_breakdown.total,
                breakdown=best_breakdown,
                candidate=None,
                evidences=best_breakdown.evidences,
                reason=reason_text,
                accepted=False,
                decision_path=decision_path,
                rule_rejections=rejection_reasons,
                llm_decision=decision,
                debug_search_refs=debug_search_refs,
                debug_lobid_search_refs=debug_lobid_search_refs,
                debug_scored=debug_scored,
                debug_llm_ranked_items=debug_llm_ranked_items,
            ),
            ctx,
        )

    decision_path.append("llm:skipped")
    reason_text = "; ".join(rejection_reasons) if rejection_reasons else "nicht akzeptiert"
    return (
        MatchResult(
            score=best_breakdown.total,
            breakdown=best_breakdown,
            candidate=None,
            evidences=best_breakdown.evidences,
            reason=reason_text,
            accepted=False,
            decision_path=decision_path,
            rule_rejections=rejection_reasons,
            llm_decision=None,
            debug_search_refs=debug_search_refs,
            debug_lobid_search_refs=debug_lobid_search_refs,
            debug_scored=debug_scored,
            debug_llm_ranked_items=debug_llm_ranked_items,
        ),
        ctx,
    )
