"""Minimal, safety-oriented normalization for dictated text."""

from __future__ import annotations

import re
import unicodedata

_REPLACED_CATEGORIES = frozenset({"Cc", "Zl", "Zp"})

# Whisper prompts can bias a spelling, but they cannot reliably enforce brand
# capitalization.  Keep this list deliberately narrow: every accepted variant
# must be distinctive enough that replacing it cannot silently rewrite normal
# German prose or another person's name.
_TERM_CORRECTIONS = (
    (
        re.compile(
            r"(?<!\w)(?:lia[\s\u00a0-]*n(?:oah?|ua)|leonor)(?!\w)",
            re.IGNORECASE,
        ),
        "LiaNoa",
    ),
    (
        re.compile(r"(?<!\w)shop[\s\u00a0-]*(?:ware|wear)(?!\w)", re.IGNORECASE),
        "Shopware",
    ),
)


def apply_term_corrections(text: str) -> str:
    """Normalize narrowly defined brand-name variants in a transcript."""

    if not isinstance(text, str):
        raise TypeError("text must be str")

    corrected = text
    for pattern, replacement in _TERM_CORRECTIONS:
        corrected = pattern.sub(replacement, corrected)
    return corrected


def normalize_transcript(text: str) -> str:
    """Make a transcript safe to paste without rewriting its content.

    Runs of control characters (including CR, LF, and TAB), Unicode line
    separators, and Unicode paragraph separators become one regular space.
    Only leading and trailing whitespace is otherwise removed before a small,
    conservative brand-name dictionary is applied.  Punctuation, unrelated
    capitalization, format characters, and repeated ordinary spaces remain
    exactly as Whisper returned them.
    """

    if not isinstance(text, str):
        raise TypeError("text must be str")

    output: list[str] = []
    replacing = False
    for character in text:
        replace = unicodedata.category(character) in _REPLACED_CATEGORIES
        if replace:
            if not replacing:
                output.append(" ")
            replacing = True
        else:
            output.append(character)
            replacing = False
    return apply_term_corrections("".join(output).strip())


__all__ = ["apply_term_corrections", "normalize_transcript"]
