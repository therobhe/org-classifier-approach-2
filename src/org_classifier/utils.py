from __future__ import annotations

import re
import unicodedata
from typing import Optional

from .constants import LEGAL_FORMS


_QUOTE_CHARS = "\"'“”„‚‘’"


def normalize_org_name(value: object) -> str:
    """Normalize organisation name cells for robust matching.

    Conservative by design: only normalizes whitespace/quotes/unicode form.
    Does NOT remove meaningful punctuation like dots in "e.V.".
    """
    if value is None:
        return ""

    text = str(value)
    text = unicodedata.normalize("NFKC", text)

    # Remove common surrounding quotes (including doubled quotes from CSV)
    text = text.strip()
    if len(text) >= 2 and text[0] in _QUOTE_CHARS and text[-1] in _QUOTE_CHARS:
        text = text[1:-1].strip()

    # Collapse repeated quotes inside strings from CSV oddities
    text = text.replace('""', '"')

    # Normalize whitespace (incl. non-breaking spaces)
    text = text.replace("\u00a0", " ")
    text = re.sub(r"\s+", " ", text).strip()

    # Remove trailing delimiter artifacts (rare when delimiter parsing fails)
    text = text.strip(";\t\r\n")

    return text


def sanitize_legal_form(raw_legal_form: Optional[str]) -> Optional[str]:
    """Normalize a raw legal-form string (typically from an LLM) to its
    canonical abbreviation using the same LEGAL_FORMS regex patterns that
    drive the name-extraction classifier.

    Examples:
        "eingetragener Verein e.V."  -> "e.V."
        "eingetragener Verein"       -> "e.V."
        "Aktiengesellschaft"         -> "AG"
        "gemeinnützige GmbH"         -> "gGmbH"
        "Gesellschaft mit beschränkter Haftung" -> "GmbH"
        "unknown"                    -> "unknown"  (passthrough)
        None                         -> None       (passthrough)
    """
    if raw_legal_form is None:
        return None

    stripped = str(raw_legal_form).strip()
    if not stripped or stripped.lower() == "unknown":
        return stripped or None

    # Run through the same ordered regex patterns used for name extraction.
    # First match wins (longest / most-specific patterns come first).
    for canonical, pattern in LEGAL_FORMS:
        if pattern.search(stripped):
            return canonical

    # No pattern matched – return the original string unchanged.
    return stripped
