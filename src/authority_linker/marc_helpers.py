from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

__all__ = [
    "load_records",
    "first_subfield",
    "all_subfields",
    "base_fields_from_record",
    "fields_per_700",
]


def record_id_from_record(record: dict[str, Any]) -> str:
    """Extrahiere eine 001-ähnliche Kennung aus einem Record."""
    value = record.get("001")
    if isinstance(value, str):
        candidate = value.strip()
        if candidate:
            return candidate
    if isinstance(value, dict):
        candidate = value.get("a")
        if isinstance(candidate, str):
            candidate = candidate.strip()
            if candidate:
                return candidate

    fields = record.get("fields")
    if isinstance(fields, list):
        for field in fields:
            if not isinstance(field, dict):
                continue
            f001 = field.get("001")
            if isinstance(f001, str):
                candidate = f001.strip()
                if candidate:
                    return candidate
            if isinstance(f001, dict):
                candidate = f001.get("a")
                if isinstance(candidate, str):
                    candidate = candidate.strip()
                    if candidate:
                        return candidate

    return ""


def load_records(path: str | Path) -> list[dict[str, Any]]:
    """Lade eine Liste von MARC-Datensätzen aus einer JSON-Datei."""
    file_path = Path(path)
    with file_path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        msg = f"Erwarte eine Liste von Datensätzen in {file_path}, erhalten: {type(data)!r}"
        raise ValueError(msg)
    return [record for record in data if isinstance(record, dict)]


def first_subfield(subfields: Iterable[dict[str, Any]], code: str) -> str | None:
    """Gib den ersten nicht-leeren Subfield-Wert für den gewünschten Code zurück."""
    for subfield in subfields:
        if not isinstance(subfield, dict):
            continue
        value = subfield.get(code)
        if isinstance(value, str):
            candidate = value.strip()
            if candidate:
                return candidate
    return None


def all_subfields(subfields: Iterable[dict[str, Any]], code: str) -> list[str]:
    """Sammle alle nicht-leeren Subfield-Werte für den gewünschten Code."""
    values: list[str] = []
    for subfield in subfields:
        if not isinstance(subfield, dict):
            continue
        value = subfield.get(code)
        if isinstance(value, str):
            candidate = value.strip()
            if candidate:
                values.append(candidate)
    return values


def base_fields_from_record(rec: dict[str, Any]) -> dict[str, Any]:
    """Extrahiere recordweite Felder (245/260/264/518/041/040/043/655/001/035$a/008)."""
    base: dict[str, Any] = {}
    fields = rec.get("fields", [])
    if not isinstance(fields, list):
        return base

    for field in fields:
        if not isinstance(field, dict):
            continue
        if "001" in field and isinstance(field["001"], str):
            base["001"] = field["001"]
        if "008" in field and isinstance(field["008"], str):
            base["008"] = field["008"]

    for field in fields:
        if not isinstance(field, dict):
            continue
        if "245" in field and isinstance(field["245"], dict) and "245" not in base:
            sub = field["245"].get("subfields", [])
            base["245"] = {
                "a": first_subfield(sub, "a"),
                "b": first_subfield(sub, "b"),
            }
        if "260" in field and isinstance(field["260"], dict) and "260" not in base:
            sub = field["260"].get("subfields", [])
            base["260"] = {"c": first_subfield(sub, "c")}
        if "264" in field and isinstance(field["264"], dict) and "264" not in base:
            sub = field["264"].get("subfields", [])
            base["264"] = {
                "a": first_subfield(sub, "a"),
                "c": first_subfield(sub, "c"),
            }
        if "518" in field and isinstance(field["518"], dict) and "518" not in base:
            sub = field["518"].get("subfields", [])
            base["518"] = {"a": first_subfield(sub, "a")}
        if "041" in field and isinstance(field["041"], dict) and "041" not in base:
            sub = field["041"].get("subfields", [])
            base["041"] = {"a": first_subfield(sub, "a")}
        if "040" in field and isinstance(field["040"], dict) and "040" not in base:
            sub = field["040"].get("subfields", [])
            base["040"] = {"b": first_subfield(sub, "b")}
        if "043" in field and isinstance(field["043"], dict) and "043" not in base:
            sub = field["043"].get("subfields", [])
            base["043"] = {"a": first_subfield(sub, "a")}
        if "035" in field and isinstance(field["035"], dict) and "035a" not in base:
            sub = field["035"].get("subfields", [])
            base["035a"] = first_subfield(sub, "a")

    subjects: list[str] = []
    for field in fields:
        if not isinstance(field, dict):
            continue
        if "655" in field and isinstance(field["655"], dict):
            sub = field["655"].get("subfields", [])
            subjects.extend(all_subfields(sub, "a"))
    if subjects:
        base["655"] = {"a": subjects}

    return base


def fields_per_700(rec: dict[str, Any]) -> list[dict[str, Any]]:
    """Erzeuge je 700-Feld ein vereinfachtes fields-Dict für build_person_context."""
    base = base_fields_from_record(rec)
    result: list[dict[str, Any]] = []
    fields = rec.get("fields", [])
    if not isinstance(fields, list):
        return result

    for field in fields:
        if not isinstance(field, dict):
            continue
        entry = field.get("700")
        if not isinstance(entry, dict):
            continue
        subfields = entry.get("subfields", [])
        if not isinstance(subfields, list):
            continue

        name = first_subfield(subfields, "a")
        if not name:
            continue
        date_value = first_subfield(subfields, "d")
        roles = all_subfields(subfields, "e")
        ids_0 = all_subfields(subfields, "0")
        ids_1 = all_subfields(subfields, "1")

        person_fields = dict(base)
        person_fields["700"] = {"a": name, "d": date_value, "e": roles}
        if ids_0:
            person_fields["700"]["0"] = ids_0 if len(ids_0) > 1 else ids_0[0]
        if ids_1:
            person_fields["700"]["1"] = ids_1 if len(ids_1) > 1 else ids_1[0]
        result.append(person_fields)
    return result
