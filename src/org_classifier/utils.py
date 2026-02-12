from __future__ import annotations

import re
import unicodedata


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
