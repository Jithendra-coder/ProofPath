import json
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AssessmentCompetency(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=80)]
    target_level: Literal["junior", "mid", "senior"]
    competency_code: Annotated[str, Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9_.-]{2,79}$")] | None = None

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value or any(ord(character) < 32 for character in value):
            raise ValueError("Enter a valid competency name.")
        return value


class AssessmentRubricCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    competency_name: Annotated[str, Field(min_length=1, max_length=80)]
    name: Annotated[str, Field(min_length=1, max_length=80)]
    description: Annotated[str, Field(min_length=1, max_length=500)]
    weight_percent: Annotated[int, Field(ge=1, le=100)]

    @field_validator("competency_name", "name", "description")
    @classmethod
    def clean_text(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value or any(ord(character) < 32 for character in value):
            raise ValueError("Enter valid rubric text.")
        return value


class PythonAssessmentTestCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    competency_code: Annotated[str, Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9_.-]{2,79}$")]
    arguments: dict[str, Any] = Field(max_length=32)
    expected: Any

    @model_validator(mode="after")
    def bounded_json(self):
        try:
            serialized = json.dumps({"arguments": self.arguments, "expected": self.expected}, allow_nan=False)
        except (TypeError, ValueError):
            raise ValueError("Test inputs and expected values must be finite JSON values.") from None
        if len(serialized.encode("utf-8")) > 8192:
            raise ValueError("Each deterministic test case must be at most 8 KB.")
        return self


class PythonAssessmentTestSuite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entrypoint: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_]*$")]
    cases: Annotated[list[PythonAssessmentTestCase], Field(min_length=1, max_length=6)]


class AssessmentDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Annotated[str, Field(min_length=3, max_length=160)]
    role_label: Annotated[str, Field(min_length=2, max_length=80)]
    assessment_type: Literal["live_project_change", "project_discussion"]
    duration_minutes: Annotated[int, Field(ge=15, le=240)]
    competencies: Annotated[list[AssessmentCompetency], Field(min_length=1, max_length=8)]
    task_instructions: Annotated[str, Field(min_length=40, max_length=5000)]
    rubric_criteria: Annotated[list[AssessmentRubricCriterion], Field(min_length=1, max_length=8)]
    allowed_tools: Annotated[list[Literal["editor", "test_runner"]], Field(min_length=1, max_length=2)]
    python_test_suite: PythonAssessmentTestSuite | None = None

    @field_validator("title", "role_label", "task_instructions")
    @classmethod
    def clean_assessment_text(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value or any(ord(character) < 32 for character in value):
            raise ValueError("Enter valid assessment text.")
        return value

    @field_validator("competencies")
    @classmethod
    def unique_competencies(cls, values: list[AssessmentCompetency]) -> list[AssessmentCompetency]:
        names = [item.name.casefold() for item in values]
        if len(names) != len(set(names)):
            raise ValueError("Competency names must be unique.")
        codes = [item.competency_code for item in values if item.competency_code]
        if len(codes) != len(set(codes)):
            raise ValueError("Choose each catalog competency only once.")
        return values

    @field_validator("rubric_criteria")
    @classmethod
    def rubric_totals_one_hundred(cls, values: list[AssessmentRubricCriterion]) -> list[AssessmentRubricCriterion]:
        names = [item.name.casefold() for item in values]
        if len(names) != len(set(names)):
            raise ValueError("Rubric criterion names must be unique.")
        if sum(item.weight_percent for item in values) != 100:
            raise ValueError("Rubric weights must total 100 percent.")
        return values

    @model_validator(mode="after")
    def rubric_matches_competencies(self):
        if len(self.title) < 3 or len(self.role_label) < 2 or len(self.task_instructions) < 40:
            raise ValueError("Normalized title, role, and task instructions must meet their minimum lengths.")
        competencies = {item.name.casefold() for item in self.competencies}
        if any(item.competency_name.casefold() not in competencies for item in self.rubric_criteria):
            raise ValueError("Each rubric criterion must map to a listed competency.")
        if self.python_test_suite:
            linked = {item.competency_code for item in self.competencies if item.competency_code}
            if self.assessment_type != "live_project_change" or "test_runner" not in self.allowed_tools:
                raise ValueError("A Python test suite requires a live project assessment with the test runner enabled.")
            if not linked or any(item.competency_code not in linked for item in self.python_test_suite.cases):
                raise ValueError("Every test case must map to a catalog-linked assessment competency.")
        return self

    @field_validator("allowed_tools")
    @classmethod
    def unique_tools(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("Allowed tools must be unique.")
        return values


class AssessmentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_key: uuid.UUID
    definition: AssessmentDefinition


class AssessmentDraftUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: Annotated[int, Field(ge=1)]
    definition: AssessmentDefinition


class AssessmentPublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: Annotated[int, Field(ge=1)]
    review_confirmation: Literal["assessment-review-v1"]


class AssessmentListItem(BaseModel):
    id: uuid.UUID
    title: str
    role_label: str
    assessment_type: Literal["live_project_change", "project_discussion"]
    duration_minutes: int
    status: Literal["draft", "published", "archived"]
    version: int
    published_version: int | None
    created_at: datetime
    updated_at: datetime


class AssessmentListResponse(BaseModel):
    assessments: list[AssessmentListItem]
    limit: int
    offset: int


class AssessmentResponse(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    status: Literal["draft", "published", "archived"]
    version: int
    published_version: int | None
    definition: AssessmentDefinition
    created_at: datetime
    updated_at: datetime
    published_at: datetime | None


class AssessmentInvitationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_key: uuid.UUID
    application_id: uuid.UUID
    expected_application_version: Annotated[int, Field(ge=1)]
    assessment_id: uuid.UUID
    assessment_version: Annotated[int, Field(ge=1)]


class AssessmentInvitationCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: Annotated[int, Field(ge=1)]
    reason: Annotated[str, Field(min_length=10, max_length=500)]

    @field_validator("reason")
    @classmethod
    def clean_reason(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 10 or any(ord(character) < 32 for character in value):
            raise ValueError("Provide a cancellation reason of at least 10 characters.")
        return value


class AssessmentInvitationRespondRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: Annotated[int, Field(ge=1)]
    action: Literal["accept", "decline"]
    consent_version: Literal["assessment-invitation-v1"] | None = None

    @model_validator(mode="after")
    def require_consent_on_accept(self):
        if self.action == "accept" and self.consent_version != "assessment-invitation-v1":
            raise ValueError("Accepting an assessment invitation requires the current explicit consent.")
        if self.action == "decline" and self.consent_version is not None:
            raise ValueError("Declining an invitation does not record assessment consent.")
        return self


class OrganizationAssessmentInvitationItem(BaseModel):
    id: uuid.UUID
    application_id: uuid.UUID
    applicant_name: str
    job_title: str
    assessment_title: str
    assessment_version: int
    status: Literal["issued", "accepted", "declined", "cancelled", "expired"]
    version: int
    expires_at: datetime
    created_at: datetime
    responded_at: datetime | None


class OrganizationAssessmentInvitationListResponse(BaseModel):
    invitations: list[OrganizationAssessmentInvitationItem]
    limit: int
    offset: int


class CandidateAssessmentInvitationListItem(BaseModel):
    id: uuid.UUID
    application_id: uuid.UUID
    organization_name: str
    job_title: str
    assessment_title: str
    assessment_version: int
    status: Literal["issued", "accepted", "declined", "cancelled", "expired"]
    version: int
    expires_at: datetime
    created_at: datetime
    responded_at: datetime | None


class CandidateAssessmentInvitationListResponse(BaseModel):
    invitations: list[CandidateAssessmentInvitationListItem]
    limit: int
    offset: int


class CandidateAssessmentDefinition(BaseModel):
    title: str
    role_label: str
    assessment_type: Literal["live_project_change", "project_discussion"]
    duration_minutes: int
    competencies: list[AssessmentCompetency]
    task_instructions: str
    rubric_criteria: list[AssessmentRubricCriterion]
    allowed_tools: list[Literal["editor", "test_runner"]]
    python_entrypoint: str | None


class CandidateAssessmentInvitationResponse(CandidateAssessmentInvitationListItem):
    assessment: CandidateAssessmentDefinition
    consent_version: Literal["assessment-invitation-v1"] | None
    consented_at: datetime | None


class AssessmentAttemptCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_key: uuid.UUID
    expected_invitation_version: Annotated[int, Field(ge=1)]


class AssessmentAttemptRecordingPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audio: Literal[False] = False
    video: Literal[False] = False
    screen: Literal[False] = False
    transcript: Literal[False] = False


class AssessmentAttemptConsentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: Annotated[int, Field(ge=1)]
    action: Literal["accept", "revoke"]
    consent_version: Literal["assessment-attempt-v1"] | None = None
    recording_preferences: AssessmentAttemptRecordingPreferences = Field(default_factory=AssessmentAttemptRecordingPreferences)

    @model_validator(mode="after")
    def validate_action_consent(self):
        if (self.action == "accept") != (self.consent_version == "assessment-attempt-v1"):
            raise ValueError("Accept requires the current consent version; revoke must omit it.")
        return self


class AssessmentAttemptConsentResponse(BaseModel):
    attempt_id: uuid.UUID
    attempt_status: Literal["created", "consented"]
    attempt_version: int
    consent_status: Literal["accepted", "revoked"]
    consent_version: Literal["assessment-attempt-v1"]
    consent_event_at: datetime
    recording_preferences: AssessmentAttemptRecordingPreferences


class AssessmentAttemptItem(BaseModel):
    id: uuid.UUID
    invitation_id: uuid.UUID
    organization_name: str
    job_title: str
    assessment_title: str
    assessment_version: int
    status: Literal["created", "consented", "ready", "active", "paused", "submitted", "processing", "completed", "review_required", "processing_failed", "cancelled"]
    version: int
    requires_consent: bool
    next_action: Literal["prepare", "none"]
    created_at: datetime
    updated_at: datetime


class AssessmentAttemptListResponse(BaseModel):
    attempts: list[AssessmentAttemptItem]
    limit: int
    offset: int


class AssessmentAttemptReadinessResponse(BaseModel):
    attempt_id: uuid.UUID
    status: Literal["created", "consented", "ready", "active", "paused", "submitted", "processing", "completed", "review_required", "processing_failed", "cancelled"]
    can_start: bool
    requires_consent: bool
    execution_available: bool
    recording_enabled: Literal[False]
    blockers: list[Literal["ASSESSMENT_CONSENT_REQUIRED", "ISOLATED_RUNTIME_UNAVAILABLE", "ASSESSMENT_TEST_SUITE_UNAVAILABLE", "ATTEMPT_CANCELLED"]]


class AssessmentWorkspaceResponse(BaseModel):
    attempt_id: uuid.UUID
    status: Literal["consented", "active"]
    version: int
    source_code: str
    assessment_title: str
    task_instructions: str
    python_entrypoint: str | None


class AssessmentWorkspaceUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: Annotated[int, Field(ge=0)]
    source_code: Annotated[str, Field(min_length=1, max_length=50000)]

    @field_validator("source_code")
    @classmethod
    def bounded_source(cls, value: str) -> str:
        try:
            byte_length = len(value.encode("utf-8"))
        except UnicodeEncodeError:
            raise ValueError("Source code must be valid UTF-8 text no larger than 50 KB.") from None
        if "\x00" in value or byte_length > 50000:
            raise ValueError("Source code must be valid UTF-8 text no larger than 50 KB.")
        return value


class AssessmentTestRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_workspace_version: Annotated[int, Field(ge=1)]
    request_key: uuid.UUID


