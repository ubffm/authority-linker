from __future__ import annotations

from authority_linker.context import PersonContext
from authority_linker.matching.scorer import score
from authority_linker.models import Candidate


def test_score_full_match() -> None:
    ctx = PersonContext(
        record_id="r1",
        name_pref="Doe, John",
        name_display="John Doe",
        roles={"Director"},
        subjects=["Documentary films."],
        work_year=2005,
    )
    cand = Candidate(
        source="wikidata",
        id="Q1",
        uri="https://www.wikidata.org/entity/Q1",
        labels={"en": "John Doe"},
        aliases={},
        instance_of=["Q5"],
        birth_year=1933,
        death_year=2006,
        occupations=["director"],
        subject_categories=["documentary films"],
        external_ids={},
        image_url=None,
    )
    breakdown = score(cand, ctx)
    assert breakdown.total == 1.0
    assert breakdown.name_match
    assert breakdown.human_instance
    assert breakdown.subject_overlap
    assert breakdown.role_overlap
    assert {
        "name:label-match",
        "p31:human",
        "subject:overlap",
        "role:overlap",
    } <= set(breakdown.evidences)


def test_score_without_subject_overlap_and_without_role_overlap() -> None:
    ctx = PersonContext(
        record_id="r2",
        name_pref="Roe, Jane",
        name_display="Jane Roe",
        roles=set(),
        subjects=["Feature films."],
        work_year=2005,
    )
    cand = Candidate(
        source="wikidata",
        id="Q2",
        uri="https://www.wikidata.org/entity/Q2",
        labels={"en": "Jane Roe"},
        aliases={},
        instance_of=[],
        birth_year=2010,
        death_year=None,
        occupations=[],
        subject_categories=["Documentary films."],
        external_ids={},
        image_url=None,
    )
    breakdown = score(cand, ctx)
    assert breakdown.total <= 0.4  # max. Namensmatch möglich, keine weiteren Punkte
    assert not breakdown.subject_overlap
    assert "subject:overlap" not in breakdown.evidences


def test_score_subject_overlap_via_sc_hints() -> None:
    ctx = PersonContext(
        record_id="r3",
        name_pref="Mustermann, Max",
        name_display="Max Mustermann",
        roles=set(),
        subjects=[],
        subject_category_hints={"15.1p"},
    )
    cand = Candidate(
        source="gnd",
        id="118540238",
        uri="https://d-nb.info/gnd/118540238",
        labels={"de": "Max Mustermann"},
        aliases={},
        instance_of=[],
        occupations=[],
        subject_categories=["15.1p"],
        external_ids={},
        image_url=None,
    )
    breakdown = score(cand, ctx)
    assert breakdown.subject_overlap
    assert "subject:overlap" in breakdown.evidences
