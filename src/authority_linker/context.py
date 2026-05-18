from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from authority_linker.config import get_settings


class PersonContext(BaseModel):
    """Kompakter Personen-Kontext für das Normdaten-Matching."""

    record_id: str

    # Namen
    name_pref: str
    name_fuller: str | None = None
    name_display: str | None = None

    # Rollen und Lebensdaten
    roles: set[str] = Field(default_factory=set)
    birth_year: int | None = None
    death_year: int | None = None

    # Werk-Kontext
    work_title: str | None = None
    work_year: int | None = None

    # Weitere Hinweise
    # thematische Schlagwörter (z. B. 650/651/689)
    subjects: list[str] = Field(default_factory=list)
    # Form-/Gattungsbegriffe (z. B. 655)
    genre_terms: list[str] = Field(default_factory=list)
    cast_raw: list[str] = Field(default_factory=list)
    place_hint: str | None = None
    region_hint: str | None = None
    lang: str | None = None

    # Domänen-Hinweis als erlaubte GND-Sachkategorie-Notationen (z. B. 15.1p)
    subject_category_hints: set[str] = Field(default_factory=set)

    # IDs
    # z. B. lokale Felder wie 001, 035a
    local_ids: dict[str, str] = Field(default_factory=dict)
    # z. B. Normdaten-IDs wie "gnd", "wikidata"
    existing_ids: dict[str, str] = Field(default_factory=dict)
    # Vollständige Liste der vorhandenen OCLC-Links aus 700$1/$1
    oclc_links: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore")

    @field_validator("name_pref", "name_fuller", "name_display")
    @classmethod
    def _strip_names(cls, v: str | None) -> str | None:
        """Entferne führende und nachgestellte Leerzeichen aus Namensfeldern."""
        return v.strip() if isinstance(v, str) else v

    @field_validator("roles", mode="before")
    @classmethod
    def _normalize_roles(cls, v) -> set[str]:
        """Normalisiere Rollenangaben zu einem bereinigten, kleingeschriebenen Set."""
        if v is None:
            return set()
        if isinstance(v, (set, list, tuple)):
            raw = v
        elif isinstance(v, str):
            raw = re.split(r"[,/;]", v)
        else:
            raw = [str(v)]

        def norm(r: str) -> str:
            """Normalisiere eine einzelne Rollenangabe."""
            r = r.lower().strip()
            r = re.sub(r"\s+", " ", r)
            r = r.strip(" .,:;")
            return r

        return {norm(r) for r in raw if str(r).strip()}

    @field_validator("subjects", "genre_terms", "cast_raw", mode="before")
    @classmethod
    def _listify(cls, v) -> list[str]:
        """Wandle Eingaben robust in eine Liste nicht-leerer Strings um."""
        if v is None:
            return []
        if isinstance(v, (list, tuple, set)):
            return [str(x).strip() for x in v if str(x).strip()]
        return [str(v).strip()]

    @field_validator("subject_category_hints", mode="before")
    @classmethod
    def _normalize_subject_category_hints(cls, v) -> set[str]:
        """Normalisiere GND-Sachkategorie-Notationen zu einem kleingeschriebenen Set."""
        if v is None:
            return set()
        if isinstance(v, str):
            raw = re.split(r"[,;]", v)
        elif isinstance(v, (set, list, tuple)):
            raw = [str(x) for x in v]
        else:
            raw = [str(v)]

        out: set[str] = set()
        for item in raw:
            value = item.strip().lower()
            if value:
                out.add(value)
        return out

    @model_validator(mode="after")
    def _plausibility(self) -> PersonContext:
        """Korrigiere offensichtliche Plausibilitätsprobleme in Jahresfeldern."""
        # Plausibilität: Geburt ≤ Tod (Eingabefehler abfedern, nicht abbrechen)
        if (
            self.birth_year is not None
            and self.death_year is not None
            and self.birth_year > self.death_year
        ):
            self.birth_year, self.death_year = self.death_year, self.birth_year

        if self.work_year is not None and self.work_year < 0:
            self.work_year = None
        return self

    @property
    def name_display_fallback(self) -> str:
        """Liefere eine Anzeigeform; invertiere bei Bedarf „Nachname, Vorname“."""
        if self.name_display:
            return self.name_display
        parts = [p.strip() for p in self.name_pref.split(",") if p.strip()]
        if len(parts) == 2:
            return f"{parts[1]} {parts[0]}"
        return self.name_pref

    def has_years(self) -> bool:
        """Prüfe, ob mindestens ein Lebensjahr vorhanden ist."""
        return self.birth_year is not None or self.death_year is not None


def build_person_context(fields: dict, record_id: str) -> PersonContext:
    """Baue einen PersonContext aus bereits vorverarbeiteten MARC-Feldern.

    Erwartetes, vereinfachtes Format von `fields` (Beispiele):
    {
        "100": {"a": "Nachname, Vorname", "d": "1933-"},
        "700": {"e": ["director", "interviewer,"]},
        "245": {"a": "Titel", "b": "Untertitel"},
        "260": {"c": "2014."},
        "264": {"c": "2014."},
        "518": {"a": "Originally produced... 2005."},
        "041": {"a": "eng"}, "040": {"b": "eng"},
        "043": {"a": "f-sg---"},
        "655": {"a": ["Documentary films."]},
        "001": "kan111...", "035a": "(OCoLC)..." ,
    }
    """

    def pick(*vals: str | None) -> str | None:
        """Wähle den ersten nicht-leeren String aus einer Werteliste."""
        for v in vals:
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None

    def year_from_text(text: str | None) -> int | None:
        """Extrahiere das erste plausible Jahr aus einem Freitext."""
        if not text:
            return None
        m = re.search(r"(1[5-9]\d{2}|20\d{2}|2100)", text)
        return int(m.group(1)) if m else None

    def year_from_008(value: str | None) -> int | None:
        """Extrahiere das Publikationsjahr positionsbasiert aus MARC 008 (Pos. 7-10)."""
        if not isinstance(value, str):
            return None
        if len(value) < 11:
            return None
        year_raw = value[7:11]
        if not re.fullmatch(r"\d{4}", year_raw):
            return None
        year = int(year_raw)
        if 1500 <= year <= 2100:
            return year
        return None

    def first_id(value) -> str | None:
        """Lies die erste verfügbare ID aus String oder String-Sequenz."""
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (list, tuple, set)):
            for item in value:
                if isinstance(item, str) and item.strip():
                    return item.strip()
        return None

    def collect_ids(value) -> list[str]:
        """Sammle alle nicht-leeren IDs aus String oder String-Sequenz."""
        items: list[str] = []
        if isinstance(value, str) and value.strip():
            items.append(value.strip())
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                if isinstance(item, str) and item.strip():
                    items.append(item.strip())
        return items

    def collect_terms_from_field(field_value: dict | None) -> list[str]:
        """Sammle Unterfeld a robust als Liste aus einem MARC-Feld-Dict."""
        if not isinstance(field_value, dict):
            return []
        value = field_value.get("a")
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(x).strip() for x in value if str(x).strip()]
        return []

    # Namen
    f100 = fields.get("100", {}) or {}
    f700 = fields.get("700", {}) or {}

    name_pref = pick(f100.get("a"), f700.get("a")) or "Unbekannt"
    name_fuller = pick(f100.get("q"))
    name_display = fields.get("name_display")  # optional bereits invertiert vorhanden

    # Lebensdaten
    d_field = pick(f100.get("d"), f700.get("d"))
    birth_year = death_year = None
    if d_field:
        # Muster: "1933-" oder "1807-1889"
        m = re.match(r"\s*(\d{3,4})\s*-\s*(\d{0,4})", d_field)
        if m:
            birth_year = int(m.group(1)) if m.group(1) else None
            death_year = int(m.group(2)) if m.group(2) else None

    # Rollen
    roles = set()
    r = f700.get("e")
    if isinstance(r, str):
        roles = {x.strip(" .,:;").lower() for x in re.split(r"[,/;]", r) if x.strip()}
    elif isinstance(r, (list, tuple, set)):
        roles = {str(x).strip(" .,:;").lower() for x in r if str(x).strip()}

    # Werk-Kontext
    f245 = fields.get("245", {}) or {}
    title_a = f245.get("a")
    title_b = f245.get("b")
    work_title = (
        " ".join([t.strip() for t in [title_a, title_b] if isinstance(t, str) and t.strip()])
        or None
    )

    # 008 positionsbasiert behandeln (MARC: Jahr in Pos. 7-10)
    yr_008 = year_from_008(fields.get("008"))

    work_year = (
        year_from_text((fields.get("518", {}) or {}).get("a"))
        or yr_008
        or year_from_text((fields.get("260", {}) or {}).get("c"))
        or year_from_text((fields.get("264", {}) or {}).get("c"))
    )

    # Hinweise
    place_hint = (fields.get("264", {}) or {}).get("a")
    region_hint = (fields.get("043", {}) or {}).get("a")
    lang = pick(((fields.get("041", {}) or {}).get("a")), ((fields.get("040", {}) or {}).get("b")))

    # Themen vs. Form/Gattung klar trennen
    subjects = (
        collect_terms_from_field(fields.get("650", {}) or {})
        + collect_terms_from_field(fields.get("651", {}) or {})
        + collect_terms_from_field(fields.get("689", {}) or {})
    )
    subjects = list(dict.fromkeys(subjects))

    genre_terms = collect_terms_from_field(fields.get("655", {}) or {})
    genre_terms = list(dict.fromkeys(genre_terms))

    # IDs
    local_ids = {}
    if "001" in fields and isinstance(fields.get("001"), str):
        local_ids["001"] = fields["001"]
    if "035a" in fields and isinstance(fields.get("035a"), str):
        local_ids["035a"] = fields["035a"]

    existing_ids: dict[str, str] = {}
    id_0 = first_id(fields.get("$0"))
    if id_0:
        existing_ids["$0"] = id_0
    if "$0" not in existing_ids:
        id_0 = first_id(f700.get("0"))
        if id_0:
            existing_ids["$0"] = id_0

    oclc_links = list(dict.fromkeys(collect_ids(fields.get("$1")) + collect_ids(f700.get("1"))))
    if oclc_links:
        existing_ids["$1"] = oclc_links[0]
    else:
        oclc_links = []

    settings = get_settings()

    return PersonContext(
        record_id=record_id,
        name_pref=name_pref,
        name_fuller=name_fuller,
        name_display=name_display,
        roles=roles,
        birth_year=birth_year,
        death_year=death_year,
        work_title=work_title,
        work_year=work_year,
        subjects=subjects,
        genre_terms=genre_terms,
        place_hint=place_hint,
        region_hint=region_hint,
        lang=lang,
        subject_category_hints=set(settings.subject_category_hints),
        local_ids=local_ids,
        oclc_links=oclc_links,
        existing_ids=existing_ids,
    )
