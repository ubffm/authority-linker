from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from authority_linker.marc_helpers import load_records

OCLC_ENTITY_PREFIX = "https://id.oclc.org/worldcat/entity/"
PERSON_TAGS = {"100", "600", "700", "800"}


def _extract_links_from_record(record: dict[str, Any]) -> set[str]:
    """Extrahiere OCLC-Entitätslinks aus einem einzelnen MARC-Datensatz."""
    if not isinstance(record, dict):
        return set()

    fields = record.get("fields")
    if not isinstance(fields, list):
        return set()

    links: set[str] = set()
    for field in fields:
        if not isinstance(field, dict):
            continue

        for tag, data in field.items():
            if tag not in PERSON_TAGS or not isinstance(data, dict):
                continue

            subfields = data.get("subfields")
            if not isinstance(subfields, list):
                continue

            for subfield in subfields:
                if not isinstance(subfield, dict):
                    continue

                value = subfield.get("1")
                if isinstance(value, str):
                    normalized = value.strip()
                    if normalized.startswith(OCLC_ENTITY_PREFIX):
                        links.add(normalized)

    return links


def collect_oclc_links(directory: Path) -> list[str]:
    """Sammle alle OCLC-Personenlinks aus MARC-JSON-Dateien eines Verzeichnisses."""
    if not directory.is_dir():
        msg = f"Eingabepfad {directory} ist kein Verzeichnis."
        raise ValueError(msg)

    found: set[str] = set()
    for json_path in sorted(directory.rglob("*.json")):
        if not json_path.is_file():
            continue

        try:
            records = load_records(json_path)
        except Exception as exc:  # noqa: BLE001
            print(f"Warnung: {json_path} konnte nicht geladen werden ({exc}).", file=sys.stderr)
            continue

        for record in records:
            if isinstance(record, dict):
                found.update(_extract_links_from_record(record))

    return sorted(found)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Durchsucht ein Verzeichnis mit MARC-JSON-Dateien nach OCLC-Personenlinks "
            "und gibt sie als JSON-Array aus."
        )
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path.cwd(),
        help=(
            "Verzeichnis, das rekursiv nach MARC-JSON-Dateien durchsucht wird "
            "(Standard: aktuelles)."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optionaler Pfad für die Ausgabedatei. Ohne Angabe erfolgt die Ausgabe auf stdout.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    links = collect_oclc_links(args.input_dir)
    count = len(links)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(links, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    else:
        json.dump(links, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")

    link_label = "OCLC-Link" if count == 1 else "OCLC-Links"
    print(f"{count} {link_label} gefunden.", file=sys.stdout)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
