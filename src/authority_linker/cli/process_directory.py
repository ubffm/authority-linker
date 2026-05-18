"""CLI zum Einlesen von Personendaten und Verknüpfung via Authority Linker."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from authority_linker.context import PersonContext, build_person_context
from authority_linker.marc_helpers import fields_per_700
from authority_linker.models import MatchResult
from authority_linker.providers.oclc_enrichment import enrich_context_with_oclc
from authority_linker.text import normalize_name

logger = logging.getLogger(__name__)


def _slugify(value: str) -> str:
    """Erzeuge einen Dateinamen-Slug für die Ergebnisdatei."""
    value = value.strip()
    if not value:
        return "person"
    collapsed = re.sub(r"\s+", "_", value)
    sanitized = re.sub(r"[^0-9A-Za-z._-]", "_", collapsed)
    sanitized = sanitized.strip("_")
    return sanitized[:80] or "person"


def _context_key(ctx: PersonContext) -> str:
    """Baue einen robusten Schlüssel, um doppelte Personen zu verhindern."""
    name_source = ctx.name_pref or ctx.name_display_fallback or ""
    name_norm = normalize_name(name_source) if name_source else ""
    if not name_norm:
        fallback = ctx.name_display_fallback or ctx.record_id or ""
        name_norm = normalize_name(fallback) if fallback else ""
    birth = str(ctx.birth_year or "-")
    death = str(ctx.death_year or "-")
    roles_collection = getattr(ctx, "roles", None) or ()
    roles = ",".join(sorted(r for r in roles_collection if r)) or "-"
    oclc_links = getattr(ctx, "oclc_links", None)
    oclc = ",".join(sorted(filter(None, oclc_links))) if oclc_links else "-"
    existing_ids = getattr(ctx, "existing_ids", None)
    if isinstance(existing_ids, dict) and existing_ids:
        external = ",".join(f"{key}:{existing_ids[key]}" for key in sorted(existing_ids))
    else:
        external = "-"
    if (
        external == "-"
        and oclc == "-"
        and roles == "-"
        and name_norm == "-"
        and birth == "-"
        and death == "-"
    ):
        identifier_hint = ctx.record_id or "-"
    else:
        identifier_hint = "-"
    return "|".join(
        [
            name_norm or "-",
            birth,
            death,
            roles,
            oclc,
            external,
            identifier_hint,
        ]
    )


def _result_path(output_dir: Path, ctx: PersonContext, key: str) -> Path:
    """Berechne den Zielpfad für eine Person auf Basis des Schlüssels."""
    base_name = ctx.name_pref or ctx.name_display_fallback or ctx.record_id or "person"
    slug = _slugify(base_name)
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    return output_dir / f"{slug}__{digest}.json"


def _dump_result(
    target_path: Path,
    ctx: PersonContext,
    result: MatchResult | None,
    *,
    error: str | None = None,
) -> None:
    """Schreibe Kontext und Matchergebnis als JSON-Datei."""
    payload: dict[str, Any] = {
        "context": ctx.model_dump(mode="json", exclude_none=True),
        "result": result.model_dump(mode="json", exclude_none=True) if result else None,
    }
    if error:
        payload["error"] = error
    target_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _record_id_from_record(record: dict[str, Any]) -> str:
    """Extrahiere eine 001-ähnliche Kennung aus einem Record."""
    value = record.get("001")
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        candidate = value.get("a")
        if isinstance(candidate, str):
            return candidate.strip()
    return ""


def process_directory(
    input_dir: Path,
    output_dir: Path,
    *,
    pattern: str = "*.json",
    comparison_data_path: Path | None = None,
) -> None:
    """Verarbeite alle Personen in JSON-Dateien eines Verzeichnisses."""
    # Wichtig: später Import, damit nach load_dotenv() in main()
    # config.DEFAULTS mit den gesetzten ENV-Werten initialisiert wird.
    from authority_linker.linker import link_agent

    if not input_dir.is_dir():
        raise ValueError(f"Eingabeverzeichnis nicht gefunden: {input_dir}")

    if comparison_data_path is None:
        comparison_data_path = Path("tests/testdata/comparison.json")

    if not comparison_data_path.exists():
        raise FileNotFoundError(f"Vergleichsdatei nicht gefunden: {comparison_data_path}")

    with comparison_data_path.open(encoding="utf-8") as f:
        comparison_dict = json.load(f)

    if isinstance(comparison_dict, dict):
        comparison_filter = comparison_dict.keys()
    else:
        comparison_filter = comparison_dict

    output_dir.mkdir(parents=True, exist_ok=True)
    seen_keys: set[str] = set()

    logger.debug(
        f"Starte Verarbeitung: input_dir={input_dir} output_dir={output_dir} pattern={pattern}"
    )

    for file_path in sorted(input_dir.glob(pattern)):
        if not file_path.is_file():
            logger.debug(f"Überspringe, da keine Datei: {file_path}")
            continue
        logger.debug(f"Verarbeite Datei: {file_path}")
        try:
            records = json.loads(file_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Ungültige JSON-Datei: {file_path}") from exc
        if not isinstance(records, list):
            logger.debug(f"Überspringe Datei (JSON-Root ist keine Liste): {file_path}")
            continue
        for record in records:
            if not isinstance(record, dict):
                logger.debug(f"Überspringe Record, da kein Objekt: {type(record)}")
                continue
            record_id = _record_id_from_record(record)
            fields_sets = fields_per_700(record)
            logger.info(
                f"Record verarbeitet: record_id={record_id or '-'} "
                f"anzahl_700_kontexte={len(fields_sets)}"
            )
            for fields in fields_sets:
                context_record_id = record_id
                field_record_id = fields.get("001") if isinstance(fields, dict) else None
                if isinstance(field_record_id, str) and field_record_id.strip():
                    context_record_id = field_record_id.strip()
                ctx = build_person_context(fields, record_id=context_record_id)
                if not ctx.oclc_links:
                    continue
                if not any(link in comparison_filter for link in ctx.oclc_links):
                    continue
                ctx = enrich_context_with_oclc(ctx)
                key = _context_key(ctx)
                if key in seen_keys:
                    logger.debug(
                        f"Überspringe Duplikat-Kontext: record_id={ctx.record_id or '-'} key={key}"
                    )
                    continue
                seen_keys.add(key)
                out_path = _result_path(output_dir, ctx, key)
                if out_path.exists():
                    logger.debug(f"Ergebnisdatei existiert bereits, überspringe: {out_path}")
                    continue
                try:
                    logger.debug(
                        f"Starte Linking: record_id={ctx.record_id or '-'} "
                        f"name_pref={ctx.name_pref or '-'} out={out_path}"
                    )
                    result, enriched_ctx = link_agent(ctx)
                except Exception as exc:  # pragma: no cover - defensive Fehlerbehandlung
                    logger.debug(
                        f"Fehler beim Linking: record_id={ctx.record_id or '-'} "
                        f"name_pref={ctx.name_pref or '-'} error={exc}"
                    )
                    _dump_result(out_path, ctx, None, error=str(exc))
                    logger.debug(f"Fehlerergebnis geschrieben: {out_path}")
                else:
                    _dump_result(out_path, enriched_ctx, result)
                    logger.debug(
                        f"Ergebnis geschrieben: {out_path} (hat_result={result is not None})"
                    )


def main(argv: list[str] | None = None) -> int:
    """CLI-Einstiegspunkt."""
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Führe das Authority-Linking für ein Verzeichnis mit Personen-Datensätzen aus.",
    )
    parser.add_argument(
        "input_dir",
        type=Path,
        help="Verzeichnis, das JSON-Dateien mit Datensätzen enthält.",
    )
    parser.add_argument(
        "output_dir",
        type=Path,
        help="Zielverzeichnis für Ergebnisdateien (eine Datei pro Person).",
    )
    parser.add_argument(
        "--pattern",
        default="*.json",
        help="Glob-Pattern zur Auswahl der Eingabedateien (Standard: *.json).",
    )
    parser.add_argument(
        "--filter-data-path",
        type=Path,
        default=Path("tests/testdata/comparison.json"),
        help=(
            "Pfad zum Filter-JSON für den OCLC-Filter (Standard: tests/testdata/comparison.json)."
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Aktiviere Debug-Logging.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    process_directory(
        args.input_dir,
        args.output_dir,
        pattern=args.pattern,
        comparison_data_path=args.filter_data_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
