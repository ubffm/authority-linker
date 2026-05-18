import re
from typing import Any

from authority_linker.text import reorder_person_name, strip_diacritics, strip_name_punctuation

_YEAR_RE = re.compile(r"(\d{4})")
_GND_ID_RE = re.compile(r"\d+(?:-\d+[Xx]?)?[Xx]?")


def name_variants(name: str) -> list[str]:
    """Ermittele alternative Namensschreibweisen inklusive Suffix- und ASCII-Varianten."""
    cleaned = strip_name_punctuation(name)
    seen: set[str] = set()
    variants: list[str] = []

    def add_variant(value: str | None) -> None:
        if not value:
            return
        value = re.sub(r"\s+", " ", value.strip())
        if value and value not in seen:
            seen.add(value)
            variants.append(value)

    add_variant(cleaned)

    parts = [part.strip() for part in cleaned.split(",") if part.strip()]
    if len(parts) >= 2:
        surname = parts[0]
        given = parts[1]
        base = f"{given} {surname}".strip()
        add_variant(base)
        suffix = " ".join(parts[2:]).strip()
        if suffix:
            add_variant(f"{given} {suffix} {surname}")

    add_variant(reorder_person_name(name))

    for value in list(variants):
        ascii_value = strip_diacritics(value)
        ascii_value = re.sub(r"\s+", " ", ascii_value.strip())
        if ascii_value != value:
            add_variant(ascii_value)

    return variants


def binding_value(binding: dict[str, Any] | None) -> str | None:
    """Extrahiere den gereinigten Wert eines SPARQL-Bindings."""
    if not isinstance(binding, dict):
        return None
    value = binding.get("value")
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def binding_text_and_lang(
    binding: dict[str, Any] | None,
    *,
    default_lang: str | None = None,
) -> tuple[str | None, str | None]:
    """Lese Text und optionalen Sprachcode aus einem SPARQL-Binding."""
    value = binding_value(binding)
    if value is None:
        return None, None
    lang: str | None = None
    if isinstance(binding, dict):
        candidate = binding.get("xml:lang")
        if isinstance(candidate, str) and candidate.strip():
            lang = candidate.strip()
    if lang is None:
        lang = default_lang
    return value, lang


def extract_year(value: str | None) -> int | None:
    """Konvertiere einen Textwert in ein vierstelliges Jahr."""
    if not isinstance(value, str):
        return None
    match = _YEAR_RE.search(value.strip())
    return int(match.group(1)) if match else None


def extract_years_from_life_dates(value: str | None) -> tuple[int | None, int | None]:
    """Leite mögliche Geburts- und Sterbejahre aus lifeDates-Texten ab."""
    if not isinstance(value, str):
        return None, None
    clean = value.strip()
    if not clean:
        return None, None
    years = [int(match) for match in re.findall(r"\d{4}", clean)]
    if not years:
        return None, None
    birth: int | None = years[0]
    death: int | None = None
    if len(years) >= 2:
        death = years[1]
    else:
        contains_death_symbol = any(symbol in clean for symbol in ("†", "✝"))
        lowered = clean.lower()
        if contains_death_symbol or any(token in lowered for token in ("gest", "verstor", "d.")):
            birth = None
            death = years[0]
    return birth, death


def is_valid_gnd_id(value: str | None) -> bool:
    """Prüfe, ob ein Wert wie eine gültige GND-Identifikatorstruktur aussieht."""
    if not value:
        return False
    return bool(_GND_ID_RE.fullmatch(value.strip()))
