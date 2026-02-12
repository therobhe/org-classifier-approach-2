from typing import Optional
from ..models import ClassificationResult
from ..constants import LEGAL_FORMS


def classify_by_name(organisation_name: str) -> Optional[ClassificationResult]:
    """
    Extract legal form from organisation name using regex patterns.
    Returns high-confidence result on match, None otherwise.
    """
    for legal_form, pattern in LEGAL_FORMS:
        if pattern.search(organisation_name):
            return ClassificationResult(
                organisation_name=organisation_name,
                legal_form=legal_form,
                confidence="high",
                source="name"
            )
    return None
