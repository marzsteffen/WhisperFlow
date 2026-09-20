from __future__ import annotations

import pytest

from local_dictation.text import apply_term_corrections, normalize_transcript


def test_replaces_control_line_and_paragraph_runs_with_one_space():
    assert normalize_transcript("  Eins\r\n\t\u2028\u2029Zwei\x00Drei  ") == "Eins Zwei Drei"


def test_only_outer_whitespace_is_otherwise_trimmed():
    source = "  Grüß  Gott!  Café—Müller.  "
    assert normalize_transcript(source) == "Grüß  Gott!  Café—Müller."


def test_format_characters_are_not_silently_removed():
    assert normalize_transcript("a\u200db") == "a\u200db"


def test_empty_and_control_only_transcripts_become_empty():
    assert normalize_transcript("") == ""
    assert normalize_transcript("\n\r\t") == ""


def test_requires_text():
    with pytest.raises(TypeError):
        normalize_transcript(None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "variant",
    (
        "Lianoah",
        "LiaNoah",
        "Lianoa",
        "Lia Noa",
        "Lia-Noa",
        "Lianua",
        "Leonor",
    ),
)
def test_lianoa_transcription_variants_use_exact_brand_spelling(variant: str):
    assert normalize_transcript(f"Bitte öffne {variant}, danke.") == (
        "Bitte öffne LiaNoa, danke."
    )


@pytest.mark.parametrize("variant", ("shopware", "Shop Ware", "Shop-Ware", "Shopwear"))
def test_shopware_transcription_variants_use_exact_brand_spelling(variant: str):
    assert normalize_transcript(f"Das läuft mit {variant}.") == "Das läuft mit Shopware."


def test_term_corrections_do_not_rewrite_similar_names_or_embedded_words():
    source = "Liana, Noah, Lian und MeinShopwarePlugin bleiben unverändert."
    assert apply_term_corrections(source) == source


def test_term_corrections_require_text():
    with pytest.raises(TypeError):
        apply_term_corrections(None)  # type: ignore[arg-type]

