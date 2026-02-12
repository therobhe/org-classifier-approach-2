import re
from typing import List, Pattern, Tuple, Optional, Dict

"""
Party detection rules for German political parties.

Provides:
- PARTY_PATTERNS: List[Tuple[label, compiled_regex]]
- classify_party(name) -> Optional[dict]

Maintainable, umlaut-safe, abbreviation-friendly patterns.
"""


def _umlaut_safe(token: str) -> str:
    mapping = {
        "ä": "(?:ä|ae)",
        "Ä": "(?:Ä|Ae)",
        "ö": "(?:ö|oe)",
        "Ö": "(?:Ö|Oe)",
        "ü": "(?:ü|ue)",
        "Ü": "(?:Ü|Ue)",
        "ß": "(?:ß|ss)",
    }
    out = []
    for ch in token:
        out.append(mapping.get(ch, re.escape(ch)))
    return "".join(out)


def _abbr_like(symbol: str) -> str:
    """Create an abbreviation-friendly pattern like `S.P.D.`, `S P D`, or `SPD`."""
    sep = r"(?:[.\s-]*)"
    return r"\b" + sep.join(re.escape(c) for c in symbol) + r"\b"


PARTY_PATTERNS: List[Tuple[str, Pattern]] = []


def _add(label: str, pattern: str):
    PARTY_PATTERNS.append((label, re.compile(pattern, re.IGNORECASE)))


# Major parties
_add("SPD (Sozialdemokratische Partei Deutschlands)",
     r"(?:\bSozialdemokratische\s+Partei\s+Deutschlands\b|" + _abbr_like("SPD") + r")")

_add("CDU (Christlich Demokratische Union)",
     r"(?:\bChristlich[-\s]*Demokratische\s+Union(?:\s+Deutschlands)?\b|" + _abbr_like("CDU") + r")")

_add("CSU (Christlich-Soziale Union)",
     r"(?:\bChristlich[-\s]*Soziale\s+Union\b|" + _abbr_like("CSU") + r")")

_add("FDP (Freie Demokratische Partei)",
     r"(?:\bFreie\s+Demokratische\s+Partei\b|" + _abbr_like("FDP") + r")")

_add("AfD (Alternative für Deutschland)",
     r"(?:\bAlternative\s+für\s+Deutschland\b|" + _abbr_like("AfD") + r")")

# Bündnis 90/Die Grünen and variants (umlaut-safe)
_green_full = (
    rf"(?:\b{_umlaut_safe('Bündnis')}\s*90\s*/\s*Die\s*{_umlaut_safe('Grünen')}\b|"
    rf"\bB90\s*/\s*{_umlaut_safe('Grünen')}\b|"
    rf"\bDie\s+{_umlaut_safe('Grünen')}\b|"
    rf"\b{_umlaut_safe('Grünen')}\b)"
)
_add("Bündnis 90/Die Grünen", _green_full)

# Die Linke
_add("Die Linke", r"(?:\bDie\s+Linke\b|\bLINKE\b|\bLinke\b)")

# Die PARTEI
_add("Die PARTEI", r"(?:\bDie\s+PARTEI\b|\bPARTEI\b)")

# Piratenpartei
_add("Piratenpartei", r"(?:\bPiratenpartei\b|\bPiraten\b)")

# Freie Wähler / FW
_add("Freie Wähler", r"(?:\bFreie\s+W(?:ä|ae)hler\b|\bFW\b)")

# ÖDP (umlaut-safe)
_add("ÖDP", rf"(?:\b{_umlaut_safe('Ökologisch')}\s*[-\s]*Demokratische\s*Partei\b|\bÖDP\b|\bOeDP\b)")

# Volt
_add("Volt", r"(?:\bVolt\b)")

# SSW
_add("SSW", r"(?:\bSüdschleswigscher\s+Wählerverband\b|\bSSW\b)")

# Bayernpartei / BP
_add("Bayernpartei", r"(?:\bBayernpartei\b|\bBP\b)")

# Tierschutzpartei / Partei Mensch Umwelt Tierschutz
_add("Tierschutzpartei", r"(?:\bTierschutzpartei\b|\bPartei\s+Mensch\s+Umwelt\s+Tierschutz\b)")

# NPD (historical)
_add("NPD", r"(?:\bNationaldemokratische\s+Partei\s+Deutschlands\b|\bNPD\b)")


def classify_party(org_name: str) -> Optional[Dict[str, str]]:
    """Return classification dict when `org_name` matches a party pattern.

    Returns:
      { 'legal_form': 'Partei', 'confidence': 'high', 'source': 'party_regex_rule', 'matched_party': label }
      or None if no match.
    """
    if not org_name:
        return None
    for label, pattern in PARTY_PATTERNS:
        if pattern.search(org_name):
            return {
                "legal_form": "Partei",
                "confidence": "medium",
                "source": "heuristic_partei",
                "matched_party": label,
            }
    return None


if __name__ == "__main__":
    examples = [
        "Sozialdemokratische Partei Deutschlands (SPD)",
        "Bündnis 90/Die Grünen - Ortsverband",
        "Verein der Sportfreunde",
        "CDU Ortsverband Berlin",
        "Die Linke e.V.",
        "Gruene Jugend",
        "S P D",
        "S.P.D.",
    ]
    for ex in examples:
        res = classify_party(ex)
        print(f"{ex} => {res}")
