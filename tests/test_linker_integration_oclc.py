from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from authority_linker import linker
from authority_linker.config import DEFAULTS
from authority_linker.context import PersonContext, build_person_context
from authority_linker.marc_helpers import fields_per_700


@pytest.mark.integration
@pytest.mark.skipif(
    not os.getenv("AUTHORITY_LINKER_RUN_INTEGRATION"),
    reason="AUTHORITY_LINKER_RUN_INTEGRATION nicht gesetzt",
)
def test_linker_links_all_oclc_entries() -> None:
    data_root = Path(__file__).parent / "testdata"
    mapping_path = data_root / "oclc-wd-gnd.json"
    mapping_raw: list[dict[str, dict[str, str]]] = json.loads(
        mapping_path.read_text(encoding="utf-8")
    )
    oclc_map: dict[str, dict[str, str]] = {
        link: payload for entry in mapping_raw for link, payload in entry.items() if payload
    }

    contexts: list[tuple[PersonContext, list[str]]] = []
    for dataset in ("kanopy", "medici_tv"):
        dataset_dir = data_root / dataset
        if not dataset_dir.exists():
            continue
        for file_path in sorted(dataset_dir.glob("*.json")):
            records: list[dict[str, Any]] = json.loads(file_path.read_text(encoding="utf-8"))
            for record in records:
                for fields in fields_per_700(record):
                    ctx = build_person_context(fields, record_id=fields.get("001", ""))
                    relevant_links = [
                        link
                        for link in ctx.oclc_links
                        if link in oclc_map and oclc_map[link].get("gnd")
                    ]
                    if relevant_links:
                        contexts.append((ctx, relevant_links))

    assert contexts, "Keine Personenkontexte mit OCLC-Link in den Testdaten gefunden."

    total = len(contexts)
    threshold = DEFAULTS.scoring_threshold

    for index, (ctx, relevant_links) in enumerate(contexts, start=1):
        record_label = ctx.record_id or ctx.name_display or ctx.name_pref
        links_str = ", ".join(relevant_links)
        print(
            f"[{index}/{total}] Verlinke {record_label} "
            f"(record_id={ctx.record_id or '-'}, name_pref={ctx.name_pref}, oclc={links_str})",
            flush=True,
        )
        result = linker.link_agent(ctx)
        assert result is not None, f"Kein Ergebnis für Kontext {ctx.record_id}."
        assert result.candidate is not None, (
            f"Kein Kandidat für Kontext {ctx.record_id or '-'} (name_pref={ctx.name_pref})"
        )
        expected_uris = {oclc_map[link]["gnd"] for link in relevant_links}
        assert result.candidate.uri in expected_uris
        assert result.score >= threshold, (
            "Score zu niedrig für Kontext "
            f"{ctx.record_id or '-'} (name_pref={ctx.name_pref}): "
            f"{result.score:.3f} < {threshold:.3f}; "
            f"Evidenzen={result.breakdown.evidences if result.breakdown else []}"
        )
        print(
            f"    ✓ Treffer: {result.candidate.uri} "
            f"(Score={result.score:.3f}, OCLC-Links={len(relevant_links)})",
            flush=True,
        )
