from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Any

from redis.asyncio import Redis
from uri_resolver.cache.redis import RedisCache
from uri_resolver.providers.oclc import OclcEntityProvider
from uri_resolver.resolver.redis_singleflight import RedisSingleflight
from uri_resolver.resolver.resolver import UriResolver

from authority_linker.context import PersonContext

logger = logging.getLogger(__name__)


def _extract_oclc_id(oclc_link: str) -> str | None:
    """Extrahiere eine numerische OCLC-ID aus typischen WorldCat/OCLC-Links."""
    if not isinstance(oclc_link, str):
        return None
    value = oclc_link.strip()
    if not value:
        return None

    patterns = [
        r"/oclc/(\d+)",
        r"ocm(\d+)",
        r"ocn(\d+)",
        r"on(\d+)",
        r"\(OCoLC\)(\d+)",
        r"^(\d+)$",
    ]
    for pattern in patterns:
        m = re.search(pattern, value, flags=re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def _extract_oclc_entity_id(oclc_link: str) -> str | None:
    """Extrahiere eine WorldCat-Entity-ID aus id.oclc.org-URIs."""
    if not isinstance(oclc_link, str):
        return None
    value = oclc_link.strip()
    if not value:
        return None

    m = re.search(
        r"(?:https?://)?id\.oclc\.org/worldcat/entity/([A-Za-z0-9]+)",
        value,
        flags=re.IGNORECASE,
    )
    if m:
        return m.group(1)
    return None


def _extract_year(value: Any) -> int | None:
    """Extrahiere ein plausibles Jahr aus String/Freitext."""
    if value is None:
        return None
    if isinstance(value, int):
        return value if 0 < value <= datetime.now().year + 1 else None

    text = str(value)
    m = re.search(r"(1[5-9]\d{2}|20\d{2}|2100)", text)
    if not m:
        return None
    year = int(m.group(1))
    if 0 < year <= datetime.now().year + 1:
        return year
    return None


def _pick_first_non_empty(*values: Any) -> str | None:
    """Gib den ersten nicht-leeren String zurück."""
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _merge_existing_ids(
    current: dict[str, str],
    additions: dict[str, str],
) -> dict[str, str]:
    """Führe existing_ids zusammen, OCLC-Werte überschreiben bestehende nur wenn nicht leer."""
    merged = dict(current)
    for key, value in additions.items():
        if not key or not value:
            continue
        merged[key] = value
    return merged


def _first_value(values: dict[str, list[str]] | None, *langs: str) -> str | None:
    if not values:
        return None
    for lang in langs:
        candidates = values.get(lang)
        if candidates:
            first = _pick_first_non_empty(*candidates)
            if first:
                return first
    for candidates in values.values():
        if candidates:
            first = _pick_first_non_empty(*candidates)
            if first:
                return first
    return None


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


async def _resolve_oclc_entity(oclc_uri: str) -> Any:
    redis = Redis.from_url("redis://localhost:6379/0", decode_responses=True)
    try:
        resolver = UriResolver(
            providers=[OclcEntityProvider()],
            redis_cache=RedisCache(redis),
            singleflight=RedisSingleflight(redis),
        )
        return await resolver.resolve(oclc_uri)
    finally:
        await redis.aclose()


def fetch_oclc_normdata(oclc_link: str) -> dict[str, Any] | None:
    """Hole Person-Normdaten zu einem OCLC-Link über den uri_resolver (ohne Fallback)."""
    entity_id = _extract_oclc_entity_id(oclc_link)
    numeric_id = _extract_oclc_id(oclc_link)

    if entity_id:
        oclc_uri = f"https://id.oclc.org/worldcat/entity/{entity_id}"
    elif numeric_id:
        oclc_uri = f"https://www.worldcat.org/oclc/{numeric_id}"
    else:
        return None

    try:
        entity = asyncio.run(_resolve_oclc_entity(oclc_uri))
    except Exception as exc:
        logger.debug("OCLC-Enrichment via uri_resolver fehlgeschlagen für %s: %s", oclc_link, exc)
        return None

    if entity is None:
        return None

    description = _first_value(entity.descriptions, "de", "en")
    if description is None:
        description = _first_value(entity.descriptions)

    payload: dict[str, Any] = {
        "preferred_label": _first_value(entity.labels, "de", "en") or _first_value(entity.labels),
        "birth_year": entity.date_of_birth.year if entity.date_of_birth else None,
        "death_year": entity.date_of_death.year if entity.date_of_death else None,
        "description": description,
        "alt_labels": _all_unique_values(entity.alt_labels),
        "types": [t for t in entity.types if isinstance(t, str) and t.strip()],
        "oclcnum": numeric_id or entity_id or "",
    }

    for same in entity.same_as:
        scheme = (same.scheme or "").strip().lower()
        uri = str(same.uri).strip() if same.uri else ""
        if not scheme or not uri:
            continue
        if scheme == "gnd":
            payload["gnd"] = uri
        elif scheme == "wikidata":
            payload["wikidata"] = uri
        elif scheme == "viaf":
            payload["viaf"] = uri
        elif scheme == "orcid":
            payload["orcid"] = uri
        elif scheme == "isni":
            payload["isni"] = uri
        elif scheme == "oclc":
            payload["oclc"] = uri

    return payload


def enrich_context_with_oclc(ctx: PersonContext) -> PersonContext:
    """Reichere einen PersonContext mit OCLC-Person-Normdaten an (nur uri_resolver)."""
    if not ctx.oclc_links:
        return ctx

    fetched: dict[str, Any] | None = None
    for link in ctx.oclc_links:
        fetched = fetch_oclc_normdata(link)
        if fetched:
            break

    if not fetched:
        return ctx

    preferred_label = _pick_first_non_empty(fetched.get("preferred_label"))
    birth_year = _extract_year(fetched.get("birth_year"))
    death_year = _extract_year(fetched.get("death_year"))
    description = _pick_first_non_empty(fetched.get("description"))

    additions: dict[str, str] = {}
    possible_id_fields = {
        "oclc": ("oclcnum", "oclc"),
        "gnd": ("gnd",),
        "wikidata": ("wikidata",),
        "viaf": ("viaf",),
        "orcid": ("orcid",),
        "isni": ("isni",),
    }
    for target_key, source_keys in possible_id_fields.items():
        for source_key in source_keys:
            raw = fetched.get(source_key)
            if isinstance(raw, str) and raw.strip():
                additions[target_key] = raw.strip()
                break
            if isinstance(raw, list):
                first_val = next(
                    (str(x).strip() for x in raw if isinstance(x, str) and x.strip()),
                    None,
                )
                if first_val:
                    additions[target_key] = first_val
                    break

    merged_existing_ids = _merge_existing_ids(ctx.existing_ids, additions)

    name_pref = preferred_label or ctx.name_pref

    merged_subjects = list(ctx.subjects)
    if description and description not in merged_subjects:
        merged_subjects.append(description)

    merged_cast_raw = list(ctx.cast_raw)
    alt_labels = fetched.get("alt_labels")
    if isinstance(alt_labels, list):
        for label in alt_labels:
            if isinstance(label, str):
                candidate = label.strip()
                if candidate and candidate not in merged_cast_raw:
                    merged_cast_raw.append(candidate)

    types = fetched.get("types")
    if isinstance(types, list):
        for type_name in types:
            if isinstance(type_name, str):
                candidate = type_name.strip()
                if candidate and candidate not in merged_subjects:
                    merged_subjects.append(candidate)

    return ctx.model_copy(
        update={
            "name_pref": name_pref,
            "birth_year": birth_year or ctx.birth_year,
            "death_year": death_year or ctx.death_year,
            "subjects": merged_subjects,
            "cast_raw": merged_cast_raw,
            "existing_ids": merged_existing_ids,
        }
    )
