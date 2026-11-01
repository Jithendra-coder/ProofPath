import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class AssessmentTestRunCompetencyResult(BaseModel):
    competency_code: str
    status: Literal["demonstrated", "insufficient"]
    passed_cases: int
    total_cases: int


class AssessmentTestRunResponse(BaseModel):
    id: uuid.UUID
    attempt_id: uuid.UUID
    workspace_version: int
    status: Literal["running", "passed", "failed", "timed_out", "infrastructure_failed", "cancelled"]
    competency_results: list[AssessmentTestRunCompetencyResult]
    created_at: datetime
    completed_at: datetime | None


