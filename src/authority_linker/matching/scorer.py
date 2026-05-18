from __future__ import annotations

from authority_linker.context import PersonContext
from authority_linker.matching.normalizer import normalize_name, normalize_role
from authority_linker.models import Candidate, ScoreBreakdown

PERSON_INSTANCE_MARKERS = {
    "q5",
    "human",
    "person",
    "differentiatedperson",
    "undifferentiatedperson",
    "individualperson",
}


def _normalize_sc_code(value: str) -> str | None:
    candidate = value.strip().lower()
    if not candidate:
        return None

    if "#" in candidate:
        candidate = candidate.rsplit("#", 1)[-1].strip()
    elif candidate.startswith("http://") or candidate.startswith("https://"):
        candidate = candidate.rsplit("/", 1)[-1].strip()

    return candidate or None


def score(candidate: Candidate, ctx: PersonContext) -> ScoreBreakdown:
    """Einfaches, nachvollziehbares Scoring und transparente Detailausgabe.

    Regeln:
    - +0.4: Name/Label-Ähnlichkeit (exakter gleich nach Normalisierung)
    - +0.2: Instance of enthält Q5 (Mensch)
    - +0.2: Sachkategorie-Overlap (ctx.subjects vs. candidate.subject_categories
            oder ctx.subject_category_hints vs. candidate.subject_categories)
    - +0.2: Rollen/Occupations-Overlap
    - +0.2: Geburtsjahr-Match (ctx.birth_year == candidate.birth_year)
    - +0.2: Sterbejahr-Match (ctx.death_year == candidate.death_year)
    """
    evidences: list[str] = []
    total = 0.0
    name_match = False
    human_instance = False
    subject_overlap = False
    role_overlap = False

    # Name/Label
    name_ctx = normalize_name(ctx.name_display or ctx.name_pref)
    label = candidate.labels.get("de") or candidate.labels.get("en") or candidate.labels.get("und")
    aliases = [
        *candidate.aliases.get("de", []),
        *candidate.aliases.get("en", []),
        *candidate.aliases.get("und", []),
    ]
    labels = [label, *aliases]
    labels = [normalize_name(x) for x in labels if x]
    if name_ctx and name_ctx in labels:
        total += 0.4
        name_match = True
        evidences.append("name:label-match")

    # Instance of Mensch
    instance_tokens: set[str] = set()
    for value in candidate.instance_of:
        if not value:
            continue
        stripped = value.strip()
        if not stripped:
            continue
        instance_tokens.add(stripped)
        for sep in ("#", "/", ":"):
            if sep in stripped:
                instance_tokens.add(stripped.rsplit(sep, 1)[-1])
    normalized_instances = {token.strip().lower() for token in instance_tokens if token.strip()}
    if normalized_instances & PERSON_INSTANCE_MARKERS:
        total += 0.2
        human_instance = True
        evidences.append("p31:human")

    # Sachkategorie-Overlap
    if candidate.subject_categories:
        has_text_overlap = False
        if ctx.subjects:
            ctx_subjects = {normalize_role(s) for s in ctx.subjects if s and s.strip()}
            cand_subjects = {
                normalize_role(s) for s in candidate.subject_categories if s and s.strip()
            }
            has_text_overlap = bool(ctx_subjects & cand_subjects)

        cand_sc_codes = {
            code
            for raw in candidate.subject_categories
            if isinstance(raw, str)
            for code in [_normalize_sc_code(raw)]
            if code
        }
        hint_codes = {
            code
            for raw in (ctx.subject_category_hints or set())
            if isinstance(raw, str)
            for code in [_normalize_sc_code(raw)]
            if code
        }
        has_hint_overlap = bool(cand_sc_codes & hint_codes)

        if has_text_overlap or has_hint_overlap:
            total += 0.2
            subject_overlap = True
            evidences.append("subject:overlap")

    # Rollen/Occupations Overlap
    if ctx.roles and candidate.occupations:
        roles_ctx = {normalize_role(r) for r in ctx.roles}
        occ = {normalize_role(o) for o in candidate.occupations}
        if roles_ctx & occ:
            total += 0.2
            role_overlap = True
            evidences.append("role:overlap")

    # Lebensdaten-Match
    if (
        ctx.birth_year is not None
        and candidate.birth_year is not None
        and ctx.birth_year == candidate.birth_year
    ):
        total += 0.2
        evidences.append("life:birth-year-match")

    if (
        ctx.death_year is not None
        and candidate.death_year is not None
        and ctx.death_year == candidate.death_year
    ):
        total += 0.2
        evidences.append("life:death-year-match")

    return ScoreBreakdown(
        total=total,
        name_match=name_match,
        human_instance=human_instance,
        subject_overlap=subject_overlap,
        role_overlap=role_overlap,
        evidences=evidences,
    )
