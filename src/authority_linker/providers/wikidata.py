from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from authority_linker.cache import JsonCache
from authority_linker.config import get_settings
from authority_linker.models import Candidate, CandidateRef
from authority_linker.providers.utils import extract_year, name_variants


def _cache_key(prefix: str, params: dict[str, Any]) -> str:
    """Erzeuge einen Cache-Schlüssel aus Präfix und sortierten Abfrageparametern."""
    return f"{prefix}:{urlencode(sorted(params.items()))}"


def _cache() -> JsonCache:
    settings = get_settings()
    return JsonCache(settings.cache_dir)


def _client() -> httpx.Client:
    """Erstelle einen vorkonfigurierten HTTP-Client für Wikidata-Anfragen."""
    settings = get_settings()
    return httpx.Client(
        headers={"User-Agent": settings.user_agent},
        timeout=httpx.Timeout(10.0, connect=5.0),
    )


def search(name: str, lang: str = "en", limit: int = 10) -> list[CandidateRef]:
    """Suche Kandidaten in Wikidata via wbsearchentities."""
    if not name:
        return []

    settings = get_settings()
    cache = _cache()

    limit = max(1, limit)
    variants = [variant for variant in name_variants(name) if variant]
    if not variants:
        return []

    seen_ids: set[str] = set()
    out: list[CandidateRef] = []

    for term in variants:
        remaining = limit - len(out)
        if remaining <= 0:
            break

        params = {
            "action": "wbsearchentities",
            "search": term,
            "language": lang,
            "uselang": lang,
            "type": "item",
            "limit": remaining,
            "format": "json",
        }
        key = _cache_key("wd:wbsearch", params)
        data: dict[str, Any] | None = cache.get(key)

        if data is None:
            with _client() as client:
                resp = client.get(settings.wikidata_api_endpoint, params=params)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                cache.set(key, data)
                time.sleep(settings.wikidata_rate_limit)

        results = data.get("search", []) if isinstance(data, dict) else []
        for r in results:
            qid = r.get("id")
            if not isinstance(qid, str) or not qid.startswith("Q"):
                continue
            if qid in seen_ids:
                continue
            seen_ids.add(qid)
            label = r.get("label")
            out.append(CandidateRef(source="wikidata", id=qid, label=label, lang=lang))
            if len(out) >= limit:
                return out

    return out


def fetch(ref_id: str) -> Candidate:
    """Lade Fakten zu einer QID via SPARQL und forme sie zu Candidate."""
    settings = get_settings()
    cache = _cache()

    qid = ref_id if ref_id.startswith("Q") else ref_id
    if not re.fullmatch(r"Q\d+", qid):
        raise ValueError("invalid QID")
    query = (
        """
PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
SELECT ?instance ?birth ?death ?gnd ?viaf ?isni
       ?label_de ?label_en ?alias_de ?alias_en ?occ_label ?image
WHERE {
"""
        + f"  VALUES ?item {{ wd:{qid} }}\n"
        + """
  OPTIONAL { ?item wdt:P31 ?instance. }
  OPTIONAL { ?item wdt:P569 ?birth. }
  OPTIONAL { ?item wdt:P570 ?death. }
  OPTIONAL { ?item wdt:P227 ?gnd. }
  OPTIONAL { ?item wdt:P214 ?viaf. }
  OPTIONAL { ?item wdt:P213 ?isni. }
  OPTIONAL {
    ?item wdt:P106 ?occ.
    ?occ rdfs:label ?occ_label.
    FILTER(LANG(?occ_label) IN ("de","en"))
  }
  OPTIONAL { ?item rdfs:label ?label_de FILTER(LANG(?label_de)="de"). }
  OPTIONAL { ?item rdfs:label ?label_en FILTER(LANG(?label_en)="en"). }
  OPTIONAL { ?item skos:altLabel ?alias_de FILTER(LANG(?alias_de)="de"). }
  OPTIONAL { ?item skos:altLabel ?alias_en FILTER(LANG(?alias_en)="en"). }
  OPTIONAL { ?item wdt:P18 ?image. }
}
"""
    )

    params = {"query": query, "format": "json"}
    key = _cache_key("wd:sparql", params)
    data: dict[str, Any] | None = cache.get(key)

    if data is None:
        with _client() as client:
            resp = client.get(
                settings.wikidata_sparql_endpoint,
                params=params,
                headers={
                    "Accept": "application/sparql-results+json",
                    "User-Agent": settings.user_agent,
                },
            )
            if resp.status_code != 200:
                # Fallback: liefere Minimal-Kandidat
                return Candidate(
                    source="wikidata",
                    id=qid,
                    uri=f"https://www.wikidata.org/entity/{qid}",
                )
            data = resp.json()
            cache.set(key, data)
            time.sleep(settings.wikidata_rate_limit)

    bindings = data.get("results", {}).get("bindings", []) if isinstance(data, dict) else []

    labels: dict[str, str] = {}
    aliases: dict[str, list[str]] = {"de": [], "en": []}
    inst_qids: set[str] = set()
    occs: set[str] = set()
    birth_year: int | None = None
    death_year: int | None = None
    gnd: str | None = None
    viaf: str | None = None
    isni: str | None = None
    image_url: str | None = None

    for b in bindings:
        if "label_de" in b:
            labels["de"] = b["label_de"]["value"]
        if "label_en" in b:
            labels["en"] = b["label_en"]["value"]
        if "alias_de" in b:
            aliases.setdefault("de", []).append(b["alias_de"]["value"])
        if "alias_en" in b:
            aliases.setdefault("en", []).append(b["alias_en"]["value"])
        if "instance" in b:
            uri = b["instance"]["value"]
            inst_qids.add(uri.rsplit("/", 1)[-1])
        if "occ_label" in b:
            occs.add(b["occ_label"]["value"])
        if "birth" in b and birth_year is None:
            birth_year = extract_year(b["birth"]["value"])
        if "death" in b and death_year is None:
            death_year = extract_year(b["death"]["value"])
        if "gnd" in b and gnd is None:
            gnd = b["gnd"]["value"]
        if "viaf" in b and viaf is None:
            viaf = b["viaf"]["value"]
        if "isni" in b and isni is None:
            isni = b["isni"]["value"]
        if "image" in b and image_url is None:
            image_url = b["image"]["value"]

    # dedupliziere Aliasse
    aliases = {k: sorted(set(v)) for k, v in aliases.items() if v}

    external_ids = {
        "gnd": gnd,
        "viaf": viaf,
        "isni": isni,
    }
    external_ids = {k: v for k, v in external_ids.items() if v}

    return Candidate(
        source="wikidata",
        id=qid,
        uri=f"https://www.wikidata.org/entity/{qid}",
        labels=labels,
        aliases=aliases,
        instance_of=sorted(inst_qids),
        birth_year=birth_year,
        death_year=death_year,
        occupations=sorted(occs),
        external_ids=external_ids,
        image_url=image_url,
    )
