from __future__ import annotations

import html
import re
from functools import lru_cache

_GND_SC_BASE_URI = "https://d-nb.info/standards/vocab/gnd/gnd-sc#"

_CONCEPT_BLOCK_RE = re.compile(
    r"<(?P<uri>https://d-nb\.info/standards/vocab/gnd/gnd-sc#[^>]+)>\s*a\s+skos:Concept\s*;\s*(?P<body>.*?)\s*\.",
    re.DOTALL,
)

_PREF_LABEL_RE = re.compile(
    r"skos:prefLabel\s+(?P<labels>.*?)(?:\s*;|\s*$)",
    re.DOTALL,
)

_LABEL_LANG_RE = re.compile(r'"(?P<label>(?:[^"\\]|\\.)*)"\s*@(?P<lang>[a-zA-Z-]+)')
_NOTATION_RE = re.compile(r'skos:notation\s+"(?P<notation>[^"]+)"')


def _extract_text_from_html_if_needed(raw: str) -> str:
    if "<pre" not in raw and "</pre>" not in raw:
        return raw
    match = re.search(r"<pre[^>]*>(?P<content>.*?)</pre>", raw, flags=re.DOTALL | re.IGNORECASE)
    if not match:
        return raw
    return html.unescape(match.group("content"))


def _normalize_code(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None

    if "#" in candidate:
        candidate = candidate.rsplit("#", 1)[-1].strip()
    elif candidate.startswith("http://") or candidate.startswith("https://"):
        candidate = candidate.rsplit("/", 1)[-1].strip()

    if candidate.startswith("gnd-sc#"):
        candidate = candidate.split("#", 1)[-1].strip()

    return candidate or None


def _parse_pref_labels(block_body: str) -> tuple[str | None, str | None]:
    pref_match = _PREF_LABEL_RE.search(block_body)
    if not pref_match:
        return None, None

    labels_blob = pref_match.group("labels")
    label_de: str | None = None
    label_en: str | None = None

    for match in _LABEL_LANG_RE.finditer(labels_blob):
        raw_label = match.group("label")
        lang = match.group("lang").lower()
        label = bytes(raw_label, "utf-8").decode("unicode_escape").strip()
        if not label:
            continue
        if lang == "de" and label_de is None:
            label_de = label
        elif lang == "en" and label_en is None:
            label_en = label

    return label_de, label_en


@lru_cache(maxsize=1)
def build_gnd_sc_map_from_ttl(ttl_content: str) -> dict[str, dict[str, str]]:
    """Baue eine SC-Map aus dem gelieferten GND-Sachgruppen-TTL-Inhalt."""
    text = _extract_text_from_html_if_needed(ttl_content)
    out: dict[str, dict[str, str]] = {}

    for concept_match in _CONCEPT_BLOCK_RE.finditer(text):
        uri = concept_match.group("uri").strip()
        body = concept_match.group("body")

        notation_match = _NOTATION_RE.search(body)
        code = notation_match.group("notation").strip() if notation_match else _normalize_code(uri)
        if not code:
            continue

        label_de, label_en = _parse_pref_labels(body)
        out[code] = {
            "code": code,
            "uri": uri if uri else f"{_GND_SC_BASE_URI}{code}",
            "label_de": label_de or "",
            "label_en": label_en or "",
        }

    return out


def resolve_gnd_sc(
    value: str | None,
    *,
    sc_map: dict[str, dict[str, str]] | None = None,
) -> dict[str, str] | None:
    """Löse eine GND-Sachgruppe (Code oder URI) zu Code/URI/Labels auf."""
    code = _normalize_code(value)
    if not code:
        return None

    mapping = sc_map or {}
    hit = mapping.get(code)
    if hit:
        uri = hit.get("uri") or f"{_GND_SC_BASE_URI}{code}"
        return {
            "code": code,
            "uri": uri,
            "label_de": (hit.get("label_de") or "").strip(),
            "label_en": (hit.get("label_en") or "").strip(),
        }

    if not code.endswith("*"):
        coarse = f"{code.split('.', 1)[0]}*"
        coarse_hit = mapping.get(coarse)
        if coarse_hit:
            return {
                "code": code,
                "uri": f"{_GND_SC_BASE_URI}{code}",
                "label_de": (coarse_hit.get("label_de") or "").strip(),
                "label_en": (coarse_hit.get("label_en") or "").strip(),
            }

    return None
