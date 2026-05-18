from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class JsonCache:
    """Einfacher On-Disk-JSON-Cache unterhalb eines Basisverzeichnisses."""

    def __init__(self, base_dir: str = ".cache/authority_linker") -> None:
        """Initialisiere den Cache und stelle sicher, dass das Basisverzeichnis existiert."""
        self.base_path = Path(base_dir)
        self.base_path.mkdir(parents=True, exist_ok=True)

    def _key_to_path(self, key: str) -> Path:
        """Leite aus einem logischen Cache-Key einen stabilen Dateipfad ab."""
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.base_path / f"{digest}.json"

    def get(self, key: str) -> Any | None:
        """Lese einen Cache-Eintrag; liefere None bei Fehlen oder ungültigem JSON."""
        p = self._key_to_path(key)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def set(self, key: str, value: Any) -> None:
        """Speichere einen Wert unter einem Cache-Key als JSON-Datei."""
        p = self._key_to_path(key)
        p.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
