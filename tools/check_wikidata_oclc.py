"""
CLI-Tool zum Batch-Abgleich von OCLC-Links gegen Wikidata.

Es liest eine JSON-Liste von OCLC-Links, fragt für jede Batch eine SPARQL-Abfrage
gegen Wikidata ab und schreibt je Link gefundene Wikidata- und GND-URIs in eine
JSON-Ausgabe.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx

from authority_linker.config import DEFAULTS
from authority_linker.providers.utils import binding_value

_SPARQL_TEMPLATE = """
SELECT ?wcr_id ?item ?gnd
WHERE {
  VALUES ?wcr_id { %s }
  ?item wdt:P10832 ?wcr_id .
  OPTIONAL { ?item wdt:P227 ?gnd . }
}
""".strip()


def _chunked(values: list[str], size: int) -> Iterable[list[str]]:
    for idx in range(0, len(values), size):
        yield values[idx : idx + size]


def _oclc_to_id(link: str) -> str:
    return link.rsplit("/", 1)[-1]


def _render_query(ids: list[str]) -> str:
    values = "\n    ".join(f'"{wcr}"' for wcr in ids)
    return _SPARQL_TEMPLATE % values


def _print_progress(processed: int, total: int) -> None:
    if total <= 0:
        return
    percent = processed / total * 100
    sys.stderr.write(f"\rVerarbeitet: {processed}/{total} ({percent:5.1f}%)")
    if processed >= total:
        sys.stderr.write("\n")
    sys.stderr.flush()


def _run_batch(client: httpx.Client, ids: list[str]) -> dict[str, dict[str, str]]:
    query = _render_query(ids)
    response = client.get(
        DEFAULTS.wikidata_sparql_endpoint,
        params={"query": query, "format": "json"},
    )
    if response.status_code != httpx.codes.OK:
        raise RuntimeError(
            f"SPARQL-Anfrage schlug fehl ({response.status_code}): {response.text}",
        )

    payload: dict[str, Any] = response.json()
    bindings = payload.get("results", {}).get("bindings", [])
    hits: dict[str, dict[str, str]] = {}

    for binding in bindings:
        wcr_id = binding_value(binding.get("wcr_id"))
        if not wcr_id:
            continue

        entry = hits.setdefault(wcr_id, {})
        wd_uri = binding_value(binding.get("item"))
        if wd_uri:
            entry["wd"] = wd_uri

        gnd_value = binding_value(binding.get("gnd"))
        if gnd_value:
            entry["gnd"] = f"https://d-nb.info/gnd/{gnd_value}"

    return hits


def collect(
    oclc_links: list[str],
    *,
    batch_size: int = 50,
    rate_limit: float | None = None,
    show_progress: bool = True,
) -> list[dict[str, dict[str, str]]]:
    if not oclc_links:
        return []

    batch_size = max(1, batch_size)
    pause = rate_limit if rate_limit is not None else DEFAULTS.wikidata_rate_limit

    oclc_to_id = {_oclc_to_id(link): link for link in oclc_links}
    aggregated: dict[str, dict[str, str]] = {}
    total = len(oclc_to_id)
    processed = 0
    if show_progress and total:
        _print_progress(processed, total)

    with httpx.Client(
        headers={
            "User-Agent": DEFAULTS.user_agent,
            "Accept": "application/sparql-results+json",
        },
        timeout=httpx.Timeout(20.0, connect=5.0),
    ) as client:
        ids = list(oclc_to_id.keys())
        for chunk in _chunked(ids, batch_size):
            batch_hits = _run_batch(client, chunk)
            aggregated.update(batch_hits)
            processed += len(chunk)
            if show_progress:
                _print_progress(processed, total)
            if pause:
                time.sleep(pause)

    output: list[dict[str, dict[str, str]]] = []
    for link in oclc_links:
        wcr_id = _oclc_to_id(link)
        entry = aggregated.get(wcr_id, {})
        output.append({link: entry})

    return output


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Batchweise Prüfung von OCLC-Links gegen Wikidata.",
    )
    parser.add_argument(
        "input",
        type=Path,
        help="Pfad zu einer JSON-Datei mit einer Liste von OCLC-Links.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Pfad für die Ausgabe (Standard: stdout).",
    )
    parser.add_argument(
        "-b",
        "--batch-size",
        type=int,
        default=50,
        help="Größe der SPARQL-Batches (Standard: 50).",
    )
    parser.add_argument(
        "--rate-limit",
        type=float,
        default=None,
        help="Optionale Pause in Sekunden zwischen Abfragen (Standard: Projektvorgabe).",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Unterdrücke die Fortschrittsanzeige auf stderr.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        links = json.loads(args.input.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"Eingabedatei konnte nicht gelesen werden: {exc}") from exc

    if not isinstance(links, list) or not all(isinstance(item, str) for item in links):
        raise SystemExit("Eingabe muss eine JSON-Liste mit String-Links sein.")

    show_progress = not args.no_progress and sys.stderr.isatty()

    result = collect(
        links,
        batch_size=args.batch_size,
        rate_limit=args.rate_limit,
        show_progress=show_progress,
    )
    output_data = json.dumps(result, indent=2, ensure_ascii=False)

    if args.output:
        args.output.write_text(output_data + "\n", encoding="utf-8")
    else:
        sys.stdout.write(output_data + "\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
