from __future__ import annotations

import re
import unicodedata

_NAME_STRIP = " ,;:/"
_MULTI_SPACE = re.compile(r"\s+")


def _collapse_whitespace(value: str) -> str:
    return _MULTI_SPACE.sub(" ", value).strip()


def strip_name_punctuation(value: str) -> str:
    """Trimme führende/abschließende Satzzeichen und kollabiere Leerzeichen."""
    if not value:
        return ""
    return _collapse_whitespace(value.strip(_NAME_STRIP))


def strip_diacritics(value: str) -> str:
    """Entferne diakritische Zeichen (ASCII-Fold)."""
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def reorder_person_name(name: str) -> str:
    """Wandle 'Nachname, Vorname [, Suffix]' in 'Vorname [Suffix] Nachname' um."""
    base = strip_name_punctuation(name)
    if "," in base:
        parts = [part.strip(_NAME_STRIP) for part in base.split(",") if part.strip(_NAME_STRIP)]
        if len(parts) == 2:
            base = f"{parts[1]} {parts[0]}"
        elif len(parts) > 2:
            reordered = parts[1:]
            reordered.append(parts[0])
            base = " ".join(reordered)
    return strip_name_punctuation(base)


def normalize_name(name: str) -> str:
    """Normalisiere Namen für exakten String-Vergleich."""
    if not name:
        return ""
    reordered = reorder_person_name(name)
    folded = strip_diacritics(reordered)
    return _collapse_whitespace(folded).strip(_NAME_STRIP)


def normalize_role(role: str) -> str:
    """Rollen/Occupations vereinheitlichen (klein, trim, Interpunktion ab)."""
    if not role:
        return ""
    lowered = role.lower()
    collapsed = _collapse_whitespace(lowered)
    return collapsed.strip(" .,:;")


__all__ = [
    "normalize_name",
    "normalize_role",
    "reorder_person_name",
    "strip_diacritics",
    "strip_name_punctuation",
]
