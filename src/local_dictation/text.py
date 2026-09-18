"""Minimal, safety-oriented normalization for dictated text."""

from __future__ import annotations

import unicodedata

_REPLACED_CATEGORIES = frozenset({"Cc", "Zl", "Zp"})


def normalize_transcript(text: str) -> str:
    """Make a transcript safe to paste without rewriting its content.

    Runs of control characters (including CR, LF, and TAB), Unicode line
    separators, and Unicode paragraph separators become one regular space.
    Only leading and trailing whitespace is otherwise removed.  In
    particular, punctuation, capitalization, format characters, and repeated
    ordinary spaces remain exactly as Whisper returned them.
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
    return "".join(output).strip()


__all__ = ["normalize_transcript"]
