# Authority Linker

Normdatenverknüpfung und -anreicherung für Personen aus bibliografischen Daten (z. B. MARC 21) in einer ETL-Pipeline. Das Modul durchsucht u. a. Wikidata und GND (via lobid), bewertet Kandidaten kontextbasiert und verknüpft gefundene Normdaten-URIs mit Person-Agenten.

Projektname (PyPI/Build): authority-linker
Import-Paket (Python): authority_linker

## Überblick

Ziel ist die zuverlässige Verlinkung von Personen (foaf:Person) mit stabilen Normdaten-URIs und das optionale Anreichern von Labels, Aliasen und Lebensdaten. Der Ansatz ist regelbasiert, kontextsensitiv und testbar.

- Kandidatensuche: Wikidata (wbsearchentities, SPARQL), GND (lobid.org/gnd)
- Scoring: Name, Aliasse, Lebensdaten, Rollen/Berufe (Occupation), Werk-Kontext (Titel/Jahr)
- Anreicherung: owl:sameAs, skos:altLabel, Birth/Death, optional foaf:depiction
- Integrierbar in bestehende ETL-Schritte (nach Agenten-Extraktion, vor Graph-Einfügen)

Details zum Konzept, Datenfeldern und Workflow: siehe doc/personen-anreicherung.md.

## Features

- Extraktion eines kompakten PersonContext aus MARC-Feldern (100/700, 245, 260/264, 518, 041/040, 043, 655, 035, 001, optional 511)
- Kandidatensuche über Wikidata und GND (lobid)
- Kontextbasiertes Scoring mit harten und weichen Kriterien
- Optionales LLM-Re-Ranking bei Grenzfällen
- Caching und Rate-Limits (für deterministische, effiziente Abläufe)
- Saubere Python-APIs, modulare Architektur, Unit- und Integrationstests

## Installation

Voraussetzungen: Python >= 3.12 und uv

- Entwicklungsinstallation (inkl. Tools):
  - uv pip install -e ".[dev]"
- Produktiv (ohne Dev-Abhängigkeiten):
  - uv pip install .

Hinweis: Der Paketname für uv/pip ist authority-linker, der Python-Import ist authority_linker.

## Schnellstart (API-Entwurf)

Die Implementierung erfolgt schrittweise entsprechend der Dokumentation. Der folgende Beispielcode zeigt die angestrebte Nutzung:

```python
from authority_linker.context import build_person_context
from authority_linker.providers.wikidata import search as wd_search, fetch as wd_fetch
from authority_linker.providers.lobid_gnd import search_gnd, fetch_gnd
from authority_linker.matching.scorer import score  # liefert (ScoreBreakdown)

# MARC-Felder als bereits geparstes Dict (vereinfacht)
fields = {
    "100": {"a": "Menuhin, Yehudi,", "d": "1916-1999"},
    "245": {"a": "The story behind 'Concert magic' :", "b": "Yehudi Menuhin in conversation..."},
    "260": {"c": "2005."},
    "518": {"a": "Recorded in 1997, in Warsaw, Poland."},
    "041": {"a": "eng"},
}
ctx = build_person_context(fields, record_id="ocn852995177")

# 1) Kandidaten aus Wikidata (de/en) suchen
candidates = []
for lang in ("de", "en"):
    candidates.extend(wd_search(name=ctx.name_pref, lang=lang))

# 2) (Optional) GND-Kandidaten ergänzen
candidates.extend(search_gnd(name=ctx.name_pref, year=ctx.work_year))

# 3) Fakten nachladen und Scoring durchführen
scored = []
for ref in candidates:
    cand = wd_fetch(ref.id) if ref.source == "wikidata" else fetch_gnd(ref.id)
    breakdown = score(cand, ctx)
    scored.append((breakdown, cand))

best = max(scored, key=lambda x: x[0].total) if scored else None
if best and best[0].total >= 0.7:
    breakdown, match = best
    print(f"Match: {match.uri} (Score={breakdown.total:.2f}, Evidenzen={breakdown.evidences})")
else:
    print("Kein sicherer Treffer.")
```

Hinweis: Die API ist in Arbeit. Signaturen können sich im Zuge der Implementierung noch leicht ändern.

## Exakter Entscheidungsprozess (Regeln, LLM, Guardrails)

Dieser Abschnitt beschreibt den aktuellen Ablauf in `src/authority_linker/linker.py` und `src/authority_linker/llm_matcher.py` möglichst konkret.

### 1) Kandidatenquellen und Reihenfolge

Für einen `PersonContext` (`ctx`) läuft `link_agent(ctx)` in dieser Reihenfolge:

1. Optional Wikidata-Suche (`wd_search`) je Sprache aus `DEFAULTS.wikidata_langs`, nur wenn `DEFAULTS.use_wikidata_search=True`.
2. GND-Suche via lobid (`search_gnd`) immer (sofern kein Fehler).
3. Deduplizierung über `(source, id)`.

Dabei werden bereits Debugdaten aufgebaut:

- `debug_search_refs`: alle gefundenen Suchtreffer (Wikidata + GND)
- `debug_lobid_search_refs`: nur Treffer aus der lobid-GND-Suche

### 2) Fetch + Scoring pro Kandidat

Für jeden CandidateRef:

- Wikidata: `wd_fetch(ref.id)`
- GND: `fetch_gnd(ref.id)`

Danach Scoring via `score(cand, ctx)`, Ergebnis ist ein `ScoreBreakdown` mit u. a.:

- `total` (Gesamtscore)
- `name_match`
- `human_instance`
- `subject_overlap`
- `role_overlap`
- `evidences`

Diese Daten landen zusätzlich in `debug_scored`.

### 3) Sortierung und „best / runner_up“

Alle gescorten Kandidaten werden sortiert nach:

1. Höherer `score.total` zuerst
2. Bei Gleichstand: Quellpräferenz `DEFAULTS.preferred_sources` (Default: `("gnd", "wikidata")`)

Danach:

- `best = scored_sorted[0]`
- `runner_up = scored_sorted[1]` falls vorhanden

Für ein mögliches LLM-Re-Ranking werden maximal die Top-3 verwendet:

- `llm_ranked_items = scored_sorted[:3]`
- inklusive Debugausgabe `debug_llm_ranked_items` (mit `rank` 1..3)

### 4) Regelbasierte Annahme/Ablehnung (`_decide_acceptance`)

Die Regeln prüfen den `best`-Kandidaten. Standardreihenfolge:

1. **Schwellwert**: `best.total >= DEFAULTS.scoring_threshold` (Default 0.7)
2. **Score-Abstand**: wenn `runner_up` existiert, muss `gap >= DEFAULTS.scoring_min_gap` (Fallback intern: 0.08)
3. **Namenssignal** (falls aktiv): `name_match == True` bei `require_name_match_evidence=True`
4. **Human-Signal** (optional): `human_instance == True` bei `require_human_instance=True`
5. **Kontextsignal** (falls aktiv): mind. eins der konfigurierten Evidenzfelder in `context_evidences_for_acceptance` (Default: `role_overlap`, `subject_overlap`) muss `True` sein

Wenn alles erfüllt ist:

- `accepted=True`
- `decision_path`: `["rules:evaluated", "rules:accepted"]`
- Kandidat wird übernommen
- Kontext wird mit Kandidatendaten angereichert

Wenn nicht, werden Gründe gesammelt in `rule_rejections`, z. B.:

- `score-unter-schwellwert`
- `score-gap-zu-klein (0.00)`
- `kein-namensmatch`
- `keine-kontext-evidenz`

### 5) Borderline-Erkennung für optionales LLM (`_is_borderline`)

LLM wird nur überhaupt erwogen, wenn der Fall „borderline“ ist. Das ist der Fall, wenn mindestens eine Bedingung zutrifft:

1. `best.total` liegt knapp unter Schwellwert:
   - `(threshold - llm_borderline_margin) <= best.total < threshold`
2. Score-Abstand zum zweiten Kandidaten ist klein:
   - `best.total - runner_up.total < llm_borderline_gap`
3. Widersprüchliches Muster:
   - `name_match=True` und `role_overlap=False`

### 6) Vor-Guardrail: LLM nur bei minimaler Evidenz

Bevor LLM aufgerufen wird, gilt eine harte Vorbedingung über Top-3:

- Mindestens ein Kandidat muss `_passes_llm_accept_guardrail` erfüllen:
  - `name_match=True` **oder**
  - `subject_overlap=True` **oder**
  - `role_overlap=True`

Falls kein Kandidat diese Mindest-Evidenz hat:

- kein LLM-Aufruf
- `decision_path` enthält `llm:skipped-no-min-evidence`
- zusätzlicher Ablehnungsgrund: `llm-kein-kandidat-mit-mindest-evidenz`
- Ergebnis bleibt `accepted=False`

### 7) LLM-Aufruf und Antwortvalidierung (`LLMMatcher`)

Wenn LLM aufgerufen wird:

1. Prompt enthält nur strukturierte Daten (Kontext + Top-3-Kandidaten + Scoring-Flags/Evidences).
2. Erlaubte Entscheidungen laut Schema:
   - `accept_best`
   - `choose_alternative`
   - `reject_all`
3. Zusätzliche Validierung:
   - `confidence >= llm_min_confidence` (Default 0.75), sonst `reject_all`
   - bei `choose_alternative` muss `chosen_candidate_id` in den Top-3 enthalten sein, sonst `reject_all`

Bei Request-/Parsingfehlern wird defensiv `reject_all` mit Fehlergrund geliefert (z. B. `llm-request-fehlgeschlagen:...`).

### 8) Nach-Guardrails: LLM darf nicht „blind“ akzeptieren

Auch nach positiver LLM-Entscheidung gelten Guardrails im Linker:

- Bei `accept_best`: `best` muss `_passes_llm_accept_guardrail` erfüllen, sonst Ablehnung mit
  - `decision_path`: `llm:rejected-guardrail`
  - `rule_rejections` ergänzt um `llm-guardrail-verhindert-accept_best`
- Bei `choose_alternative`: der gewählte Alternativkandidat muss ebenfalls Guardrail erfüllen, sonst
  - `llm:rejected-guardrail`
  - `llm-guardrail-verhindert-choose_alternative`

Nur wenn Guardrail passt, wird der jeweilige Kandidat akzeptiert.

### 9) Finales Ergebnis im Output-JSON

`MatchResult` enthält neben Score/Resultat jetzt Transparenzfelder:

- `accepted` (bool)
- `decision_path` (z. B. `rules:evaluated -> rules:rejected -> llm:invoked -> llm:choose_alternative`)
- `rule_rejections` (Liste konkreter Gründe)
- `llm_decision` (strukturierte LLM-Entscheidung)
- `debug_search_refs`
- `debug_lobid_search_refs`
- `debug_scored`
- `debug_llm_ranked_items`

Damit ist nachvollziehbar:

1. **Welche Kandidaten gefunden wurden**
2. **wie sie gescored wurden**
3. **welche ins LLM gingen**
4. **warum final akzeptiert oder abgelehnt wurde**

## CLI für Batch-Verarbeitung

Für die Verarbeitung ganzer Verzeichnisse steht das Kommando `authority-linker-process-directory` zur Verfügung. Es liest alle JSON-Dateien eines Eingabeverzeichnisses ein, bildet pro gefundenem Datensatz einen `PersonContext`, dedupliziert identische Personen anhand ihrer Kernmerkmale und schreibt die Ergebnisse (inkl. Kontext) als einzelne JSON-Dateien in das Ausgabeziel. Existiert für eine Person bereits eine Ergebnisdatei, wird sie übersprungen.

```bash
authority-linker-process-directory tests/testdata/kanopy output/kanopy-personen
```

Optionen:

- `input_dir`: Verzeichnis mit den Eingabe-Dateien (Glob-Pattern `*.json` als Standard).
- `output_dir`: Zielordner, wird bei Bedarf angelegt.
- `--pattern`: Optionales Glob-Pattern, z. B. `--pattern '*_Records_*.json'`.
- `--filter-data-path`: Pfad zur OCLC-Filterdatei (Standard: `tests/testdata/comparison.json`).
- `--debug`: Aktiviert Debug-Logging.

Jede Ergebnisdatei enthält unter `context` den zugrundeliegenden `PersonContext` und unter `result` das zurückgelieferte `MatchResult` (oder `null`, falls kein Treffer oder Fehler). Bei verarbeiteten Personen wird ein stabiler Hash an den Dateinamen angehängt, sodass wiederholte Ausführungen bereits vorhandene Resultate erkennen und nicht erneut anfragen.

## LLM-Konfiguration

Das optionale LLM-Re-Ranking wird über Umgebungsvariablen gesteuert:

- `AUTHORITY_LINKER_LLM_ENABLED` (Default: `true`)
- `AUTHORITY_LINKER_LLM_PROVIDER` (Default: `openai`)
- `AUTHORITY_LINKER_LLM_MODEL` (Default: `llama3.1:8b`)
- `AUTHORITY_LINKER_LLM_API_BASE` (optional; Default: `https://litellm.s.studiumdigitale.uni-frankfurt.de/v1`)
- `AUTHORITY_LINKER_LLM_API_KEY` (erforderlich bei aktivem LLM)
- `AUTHORITY_LINKER_LLM_TIMEOUT_S` (Default: `20.0`)
- `AUTHORITY_LINKER_LLM_TEMPERATURE` (Default: `0.0`)
- `AUTHORITY_LINKER_LLM_MAX_TOKENS` (Default: `400`)
- `AUTHORITY_LINKER_LLM_MIN_CONFIDENCE` (Default: `0.75`)

Beispiel:

```bash
export AUTHORITY_LINKER_LLM_ENABLED=true
export AUTHORITY_LINKER_LLM_PROVIDER=openai
export AUTHORITY_LINKER_LLM_MODEL=llama3.1:8b
export AUTHORITY_LINKER_LLM_API_BASE=https://litellm.s.studiumdigitale.uni-frankfurt.de/v1
export AUTHORITY_LINKER_LLM_API_KEY=...
```

## Architektur

Quellcode liegt unter src/authority_linker/. Wichtige Module:

- `context.py` – PersonContext (Pydantic) + `build_person_context(...)`
- `models.py` – `CandidateRef`, `Candidate`, `MatchResult`
- `providers/`
  - `wikidata.py` – Suche (wbsearchentities) & fetch (SPARQL)
  - `lobid_gnd.py` – Suche/Fetch für GND via lobid.org/gnd bzw. UriResolver
- `matching/`
  - `normalizer.py` – Normalisierung von Namen/Rollen
  - `scorer.py` – regelbasiertes Scoring
  - `rules.py` – Schwellenwerte, Defaults
- `linker.py` – Orchestrierung (Suche → Fetch → Score → Entscheidung)
- `cache.py` – einfacher On-Disk-Cache
- `config.py` – Default-Settings (Sprachen, Limits, Schwellenwerte)
- `llm_matcher.py` – optionales Re-Ranking bei unklaren Fällen
- `cli/process_directory.py` – Batch-CLI für Verzeichnisse

Die konzeptionellen Hintergründe und Feldzuordnungen sind in `doc/personen-anreicherung.md` beschrieben.

## Konfiguration

Typische Konfigurationsschlüssel (Beispiele):

- `use_wikidata_search`: bool
- `use_gnd_uri_resolver`: bool
- `wikidata_langs`: `("de", "en")`
- `wikidata_rate_limit`: float (Sekunden pro Request)
- `scoring_threshold`: float (z. B. 0.7)
- `preferred_sources`: Reihenfolge für Quell-Priorisierung (z. B. `("gnd", "wikidata")`)

## Entwickeln

- Code-Stil: Ruff (Lint & Format) – Konfiguration in `pyproject.toml`
- Tests: Pytest (via `uv run`), Tests liegen unter `tests/`
- Testdaten: Reale Beispielfälle unter `tests/testdata/kanopy/` und `tests/testdata/medici_tv/`

Empfehlung: kleine, fokussierte PRs; umfangreiche Änderungen mit Tests abdecken.

## Lizenz

Sofern nicht anders angegeben, siehe `LICENSE` im Repository. Fehlt eine Lizenzdatei, bitte vor Veröffentlichung ergänzen.
