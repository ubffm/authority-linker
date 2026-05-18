from __future__ import annotations

from authority_linker import linker
from authority_linker.config import DEFAULTS
from authority_linker.context import PersonContext
from authority_linker.models import Candidate, CandidateRef


def test_linker_picks_best_candidate(monkeypatch) -> None:
    # Mock Provider-Suche
    monkeypatch.setattr(
        linker,
        "wd_search",
        lambda name, year, lang: [CandidateRef(source="wikidata", id="Q123", label="John Doe")],
    )
    # Mock Provider-Fetch
    monkeypatch.setattr(
        linker,
        "wd_fetch",
        lambda qid: Candidate(
            source="wikidata",
            id=qid,
            uri=f"https://www.wikidata.org/entity/{qid}",
            labels={"en": "John Doe"},
            aliases={},
            instance_of=["Q5"],
            birth_year=1933,
            death_year=2006,
            occupations=["director"],
            external_ids={},
            image_url=None,
        ),
    )
    # Keine GND-Ergebnisse
    monkeypatch.setattr(linker, "search_gnd", lambda name, year: [])

    ctx = PersonContext(
        record_id="r1",
        name_pref="Doe, John",
        name_display="John Doe",
        roles={"director"},
        work_year=2005,
    )
    res = linker.link_agent(ctx)
    assert res is not None
    assert res.candidate is not None
    assert res.candidate.id == "Q123"
    assert res.score >= DEFAULTS.scoring_threshold


def test_linker_below_threshold(monkeypatch) -> None:
    monkeypatch.setattr(
        linker,
        "wd_search",
        lambda name, year, lang: [CandidateRef(source="wikidata", id="Q999", label="Jane Roe")],
    )
    # Liefere Kandidat ohne Punkte (kein Namensmatch, keine Daten)
    monkeypatch.setattr(
        linker,
        "wd_fetch",
        lambda qid: Candidate(
            source="wikidata",
            id=qid,
            uri=f"https://www.wikidata.org/entity/{qid}",
            labels={"en": "Other Person"},
            aliases={},
            instance_of=[],
            birth_year=None,
            death_year=None,
            occupations=[],
            external_ids={},
            image_url=None,
        ),
    )
    monkeypatch.setattr(linker, "search_gnd", lambda name, year: [])

    ctx = PersonContext(
        record_id="r2",
        name_pref="Roe, Jane",
        name_display="Jane Roe",
        roles=set(),
        work_year=2005,
    )
    res = linker.link_agent(ctx)
    assert res is not None
    assert res.candidate is None
    assert res.reason == "unterhalb Schwelle"


def test_linker_borderline_uses_llm_accept_best(monkeypatch) -> None:
    monkeypatch.setattr(
        linker,
        "wd_search",
        lambda name, year, lang: [CandidateRef(source="wikidata", id="Q555", label="Jane Roe")],
    )
    monkeypatch.setattr(linker, "search_gnd", lambda name, year: [])
    monkeypatch.setattr(
        linker,
        "wd_fetch",
        lambda qid: Candidate(
            source="wikidata",
            id=qid,
            uri=f"https://www.wikidata.org/entity/{qid}",
            labels={"en": "Jane Roe"},
            aliases={},
            instance_of=["Q5"],
            birth_year=None,
            death_year=None,
            occupations=[],
            external_ids={},
            image_url=None,
        ),
    )

    class _DummyLLMMatcher:
        def __init__(self, *, min_confidence: float = 0.75) -> None:
            _ = min_confidence

        def decide(self, *, ctx, ranked_items):
            _ = ctx, ranked_items

            class _Decision:
                decision = "accept_best"
                chosen_candidate_id = None
                confidence = 0.9
                reason = "llm-best-ok"

            return _Decision()

    monkeypatch.setattr(linker, "LLMMatcher", _DummyLLMMatcher)

    ctx = PersonContext(
        record_id="r3",
        name_pref="Roe, Jane",
        name_display="Jane Roe",
        roles=set(),
        work_year=None,
    )
    res = linker.link_agent(ctx)
    assert res is not None
    assert res.candidate is not None
    assert res.candidate.id == "Q555"
    assert any(ev.startswith("llm:accept_best") for ev in res.evidences)
