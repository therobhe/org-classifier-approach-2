from typing import Optional, Literal
from pydantic import BaseModel, Field


class OrgRecord(BaseModel):
    """Input organisation record"""
    organisation_name: str


class ClassificationResult(BaseModel):
    """Classification result for an organisation"""
    organisation_name: str
    legal_form: Optional[str] = None
    confidence: Literal["high", "medium", "low", "unknown"] = "unknown"
    source: Optional[str] = None
