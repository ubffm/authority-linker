from __future__ import annotations

from authority_linker.matching.normalizer import normalize_name, normalize_role


def test_normalize_name_diacritics_and_spaces() -> None:
    assert normalize_name("  Beyoncé   Knowles ") == "Beyonce Knowles"
    assert normalize_name("Jürgen  Habermas") == "Jurgen Habermas"


def test_normalize_role_punctuation_and_case() -> None:
    assert normalize_role("Interviewer, ") == "interviewer"
    assert normalize_role("Director.") == "director"
