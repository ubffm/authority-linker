from __future__ import annotations

from pathlib import Path

from authority_linker.context import PersonContext, build_person_context
from authority_linker.marc_helpers import fields_per_700, load_records

DATA_PATH = Path("tests/testdata/kanopy/1000_Kanopy_MARC_Records_fiddk_full.json")


def test_context_for_miller_roles_and_year() -> None:
    recs = load_records(DATA_PATH)
    # wähle Datensatz mit 001 == kan1118710 ("The atheism tapes.")
    record = next(r for r in recs if any(f.get("001") == "kan1118710" for f in r["fields"]))
    contexts: list[PersonContext] = []
    for fields in fields_per_700(record):
        ctx = build_person_context(fields, record_id=fields.get("001", ""))
        contexts.append(ctx)

    miller = next(c for c in contexts if c.name_pref.startswith("Miller, Jonathan"))
    assert miller.work_year == 2005
    # Rollen normalisiert (Satzzeichen entfernt, kleingeschrieben)
    assert "interviewer" in miller.roles
    assert "director" in miller.roles
    assert miller.place_hint and "San Francisco" in miller.place_hint


def test_context_region_hint_and_year_from_518() -> None:
    recs = load_records(DATA_PATH)
    # wähle Datensatz mit 001 == kan1116281 (enthält 043 f-sg---)
    record = next(r for r in recs if any(f.get("001") == "kan1116281" for f in r["fields"]))
    ctxs = [build_person_context(f, record_id=f.get("001", "")) for f in fields_per_700(record)]
    assert ctxs, "Erwarte mindestens einen Kontext aus 700"
    ctx = ctxs[0]
    assert ctx.region_hint == "f-sg---"
    assert ctx.work_year == 2002
    assert ctx.place_hint and "San Francisco" in ctx.place_hint


def test_birth_death_parsing_from_700d() -> None:
    recs = load_records(DATA_PATH)
    record = next(r for r in recs if any(f.get("001") == "kan1118710" for f in r["fields"]))
    ctxs = [build_person_context(f, record_id=f.get("001", "")) for f in fields_per_700(record)]
    dennett = next(c for c in ctxs if c.name_pref.startswith("Dennett, Daniel"))
    # In Testdaten absichtlich falsch: "1807-1889," → wir prüfen nur Parser-Verhalten
    assert dennett.birth_year == 1807
    assert dennett.death_year == 1889
    assert dennett.work_year == 2005


def test_worldcat_links_from_700_subfield_1() -> None:
    record = {
        "fields": [
            {"001": "test"},
            {
                "700": {
                    "subfields": [
                        {"a": "Beispiel, Clara"},
                        {"d": "1950-"},
                        {"1": "https://id.oclc.org/worldcat/entity/E39PBJexample1"},
                        {"1": "https://id.oclc.org/worldcat/entity/E39PBJexample2"},
                        {"0": "(DE-588)118540238"},
                        {"e": "Performer"},
                    ]
                }
            },
        ]
    }

    person_fields = fields_per_700(record)
    assert person_fields and "700" in person_fields[0]
    link_value = person_fields[0]["700"]["1"]
    links = link_value if isinstance(link_value, list) else [link_value]
    assert links == [
        "https://id.oclc.org/worldcat/entity/E39PBJexample1",
        "https://id.oclc.org/worldcat/entity/E39PBJexample2",
    ]

    ctx = build_person_context(person_fields[0], record_id=person_fields[0].get("001", ""))
    assert ctx.oclc_links == [
        "https://id.oclc.org/worldcat/entity/E39PBJexample1",
        "https://id.oclc.org/worldcat/entity/E39PBJexample2",
    ]
    assert ctx.existing_ids.get("$1") == "https://id.oclc.org/worldcat/entity/E39PBJexample1"
