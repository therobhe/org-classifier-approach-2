from typing import Optional
from ..models import ClassificationResult
from ..constants import HEURISTIC_KEYWORDS


def classify_by_heuristic(organisation_name: str) -> Optional[ClassificationResult]:
    """
    Apply conservative keyword-based heuristics to classify legal form.
    Returns low-confidence result on match, None otherwise.
    """
    name_lower = organisation_name.lower()
    
    for keyword, legal_form in HEURISTIC_KEYWORDS.items():
        if keyword in name_lower:
            return ClassificationResult(
                organisation_name=organisation_name,
                legal_form=legal_form,
                confidence="low",
                source="heuristic"
            )
    
    # Conservative fallback: if the name contains the substring 'verein' assume e.V. (low confidence).
    # This intentionally matches compounds like 'Sportverein' as well as 'Verein XYZ'.
    if any(sub in name_lower for sub in ("verein", "vereins")):
        return ClassificationResult(
            organisation_name=organisation_name,
            legal_form="e.V.",
            confidence="low",
            source="heuristic_verein",
        )

    return None


def classify_as_unknown(organisation_name: str) -> ClassificationResult:
    """
    Return an unknown classification result.
    """
    return ClassificationResult(
        organisation_name=organisation_name,
        legal_form=None,
        confidence="unknown",
        source=None
    )
