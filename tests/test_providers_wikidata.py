from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from authority_linker.providers import wikidata as wd


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


def test_wikidata_search_wbsearchentities(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "www.wikidata.org"
        assert request.url.path == "/w/api.php"
        params = dict(request.url.params)
        assert params.get("action") == "wbsearchentities"
        data = {
            "search": [
                {"id": "Q42", "label": "Douglas Adams"},
                {"id": "Q1", "label": "Universe"},
            ]
        }
        return httpx.Response(200, json=data)

    monkeypatch.setattr(wd, "_client", lambda: _mk_client(handler))
    monkeypatch.setattr(wd, "_cache", DummyCache(), raising=False)
    monkeypatch.setattr(wd.time, "sleep", lambda *_: None)

    # year-Argument entfernt, da search() dieses nicht mehr unterstützt
    results = wd.search(name="Douglas Adams", lang="en")
    assert results, "Erwarte mindestens einen Treffer"
    assert results[0].source == "wikidata"
    assert results[0].id == "Q42"
    assert results[0].label == "Douglas Adams"
    assert results[0].lang == "en"


def test_wikidata_fetch_sparql_builds_candidate(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "query.wikidata.org"
        assert request.url.path == "/sparql"
        # Simuliere SPARQL-Ergebnis mit mehreren Bindings (inkl. Duplikaten)
        data = {
            "head": {
                "vars": [
                    "instance",
                    "birth",
                    "death",
                    "gnd",
                    "viaf",
                    "isni",
                    "label_de",
                    "label_en",
                    "alias_de",
                    "alias_en",
                    "occ_label",
                    "image",
                ]
            },
            "results": {
                "bindings": [
                    {"instance": {"value": "http://www.wikidata.org/entity/Q5"}},
                    {"label_en": {"value": "John Doe"}},
                    {"label_de": {"value": "Johann Doe"}},
                    {"alias_en": {"value": "J. Doe"}},
                    {"alias_en": {"value": "J. Doe"}},  # Duplikat zum Deduplizieren
                    {"alias_de": {"value": "Joh. Doe"}},
                    {"occ_label": {"value": "director"}},
                    {"occ_label": {"value": "actor"}},
                    {"birth": {"value": "1933-01-01"}},
                    {"death": {"value": "2005-03-15"}},
                    {"gnd": {"value": "118540238"}},
                    {"viaf": {"value": "123456789"}},
                    {"isni": {"value": "0000000121032683"}},
                    {"image": {"value": "https://upload.wikimedia.org/example.jpg"}},
                ]
            },
        }
        return httpx.Response(200, json=data)

    monkeypatch.setattr(wd, "_client", lambda: _mk_client(handler))
    monkeypatch.setattr(wd, "_cache", DummyCache(), raising=False)
    monkeypatch.setattr(wd.time, "sleep", lambda *_: None)

    cand = wd.fetch("Q12345")
    assert cand.source == "wikidata"
    assert cand.id == "Q12345"
    assert cand.uri == "https://www.wikidata.org/entity/Q12345"
    assert cand.labels.get("en") == "John Doe"
    assert cand.labels.get("de") == "Johann Doe"
    assert "J. Doe" in cand.aliases.get("en", [])
    # Duplikate entfernt
    assert cand.aliases.get("en", []).count("J. Doe") == 1
    assert "Joh. Doe" in cand.aliases.get("de", [])
    assert "Q5" in set(cand.instance_of)
    assert cand.birth_year == 1933
    assert cand.death_year == 2005
    assert "director" in cand.occupations
    assert "actor" in cand.occupations
    assert cand.external_ids.get("gnd") == "118540238"
    assert cand.external_ids.get("viaf") == "123456789"
    assert cand.external_ids.get("isni") == "0000000121032683"
    assert cand.image_url and cand.image_url.startswith("https://")


def test_wikidata_fetch_fallback_on_non_200(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "server error"})

    monkeypatch.setattr(wd, "_client", lambda: _mk_client(handler))
    monkeypatch.setattr(wd, "_cache", DummyCache(), raising=False)
    monkeypatch.setattr(wd.time, "sleep", lambda *_: None)

    cand = wd.fetch("Q999")
    assert cand.source == "wikidata"
    assert cand.id == "Q999"
    assert cand.uri == "https://www.wikidata.org/entity/Q999"
    # Bei Fallback fehlen Details
    assert not cand.labels
    assert not cand.instance_of
    assert cand.birth_year is None
    assert not cand.external_ids
