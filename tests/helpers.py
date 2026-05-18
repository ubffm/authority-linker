from __future__ import annotations

from pathlib import Path
from typing import Any

from authority_linker.context import PersonContext, build_person_context
from authority_linker.marc_helpers import base_fields_from_record, fields_per_700, load_records, record_id_from_record


def _iter_ctx_values(value: Any):
    """Liefere rekursiv alle String-Werte aus verschachtelten Strukturen."""
    if value is None:
        return
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, dict):
        for v in value.values():
            yield from _iter_ctx_values(v)
        return
    if isinstance(value, (list, tuple, set)):
        for v in value:
            yield from _iter_ctx_values(v)
        return


def _ctx_contains_oclc_link(ctx: PersonContext, wanted: str) -> bool:
    """
    Prüfe robust, ob ein PersonContext den gesuchten OCLC-Link enthält.

    Hintergrund:
    PersonContext hat nicht zwingend ein Attribut `identifiers`.
    Deshalb durchsuchen wir das serialisierte Model rekursiv nach Stringwerten.
    """
    data = ctx.model_dump()

    for raw in _iter_ctx_values(data):
        current = raw.strip().rstrip("/")
        if current == wanted:
            return True

    return False


def find_person_context_by_oclc_link(
    *,
    input_dir: Path,
    target_oclc_link: str,
    pattern: str = "*.json",
) -> tuple[PersonContext, str] | None:
    """
    Durchsuche MARC-JSON-Dateien in einem Verzeichnis nach einem PersonContext,
    dessen OCLC-Link passt.

    Es wird beim ersten Treffer abgebrochen.

    Args:
        input_dir: Verzeichnis mit JSON-Dateien.
        target_oclc_link: Gesuchter OCLC-Entity-Link.
        pattern: Glob-Pattern zur Dateiauswahl (Default: "*.json").

    Returns:
        Tuple aus (PersonContext, record_id) beim ersten Treffer, sonst None.
    """
    wanted = target_oclc_link.strip().rstrip("/")

    for path in sorted(input_dir.glob(pattern)):
        if not path.is_file():
            continue

        records = load_records(path)
        for rec in records:
            base = base_fields_from_record(rec)
            rec_id = record_id_from_record(rec)

            for per700 in fields_per_700(rec):
                fields: dict[str, Any] = dict(base)
                fields.update(per700)

                ctx = build_person_context(fields, record_id=rec_id)

                if _ctx_contains_oclc_link(ctx, wanted):
                    return ctx, rec_id

    return None
