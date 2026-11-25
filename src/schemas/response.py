from typing import Literal
from pydantic import BaseModel, Field, ConfigDict

class EvidenceReference(BaseModel):
    model_config=ConfigDict(extra='forbid')
    issue_key: str
    comment_id: str
    quote: str = Field(min_length=1,description='Exact substring of the recorded Jira comment')
    explanation: str

class RequirementDecision(BaseModel):
    model_config=ConfigDict(extra='forbid')
    requirement_id: str
    decision: Literal['PASS','FAIL','INSUFFICIENT_EVIDENCE']
    reason: str
    supporting_evidence: list[EvidenceReference]
    rejected_evidence: list[EvidenceReference]
    outstanding_verification: list[str]


class BatchDecision(BaseModel):
    """One independent decision for every requirement supplied in the batch."""
    model_config=ConfigDict(extra='forbid')
    decisions: list[RequirementDecision] = Field(min_length=1)
