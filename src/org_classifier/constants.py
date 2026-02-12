import re
from typing import Dict, Pattern, List, Tuple


# German legal forms with regex patterns.
# CRITICAL: Order is LONGEST / MOST-SPECIFIC first. The first match wins.
# Compound forms (e.g. "Stiftung & Co. KG") MUST appear before their
# shorter components ("Stiftung", "KG").
LEGAL_FORMS: List[Tuple[str, Pattern]] = [
    # ── Compound partnership forms (longest first) ──────────────────────
    ("GmbH & Co. KGaA", re.compile(r'\bGmbH\s*&\s*Co\.?\s*KGaA\b', re.IGNORECASE)),
    ("AG & Co. KGaA",   re.compile(r'\bAG\s*&\s*Co\.?\s*KGaA\b', re.IGNORECASE)),
    ("SE & Co. KGaA",   re.compile(r'\bSE\s*&\s*Co\.?\s*KGaA\b', re.IGNORECASE)),
    ("Stiftung & Co. KGaA", re.compile(r'\bStiftung\s*&\s*Co\.?\s*KGaA\b', re.IGNORECASE)),
    ("GmbH & Co. KG",   re.compile(r'\bGmbH\s*&\s*Co\.?\s*KG\b', re.IGNORECASE)),
    ("AG & Co. KG",     re.compile(r'\bAG\s*&\s*Co\.?\s*KG\b', re.IGNORECASE)),
    ("SE & Co. KG",     re.compile(r'\bSE\s*&\s*Co\.?\s*KG\b', re.IGNORECASE)),
    ("Stiftung & Co. KG", re.compile(r'\bStiftung\s*&\s*Co\.?\s*KG\b', re.IGNORECASE)),
    ("GmbH & Co. OHG",  re.compile(r'\bGmbH\s*&\s*Co\.?\s*OHG\b', re.IGNORECASE)),

    # ── KGaA (before KG and AG) ─────────────────────────────────────────
    ("KGaA", re.compile(r'\bKGaA\b', re.IGNORECASE)),

    # ── Limited liability companies ─────────────────────────────────────
    ("gGmbH", re.compile(r'\bgemeinnützige\s+GmbH\b', re.IGNORECASE)),
    ("gGmbH",  re.compile(r'\bgGmbH\b', re.IGNORECASE)),
    ("GmbH",   re.compile(r'\bGmbH\b', re.IGNORECASE)),
    ("UG (haftungsbeschränkt)", re.compile(r'\bUG\s*\(haftungsbeschränkt\)', re.IGNORECASE)),
    ("UG",     re.compile(r'\bUG(?=\s|$|[,;.)\-])', re.IGNORECASE)),
    ("gUG",    re.compile(r'\bgUG(?=\s|$|[,;.)\-])', re.IGNORECASE)),

    # ── Partnership long forms (before short KG / OHG) ──────────────────
    ("PartG mbB", re.compile(r'\bPartG\s+mbB\b', re.IGNORECASE)),
    ("Partnerschaftsgesellschaft", re.compile(r'\bPartnerschaftsgesellschaft\b', re.IGNORECASE)),
    ("PartG",  re.compile(r'\bPartG\b', re.IGNORECASE)),
    ("Kommanditgesellschaft", re.compile(r'\bKommanditgesellschaft\b', re.IGNORECASE)),
    ("Offene Handelsgesellschaft", re.compile(r'\bOffene\s+Handelsgesellschaft\b', re.IGNORECASE)),
    ("Gesellschaft bürgerlichen Rechts", re.compile(r'\bGesellschaft\s+bürgerlichen\s+Rechts\b', re.IGNORECASE)),

    # ── Short partnership abbreviations ──────────────────────────────────
    ("OHG", re.compile(r'\bOHG\b', re.IGNORECASE)),
    ("KG",  re.compile(r'\bKG(?=\s|$|[,;.)\-])', re.IGNORECASE)),
    ("GbR", re.compile(r'\bGbR\b', re.IGNORECASE)),

    # ── Stock corporations ──────────────────────────────────────────────
    ("Aktiengesellschaft", re.compile(r'\bAktiengesellschaft\b', re.IGNORECASE)),
    ("AG",  re.compile(r'\bAG(?=\s|$|[,;.)\-])', re.IGNORECASE)),

    # ── European company ────────────────────────────────────────────────
    ("Societas Europaea", re.compile(r'\bSocietas\s+Europaea\b', re.IGNORECASE)),
    ("SE",  re.compile(r'\bSE(?=\s|$|[,;.)\-])', re.IGNORECASE)),

    # ── Cooperatives ────────────────────────────────────────────────────
    ("eingetragene Genossenschaft", re.compile(r'\beingetragene\s+Genossenschaft\b', re.IGNORECASE)),
    ("eG",  re.compile(r'\beG(?=\s|$|[,;.)\-])', re.IGNORECASE)),

    # ── Associations ────────────────────────────────────────────────────
    # e.V. appears in the wild as: e.V, e.V., e. V., e.V-, e.V, e V and all occurances of small e big V without space between
    # Match either a dot or whitespace between 'e' and 'V' (but not no-separator "EV").
    ("e.V.", re.compile(r"\be\s*\.?\s*v\.?(?=\b|[^a-zA-Z])", re.IGNORECASE)),
    ("e.V.", re.compile(r'\beingetragener\s+Verein\b', re.IGNORECASE)),

    # ── Foundations (long forms first) ──────────────────────────────────
    ("Stiftung des öffentlichen Rechts", re.compile(r'\bStiftung\s+des\s+öffentlichen\s+Rechts\b', re.IGNORECASE)),
    ("Stiftung des bürgerlichen Rechts", re.compile(r'\bStiftung\s+des\s+bürgerlichen\s+Rechts\b', re.IGNORECASE)),
    ("Stiftung", re.compile(r'\bStiftung\b', re.IGNORECASE)),

    # ── Public entities ─────────────────────────────────────────────────
    ("Anstalt des öffentlichen Rechts", re.compile(r'\bAnstalt\s+des\s+öffentlichen\s+Rechts\b', re.IGNORECASE)),
    ("AöR",  re.compile(r'\bAöR\b', re.IGNORECASE)),
    ("Körperschaft des öffentlichen Rechts", re.compile(r'\bKörperschaft\s+des\s+öffentlichen\s+Rechts\b', re.IGNORECASE)),
    ("KdöR", re.compile(r'\bKdöR\b', re.IGNORECASE)),

    # Betrieb gewerblicher Art / Eigenbetrieb -> BgA (Betrieb gewerblicher Art)
    ("BgA", re.compile(r"\b(?:Eigenbetrieb|Betrieb\s+gewerblicher\s+Art|\bBgA\b)\b", re.IGNORECASE)),

    # ── Sole proprietorships ────────────────────────────────────────────
    ("e.K.", re.compile(r'\be\.\s?K\.', re.IGNORECASE)),
    ("e.K.",  re.compile(r'\beingetragener\s+Kaufmann\b', re.IGNORECASE)),
    ("e.K.",  re.compile(r'\beingetragene\s+Kauffrau\b', re.IGNORECASE)),
]


# Heuristic keywords for fallback classification.
# ONLY include keywords that strongly imply a specific legal form.
# Never include generic words like "gesellschaft", "company", "firma"
# which appear in many names regardless of legal form.
HEURISTIC_KEYWORDS: Dict[str, str] = {
    "förderverein": "e.V.",
    "sportverein": "e.V.",
    "turnverein": "e.V.",
    "musikverein": "e.V.",
    "heimatverein": "e.V.",
    "bürgerverein": "e.V.",
    "karnevalsverein": "e.V.",
    "schützenverein": "e.V.",
    "Wohlfahrtsverband": "e.V.",
    "Gesamtverband": "e.V.",
    "genossenschaft": "eG",
}
