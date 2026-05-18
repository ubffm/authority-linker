from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from importlib import resources
from typing import Any
from urllib.parse import urlencode

import httpx
from jinja2 import Environment, StrictUndefined
from redis.asyncio import Redis
from uri_resolver.cache.redis import RedisCache
from uri_resolver.providers.gnd import GNDProvider
from uri_resolver.resolver.redis_singleflight import RedisSingleflight
from uri_resolver.resolver.resolver import UriResolver

from authority_linker.cache import JsonCache
from authority_linker.config import get_settings
from authority_linker.models import Candidate, CandidateRef
from authority_linker.providers.gnd_subjects import build_gnd_sc_map_from_ttl, resolve_gnd_sc
from authority_linker.providers.utils import (
    binding_text_and_lang,
    binding_value,
    extract_year,
    extract_years_from_life_dates,
    is_valid_gnd_id,
    name_variants,
)

_sparql_env = Environment(
    autoescape=False,
    trim_blocks=True,
    lstrip_blocks=True,
    undefined=StrictUndefined,
)

_GND_FETCH_BASICS_QUERY_TEMPLATE = _sparql_env.from_string(
    """\
PREFIX gndo: <https://d-nb.info/standards/elementset/gnd#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
SELECT DISTINCT ?pref ?variant ?birth ?death ?life_dates ?same_as ?type
WHERE {
  VALUES ?person {
    {% for uri in person_uris %}
    <{{ uri }}>
    {% endfor %}
  }
  OPTIONAL { ?person gndo:preferredNameForThePerson ?pref. }
  OPTIONAL { ?person gndo:variantNameForThePerson ?variant. }
  OPTIONAL { ?person gndo:dateOfBirth ?birth. }
  OPTIONAL { ?person gndo:dateOfDeath ?death. }
  OPTIONAL { ?person gndo:lifeDates ?life_dates. }
  OPTIONAL { ?person owl:sameAs ?same_as. }
  OPTIONAL { ?person a ?type. }
}
"""
)

_GND_FETCH_OCCUPATIONS_QUERY_TEMPLATE = _sparql_env.from_string(
    """\
PREFIX gndo: <https://d-nb.info/standards/elementset/gnd#>
SELECT DISTINCT ?p ?occ_node ?occ_label
WHERE {
  VALUES ?person {
    {% for uri in person_uris %}
    <{{ uri }}>
    {% endfor %}
  }
  ?person gndo:professionOrOccupation ?occ_seq.
  ?occ_seq ?p ?occ_node.
  OPTIONAL { ?occ_node gndo:preferredNameForTheSubjectHeading ?occ_label. }
}
"""
)

_GND_FETCH_SC_QUERY_TEMPLATE = _sparql_env.from_string(
    """\
PREFIX gndo: <https://d-nb.info/standards/elementset/gnd#>
SELECT DISTINCT ?sc
WHERE {
  VALUES ?person {
    {% for uri in person_uris %}
    <{{ uri }}>
    {% endfor %}
  }
  ?person gndo:gndSubjectCategory ?sc.
}
"""
)

logger = logging.getLogger(__name__)


def _settings():
    return get_settings()


def _cache() -> JsonCache:
    return JsonCache(_settings().cache_dir)


def _person_uris(gid: str) -> list[str]:
    return [
        f"https://d-nb.info/gnd/{gid}",
        f"http://d-nb.info/gnd/{gid}",
    ]


def _render_gnd_fetch_query(gid: str) -> str:
    return _GND_FETCH_BASICS_QUERY_TEMPLATE.render(person_uris=_person_uris(gid))


def _render_gnd_fetch_basics_query(gid: str) -> str:
    return _GND_FETCH_BASICS_QUERY_TEMPLATE.render(person_uris=_person_uris(gid))


def _render_gnd_fetch_occupations_query(gid: str) -> str:
    return _GND_FETCH_OCCUPATIONS_QUERY_TEMPLATE.render(person_uris=_person_uris(gid))


def _render_gnd_fetch_subject_categories_query(gid: str) -> str:
    return _GND_FETCH_SC_QUERY_TEMPLATE.render(person_uris=_person_uris(gid))


def _cache_key(prefix: str, params: dict[str, Any]) -> str:
    """Erzeuge einen Cache-Schlüssel aus Präfix und sortierten Abfrageparametern."""
    return f"{prefix}:{urlencode(sorted(params.items()))}"


def _cache_query_token(query: str) -> str:
    """Bilde einen stabilen Hash für eine SPARQL-Abfrage."""
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def _client() -> httpx.Client:
    """Erstelle einen vorkonfigurierten HTTP-Client für lobid-Anfragen."""
    settings = _settings()
    return httpx.Client(
        headers={
            "User-Agent": settings.user_agent,
            "Accept": "application/json",
        },
        timeout=httpx.Timeout(10.0, connect=5.0),
    )


def _sparql_client() -> httpx.Client:
    """Erstelle einen HTTP-Client für lobid-SPARQL-Abfragen."""
    settings = _settings()
    return httpx.Client(
        headers={
            "User-Agent": settings.user_agent,
            "Accept": "application/sparql-results+json",
        },
        timeout=httpx.Timeout(20.0, connect=5.0),
    )


def _fetch_sparql_json(query: str, cache_key: str, gid: str) -> dict[str, Any] | None:
    settings = _settings()
    cache = _cache()

    data: dict[str, Any] | None = cache.get(cache_key)
    if data is not None:
        return data

    params = {"query": query, "format": "application/sparql-results+json"}

    with _sparql_client() as client:
        try:
            resp = client.get(settings.gnd_sparql_endpoint, params=params)
            resp.raise_for_status()
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            logger.warning(
                "GND-SPARQL-Abfrage für %s fehlgeschlagen (HTTP/Timeout): %s",
                gid,
                exc,
                exc_info=True,
            )
            return None
        try:
            data = resp.json()
        except ValueError as exc:
            logger.warning(
                "Antwort der GND-SPARQL-Abfrage für %s konnte nicht als JSON geparst werden: %s",
                gid,
                exc,
                exc_info=True,
            )
            return None

    cache.set(cache_key, data)
    time.sleep(settings.wikidata_rate_limit)
    return data


def _read_local_gnd_sc_ttl() -> str | None:
    try:
        ttl_resource = resources.files("authority_linker.data").joinpath("gnd-sc.ttl")
        return ttl_resource.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):
        return None


def _fetch_gnd_sc_ttl() -> str | None:
    cache = _cache()

    key = "gnd:sc:ttl"
    cached = cache.get(key)
    if isinstance(cached, str) and cached.strip():
        return cached

    local_ttl = _read_local_gnd_sc_ttl()
    if local_ttl and local_ttl.strip():
        cache.set(key, local_ttl)
        return local_ttl

    with _client() as client:
        try:
            resp = client.get("https://d-nb.info/standards/vocab/gnd/gnd-sc.ttl")
            resp.raise_for_status()
            text = resp.text
        except (httpx.TimeoutException, httpx.HTTPError):
            return None

    if text.strip():
        cache.set(key, text)
        return text
    return None


def _extract_sc_code(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if "#" in candidate:
        candidate = candidate.rsplit("#", 1)[-1].strip()
    elif candidate.startswith("http://") or candidate.startswith("https://"):
        candidate = candidate.rsplit("/", 1)[-1].strip()
    if not candidate:
        return None
    return candidate


def _subject_categories_from_sc_values(sc_values: set[str]) -> list[str]:
    ttl = _fetch_gnd_sc_ttl()
    if not ttl:
        return sorted(sc_values)

    sc_map = build_gnd_sc_map_from_ttl(ttl)
    resolved_labels: list[str] = []
    for raw in sorted(sc_values):
        resolved = resolve_gnd_sc(raw, sc_map=sc_map)
        if not resolved:
            resolved_labels.append(raw)
            continue
        label = resolved.get("label_de") or resolved.get("label_en") or resolved.get("code") or raw
        label = label.strip()
        if label:
            resolved_labels.append(label)

    # dedupe case-insensitive, Reihenfolge stabil
    seen: set[str] = set()
    out: list[str] = []
    for label in resolved_labels:
        key = label.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(label)
    return out


def search_gnd(name: str, year: int | None = None) -> list[CandidateRef]:
    """Suche GND-Kandidaten über lobid.org/gnd."""
    if not name:
        return []

    settings = _settings()
    cache = _cache()

    variants = [variant for variant in name_variants(name) if variant]
    if not variants:
        return []

    seen_ids: set[str] = set()
    out: list[CandidateRef] = []

    for variant in variants:
        term = variant
        params = {"q": term, "filter": "type:Person", "format": "json"}
        key = _cache_key("gnd:search", params)
        data: dict[str, Any] | None = cache.get(key)

        if data is None:
            with _client() as client:
                resp = client.get(f"{settings.gnd_api_base}search", params=params)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                cache.set(key, data)
                time.sleep(settings.wikidata_rate_limit)

        members = data.get("member", []) if isinstance(data, dict) else []
        for m in members:
            gnd_id: str | None = None
            label: str | None = None
            if isinstance(m, dict):
                gnd_id = m.get("gndIdentifier")
                if not gnd_id and isinstance(m.get("@id"), str):
                    at_id = m["@id"]
                    gnd_id = at_id.rsplit("/", 1)[-1]
                label = m.get("preferredName") or m.get("name")
            if not isinstance(gnd_id, str) or not gnd_id.strip():
                continue
            normalized_id = gnd_id.strip()
            if normalized_id in seen_ids:
                continue
            seen_ids.add(normalized_id)
            out.append(CandidateRef(source="gnd", id=normalized_id, label=label, lang="de"))

    return out


def _extract_external_id_from_uri(uri: str, marker: str) -> str | None:
    value = uri.strip()
    if marker not in value:
        return None
    return value.rsplit("/", 1)[-1].strip() or None


def _to_candidate_from_resolved_entity(entity: Any, gid: str) -> Candidate:
    labels: dict[str, str] = {}
    aliases: dict[str, list[str]] = {}
    occupations: list[str] = []
    descriptions: dict[str, list[str]] = {}
    subject_categories: list[str] = []
    instance_of: list[str] = []
    external_ids: dict[str, str] = {"gnd": gid}

    if isinstance(getattr(entity, "labels", None), dict):
        for lang, values in entity.labels.items():
            if not isinstance(lang, str) or not isinstance(values, list):
                continue
            first = next((v.strip() for v in values if isinstance(v, str) and v.strip()), None)
            if first:
                labels[lang] = first

    if isinstance(getattr(entity, "alt_labels", None), dict):
        for lang, values in entity.alt_labels.items():
            if not isinstance(lang, str) or not isinstance(values, list):
                continue
            cleaned = sorted({v.strip() for v in values if isinstance(v, str) and v.strip()})
            if cleaned:
                aliases[lang] = cleaned

    if isinstance(getattr(entity, "professions", None), dict):
        occ_set: set[str] = set()
        for values in entity.professions.values():
            if not isinstance(values, list):
                continue
            for v in values:
                if isinstance(v, str) and v.strip():
                    occ_set.add(v.strip().lower())
        occupations = sorted(occ_set)

    if isinstance(getattr(entity, "descriptions", None), dict):
        for lang, values in entity.descriptions.items():
            if not isinstance(lang, str) or not isinstance(values, list):
                continue
            cleaned = sorted({v.strip() for v in values if isinstance(v, str) and v.strip()})
            if cleaned:
                descriptions[lang] = cleaned

    if isinstance(getattr(entity, "subject_categories", None), list):
        raw_values = sorted(
            {v.strip() for v in entity.subject_categories if isinstance(v, str) and v.strip()}
        )
        subject_categories = _subject_categories_from_sc_values(set(raw_values))

    if isinstance(getattr(entity, "types", None), list):
        instance_of = [t.strip() for t in entity.types if isinstance(t, str) and t.strip()]

    birth_year = entity.date_of_birth.year if getattr(entity, "date_of_birth", None) else None
    death_year = entity.date_of_death.year if getattr(entity, "date_of_death", None) else None

    if isinstance(getattr(entity, "same_as", None), list):
        for same in entity.same_as:
            scheme = (getattr(same, "scheme", "") or "").strip().lower()
            uri = str(getattr(same, "uri", "") or "").strip()
            if not scheme or not uri:
                continue
            if scheme == "viaf":
                viaf_id = _extract_external_id_from_uri(uri, "viaf.org/viaf/")
                if viaf_id:
                    external_ids["viaf"] = viaf_id
            elif scheme == "isni":
                isni_id = _extract_external_id_from_uri(uri, "isni.org/isni/")
                if isni_id:
                    external_ids["isni"] = isni_id
            elif scheme == "wikidata":
                wikidata_id = _extract_external_id_from_uri(uri, "wikidata.org/entity/")
                if wikidata_id:
                    external_ids["wikidata"] = wikidata_id

    return Candidate(
        source="gnd",
        id=gid,
        uri=f"https://d-nb.info/gnd/{gid}",
        labels=labels,
        aliases=aliases,
        instance_of=instance_of,
        birth_year=birth_year,
        death_year=death_year,
        occupations=occupations,
        descriptions=descriptions,
        subject_categories=subject_categories,
        external_ids=external_ids,
    )


async def _resolve_gnd_entity(gid: str) -> Any:
    redis = Redis.from_url("redis://localhost:6379/0", decode_responses=True)
    try:
        resolver = UriResolver(
            providers=[GNDProvider()],
            redis_cache=RedisCache(redis),
            singleflight=RedisSingleflight(redis),
        )
        return await resolver.resolve(f"https://d-nb.info/gnd/{gid}")
    finally:
        await redis.aclose()


def fetch_gnd_via_resolver(gnd_id: str) -> Candidate:
    """Lade GND-Daten über UriResolver und mappe sie auf Candidate."""
    gid = gnd_id.strip()
    if not is_valid_gnd_id(gid):
        raise ValueError("invalid GND id")
    gid = gid.upper()

    try:
        entity = asyncio.run(_resolve_gnd_entity(gid))
    except Exception as exc:
        logger.warning("GND-Resolver-Abfrage für %s fehlgeschlagen: %s", gid, exc, exc_info=True)
        return Candidate(
            source="gnd",
            id=gid,
            uri=f"https://d-nb.info/gnd/{gid}",
        )

    if entity is None:
        return Candidate(
            source="gnd",
            id=gid,
            uri=f"https://d-nb.info/gnd/{gid}",
        )

    return _to_candidate_from_resolved_entity(entity, gid)


def _fetch_gnd_via_lobid(gnd_id: str) -> Candidate:
    """Lade GND-Daten (über lobid) und konvertiere sie zu Candidate."""
    gid = gnd_id.strip()
    if not is_valid_gnd_id(gid):
        raise ValueError("invalid GND id")
    gid = gid.upper()

    basics_query = _render_gnd_fetch_basics_query(gid)
    basics_key = _cache_key(
        "gnd:entity:basics",
        {"id": gid, "query_hash": _cache_query_token(basics_query)},
    )
    basics_data = _fetch_sparql_json(basics_query, basics_key, gid)

    if basics_data is None:
        return Candidate(
            source="gnd",
            id=gid,
            uri=f"https://d-nb.info/gnd/{gid}",
        )

    basics_bindings = (
        basics_data.get("results", {}).get("bindings", []) if isinstance(basics_data, dict) else []
    )

    labels: dict[str, str] = {}
    aliases: dict[str, list[str]] = {}
    occupations: set[str] = set()
    instance_of_values: set[str] = set()
    birth_year: int | None = None
    death_year: int | None = None
    viaf_id: str | None = None
    isni_id: str | None = None
    life_dates_values: list[str] = []

    for row in basics_bindings:
        pref_value, pref_lang = binding_text_and_lang(row.get("pref"), default_lang="de")
        if pref_value:
            lang = pref_lang or "und"
            labels.setdefault(lang, pref_value)

        variant_value, variant_lang = binding_text_and_lang(row.get("variant"), default_lang="de")
        if variant_value:
            lang = variant_lang or "und"
            aliases.setdefault(lang, []).append(variant_value)

        if birth_year is None:
            birth_value = binding_value(row.get("birth"))
            if birth_value:
                birth_year = extract_year(birth_value)

        if death_year is None:
            death_value = binding_value(row.get("death"))
            if death_value:
                death_year = extract_year(death_value)

        life_value = binding_value(row.get("life_dates"))
        if life_value:
            life_dates_values.append(life_value)

        same_as_value = binding_value(row.get("same_as"))
        if same_as_value and viaf_id is None:
            normalized_viaf = same_as_value.strip()
            if normalized_viaf.startswith("http://viaf.org/viaf/"):
                viaf_id = normalized_viaf.removeprefix("http://viaf.org/viaf/")
            elif normalized_viaf.startswith("https://viaf.org/viaf/"):
                viaf_id = normalized_viaf.removeprefix("https://viaf.org/viaf/")

        if same_as_value and isni_id is None:
            normalized_isni = same_as_value.replace(" ", "")
            if normalized_isni.startswith("https://isni.org/isni/"):
                isni_id = normalized_isni.removeprefix("https://isni.org/isni/")
            elif normalized_isni.startswith("http://isni.org/isni/"):
                isni_id = normalized_isni.removeprefix("http://isni.org/isni/")
            elif len(normalized_isni) == 16 and normalized_isni.isdigit():
                isni_id = normalized_isni

        type_value = binding_value(row.get("type"))
        if type_value:
            stripped_type = type_value.strip()
            if stripped_type:
                instance_of_values.add(stripped_type)

    if (birth_year is None or death_year is None) and life_dates_values:
        for life_value in life_dates_values:
            life_birth, life_death = extract_years_from_life_dates(life_value)
            if birth_year is None and life_birth is not None:
                birth_year = life_birth
            if death_year is None and life_death is not None:
                death_year = life_death
            if birth_year is not None and death_year is not None:
                break

    occupations_query = _render_gnd_fetch_occupations_query(gid)
    occupations_key = _cache_key(
        "gnd:entity:occupations",
        {"id": gid, "query_hash": _cache_query_token(occupations_query)},
    )
    occupations_data = _fetch_sparql_json(occupations_query, occupations_key, gid)

    if occupations_data is not None:
        occupation_bindings = (
            occupations_data.get("results", {}).get("bindings", [])
            if isinstance(occupations_data, dict)
            else []
        )
        for row in occupation_bindings:
            predicate_value = binding_value(row.get("p"))
            if not predicate_value or not predicate_value.startswith(
                "http://www.w3.org/1999/02/22-rdf-syntax-ns#_"
            ):
                continue

            occ_label_value = binding_value(row.get("occ_label"))
            if occ_label_value:
                occ_text = occ_label_value.strip()
                if occ_text:
                    occupations.add(occ_text.lower())
                continue

            occ_value = binding_value(row.get("occ_node"))
            if occ_value:
                occ_text = occ_value.rsplit("/", 1)[-1]
                if "#" in occ_text:
                    occ_text = occ_text.split("#", 1)[-1]
                occ_text = occ_text.replace("_", " ").strip()
                if occ_text and any(ch.isalpha() for ch in occ_text):
                    occupations.add(occ_text.lower())

    sc_query = _render_gnd_fetch_subject_categories_query(gid)
    sc_key = _cache_key(
        "gnd:entity:subject_categories",
        {"id": gid, "query_hash": _cache_query_token(sc_query)},
    )
    sc_data = _fetch_sparql_json(sc_query, sc_key, gid)

    sc_values: set[str] = set()
    if sc_data is not None:
        sc_bindings = (
            sc_data.get("results", {}).get("bindings", []) if isinstance(sc_data, dict) else []
        )
        for row in sc_bindings:
            raw = binding_value(row.get("sc"))
            code = _extract_sc_code(raw)
            if code:
                sc_values.add(code)

    subject_categories = _subject_categories_from_sc_values(sc_values)

    aliases = {lang: sorted(set(values)) for lang, values in aliases.items() if values}
    occupations_sorted = sorted(occupations)
    instance_of_sorted = sorted(instance_of_values)

    external_ids = {
        "gnd": gid,
        "viaf": viaf_id,
        "isni": isni_id,
    }
    external_ids = {k: v for k, v in external_ids.items() if v}

    return Candidate(
        source="gnd",
        id=gid,
        uri=f"https://d-nb.info/gnd/{gid}",
        labels=labels,
        aliases=aliases,
        instance_of=instance_of_sorted,
        birth_year=birth_year,
        death_year=death_year,
        occupations=occupations_sorted,
        subject_categories=subject_categories,
        external_ids=external_ids,
    )


def fetch_gnd(gnd_id: str, *, use_uri_resolver: bool | None = None) -> Candidate:
    """Lade GND-Daten; standardmäßig über Settings via UriResolver konfiguriert."""
    settings = _settings()
    use_resolver = settings.use_gnd_uri_resolver if use_uri_resolver is None else use_uri_resolver

    if use_resolver:
        return fetch_gnd_via_resolver(gnd_id)

    return _fetch_gnd_via_lobid(gnd_id)
