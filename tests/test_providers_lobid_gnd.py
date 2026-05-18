from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import httpx
import pytest

from authority_linker.providers import lobid_gnd as lg
from authority_linker.text import strip_diacritics


class DummyCache:
    def __init__(self) -> None:
        self._store: dict[str, Any] = {}

    def get(self, key: str) -> Any | None:
        return self._store.get(key)

    def set(self, key: str, value: Any) -> None:
        self._store[key] = value


def _mk_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    transport = httpx.MockTransport(handler)
    return httpx.Client(
        transport=transport,
        headers={"User-Agent": "authority-linker-tests"},
        timeout=httpx.Timeout(5.0),
    )


@pytest.fixture(autouse=True)
def _configure_lobid_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    from authority_linker.config import DEFAULTS as CONFIG_DEFAULTS

    monkeypatch.setattr(
        lg,
        "DEFAULTS",
        replace(
            CONFIG_DEFAULTS,
            gnd_sparql_endpoint="https://sparql.dnb.de/api/gnd",
        ),
    )


def test_gnd_search_parses_members(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        expected = httpx.URL(f"{lg.DEFAULTS.gnd_api_base}search")
        assert request.url.host == expected.host
        assert request.url.path == expected.path
        data = {
            "member": [
                {
                    "@id": "https://lobid.org/gnd/118540238",
                    "gndIdentifier": "118540238",
                    "preferredName": "Menuhin, Yehudi",
                },
                {
                    "@id": "https://lobid.org/gnd/000000001",
                    "preferredName": "John Doe",
                },
            ]
        }
        return httpx.Response(200, json=data)

    monkeypatch.setattr(lg, "_client", lambda: _mk_client(handler))
    monkeypatch.setattr(lg, "_cache", DummyCache(), raising=False)
    monkeypatch.setattr(lg.time, "sleep", lambda *_: None)

    res = lg.search_gnd("Menuhin")
    assert res and res[0].source == "gnd"
    assert res[0].id == "118540238"
    assert res[0].label == "Menuhin, Yehudi"


def test_gnd_fetch_builds_candidate(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        expected = httpx.URL(lg.DEFAULTS.gnd_sparql_endpoint)
        assert request.url.host == expected.host
        assert request.url.path == expected.path
        params = dict(request.url.params)
        assert "query" in params
        query = params["query"]
        if "professionOrOccupation" in query:
            data = {
                "head": {"vars": ["p", "occ_node", "occ_label"]},
                "results": {
                    "bindings": [
                        {
                            "p": {
                                "type": "uri",
                                "value": "http://www.w3.org/1999/02/22-rdf-syntax-ns#_1",
                            },
                            "occ_label": {
                                "type": "literal",
                                "value": "Violinist",
                                "xml:lang": "en",
                            },
                        },
                        {
                            "p": {
                                "type": "uri",
                                "value": "http://www.w3.org/1999/02/22-rdf-syntax-ns#_2",
                            },
                            "occ_node": {
                                "type": "uri",
                                "value": "https://example.org/occupation/Conductor",
                            },
                        },
                    ]
                },
            }
        else:
            data = {
                "head": {"vars": ["pref", "variant", "birth", "death", "life_dates", "same_as"]},
                "results": {
                    "bindings": [
                        {
                            "pref": {
                                "type": "literal",
                                "value": "Menuhin, Yehudi",
                                "xml:lang": "de",
                            },
                            "variant": {
                                "type": "literal",
                                "value": "Yehudi Menuhin",
                                "xml:lang": "de",
                            },
                            "birth": {"type": "literal", "value": "1916"},
                            "death": {"type": "literal", "value": "1999"},
                            "life_dates": {"type": "literal", "value": "1916-1999"},
                            "same_as": {
                                "type": "uri",
                                "value": "http://viaf.org/viaf/12312362",
                            },
                        },
                        {
                            "variant": {
                                "type": "literal",
                                "value": "Menuhin Yehudi",
                                "xml:lang": "de",
                            },
                        },
                    ]
                },
            }
        return httpx.Response(200, json=data)

    monkeypatch.setattr(lg, "_sparql_client", lambda: _mk_client(handler))
    monkeypatch.setattr(lg, "_cache", DummyCache(), raising=False)
    monkeypatch.setattr(lg.time, "sleep", lambda *_: None)

    cand = lg.fetch_gnd("118540238")
    assert cand.source == "gnd"
    assert cand.id == "118540238"
    assert cand.uri == "https://d-nb.info/gnd/118540238"
    assert cand.labels.get("de") == "Menuhin, Yehudi"
    assert "yehudi menuhin" in {a.lower() for a in cand.aliases.get("de", [])}
    assert cand.birth_year == 1916
    assert cand.death_year == 1999
    assert "violinist" in cand.occupations
    assert "conductor" in cand.occupations
    assert cand.external_ids.get("gnd") == "118540238"


def test_gnd_fetch_accepts_id_with_hyphen(monkeypatch) -> None:
    gnd_id = "10192571-2"

    def handler(request: httpx.Request) -> httpx.Response:
        expected = httpx.URL(lg.DEFAULTS.gnd_sparql_endpoint)
        assert request.url.host == expected.host
        assert request.url.path == expected.path
        params = dict(request.url.params)
        assert "query" in params
        query = params["query"]
        assert f"https://d-nb.info/gnd/{gnd_id}" in query
        assert f"http://d-nb.info/gnd/{gnd_id}" in query
        data = {"head": {"vars": []}, "results": {"bindings": []}}
        return httpx.Response(200, json=data)

    monkeypatch.setattr(lg, "_sparql_client", lambda: _mk_client(handler))
    monkeypatch.setattr(lg, "_cache", DummyCache(), raising=False)
    monkeypatch.setattr(lg.time, "sleep", lambda *_: None)

    cand = lg.fetch_gnd(gnd_id)
    assert cand.id == gnd_id
    assert cand.uri == f"https://d-nb.info/gnd/{gnd_id}"


def test_gnd_fetch_collects_external_ids(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        expected = httpx.URL(lg.DEFAULTS.gnd_sparql_endpoint)
        assert request.url.host == expected.host
        assert request.url.path == expected.path
        params = dict(request.url.params)
        assert "query" in params
        query = params["query"]
        if "professionOrOccupation" in query:
            data = {
                "head": {"vars": ["p", "occ_node", "occ_label"]},
                "results": {"bindings": []},
            }
        else:
            data = {
                "head": {"vars": ["pref", "same_as"]},
                "results": {
                    "bindings": [
                        {
                            "pref": {
                                "type": "literal",
                                "value": "Doe, Jane",
                                "xml:lang": "de",
                            },
                            "same_as": {
                                "type": "uri",
                                "value": "http://viaf.org/viaf/123456789",
                            },
                        },
                        {
                            "same_as": {
                                "type": "uri",
                                "value": "https://isni.org/isni/0000 0001 2193 4335",
                            },
                        },
                    ]
                },
            }
        return httpx.Response(200, json=data)

    monkeypatch.setattr(lg, "_sparql_client", lambda: _mk_client(handler))
    monkeypatch.setattr(lg, "_cache", DummyCache(), raising=False)
    monkeypatch.setattr(lg.time, "sleep", lambda *_: None)

    cand = lg.fetch_gnd("123456789")
    assert cand.labels.get("de") == "Doe, Jane"
    assert cand.external_ids.get("gnd") == "123456789"
    assert cand.external_ids.get("viaf") == "123456789"
    assert cand.external_ids.get("isni") == "0000000121934335"


def test_gnd_fetch_uses_life_dates_fallback(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        expected = httpx.URL(lg.DEFAULTS.gnd_sparql_endpoint)
        assert request.url.host == expected.host
        assert request.url.path == expected.path
        params = dict(request.url.params)
        assert "query" in params
        data = {
            "head": {"vars": ["pref", "life_dates"]},
            "results": {
                "bindings": [
                    {
                        "pref": {
                            "type": "literal",
                            "value": "Doe, John",
                            "xml:lang": "de",
                        },
                        "life_dates": {"type": "literal", "value": "1900-1980"},
                    }
                ]
            },
        }
        return httpx.Response(200, json=data)

    monkeypatch.setattr(lg, "_sparql_client", lambda: _mk_client(handler))
    monkeypatch.setattr(lg, "_cache", DummyCache(), raising=False)
    monkeypatch.setattr(lg.time, "sleep", lambda *_: None)

    cand = lg.fetch_gnd("118540238")
    assert cand.birth_year == 1900
    assert cand.death_year == 1980


def test_gnd_fetch_fallback_on_non_200(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        expected = httpx.URL(lg.DEFAULTS.gnd_sparql_endpoint)
        assert request.url.host == expected.host
        assert request.url.path == expected.path
        return httpx.Response(500, json={"error": "server error"})

    monkeypatch.setattr(lg, "_sparql_client", lambda: _mk_client(handler))
    monkeypatch.setattr(lg, "_cache", DummyCache(), raising=False)
    monkeypatch.setattr(lg.time, "sleep", lambda *_: None)

    cand = lg.fetch_gnd("118540238")
    assert cand.source == "gnd"
    assert cand.id == "118540238"
    assert cand.uri == "https://d-nb.info/gnd/118540238"
    assert not cand.labels


@pytest.mark.integration
def test_gnd_fetch_matches_public_record() -> None:
    if os.getenv("RUN_GND_INTEGRATION") != "1":
        pytest.skip("Integrationstest übersprungen (RUN_GND_INTEGRATION != 1).")

    # Verwende einen flüchtigen Cache, um potenziell veraltete lokale Daten zu vermeiden.
    lg._cache = DummyCache()  # type: ignore[attr-defined]

    candidate = lg.fetch_gnd("118580906")

    assert candidate.source == "gnd"
    assert candidate.id == "118580906"
    assert candidate.uri == "https://d-nb.info/gnd/118580906"
    assert candidate.labels.get("de") == "Menuhin, Yehudi"

    normalized_aliases = {
        strip_diacritics(alias).lower()
        for aliases_by_lang in candidate.aliases.values()
        for alias in aliases_by_lang
    }
    expected_aliases = {
        strip_diacritics("Menuhin, Yehuhdi").lower(),
        strip_diacritics("Menuhin, Yehudin").lower(),
        strip_diacritics("Menuhin, Jehudi").lower(),
        strip_diacritics("Menuhin of Stoke d'Abernon, Yehudi").lower(),
    }
    assert expected_aliases.issubset(normalized_aliases)

    assert candidate.birth_year == 1916
    assert candidate.death_year == 1999

    assert candidate.external_ids.get("gnd") == "118580906"
    assert candidate.external_ids.get("viaf") == "12312362"
    assert candidate.external_ids.get("isni") == "0000000121208271"

    occupations = {occ.lower() for occ in candidate.occupations}
    assert any(sub in occ for occ in occupations for sub in ("violinist", "violin", "geiger"))
    assert any(sub in occ for occ in occupations for sub in ("conductor", "dirigent"))
