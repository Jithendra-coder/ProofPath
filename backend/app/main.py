import hmac
import hashlib
import json
import logging
import os
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Iterator, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg
from psycopg.types.json import Jsonb
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, Response, Security
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import APIKeyCookie, APIKeyHeader
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.app.db import connect_database, migrate_database
from backend.app.assessment_runner import PodmanAssessmentRunner
from backend.app.email_delivery import Mailer, create_mailer
from backend.app.rate_limit import PostgresRateLimiter
from backend.app.security import DUMMY_PASSWORD_HASH, hash_password, hash_token, verify_password


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = PROJECT_ROOT / "frontend"
load_dotenv(PROJECT_ROOT / ".env")

SESSION_COOKIE = "__Host-proofpath_session"
CSRF_COOKIE = "__Host-proofpath_csrf"
SESSION_SECONDS = 60 * 60 * 12
session_cookie_scheme = APIKeyCookie(
    name=SESSION_COOKIE,
    scheme_name="ProofpathSession",
    description="Opaque server-side session cookie.",
    auto_error=False,
)
csrf_header_scheme = APIKeyHeader(
    name="X-CSRF-Token",
    scheme_name="ProofpathCSRF",
    description=f"Must match the {CSRF_COOKIE} cookie.",
    auto_error=False,
)
logger = logging.getLogger("proofpath.auth")
PROJECT_SOURCE_LINKS_SELECT = """COALESCE((
    SELECT jsonb_agg(jsonb_build_object('id', source.id, 'url', source.url, 'created_at', source.created_at)
                     ORDER BY source.created_at, source.id)
    FROM candidate_project_source_links AS source WHERE source.project_id = p.id
), '[]'::jsonb) AS source_links"""
PROJECT_COMPETENCY_EVIDENCE_SELECT = """COALESCE((
    SELECT jsonb_agg(jsonb_build_object(
        'id', evidence.id, 'competency_code', competency.code, 'competency_name', competency.name,
        'statement', evidence.candidate_statement, 'source_type', evidence.source_type,
        'verification_method', evidence.verification_method, 'verification_status', 'unverified',
        'review_status', evidence.review_status, 'sharing_scope', evidence.sharing_scope,
        'evidence_limitations', evidence.evidence_limitations, 'recorded_at', evidence.recorded_at
    ) ORDER BY competency.sort_order)
    FROM candidate_competency_evidence AS evidence
    JOIN competencies AS competency ON competency.code = evidence.competency_code
    WHERE evidence.candidate_user_id = p.user_id AND evidence.project_id = p.id
      AND evidence.source_type = 'project_claim' AND evidence.withdrawn_at IS NULL
), '[]'::jsonb) AS competency_claims"""
PROJECT_CLAIM_LIMITATIONS = (
    "Candidate-authored claim; project content and linked repository contents were not independently verified."
)
JOB_COMPETENCY_REQUIREMENTS_SELECT = """COALESCE((
    SELECT jsonb_agg(jsonb_build_object(
        'competency_code', competency.code, 'competency_name', competency.name,
        'target_level', requirement.target_level,
        'accepted_verification_methods', requirement.accepted_verification_methods,
        'max_age_days', requirement.max_age_days
    ) ORDER BY competency.sort_order)
    FROM organization_job_competency_requirements AS requirement
    JOIN competencies AS competency ON competency.code = requirement.competency_code
    WHERE requirement.organization_id = job.organization_id AND requirement.job_id = job.id
), '[]'::jsonb) AS competency_requirements"""
JOB_SELECT = f"""SELECT job.id, job.organization_id, organization.name AS organization_name,
                  job.title, job.description, job.status, job.version, job.published_at, job.closed_at,
                  job.created_at, job.updated_at, {JOB_COMPETENCY_REQUIREMENTS_SELECT}
           FROM organization_jobs AS job JOIN organizations AS organization
             ON organization.id = job.organization_id"""
ASSESSMENT_SELECT = """SELECT assessment.id, assessment.organization_id, assessment.status, assessment.version,
                            assessment.definition, assessment.created_at, assessment.updated_at,
                            assessment.published_at, published.version_number AS published_version,
                            COALESCE(published.definition, assessment.definition) AS effective_definition
                     FROM organization_assessments AS assessment
                     LEFT JOIN LATERAL (
                         SELECT version_number, definition FROM organization_assessment_versions
                         WHERE assessment_id = assessment.id ORDER BY version_number DESC LIMIT 1
                     ) AS published ON TRUE"""
INTERVIEW_SELECT = """SELECT interview.id, interview.application_id,
                           application.job_snapshot->>'title' AS job_title,
                           organization.name AS organization_name,
                           application.portfolio_snapshot->>'display_name' AS candidate_name,
                           interview.request_payload_hash,
                           interviewer.full_name AS interviewer_name, interview.scheduled_at,
                           interview.duration_minutes, interview.display_timezone, interview.meeting_url,
                           interview.candidate_note, interview.status, interview.candidate_response,
                           interview.response_message, interview.responded_at, interview.cancellation_reason,
                           interview.version, interview.created_at, interview.updated_at
                    FROM application_interviews AS interview
                    JOIN job_applications AS application ON application.id = interview.application_id
                    JOIN organizations AS organization ON organization.id = interview.organization_id
                    JOIN users AS interviewer ON interviewer.id = interview.interviewer_user_id"""


class RegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: Annotated[str, Field(min_length=1, max_length=120)]
    email: Annotated[EmailStr, Field(max_length=254)]
    password: Annotated[str, Field(min_length=12, max_length=128)]
    initial_purpose: Literal["candidate", "employer"]
    terms_version: Annotated[str, Field(min_length=1, max_length=64)]

    @field_validator("full_name")
    @classmethod
    def clean_full_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value or any(ord(character) < 32 for character in value):
            raise ValueError("Enter a valid name.")
        return value


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: Annotated[str, Field(min_length=1, max_length=128)]


class VerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: Annotated[str, Field(min_length=32, max_length=128)]


class PasswordResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr


class PasswordResetConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: Annotated[str, Field(min_length=32, max_length=128)]
    new_password: Annotated[str, Field(min_length=12, max_length=128)]


class RegistrationResponse(BaseModel):
    next_action: Literal["verify_email"] = "verify_email"


class EmailVerificationResponse(BaseModel):
    email_verified: bool


class PasswordResetRequestedResponse(BaseModel):
    message: Literal["If the account exists, a password reset link will be sent."] = (
        "If the account exists, a password reset link will be sent."
    )


class PasswordResetConfirmationResponse(BaseModel):
    password_reset: bool


class AuthUser(BaseModel):
    id: uuid.UUID
    email: EmailStr
    email_verified: bool
    initial_purpose: Literal["candidate", "employer"]


class LoginResponse(BaseModel):
    user: AuthUser
    next_action: Literal["candidate_onboarding", "employer_onboarding"]


class SessionResponse(BaseModel):
    user: AuthUser
    next_action: Literal["candidate_onboarding", "employer_onboarding"]


class AccountSessionItem(BaseModel):
    session_id: uuid.UUID
    created_at: datetime
    expires_at: datetime
    is_current: bool


class AccountSessionListResponse(BaseModel):
    sessions: list[AccountSessionItem]


class RevokeOtherSessionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: Annotated[str, Field(min_length=1, max_length=128)]


class RevokeOtherSessionsResponse(BaseModel):
    revoked_session_count: Annotated[int, Field(ge=0)]


class AccountDataExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: Annotated[str, Field(min_length=1, max_length=128)]


class AccountDataExportResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["proofpath-account-export-v1"]
    generated_at: datetime
    account: dict[str, Any]
    candidate_profile: dict[str, Any] | None
    projects: list[dict[str, Any]]
    portfolio: dict[str, Any] | None
    portfolio_versions: list[dict[str, Any]]
    plan_entitlements: dict[str, Any]
    evidence: list[dict[str, Any]]
    saved_jobs: list[dict[str, Any]]
    applications: list[dict[str, Any]]
    interviews: list[dict[str, Any]]
    notifications: list[dict[str, Any]]
    assessment_invitations: list[dict[str, Any]]
    assessment_attempts: list[dict[str, Any]]
    organization_memberships: list[dict[str, Any]]
    organization_invitations: list[dict[str, Any]]
    security_sessions: list[dict[str, Any]]
    scope_notes: list[str]


class CandidateOnboardingPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: Annotated[int, Field(ge=0)]
    full_name: Annotated[str, Field(min_length=1, max_length=120)] | None = None
    target_roles: Annotated[list[Annotated[str, Field(min_length=1, max_length=80)]] | None, Field(max_length=5)] = None
    skills: Annotated[list[Annotated[str, Field(min_length=1, max_length=80)]] | None, Field(max_length=20)] = None

    @field_validator("full_name")
    @classmethod
    def clean_full_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.split())
        if not value or any(ord(character) < 32 for character in value):
            raise ValueError("Enter a valid name.")
        return value

    @field_validator("target_roles", "skills")
    @classmethod
    def clean_labels(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        normalized = []
        seen = set()
        for value in values:
            value = " ".join(value.split())
            key = value.casefold()
            if not value or any(ord(character) < 32 for character in value):
                raise ValueError("Enter a valid label.")
            if key in seen:
                raise ValueError("Duplicate labels are not allowed.")
            seen.add(key)
            normalized.append(value)
        return normalized

    @model_validator(mode="after")
    def require_patch_fields(self):
        if not self.model_fields_set.intersection({"full_name", "target_roles", "skills"}):
            raise ValueError("Provide at least one onboarding field.")
        if any(getattr(self, field) is None for field in self.model_fields_set & {"full_name", "target_roles", "skills"}):
            raise ValueError("Onboarding fields cannot be null.")
        return self


class CandidateOnboardingCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: Annotated[int, Field(ge=0)]


class CandidateOnboardingResponse(BaseModel):
    full_name: str
    target_roles: list[str]
    skills: list[str]
    status: Literal["draft", "complete"]
    version: int
    updated_at: datetime | None


class CandidateDashboardResponse(BaseModel):
    candidate_name: str
    candidate_setup_complete: bool
    portfolio_public: bool
    public_handle: str | None
    public_project_count: int
    application_count: int
    saved_job_count: int
    pending_assessment_invitation_count: int
    active_assessment_attempt_count: int


class CompetencyCatalogItem(BaseModel):
    code: str
    track: Literal["python-fastapi-backend"]
    name: str
    description: str


class CompetencyCatalogResponse(BaseModel):
    track: Literal["python-fastapi-backend"]
    competencies: list[CompetencyCatalogItem]


class ProjectCompetencyClaimInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    competency_code: Annotated[str, Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9_.-]{2,79}$")]
    statement: Annotated[str, Field(min_length=20, max_length=1000)]

    @field_validator("statement")
    @classmethod
    def clean_statement(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 20 or any(ord(character) < 32 for character in value):
            raise ValueError("Describe how the project relates to this competency.")
        return value


def validate_unique_competency_claims(values: list[ProjectCompetencyClaimInput]) -> list[ProjectCompetencyClaimInput]:
    codes = [item.competency_code for item in values]
    if len(codes) != len(set(codes)):
        raise ValueError("Choose each competency only once.")
    return sorted(values, key=lambda item: item.competency_code)


class ProjectCompetencyEvidenceResponse(BaseModel):
    id: uuid.UUID
    competency_code: str
    competency_name: str
    statement: str
    source_type: Literal["project_claim"]
    verification_method: Literal["self_reported"]
    verification_status: Literal["unverified"]
    review_status: Literal["unreviewed"]
    sharing_scope: Literal["candidate_only"]
    evidence_limitations: str
    recorded_at: datetime


class ProjectCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_key: uuid.UUID
    name: Annotated[str, Field(min_length=2, max_length=160)]
    problem_statement: Annotated[str, Field(max_length=2000)] = ""
    contribution: Annotated[str, Field(max_length=4000)] = ""
    technologies: Annotated[list[Annotated[str, Field(min_length=1, max_length=40)]], Field(max_length=12)] = Field(default_factory=list)
    competency_claims: Annotated[list[ProjectCompetencyClaimInput], Field(max_length=8)] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def clean_project_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 2 or any(ord(character) < 32 for character in value):
            raise ValueError("Enter a valid project name.")
        return value

    @field_validator("problem_statement", "contribution")
    @classmethod
    def clean_project_description(cls, value: str) -> str:
        value = " ".join(value.split())
        if any(ord(character) < 32 for character in value):
            raise ValueError("Enter valid project text.")
        return value

    @field_validator("technologies")
    @classmethod
    def clean_technologies(cls, values: list[str]) -> list[str]:
        normalized = []
        seen = set()
        for value in values:
            value = " ".join(value.split())
            key = value.casefold()
            if not value or any(ord(character) < 32 for character in value) or key in seen:
                raise ValueError("Enter unique, valid technologies.")
            seen.add(key)
            normalized.append(value)
        return normalized

    @field_validator("competency_claims")
    @classmethod
    def unique_competency_claims(cls, values: list[ProjectCompetencyClaimInput]) -> list[ProjectCompetencyClaimInput]:
        return validate_unique_competency_claims(values)


class ProjectSourceLinkCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: Annotated[str, Field(min_length=1, max_length=300)]

    @field_validator("url")
    @classmethod
    def validate_github_repository_url(cls, value: str) -> str:
        value = value.strip()
        try:
            parsed = urlsplit(value)
            if parsed.port is not None:
                raise ValueError
        except ValueError as error:
            raise ValueError("Enter a public GitHub repository URL.") from error
        segments = parsed.path.strip("/").split("/")
        valid_segment = lambda item: item not in (".", "..") and all(
            character.isascii() and (character.isalnum() or character in "_.-") for character in item
        )
        if (
            parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or len(segments) != 2
            or not all(valid_segment(item) for item in segments)
        ):
            raise ValueError("Enter a public GitHub repository URL.")
        return f"https://github.com/{segments[0]}/{segments[1]}"


class ProjectSourceLinkResponse(BaseModel):
    id: uuid.UUID
    url: str
    created_at: datetime


class ProjectPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: Annotated[int, Field(ge=0)]
    name: Annotated[str, Field(min_length=2, max_length=160)] | None = None
    problem_statement: Annotated[str, Field(max_length=2000)] | None = None
    contribution: Annotated[str, Field(max_length=4000)] | None = None
    technologies: Annotated[list[Annotated[str, Field(min_length=1, max_length=40)]], Field(max_length=12)] | None = None
    competency_claims: Annotated[list[ProjectCompetencyClaimInput], Field(max_length=8)] | None = None

    @field_validator("name")
    @classmethod
    def clean_project_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.split())
        if len(value) < 2 or any(ord(character) < 32 for character in value):
            raise ValueError("Enter a valid project name.")
        return value

    @field_validator("problem_statement", "contribution")
    @classmethod
    def clean_project_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.split())
        if any(ord(character) < 32 for character in value):
            raise ValueError("Enter valid project text.")
        return value

    @field_validator("technologies")
    @classmethod
    def clean_technologies(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        normalized = []
        seen = set()
        for value in values:
            value = " ".join(value.split())
            key = value.casefold()
            if not value or any(ord(character) < 32 for character in value) or key in seen:
                raise ValueError("Enter unique, valid technologies.")
            seen.add(key)
            normalized.append(value)
        return normalized

    @field_validator("competency_claims")
    @classmethod
    def unique_competency_claims(cls, values: list[ProjectCompetencyClaimInput] | None) -> list[ProjectCompetencyClaimInput] | None:
        return None if values is None else validate_unique_competency_claims(values)

    @model_validator(mode="after")
    def require_project_fields(self):
        fields = self.model_fields_set & {"name", "problem_statement", "contribution", "technologies", "competency_claims"}
        if not fields or any(getattr(self, field) is None for field in fields):
            raise ValueError("Provide at least one project field, without null values.")
        return self


class ProjectResponse(BaseModel):
    id: uuid.UUID
    name: str
    problem_statement: str
    contribution: str
    technologies: list[str]
    source_links: list[ProjectSourceLinkResponse] = Field(default_factory=list)
    competency_claims: list[ProjectCompetencyEvidenceResponse] = Field(default_factory=list)
    status: Literal["draft"]
    visibility: Literal["private", "public"]
    version: int
    created_at: datetime
    updated_at: datetime


class ProjectListResponse(BaseModel):
    projects: list[ProjectResponse]


class PortfolioReorderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: Annotated[int, Field(ge=0)]
    project_ids: list[uuid.UUID]

    @field_validator("project_ids")
    @classmethod
    def unique_project_ids(cls, values: list[uuid.UUID]) -> list[uuid.UUID]:
        if len(values) != len(set(values)):
            raise ValueError("A project can appear only once.")
        return values


class PortfolioResponse(BaseModel):
    visibility: Literal["private"]
    version: int
    projects: list[ProjectResponse]
    updated_at: datetime | None
    public_handle: str | None
    public_handle_locked: bool
    published_at: datetime | None


class PortfolioDesign(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_key: Literal["editorial", "engineer", "case-study", "compact-resume"]
    theme_key: Literal["coral", "teal", "ink"]
    sections: Annotated[list[Literal["roles", "skills", "projects"]], Field(min_length=1, max_length=3)]

    @field_validator("sections")
    @classmethod
    def unique_sections(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("A section can appear only once.")
        return values


class PortfolioVersionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_draft_version: Annotated[int, Field(ge=0)]
    design: PortfolioDesign


class PortfolioVersionResponse(BaseModel):
    id: uuid.UUID
    version_number: int
    template_key: Literal["editorial", "engineer", "case-study", "compact-resume"]
    theme_key: Literal["coral", "teal", "ink"]
    sections: list[Literal["roles", "skills", "projects"]]
    created_at: datetime
    published_at: datetime | None
    is_draft: bool
    is_published: bool


class PortfolioVersionsResponse(BaseModel):
    draft_version_id: uuid.UUID | None
    published_version_id: uuid.UUID | None
    versions: list[PortfolioVersionResponse]


class CandidateEntitlementsResponse(BaseModel):
    candidate_plan: Literal["free", "pro"]
    portfolio_customization: bool


class CandidateProfileHandleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    handle: Annotated[str, Field(min_length=3, max_length=30)]

    @field_validator("handle")
    @classmethod
    def validate_public_handle(cls, value: str) -> str:
        value = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,28}[a-z0-9]", value):
            raise ValueError("Use 3–30 letters, numbers, or internal hyphens.")
        if value in {"admin", "api", "help", "jobs", "login", "portfolio", "public", "settings", "signup", "support"}:
            raise ValueError("That profile address is reserved.")
        return value


class CandidateProfileHandleResponse(BaseModel):
    handle: str
    published: bool


class CandidateProfileDiscoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    discoverable: Annotated[bool, Field(strict=True)]
    consent_version: Literal["candidate-discovery-v1"]


class CandidateProfileDiscoveryResponse(BaseModel):
    discoverable: bool
    profile_published: bool
    consent_version: Literal["candidate-discovery-v1"] | None


class DiscoverableCandidateItem(BaseModel):
    display_name: str
    target_roles: list[str]
    skills: list[str]
    profile_path: str


class DiscoverableCandidateListResponse(BaseModel):
    candidates: list[DiscoverableCandidateItem]
    limit: int
    offset: int
    has_more: bool


class PortfolioPublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consent_version: Literal["public-portfolio-v1"]
    draft_version_id: uuid.UUID | None = None


class ProjectPublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: Annotated[int, Field(ge=0)]
    consent_version: Literal["public-project-v1"]


class PublicProjectResponse(BaseModel):
    name: str
    problem_statement: str
    contribution: str
    technologies: list[str]
    source_links: list[str]


class PublicPortfolioResponse(BaseModel):
    display_name: str
    target_roles: list[str]
    skills: list[str]
    projects: list[PublicProjectResponse]
    design: PortfolioDesign | None = None


class OrganizationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_key: uuid.UUID
    name: Annotated[str, Field(min_length=2, max_length=160)]

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 2 or any(ord(character) < 32 for character in value):
            raise ValueError("Enter a valid organization name.")
        return value


class OrganizationOnboardingPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: Annotated[int, Field(ge=0)]
    name: Annotated[str, Field(min_length=2, max_length=160)]

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 2 or any(ord(character) < 32 for character in value):
            raise ValueError("Enter a valid organization name.")
        return value


class OrganizationOnboardingCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: Annotated[int, Field(ge=0)]


class OrganizationResponse(BaseModel):
    id: uuid.UUID
    name: str
    member_role: Literal["owner", "admin", "recruiter", "member"]
    onboarding_status: Literal["draft", "complete"]
    created_at: datetime


class OrganizationOnboardingResponse(BaseModel):
    organization_id: uuid.UUID
    name: str
    status: Literal["draft", "complete"]
    version: int
    updated_at: datetime


class OrganizationInvitationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    role: Literal["admin", "recruiter", "member"]

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: EmailStr) -> str:
        return str(value).lower()


class OrganizationInvitationResponse(BaseModel):
    id: uuid.UUID
    email_masked: str
    role: Literal["admin", "recruiter", "member"]
    status: Literal["pending", "accepted", "cancelled", "expired"]
    expires_at: datetime
    created_at: datetime


class OrganizationInvitationListResponse(BaseModel):
    invitations: list[OrganizationInvitationResponse]


class OrganizationInvitationAcceptanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: Annotated[str, Field(min_length=32, max_length=128)]


class OrganizationInvitationAcceptanceResponse(BaseModel):
    organization_id: uuid.UUID
    organization_name: str
    member_role: Literal["admin", "recruiter", "member"]


class OrganizationMemberResponse(BaseModel):
    user_id: uuid.UUID
    display_name: str
    role: Literal["owner", "admin", "recruiter", "member"]
    joined_at: datetime


class OrganizationMemberListResponse(BaseModel):
    members: list[OrganizationMemberResponse]


class OrganizationMemberRolePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["admin", "recruiter", "member"]


class JobCompetencyRequirementInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    competency_code: Annotated[str, Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9_.-]{2,79}$")]
    target_level: Literal["junior", "mid", "senior"]
    accepted_verification_methods: Annotated[
        list[Literal["deterministic_test", "human_review"]], Field(min_length=1, max_length=2)
    ]
    max_age_days: Annotated[int, Field(ge=1, le=3650)] = 365

    @field_validator("accepted_verification_methods")
    @classmethod
    def unique_verification_methods(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("Choose each accepted evidence method only once.")
        return sorted(values)


def validate_unique_job_competency_requirements(
    values: list[JobCompetencyRequirementInput],
) -> list[JobCompetencyRequirementInput]:
    codes = [item.competency_code for item in values]
    if len(codes) != len(set(codes)):
        raise ValueError("Choose each competency only once.")
    return sorted(values, key=lambda item: item.competency_code)


class JobCompetencyRequirementResponse(BaseModel):
    competency_code: str
    competency_name: str
    target_level: Literal["junior", "mid", "senior"]
    accepted_verification_methods: list[Literal["deterministic_test", "human_review"]]
    max_age_days: int


class JobCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_key: uuid.UUID
    title: Annotated[str, Field(min_length=3, max_length=160)]
    description: Annotated[str, Field(min_length=20, max_length=10000)]
    competency_requirements: Annotated[list[JobCompetencyRequirementInput], Field(max_length=8)] = Field(default_factory=list)

    @field_validator("title")
    @classmethod
    def clean_job_title(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 3 or any(ord(character) < 32 for character in value):
            raise ValueError("Enter a valid job title.")
        return value

    @field_validator("description")
    @classmethod
    def clean_job_description(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 20 or any(ord(character) < 32 for character in value):
            raise ValueError("Enter a job description with at least 20 characters.")
        return value

    @field_validator("competency_requirements")
    @classmethod
    def unique_competency_requirements(cls, values: list[JobCompetencyRequirementInput]) -> list[JobCompetencyRequirementInput]:
        return validate_unique_job_competency_requirements(values)


class JobPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: Annotated[int, Field(ge=0)]
    title: Annotated[str, Field(min_length=3, max_length=160)] | None = None
    description: Annotated[str, Field(min_length=20, max_length=10000)] | None = None
    competency_requirements: Annotated[list[JobCompetencyRequirementInput], Field(max_length=8)] | None = None

    @field_validator("title")
    @classmethod
    def clean_job_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.split())
        if len(value) < 3 or any(ord(character) < 32 for character in value):
            raise ValueError("Enter a valid job title.")
        return value

    @field_validator("description")
    @classmethod
    def clean_job_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.split())
        if len(value) < 20 or any(ord(character) < 32 for character in value):
            raise ValueError("Enter a job description with at least 20 characters.")
        return value

    @field_validator("competency_requirements")
    @classmethod
    def unique_competency_requirements(cls, values: list[JobCompetencyRequirementInput] | None) -> list[JobCompetencyRequirementInput] | None:
        return None if values is None else validate_unique_job_competency_requirements(values)

    @model_validator(mode="after")
    def require_job_fields(self):
        fields = self.model_fields_set & {"title", "description", "competency_requirements"}
        if not fields or any(getattr(self, field) is None for field in fields):
            raise ValueError("Provide at least one job field, without null values.")
        return self


class JobTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: Annotated[int, Field(ge=0)]


class JobResponse(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    organization_name: str
    title: str
    description: str
    status: Literal["draft", "published", "closed"]
    version: int
    published_at: datetime | None
    closed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    competency_requirements: list[JobCompetencyRequirementResponse] = Field(default_factory=list)


class JobListResponse(BaseModel):
    jobs: list[JobResponse]


class PublicJobResponse(BaseModel):
    id: uuid.UUID
    organization_name: str
    title: str
    description: str
    published_at: datetime
    competency_requirements: list[JobCompetencyRequirementResponse] = Field(default_factory=list)


class PublicJobListResponse(BaseModel):
    jobs: list[PublicJobResponse]
    limit: int
    offset: int


class SavedJobItem(BaseModel):
    id: uuid.UUID
    organization_name: str
    title: str
    description: str
    status: Literal["draft", "published", "closed"]
    published_at: datetime | None
    saved_at: datetime


class SavedJobListResponse(BaseModel):
    jobs: list[SavedJobItem]


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


class ApplicationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_key: uuid.UUID
    portfolio_consent_version: Literal["job-application-public-portfolio-v1"]
    cover_note: Annotated[str, Field(max_length=2000)] = ""

    @field_validator("cover_note")
    @classmethod
    def clean_cover_note(cls, value: str) -> str:
        value = " ".join(value.split())
        if any(ord(character) < 32 for character in value):
            raise ValueError("Enter valid application text.")
        return value


class ApplicationWithdrawRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: Annotated[int, Field(ge=1)]


class ApplicationStageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: Annotated[int, Field(ge=1)]
    stage: Literal["in_review", "assessment", "interview", "offer"]


class ApplicationDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: Annotated[int, Field(ge=1)]
    decision: Literal["hired", "rejected"]
    reason: Annotated[str, Field(min_length=10, max_length=2000)]

    @field_validator("reason")
    @classmethod
    def clean_reason(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 10 or any(ord(character) < 32 for character in value):
            raise ValueError("Provide a decision reason of at least 10 characters.")
        return value


class ApplicationReviewerAssignmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_key: uuid.UUID
    expected_version: Annotated[int, Field(ge=0)]
    reviewer_user_id: uuid.UUID


class ApplicationReviewerAssignmentResponse(BaseModel):
    application_id: uuid.UUID
    reviewer_user_id: uuid.UUID
    reviewer_name: str
    assigned_by_user_id: uuid.UUID
    version: int
    assigned_at: datetime
    updated_at: datetime


class PortfolioSnapshotProject(BaseModel):
    name: str
    problem_statement: str
    contribution: str
    technologies: list[str]
    source_links: list[str]


class PortfolioSnapshotResponse(BaseModel):
    display_name: str
    target_roles: list[str]
    skills: list[str]
    projects: list[PortfolioSnapshotProject]


class CandidateApplicationListItem(BaseModel):
    id: uuid.UUID
    job_id: uuid.UUID
    job_title: str
    organization_name: str
    status: Literal["submitted", "in_review", "assessment", "interview", "offer", "hired", "rejected", "withdrawn"]
    submitted_at: datetime
    version: int


class CandidateApplicationListResponse(BaseModel):
    applications: list[CandidateApplicationListItem]


class CandidateApplicationResponse(CandidateApplicationListItem):
    cover_note: str
    job_description: str
    portfolio_snapshot: PortfolioSnapshotResponse


class InterviewScheduleFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scheduled_at: datetime
    duration_minutes: Annotated[int, Field(ge=15, le=180)]
    display_timezone: Annotated[str, Field(min_length=1, max_length=64)]
    meeting_url: Annotated[str, Field(max_length=2048)] | None = None
    candidate_note: Annotated[str, Field(max_length=1000)] = ""

    @field_validator("scheduled_at")
    @classmethod
    def validate_scheduled_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Choose an interview time with an explicit UTC offset.")
        now = datetime.now(timezone.utc)
        if value < now + timedelta(minutes=5) or value > now + timedelta(days=365):
            raise ValueError("Choose an interview time between five minutes and one year from now.")
        return value

    @field_validator("display_timezone")
    @classmethod
    def validate_display_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("Choose a valid IANA time zone.") from exc
        return value

    @field_validator("meeting_url")
    @classmethod
    def validate_meeting_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or any(ord(character) < 33 for character in value):
            raise ValueError("Meeting links must use HTTPS and cannot contain credentials.")
        return value

    @field_validator("candidate_note")
    @classmethod
    def clean_candidate_note(cls, value: str) -> str:
        value = " ".join(value.split())
        if any(ord(character) < 32 for character in value):
            raise ValueError("Enter a valid interview note.")
        return value


class InterviewCreateRequest(InterviewScheduleFields):
    application_id: uuid.UUID
    request_key: uuid.UUID


class InterviewUpdateRequest(InterviewScheduleFields):
    expected_version: Annotated[int, Field(ge=1)]


class InterviewResponseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: Annotated[int, Field(ge=1)]
    action: Literal["accept", "decline", "request_reschedule"]
    message: Annotated[str, Field(max_length=1000)] = ""

    @field_validator("message")
    @classmethod
    def clean_message(cls, value: str) -> str:
        value = " ".join(value.split())
        if any(ord(character) < 32 for character in value):
            raise ValueError("Enter a valid response message.")
        return value


class InterviewCancelRequest(BaseModel):
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


class InterviewItem(BaseModel):
    id: uuid.UUID
    application_id: uuid.UUID
    job_title: str
    organization_name: str
    candidate_name: str | None = None
    interviewer_name: str
    scheduled_at: datetime
    duration_minutes: int
    display_timezone: str
    meeting_url: str | None
    candidate_note: str
    status: Literal["scheduled", "cancelled"]
    candidate_response: Literal["pending", "accepted", "declined", "reschedule_requested"]
    response_message: str | None
    responded_at: datetime | None
    cancellation_reason: str | None = None
    version: int
    created_at: datetime
    updated_at: datetime


class InterviewListResponse(BaseModel):
    interviews: list[InterviewItem]


class NotificationItem(BaseModel):
    id: uuid.UUID
    notification_type: Literal["interview_scheduled", "interview_rescheduled", "interview_cancelled", "interview_response"]
    title: str
    message: str
    created_at: datetime
    is_read: bool


class NotificationListResponse(BaseModel):
    notifications: list[NotificationItem]
    unread_count: int


class NotificationPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_read: bool


class ApplicationEvidenceShareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: Annotated[int, Field(ge=0)]
    action: Literal["share", "revoke"]
    consent_version: Literal["candidate-application-evidence-v1"] | None = None
    evidence_ids: Annotated[list[uuid.UUID], Field(max_length=40)] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_share_action(self):
        if self.action == "share":
            if self.consent_version != "candidate-application-evidence-v1" or not self.evidence_ids:
                raise ValueError("Sharing requires the current consent version and at least one evidence item.")
            if len(set(self.evidence_ids)) != len(self.evidence_ids):
                raise ValueError("Evidence items must be unique.")
        elif self.consent_version is not None or self.evidence_ids:
            raise ValueError("Revoking sharing must omit consent and evidence items.")
        return self


class ApplicationEvidenceItem(BaseModel):
    id: uuid.UUID
    competency_code: str
    competency_name: str
    source_type: Literal["project_claim", "assessment_result"]
    project_name: str | None
    assessment_title: str | None
    candidate_statement: str | None
    observed_outcome: str | None
    verification_method: Literal["self_reported", "deterministic_test", "human_review"]
    proficiency_level: Literal["junior", "mid", "senior"] | None
    review_status: Literal["unreviewed", "accepted", "rejected"]
    evidence_limitations: str
    test_run_status: str | None
    test_competency_status: Literal["demonstrated", "insufficient"] | None
    passed_cases: int | None
    total_cases: int | None
    recorded_at: datetime


class ApplicationCompetencyGap(BaseModel):
    competency_code: str
    competency_name: str
    target_level: Literal["junior", "mid", "senior"]
    accepted_verification_methods: list[Literal["deterministic_test", "human_review"]]
    max_age_days: int
    status: Literal["demonstrated", "insufficient", "unverified"]
    explanation: str
    evidence: list[ApplicationEvidenceItem]


class CandidateApplicationEvidenceResponse(BaseModel):
    application_id: uuid.UUID
    share_version: int
    share_status: Literal["not_shared", "shared", "revoked"]
    can_share: bool
    shared_evidence_ids: list[uuid.UUID]
    available_evidence: list[ApplicationEvidenceItem]
    private_preview: list[ApplicationCompetencyGap]


class FocusedAssessmentRecommendation(BaseModel):
    assessment_id: uuid.UUID
    assessment_title: str
    assessment_version: int
    competency_codes: list[str]
    focused_test_case_count: int
    reason: str


class OrganizationApplicationEvidenceReport(BaseModel):
    application_id: uuid.UUID
    share_status: Literal["not_shared", "shared", "revoked"]
    requirements: list[ApplicationCompetencyGap]
    recommended_assessment: FocusedAssessmentRecommendation | None = None


class OrganizationApplicationListItem(BaseModel):
    id: uuid.UUID
    applicant_name: str
    status: Literal["submitted", "in_review", "assessment", "interview", "offer", "hired", "rejected"]
    submitted_at: datetime
    version: int
    assigned_reviewer_user_id: uuid.UUID | None
    assigned_reviewer_name: str | None


class OrganizationApplicationListResponse(BaseModel):
    applications: list[OrganizationApplicationListItem]


class OrganizationApplicationResponse(BaseModel):
    id: uuid.UUID
    job_id: uuid.UUID
    job_title: str
    status: Literal["submitted", "in_review", "assessment", "interview", "offer", "hired", "rejected"]
    applicant_name: str
    cover_note: str
    portfolio_snapshot: PortfolioSnapshotResponse
    competency_requirements: list[JobCompetencyRequirementResponse] = Field(default_factory=list)
    submitted_at: datetime
    version: int
    decided_by_user_id: uuid.UUID | None
    decided_at: datetime | None
    assigned_reviewer_user_id: uuid.UUID | None
    assigned_reviewer_name: str | None
    reviewer_assignment_version: int
    reviewer_assigned_at: datetime | None


class ApplicationEventItem(BaseModel):
    id: uuid.UUID
    actor_id: uuid.UUID
    actor_type: Literal["candidate", "organization"]
    action: Literal["submitted", "stage_changed", "decision_recorded", "withdrawn"]
    from_status: str | None
    to_status: str
    occurred_at: datetime


class ApplicationHistoryResponse(BaseModel):
    events: list[ApplicationEventItem]


class WorkspaceSummary(BaseModel):
    id: uuid.UUID
    type: Literal["candidate", "organization"]
    name: str
    role: Literal["candidate", "owner", "admin", "recruiter", "member"]
    onboarding_status: Literal["draft", "complete"]


class WorkspaceListResponse(BaseModel):
    workspaces: list[WorkspaceSummary]


class PublicMetadata(BaseModel):
    initial_purposes: list[Literal["candidate", "employer"]]
    enabled_auth_methods: list[Literal["email_password"]]
    registration_enabled: bool
    terms_version: str | None
    terms_url: str | None


class ValidationFieldError(BaseModel):
    field: str
    message: str
    code: str


class ProblemDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    title: str
    status: int
    detail: str
    instance: str
    request_id: str | None = None
    code: str
    field_errors: list[ValidationFieldError] | None = None


def problem_response(description: str) -> dict:
    return {
        "description": description,
        "content": {
            "application/problem+json": {
                "schema": {"$ref": "#/components/schemas/ProblemDetails"}
            }
        },
    }


def load_candidate_onboarding(
    connection: psycopg.Connection, user_id: uuid.UUID
) -> CandidateOnboardingResponse:
    row = connection.execute(
        """SELECT u.full_name, COALESCE(c.target_roles, '{}'::text[]) AS target_roles,
                  COALESCE(c.skills, '{}'::text[]) AS skills, c.completed_at,
                  COALESCE(c.version, 0) AS version, c.updated_at
           FROM users AS u LEFT JOIN candidate_onboardings AS c ON c.user_id = u.id
           WHERE u.id = %s""",
        (user_id,),
    ).fetchone()
    if row is None:
        raise api_error(401, "AUTHENTICATION_REQUIRED", "Authentication required", "Sign in to continue.")
    return CandidateOnboardingResponse(
        full_name=row["full_name"],
        target_roles=row["target_roles"],
        skills=row["skills"],
        status="complete" if row["completed_at"] else "draft",
        version=row["version"],
        updated_at=row["updated_at"],
    )


def organization_access(
    connection: psycopg.Connection,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    *,
    for_update: bool = False,
):
    query = """SELECT o.id, o.name, o.created_at, o.updated_at, o.version,
                      o.onboarding_completed_at, m.role AS member_role
               FROM organizations AS o
               JOIN organization_memberships AS m ON m.organization_id = o.id
               WHERE o.id = %s AND m.user_id = %s"""
    if for_update:
        query += " FOR UPDATE OF o"
    organization = connection.execute(query, (organization_id, user_id)).fetchone()
    if organization is None:
        raise api_error(404, "ORGANIZATION_NOT_FOUND", "Organization not found", "The organization was not found.")
    return organization


def require_application_reviewer_access(
    connection: psycopg.Connection,
    organization_id: uuid.UUID,
    application_id: uuid.UUID,
    user_id: uuid.UUID,
    member_role: str,
    *,
    lock: bool = False,
) -> None:
    if member_role in {"owner", "admin"}:
        return
    query = """SELECT 1 FROM application_reviewer_assignments
               WHERE organization_id = %s AND application_id = %s AND reviewer_user_id = %s"""
    if lock:
        query += " FOR SHARE"
    if member_role != "recruiter" or not connection.execute(
        query, (organization_id, application_id, user_id)
    ).fetchone():
        raise api_error(404, "APPLICATION_NOT_FOUND", "Application not found", "The application was not found in this organization.")


def validate_job_competency_codes(
    connection: psycopg.Connection, requirements: list[JobCompetencyRequirementInput]
) -> None:
    if not requirements:
        return
    codes = [requirement.competency_code for requirement in requirements]
    valid_codes = connection.execute(
        """SELECT code FROM competencies
           WHERE track = 'python-fastapi-backend' AND active AND code = ANY(%s)""",
        (codes,),
    ).fetchall()
    if len(valid_codes) != len(codes):
        raise api_error(422, "COMPETENCY_NOT_SUPPORTED", "Unsupported competency", "Choose an active Python/FastAPI competency.")


def validate_assessment_competency_codes(
    connection: psycopg.Connection, definition: AssessmentDefinition
) -> None:
    codes = [item.competency_code for item in definition.competencies if item.competency_code]
    if not codes:
        return
    valid_codes = connection.execute(
        "SELECT code FROM competencies WHERE track = 'python-fastapi-backend' AND active AND code = ANY(%s)",
        (codes,),
    ).fetchall()
    if len(valid_codes) != len(codes):
        raise api_error(422, "COMPETENCY_NOT_SUPPORTED", "Unsupported competency", "Choose an active Python/FastAPI competency.")


def sync_job_competency_requirements(
    connection: psycopg.Connection,
    organization_id: uuid.UUID,
    job_id: uuid.UUID,
    requirements: list[JobCompetencyRequirementInput],
) -> None:
    connection.execute(
        "DELETE FROM organization_job_competency_requirements WHERE organization_id = %s AND job_id = %s",
        (organization_id, job_id),
    )
    for requirement in requirements:
        connection.execute(
            """INSERT INTO organization_job_competency_requirements
               (organization_id, job_id, competency_code, target_level, accepted_verification_methods, max_age_days)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (organization_id, job_id, requirement.competency_code, requirement.target_level,
             requirement.accepted_verification_methods, requirement.max_age_days),
        )


def job_response(row) -> JobResponse:
    return JobResponse(**row)


def assessment_response(row) -> AssessmentResponse:
    return AssessmentResponse(
        id=row["id"],
        organization_id=row["organization_id"],
        status=row["status"],
        version=row["version"],
        published_version=row["published_version"],
        definition=AssessmentDefinition.model_validate(row["effective_definition"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        published_at=row["published_at"],
    )


def expire_assessment_invitations(
    connection: psycopg.Connection, *, scope: str, scope_id: uuid.UUID, request_id: str
) -> int:
    if scope not in {"organization_id", "candidate_user_id"}:
        raise ValueError("Unsupported invitation scope.")
    expired = connection.execute(
        f"""UPDATE assessment_invitations SET status = 'expired', version = version + 1, updated_at = now()
            WHERE {scope} = %s AND status = 'issued' AND expires_at <= now()
            RETURNING id, organization_id""",
        (scope_id,),
    ).fetchall()
    for invitation in expired:
        connection.execute(
            """INSERT INTO audit_events
               (id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome)
               VALUES (%s, 'system', %s, 'assessment_invitation', %s, 'assessment.invitation_expired', %s, 'success')""",
            (uuid.uuid4(), invitation["organization_id"], invitation["id"], request_id),
        )
    return len(expired)


def candidate_assessment_invitation_response(row) -> CandidateAssessmentInvitationResponse:
    return CandidateAssessmentInvitationResponse(
        id=row["id"],
        application_id=row["application_id"],
        organization_name=row["organization_name"],
        job_title=row["job_title"],
        assessment_title=row["assessment_title"],
        assessment_version=row["assessment_version"],
        status=row["status"],
        version=row["version"],
        expires_at=row["expires_at"],
        created_at=row["created_at"],
        responded_at=row["responded_at"],
        assessment=CandidateAssessmentDefinition(
            **AssessmentDefinition.model_validate(row["assessment_definition"]).model_dump(
                exclude={"python_test_suite"}
            ),
            python_entrypoint=(row["assessment_definition"].get("python_test_suite") or {}).get("entrypoint"),
        ),
        consent_version=row["consent_version"],
        consented_at=row["consented_at"],
    )


def build_account_data_export(
    connection: psycopg.Connection, user_id: uuid.UUID, generated_at: datetime
) -> AccountDataExportResponse:
    account = connection.execute(
        """SELECT full_name, email, email_verified_at, initial_purpose, terms_version,
                  terms_accepted_at, created_at
           FROM users WHERE id = %s""",
        (user_id,),
    ).fetchone()
    if account is None:
        raise api_error(401, "AUTHENTICATION_REQUIRED", "Authentication required", "Sign in to continue.")
    candidate_profile = connection.execute(
        """SELECT target_roles, skills, completed_at, version, updated_at
           FROM candidate_onboardings WHERE user_id = %s""",
        (user_id,),
    ).fetchone()
    portfolio = connection.execute(
        """SELECT version, updated_at, public_handle, published_at, publication_consent_at,
                  publication_consent_version, public_handle_locked_at, draft_version_id,
                  published_version_id, discoverable_at, discovery_consent_at,
                  discovery_consent_version
           FROM candidate_portfolios WHERE user_id = %s""",
        (user_id,),
    ).fetchone()
    projects = connection.execute(
        """SELECT project.id, project.name, project.problem_statement, project.contribution,
                  project.technologies, project.version, project.sort_order, project.published_at,
                  project.created_at, project.updated_at,
                  COALESCE((
                      SELECT jsonb_agg(jsonb_build_object('id', link.id, 'url', link.url, 'created_at', link.created_at)
                                      ORDER BY link.created_at, link.id)
                      FROM candidate_project_source_links AS link WHERE link.project_id = project.id
                  ), '[]'::jsonb) AS source_links
           FROM candidate_projects AS project WHERE project.user_id = %s
           ORDER BY project.sort_order, project.id""",
        (user_id,),
    ).fetchall()
    portfolio_versions = connection.execute(
        """SELECT id, version_number, template_key, theme_key, sections, created_at, published_at
           FROM candidate_portfolio_versions WHERE user_id = %s
           ORDER BY version_number""",
        (user_id,),
    ).fetchall()
    entitlement = connection.execute(
        """SELECT EXISTS (
               SELECT 1 FROM candidate_entitlement_grants
               WHERE user_id = %s AND feature_key = 'portfolio_customization'
                 AND starts_at <= now() AND (expires_at IS NULL OR expires_at > now())
                 AND revoked_at IS NULL
           ) AS portfolio_customization""",
        (user_id,),
    ).fetchone()
    evidence = connection.execute(
        """SELECT evidence.id, competency.code AS competency_code, competency.name AS competency_name,
                  evidence.source_type, evidence.project_id, evidence.assessment_attempt_id,
                  evidence.candidate_statement, evidence.observed_outcome,
                  evidence.verification_method, evidence.review_status, evidence.reviewed_at,
                  evidence.evidence_limitations, evidence.sharing_scope, evidence.recorded_at,
                  evidence.withdrawn_at, evidence.source_test_run_id, evidence.proficiency_level
           FROM candidate_competency_evidence AS evidence
           JOIN competencies AS competency ON competency.code = evidence.competency_code
           WHERE evidence.candidate_user_id = %s
           ORDER BY evidence.recorded_at, evidence.id""",
        (user_id,),
    ).fetchall()
    saved_jobs = connection.execute(
        """SELECT job.id AS job_id, job.title, organization.name AS organization_name,
                  job.status AS current_job_status, saved.created_at AS saved_at
           FROM saved_jobs AS saved
           JOIN organization_jobs AS job ON job.id = saved.job_id
           JOIN organizations AS organization ON organization.id = job.organization_id
           WHERE saved.user_id = %s ORDER BY saved.created_at, job.id""",
        (user_id,),
    ).fetchall()
    applications = connection.execute(
        """SELECT application.id, application.status, application.version,
                  application.portfolio_consent_version, application.portfolio_consented_at,
                  application.portfolio_snapshot, application.job_snapshot, application.cover_note,
                  application.withdrawn_at, application.submitted_at, application.updated_at,
                  COALESCE((
                      SELECT jsonb_agg(jsonb_build_object(
                          'actor_type', event.actor_type, 'action', event.action,
                          'from_status', event.from_status, 'to_status', event.to_status,
                          'occurred_at', event.occurred_at
                      ) ORDER BY event.event_number)
                      FROM application_events AS event WHERE event.application_id = application.id
                  ), '[]'::jsonb) AS status_history,
                  COALESCE((
                      SELECT jsonb_agg(jsonb_build_object(
                          'version', share.version, 'action', share.action,
                          'consent_version', share.consent_version, 'created_at', share.created_at,
                          'evidence', COALESCE((
                              SELECT jsonb_agg(jsonb_build_object(
                                  'competency_code', competency.code, 'competency_name', competency.name,
                                  'statement', evidence.candidate_statement, 'observed_outcome', evidence.observed_outcome,
                                  'verification_method', evidence.verification_method,
                                  'recorded_at', evidence.recorded_at
                              ) ORDER BY competency.sort_order)
                              FROM candidate_application_evidence_share_items AS item
                              JOIN candidate_competency_evidence AS evidence
                                ON evidence.id = item.evidence_id AND evidence.candidate_user_id = item.candidate_user_id
                              JOIN competencies AS competency ON competency.code = evidence.competency_code
                              WHERE item.share_event_id = share.id AND item.candidate_user_id = %s
                          ), '[]'::jsonb)
                      ) ORDER BY share.version)
                      FROM candidate_application_evidence_share_events AS share
                      WHERE share.application_id = application.id AND share.candidate_user_id = %s
                  ), '[]'::jsonb) AS evidence_sharing_history
           FROM job_applications AS application
           WHERE application.user_id = %s ORDER BY application.submitted_at, application.id""",
        (user_id, user_id, user_id),
    ).fetchall()
    interviews = connection.execute(
        """SELECT interview.id, interview.application_id, application.job_snapshot->>'title' AS job_title,
                  organization.name AS organization_name, interviewer.full_name AS interviewer_name,
                  interview.scheduled_at, interview.duration_minutes, interview.display_timezone,
                  interview.meeting_url, interview.candidate_note, interview.status,
                  interview.candidate_response, interview.response_message, interview.responded_at,
                  interview.cancellation_reason, interview.version, interview.created_at, interview.updated_at
           FROM application_interviews AS interview
           JOIN job_applications AS application ON application.id = interview.application_id
           JOIN organizations AS organization ON organization.id = interview.organization_id
           JOIN users AS interviewer ON interviewer.id = interview.interviewer_user_id
           WHERE interview.candidate_user_id = %s ORDER BY interview.scheduled_at, interview.id""",
        (user_id,),
    ).fetchall()
    notifications = connection.execute(
        """SELECT notification_type, title, message, created_at, read_at,
                  read_at IS NOT NULL AS is_read
           FROM user_notifications WHERE user_id = %s ORDER BY created_at, id""",
        (user_id,),
    ).fetchall()
    assessment_invitations = connection.execute(
        """SELECT invitation.id, invitation.application_id,
                  organization.name AS organization_name, application.job_snapshot->>'title' AS job_title,
                  version.definition->>'title' AS assessment_title, invitation.assessment_version,
                  invitation.status, invitation.version, invitation.expires_at, invitation.created_at,
                  invitation.responded_at, invitation.consent_version, invitation.consented_at,
                  version.definition - 'python_test_suite' AS assessment,
                  version.definition #>> '{python_test_suite,entrypoint}' AS python_entrypoint
           FROM assessment_invitations AS invitation
           JOIN organizations AS organization ON organization.id = invitation.organization_id
           JOIN job_applications AS application ON application.id = invitation.application_id
           JOIN organization_assessment_versions AS version
             ON version.assessment_id = invitation.assessment_id AND version.version_number = invitation.assessment_version
           WHERE invitation.candidate_user_id = %s
           ORDER BY invitation.created_at, invitation.id""",
        (user_id,),
    ).fetchall()
    assessment_attempts = connection.execute(
        """SELECT attempt.id, attempt.invitation_id, version.definition->>'title' AS assessment_title,
                  attempt.assessment_version, attempt.status, attempt.version, attempt.created_at, attempt.updated_at,
                  (SELECT jsonb_build_object('version', workspace.version, 'source_code', workspace.source_code,
                                             'updated_at', workspace.updated_at)
                   FROM assessment_attempt_workspaces AS workspace
                   WHERE workspace.attempt_id = attempt.id AND workspace.candidate_user_id = %s) AS workspace,
                  COALESCE((
                      SELECT jsonb_agg(jsonb_build_object(
                          'action', consent.action, 'consent_version', consent.consent_version,
                          'recording_preferences', consent.recording_preferences, 'created_at', consent.created_at
                      ) ORDER BY consent.attempt_version)
                      FROM assessment_attempt_consent_events AS consent
                      WHERE consent.attempt_id = attempt.id AND consent.candidate_user_id = %s
                  ), '[]'::jsonb) AS consent_history,
                  COALESCE((
                      SELECT jsonb_agg(jsonb_build_object(
                          'workspace_version', run.workspace_version, 'source_snapshot', run.source_snapshot,
                          'status', run.status, 'competency_results', run.competency_results,
                          'created_at', run.created_at, 'completed_at', run.completed_at
                      ) ORDER BY run.created_at, run.id)
                      FROM assessment_test_runs AS run
                      WHERE run.attempt_id = attempt.id AND run.candidate_user_id = %s
                  ), '[]'::jsonb) AS test_runs
           FROM assessment_attempts AS attempt
           JOIN organization_assessment_versions AS version
             ON version.assessment_id = attempt.assessment_id AND version.version_number = attempt.assessment_version
           WHERE attempt.candidate_user_id = %s ORDER BY attempt.created_at, attempt.id""",
        (user_id, user_id, user_id, user_id),
    ).fetchall()
    organization_memberships = connection.execute(
        """SELECT organization.name AS organization_name, membership.role, membership.created_at AS joined_at
           FROM organization_memberships AS membership
           JOIN organizations AS organization ON organization.id = membership.organization_id
           WHERE membership.user_id = %s ORDER BY membership.created_at, organization.name""",
        (user_id,),
    ).fetchall()
    organization_invitations = connection.execute(
        """SELECT organization.name AS organization_name, invitation.role,
                  CASE WHEN invitation.accepted_at IS NOT NULL THEN 'accepted'
                       WHEN invitation.cancelled_at IS NOT NULL THEN 'cancelled'
                       WHEN invitation.expires_at <= now() THEN 'expired'
                       ELSE 'pending' END AS status,
                  invitation.expires_at, invitation.accepted_at, invitation.cancelled_at, invitation.created_at
           FROM organization_invitations AS invitation
           JOIN organizations AS organization ON organization.id = invitation.organization_id
           WHERE invitation.email = %s ORDER BY invitation.created_at, invitation.id""",
        (account["email"],),
    ).fetchall()
    security_sessions = connection.execute(
        """SELECT created_at, expires_at, revoked_at,
                  revoked_at IS NULL AND expires_at > now() AS active
           FROM auth_sessions WHERE user_id = %s ORDER BY created_at""",
        (user_id,),
    ).fetchall()

    return AccountDataExportResponse(
        schema_version="proofpath-account-export-v1",
        generated_at=generated_at,
        account=account,
        candidate_profile=candidate_profile,
        projects=projects,
        portfolio=portfolio,
        portfolio_versions=portfolio_versions,
        plan_entitlements={
            "candidate_plan": "pro" if entitlement["portfolio_customization"] else "free",
            "portfolio_customization": entitlement["portfolio_customization"],
        },
        evidence=evidence,
        saved_jobs=saved_jobs,
        applications=applications,
        interviews=interviews,
        notifications=notifications,
        assessment_invitations=assessment_invitations,
        assessment_attempts=assessment_attempts,
        organization_memberships=organization_memberships,
        organization_invitations=organization_invitations,
        security_sessions=security_sessions,
        scope_notes=[
            "Credential hashes, session and CSRF tokens, verification/reset tokens, and internal audit records are excluded.",
            "Organization member data and private employer-only assessment test inputs are excluded.",
            "Hiring-team schedules for other candidates are excluded from employer account exports.",
            "Provider billing history and uploaded artifacts are not included because those integrations are not configured.",
        ],
    )


def candidate_application_response(row) -> CandidateApplicationResponse:
    job_snapshot = row["job_snapshot"]
    return CandidateApplicationResponse(
        id=row["id"],
        job_id=row["job_id"],
        job_title=job_snapshot["title"],
        organization_name=job_snapshot["organization_name"],
        status=row["status"],
        submitted_at=row["submitted_at"],
        version=row["version"],
        cover_note=row["cover_note"],
        job_description=job_snapshot["description"],
        portfolio_snapshot=PortfolioSnapshotResponse.model_validate(row["portfolio_snapshot"]),
    )


def organization_application_response(row) -> OrganizationApplicationResponse:
    job_snapshot = row["job_snapshot"]
    return OrganizationApplicationResponse(
        id=row["id"],
        job_id=row["job_id"],
        job_title=job_snapshot["title"],
        status=row["status"],
        applicant_name=row["applicant_name"],
        cover_note=row["cover_note"],
        portfolio_snapshot=PortfolioSnapshotResponse.model_validate(row["portfolio_snapshot"]),
        competency_requirements=job_snapshot.get("competency_requirements", []),
        submitted_at=row["submitted_at"],
        version=row["version"],
        decided_by_user_id=row["decided_by_user_id"],
        decided_at=row["decided_at"],
        assigned_reviewer_user_id=row["assigned_reviewer_user_id"],
        assigned_reviewer_name=row["assigned_reviewer_name"],
        reviewer_assignment_version=row["reviewer_assignment_version"],
        reviewer_assigned_at=row["reviewer_assigned_at"],
    )


def interview_item(row) -> InterviewItem:
    return InterviewItem(**row)


def candidate_interview_item(row) -> InterviewItem:
    candidate_view = dict(row)
    candidate_view["candidate_name"] = None
    return InterviewItem(**candidate_view)


def create_user_notification(
    connection: psycopg.Connection,
    user_id: uuid.UUID,
    interview_id: uuid.UUID,
    notification_type: str,
    source_version: int,
    title: str,
    message: str,
) -> None:
    connection.execute(
        """INSERT INTO user_notifications
           (id, user_id, interview_id, notification_type, source_version, title, message)
           VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
        (uuid.uuid4(), user_id, interview_id, notification_type, source_version, title, message),
    )


def ensure_interviewer_slot(
    connection: psycopg.Connection,
    interviewer_user_id: uuid.UUID,
    scheduled_at: datetime,
    duration_minutes: int,
    *,
    exclude_interview_id: uuid.UUID | None = None,
) -> None:
    # A per-interviewer transaction lock makes the overlap check atomic across API workers.
    connection.execute(
        "SELECT pg_advisory_xact_lock(2147483000, hashtext(%s))", (str(interviewer_user_id),)
    )
    conflict = connection.execute(
        """SELECT id FROM application_interviews
           WHERE interviewer_user_id = %s AND status = 'scheduled'
             AND candidate_response NOT IN ('declined', 'reschedule_requested')
             AND scheduled_at < %s::timestamptz + (%s * interval '1 minute')
             AND scheduled_at + (duration_minutes * interval '1 minute') > %s::timestamptz
             AND (%s::uuid IS NULL OR id <> %s::uuid)
           LIMIT 1""",
        (interviewer_user_id, scheduled_at, duration_minutes, scheduled_at, exclude_interview_id, exclude_interview_id),
    ).fetchone()
    if conflict:
        raise api_error(409, "INTERVIEW_TIME_CONFLICT", "Interviewer unavailable", "This interviewer already has an interview during that time.")


def cancel_application_interviews(
    connection: psycopg.Connection,
    application_id: uuid.UUID,
    organization_id: uuid.UUID,
    actor_id: uuid.UUID,
    request_id: str,
    action: str,
    reason: str,
) -> None:
    cancelled = connection.execute(
        """UPDATE application_interviews SET status = 'cancelled', cancelled_by_user_id = %s,
                  cancelled_at = now(), cancellation_reason = %s, version = version + 1, updated_at = now()
           WHERE application_id = %s AND status = 'scheduled' RETURNING id, version, candidate_user_id""",
        (actor_id, reason, application_id),
    ).fetchall()
    for interview in cancelled:
        if action.startswith("organization."):
            create_user_notification(
                connection, interview["candidate_user_id"], interview["id"],
                "interview_cancelled", interview["version"], "Interview cancelled",
                "Your interview was cancelled after the application reached a final decision.",
            )
        connection.execute(
            """INSERT INTO audit_events
               (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome)
               VALUES (%s, %s, 'user', %s, 'application_interview', %s, %s, %s, 'success')""",
            (uuid.uuid4(), actor_id, organization_id, interview["id"], action, request_id),
        )


def build_application_gap_report(
    requirements: list[dict],
    evidence_items: list[ApplicationEvidenceItem],
    *,
    evidence_shared: bool,
    now: datetime | None = None,
) -> list[ApplicationCompetencyGap]:
    levels = {"junior": 1, "mid": 2, "senior": 3}
    now = now or datetime.now(timezone.utc)
    report = []
    for requirement in requirements:
        matching = [item for item in evidence_items if item.competency_code == requirement["competency_code"]]
        accepted_methods = requirement["accepted_verification_methods"]
        reasons = set()
        demonstrated = False
        for item in matching:
            if item.verification_method not in accepted_methods:
                continue
            if item.verification_method == "deterministic_test" and item.test_competency_status != "demonstrated":
                reasons.add("The deterministic assessment cases did not all pass.")
                continue
            if item.verification_method == "human_review" and item.review_status != "accepted":
                reasons.add("The evidence has not been accepted through human review.")
                continue
            if item.proficiency_level is None:
                reasons.add("The evidence does not include a demonstrated proficiency level.")
                continue
            if levels[item.proficiency_level] < levels[requirement["target_level"]]:
                reasons.add("The demonstrated proficiency is below the job target.")
                continue
            if item.recorded_at < now - timedelta(days=requirement["max_age_days"]):
                reasons.add("The evidence is older than the employer's freshness limit.")
                continue
            demonstrated = True
            break
        if demonstrated:
            status = "demonstrated"
            explanation = "Shared evidence meets the accepted method, proficiency and freshness policy."
        elif not evidence_shared:
            status = "unverified"
            explanation = "The candidate has not shared evidence for this application."
        elif not matching:
            status = "unverified"
            explanation = "No shared evidence supports this competency."
        elif reasons:
            status = "insufficient"
            explanation = " ".join(sorted(reasons))
        else:
            status = "unverified"
            explanation = "Shared evidence does not use a verification method accepted by this job."
        report.append(ApplicationCompetencyGap(
            competency_code=requirement["competency_code"],
            competency_name=requirement["competency_name"],
            target_level=requirement["target_level"],
            accepted_verification_methods=accepted_methods,
            max_age_days=requirement["max_age_days"],
            status=status,
            explanation=explanation,
            evidence=matching,
        ))
    return report


def recommend_focused_assessment(
    connection: psycopg.Connection,
    organization_id: uuid.UUID,
    application_id: uuid.UUID,
    requirements: list[dict],
    gap_report: list[ApplicationCompetencyGap],
) -> FocusedAssessmentRecommendation | None:
    remaining = {
        item.competency_code: item for item in gap_report
        if item.status != "demonstrated" and "deterministic_test" in item.accepted_verification_methods
    }
    if not remaining:
        return None
    levels = {"junior": 1, "mid": 2, "senior": 3}
    candidates = []
    rows = connection.execute(
        """SELECT assessment.id, version.version_number, version.definition
           FROM organization_assessments AS assessment
           JOIN LATERAL (
               SELECT version_number, definition FROM organization_assessment_versions
               WHERE organization_id = assessment.organization_id AND assessment_id = assessment.id
               ORDER BY version_number DESC LIMIT 1
           ) AS version ON true
           WHERE assessment.organization_id = %s AND assessment.status = 'published'
             AND NOT EXISTS (
                 SELECT 1 FROM assessment_invitations AS invitation
                 WHERE invitation.application_id = %s AND invitation.assessment_id = assessment.id
             )
           ORDER BY assessment.created_at DESC, assessment.id LIMIT 50""",
        (organization_id, application_id),
    ).fetchall()
    for row in rows:
        definition = row["definition"]
        suite = definition.get("python_test_suite") or {}
        cases = suite.get("cases", [])
        if not cases:
            continue
        assessment_levels = {
            item["competency_code"]: item["target_level"]
            for item in definition.get("competencies", []) if item.get("competency_code")
        }
        covered = {
            case["competency_code"] for case in cases
            if case["competency_code"] in remaining
            and case["competency_code"] in assessment_levels
            and levels[assessment_levels[case["competency_code"]]] >= levels[remaining[case["competency_code"]].target_level]
        }
        if not covered:
            continue
        codes = [item["competency_code"] for item in requirements if item["competency_code"] in covered]
        focused_case_count = sum(case["competency_code"] in covered for case in cases)
        recommendation = FocusedAssessmentRecommendation(
            assessment_id=row["id"], assessment_title=definition["title"],
            assessment_version=row["version_number"], competency_codes=codes,
            focused_test_case_count=focused_case_count,
            reason=f"Covers {len(codes)} remaining job requirement{'s' if len(codes) != 1 else ''} with {focused_case_count} deterministic test case{'s' if focused_case_count != 1 else ''} at or above the required level.",
        )
        candidates.append(( -len(codes), focused_case_count, definition["title"].casefold(), str(row["id"]), recommendation))
    return min(candidates, key=lambda item: item[:4])[-1] if candidates else None


def load_application_evidence(
    connection: psycopg.Connection,
    candidate_user_id: uuid.UUID,
    evidence_ids: list[uuid.UUID] | None = None,
) -> list[ApplicationEvidenceItem]:
    query = """SELECT evidence.id, evidence.competency_code, competency.name AS competency_name,
                      evidence.source_type, project.name AS project_name,
                      assessment_version.definition->>'title' AS assessment_title,
                      evidence.candidate_statement, evidence.observed_outcome,
                      evidence.verification_method, evidence.proficiency_level, evidence.review_status,
                      evidence.evidence_limitations, evidence.recorded_at,
                      run.status AS test_run_status, run.competency_results
               FROM candidate_competency_evidence AS evidence
               JOIN competencies AS competency ON competency.code = evidence.competency_code
               LEFT JOIN candidate_projects AS project
                 ON project.id = evidence.project_id AND project.user_id = evidence.candidate_user_id
               LEFT JOIN assessment_attempts AS attempt
                 ON attempt.id = evidence.assessment_attempt_id AND attempt.candidate_user_id = evidence.candidate_user_id
               LEFT JOIN organization_assessment_versions AS assessment_version
                 ON assessment_version.assessment_id = attempt.assessment_id
                AND assessment_version.version_number = attempt.assessment_version
               LEFT JOIN assessment_test_runs AS run ON run.id = evidence.source_test_run_id
               WHERE evidence.candidate_user_id = %s AND evidence.withdrawn_at IS NULL"""
    params: tuple = (candidate_user_id,)
    if evidence_ids is not None:
        query += " AND evidence.id = ANY(%s)"
        params += (evidence_ids,)
    query += " ORDER BY evidence.recorded_at DESC, evidence.id"
    rows = connection.execute(query, params).fetchall()
    result = []
    for row in rows:
        test_item = next((item for item in (row["competency_results"] or [])
                          if item["competency_code"] == row["competency_code"]), None)
        result.append(ApplicationEvidenceItem(
            id=row["id"], competency_code=row["competency_code"], competency_name=row["competency_name"],
            source_type=row["source_type"], project_name=row["project_name"],
            assessment_title=row["assessment_title"], candidate_statement=row["candidate_statement"],
            observed_outcome=row["observed_outcome"], verification_method=row["verification_method"],
            proficiency_level=row["proficiency_level"], review_status=row["review_status"],
            evidence_limitations=row["evidence_limitations"], test_run_status=row["test_run_status"],
            test_competency_status=test_item["status"] if test_item else None,
            passed_cases=test_item["passed_cases"] if test_item else None,
            total_cases=test_item["total_cases"] if test_item else None,
            recorded_at=row["recorded_at"],
        ))
    return result


def candidate_application_evidence_response(
    connection: psycopg.Connection, application, candidate_user_id: uuid.UUID
) -> CandidateApplicationEvidenceResponse:
    event = connection.execute(
        """SELECT id, version, action FROM candidate_application_evidence_share_events
           WHERE application_id = %s AND candidate_user_id = %s ORDER BY version DESC LIMIT 1""",
        (application["id"], candidate_user_id),
    ).fetchone()
    shared_ids = []
    if event and event["action"] == "shared":
        shared_ids = [row["evidence_id"] for row in connection.execute(
            "SELECT evidence_id FROM candidate_application_evidence_share_items WHERE share_event_id = %s ORDER BY evidence_id",
            (event["id"],),
        ).fetchall()]
    available = load_application_evidence(connection, candidate_user_id)
    requirements = (application["job_snapshot"] or {}).get("competency_requirements", [])
    required_codes = {item["competency_code"] for item in requirements}
    available = [item for item in available if item.competency_code in required_codes]
    return CandidateApplicationEvidenceResponse(
        application_id=application["id"], share_version=event["version"] if event else 0,
        share_status=(event["action"] if event else "not_shared"),
        can_share=application["status"] not in {"hired", "rejected", "withdrawn"},
        shared_evidence_ids=shared_ids, available_evidence=available,
        private_preview=build_application_gap_report(requirements, available, evidence_shared=True),
    )


def organization_onboarding_response(row) -> OrganizationOnboardingResponse:
    return OrganizationOnboardingResponse(
        organization_id=row["id"],
        name=row["name"],
        status="complete" if row["onboarding_completed_at"] else "draft",
        version=row["version"],
        updated_at=row["updated_at"],
    )


def organization_invitation_response(row) -> OrganizationInvitationResponse:
    email_local, _, email_domain = row["email"].partition("@")
    return OrganizationInvitationResponse(
        id=row["id"],
        email_masked=f"{email_local[:1]}***@{email_domain}",
        role=row["role"],
        status=row.get("status", "pending"),
        expires_at=row["expires_at"],
        created_at=row["created_at"],
    )


def validate_project_competency_codes(
    connection: psycopg.Connection, claims: list[ProjectCompetencyClaimInput]
) -> None:
    if not claims:
        return
    codes = [claim.competency_code for claim in claims]
    valid_codes = connection.execute(
        """SELECT code FROM competencies
           WHERE track = 'python-fastapi-backend' AND active AND code = ANY(%s)""",
        (codes,),
    ).fetchall()
    if len(valid_codes) != len(codes):
        raise api_error(422, "COMPETENCY_NOT_SUPPORTED", "Unsupported competency", "Choose an active Python/FastAPI competency.")


def sync_project_competency_claims(
    connection: psycopg.Connection,
    candidate_user_id: uuid.UUID,
    project_id: uuid.UUID,
    claims: list[ProjectCompetencyClaimInput],
) -> None:
    current = connection.execute(
        """SELECT id, competency_code, candidate_statement
           FROM candidate_competency_evidence
           WHERE candidate_user_id = %s AND project_id = %s
             AND source_type = 'project_claim' AND withdrawn_at IS NULL
           FOR UPDATE""",
        (candidate_user_id, project_id),
    ).fetchall()
    existing = {row["competency_code"]: row for row in current}
    incoming = {claim.competency_code: claim for claim in claims}
    for code, row in existing.items():
        claim = incoming.get(code)
        if claim is None or claim.statement != row["candidate_statement"]:
            connection.execute(
                "UPDATE candidate_competency_evidence SET withdrawn_at = now() WHERE id = %s AND withdrawn_at IS NULL",
                (row["id"],),
            )
    for code, claim in incoming.items():
        previous = existing.get(code)
        if previous is not None and previous["candidate_statement"] == claim.statement:
            continue
        connection.execute(
            """INSERT INTO candidate_competency_evidence
               (id, candidate_user_id, competency_code, source_type, project_id, candidate_statement,
                verification_method, review_status, evidence_limitations, sharing_scope)
               VALUES (%s, %s, %s, 'project_claim', %s, %s, 'self_reported', 'unreviewed', %s, 'candidate_only')""",
            (uuid.uuid4(), candidate_user_id, code, project_id, claim.statement, PROJECT_CLAIM_LIMITATIONS),
        )


def project_response(row) -> ProjectResponse:
    return ProjectResponse(
        id=row["id"],
        name=row["name"],
        problem_statement=row["problem_statement"],
        contribution=row["contribution"],
        technologies=row["technologies"],
        source_links=row.get("source_links", []),
        competency_claims=row.get("competency_claims", []),
        status="draft",
        visibility="public" if row["published_at"] else "private",
        version=row["version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def load_private_portfolio(connection: psycopg.Connection, user_id: uuid.UUID) -> PortfolioResponse:
    connection.execute("SELECT id FROM users WHERE id = %s FOR SHARE", (user_id,))
    portfolio = connection.execute(
        "SELECT version, updated_at, public_handle, public_handle_locked_at, published_at FROM candidate_portfolios WHERE user_id = %s", (user_id,)
    ).fetchone()
    projects = connection.execute(
        f"""SELECT p.id, p.name, p.problem_statement, p.contribution, p.technologies,
                  p.version, p.created_at, p.updated_at, p.published_at, {PROJECT_SOURCE_LINKS_SELECT}, {PROJECT_COMPETENCY_EVIDENCE_SELECT}
           FROM candidate_projects AS p WHERE p.user_id = %s ORDER BY p.sort_order, p.id""",
        (user_id,),
    ).fetchall()
    return PortfolioResponse(
        visibility="private",
        version=portfolio["version"] if portfolio else 0,
        projects=[project_response(row) for row in projects],
        updated_at=portfolio["updated_at"] if portfolio else None,
        public_handle=portfolio["public_handle"] if portfolio else None,
        public_handle_locked=portfolio["public_handle_locked_at"] is not None if portfolio else False,
        published_at=portfolio["published_at"] if portfolio else None,
    )


def has_portfolio_customization(connection: psycopg.Connection, user_id: uuid.UUID) -> bool:
    return connection.execute(
        """SELECT EXISTS (
               SELECT 1 FROM candidate_entitlement_grants
               WHERE user_id = %s AND feature_key = 'portfolio_customization'
                 AND starts_at <= now() AND (expires_at IS NULL OR expires_at > now())
                 AND revoked_at IS NULL
           ) AS granted""",
        (user_id,),
    ).fetchone()["granted"]


def lock_portfolio_customization(connection: psycopg.Connection, user_id: uuid.UUID) -> bool:
    return connection.execute(
        """SELECT id FROM candidate_entitlement_grants
           WHERE user_id = %s AND feature_key = 'portfolio_customization'
             AND starts_at <= now() AND (expires_at IS NULL OR expires_at > now())
             AND revoked_at IS NULL
           ORDER BY created_at DESC LIMIT 1 FOR SHARE""",
        (user_id,),
    ).fetchone() is not None


def load_portfolio_versions(connection: psycopg.Connection, user_id: uuid.UUID) -> PortfolioVersionsResponse:
    portfolio = connection.execute(
        "SELECT draft_version_id, published_version_id FROM candidate_portfolios WHERE user_id = %s",
        (user_id,),
    ).fetchone()
    rows = connection.execute(
        """SELECT version.id, version.version_number, version.template_key, version.theme_key,
                  version.sections, version.created_at, version.published_at,
                  COALESCE(portfolio.draft_version_id = version.id, false) AS is_draft,
                  COALESCE(portfolio.published_version_id = version.id, false) AS is_published
           FROM candidate_portfolio_versions AS version
           LEFT JOIN candidate_portfolios AS portfolio ON portfolio.user_id = version.user_id
           WHERE version.user_id = %s ORDER BY version.version_number DESC""",
        (user_id,),
    ).fetchall()
    return PortfolioVersionsResponse(
        draft_version_id=portfolio["draft_version_id"] if portfolio else None,
        published_version_id=portfolio["published_version_id"] if portfolio else None,
        versions=[PortfolioVersionResponse(**row) for row in rows],
    )


def load_candidate_discovery(
    connection: psycopg.Connection, user_id: uuid.UUID
) -> CandidateProfileDiscoveryResponse:
    row = connection.execute(
        "SELECT discoverable_at, discovery_consent_version, published_at FROM candidate_portfolios WHERE user_id = %s",
        (user_id,),
    ).fetchone()
    discoverable = bool(row and row["discoverable_at"])
    return CandidateProfileDiscoveryResponse(
        discoverable=discoverable,
        profile_published=bool(row and row["published_at"]),
        consent_version=row["discovery_consent_version"] if discoverable else None,
    )


@dataclass(frozen=True)
class SessionContext:
    session_id: uuid.UUID
    csrf_token_hash: bytes
    user: AuthUser


def api_error(status: int, code: str, title: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail=detail,
        headers={"X-Problem-Code": code, "X-Problem-Title": title},
    )


def create_app(
    database_url: str | None = None,
    *,
    app_env: str | None = None,
    terms_version: str | None = None,
    terms_url: str | None = None,
    mailer: Mailer | None = None,
    dev_mailbox: Path | None = None,
    assessment_runner=None,
) -> FastAPI:
    dsn = database_url or os.getenv("DATABASE_URL")
    environment = app_env or os.getenv("APP_ENV", "development")
    active_terms_version = (terms_version if terms_version is not None else os.getenv("PROOFPATH_TERMS_VERSION")) or None
    active_terms_url = (terms_url if terms_url is not None else os.getenv("PROOFPATH_TERMS_URL")) or None
    rate_limit_secret = os.getenv("PROOFPATH_RATE_LIMIT_SECRET")
    public_url = os.getenv("PROOFPATH_PUBLIC_URL", "http://127.0.0.1:8000").rstrip("/")
    delivery = mailer or create_mailer(
        environment,
        mailbox_path=dev_mailbox or Path(os.getenv("PROOFPATH_DEV_MAILBOX", PROJECT_ROOT / "backend" / "data" / "dev-mailbox.jsonl")),
    )

    if environment == "production":
        if not active_terms_version or not active_terms_url:
            raise RuntimeError("Approved Terms of Service version and URL must be configured in production.")
        if not public_url.startswith("https://"):
            raise RuntimeError("PROOFPATH_PUBLIC_URL must use HTTPS in production.")
        if not active_terms_url.startswith("https://"):
            raise RuntimeError("PROOFPATH_TERMS_URL must use HTTPS in production.")
        if not rate_limit_secret or len(rate_limit_secret.encode("utf-8")) < 32:
            raise RuntimeError("PROOFPATH_RATE_LIMIT_SECRET must contain at least 32 bytes in production.")

    async def lifespan(_: FastAPI):
        if not dsn:
            raise RuntimeError("DATABASE_URL must be configured.")
        migrate_database(dsn)
        yield

    app = FastAPI(title="Proofpath API", version="0.3.0", lifespan=lifespan)
    app.state.rate_limiter = PostgresRateLimiter(dsn, rate_limit_secret or f"proofpath-{environment}-rate-limits-local")
    app.state.assessment_runner = assessment_runner or (None if environment == "test" else PodmanAssessmentRunner())

    def openapi_with_problem_details():
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        problem_schema = ProblemDetails.model_json_schema(ref_template="#/components/schemas/{model}")
        schema.setdefault("components", {}).setdefault("schemas", {}).update(problem_schema.pop("$defs", {}))
        schema["components"]["schemas"]["ProblemDetails"] = problem_schema
        schema["paths"]["/api/v1/competencies"]["get"]["security"] = []
        schema["paths"]["/api/v1/auth/logout"]["post"]["security"] = [
            {"ProofpathSession": [], "ProofpathCSRF": []}
        ]
        for path, method in (
            ("/api/v1/me/candidate/onboarding", "patch"),
            ("/api/v1/me/candidate/onboarding/complete", "post"),
            ("/api/v1/me/security/sessions/{session_id}", "delete"),
            ("/api/v1/me/security/sessions/revoke-others", "post"),
            ("/api/v1/me/data-exports", "post"),
            ("/api/v1/me/projects", "post"),
            ("/api/v1/me/projects/{project_id}", "patch"),
            ("/api/v1/me/projects/{project_id}", "delete"),
            ("/api/v1/me/projects/{project_id}/source-links", "post"),
            ("/api/v1/me/projects/{project_id}/source-links/{link_id}", "delete"),
            ("/api/v1/me/portfolio", "patch"),
            ("/api/v1/me/candidate-profile/handle", "post"),
            ("/api/v1/me/candidate-profile/discovery", "patch"),
            ("/api/v1/me/portfolio/versions", "post"),
            ("/api/v1/me/portfolio/publish", "post"),
            ("/api/v1/me/portfolio/unpublish", "post"),
            ("/api/v1/me/projects/{project_id}/publish", "post"),
            ("/api/v1/me/projects/{project_id}/unpublish", "post"),
            ("/api/v1/organizations/{org_id}/jobs", "post"),
            ("/api/v1/organizations/{org_id}/jobs/{job_id}", "patch"),
            ("/api/v1/organizations/{org_id}/jobs/{job_id}/publish", "post"),
            ("/api/v1/organizations/{org_id}/jobs/{job_id}/close", "post"),
            ("/api/v1/organizations/{org_id}/assessments", "post"),
            ("/api/v1/organizations/{org_id}/assessments/{assessment_id}", "patch"),
            ("/api/v1/organizations/{org_id}/assessments/{assessment_id}/publish", "post"),
            ("/api/v1/organizations/{org_id}/assessment-invitations", "post"),
            ("/api/v1/organizations/{org_id}/assessment-invitations/{invite_id}/cancel", "post"),
            ("/api/v1/me/assessment-invitations/{invite_id}/respond", "post"),
            ("/api/v1/assessment-invitations/{invite_id}/attempts", "post"),
            ("/api/v1/attempts/{attempt_id}/consents", "put"),
            ("/api/v1/attempts/{attempt_id}/workspace", "put"),
            ("/api/v1/attempts/{attempt_id}/test-runs", "post"),
            ("/api/v1/me/saved-jobs/{job_id}", "put"),
            ("/api/v1/me/saved-jobs/{job_id}", "delete"),
            ("/api/v1/jobs/{job_id}/applications", "post"),
            ("/api/v1/me/applications/{application_id}/withdraw", "post"),
            ("/api/v1/me/applications/{application_id}/evidence-sharing", "put"),
            ("/api/v1/me/notifications/{notification_id}", "patch"),
            ("/api/v1/me/notifications/read-all", "post"),
            ("/api/v1/me/interviews/{interview_id}/respond", "post"),
            ("/api/v1/organizations/{org_id}/interviews", "post"),
            ("/api/v1/organizations/{org_id}/interviews/{interview_id}", "patch"),
            ("/api/v1/organizations/{org_id}/interviews/{interview_id}/cancel", "post"),
            ("/api/v1/organizations/{org_id}/applications/{application_id}/stage", "patch"),
            ("/api/v1/organizations/{org_id}/applications/{application_id}/assign-reviewer", "post"),
            ("/api/v1/organizations/{org_id}/applications/{application_id}/decision", "post"),
            ("/api/v1/organizations", "post"),
            ("/api/v1/organizations/{org_id}/onboarding", "patch"),
            ("/api/v1/organizations/{org_id}/onboarding/complete", "post"),
            ("/api/v1/organizations/{org_id}/invitations", "post"),
            ("/api/v1/organizations/{org_id}/invitations/{invite_id}", "delete"),
            ("/api/v1/organization-invitations/accept", "post"),
            ("/api/v1/organizations/{org_id}/members/{member_id}/role", "patch"),
            ("/api/v1/organizations/{org_id}/members/{member_id}", "delete"),
        ):
            schema["paths"][path][method]["security"] = [
                {"ProofpathSession": [], "ProofpathCSRF": []}
            ]
        app.openapi_schema = schema
        return schema

    app.openapi = openapi_with_problem_details

    @app.middleware("http")
    async def add_request_id(request: Request, call_next):
        request.state.request_id = uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(HTTPException)
    @app.exception_handler(StarletteHTTPException)
    async def problem_from_http(request: Request, error: HTTPException):
        headers = error.headers or {}
        status = error.status_code
        return JSONResponse(
            status_code=status,
            media_type="application/problem+json",
            content={
                "type": f"urn:proofpath:problems:{headers.get('X-Problem-Code', 'HTTP_ERROR').lower()}",
                "title": headers.get("X-Problem-Title", "Request failed"),
                "status": status,
                "detail": error.detail if isinstance(error.detail, str) else "The request could not be completed.",
                "instance": request.url.path,
                "request_id": getattr(request.state, "request_id", None),
                "code": headers.get("X-Problem-Code", "HTTP_ERROR"),
            },
        )

    @app.exception_handler(RequestValidationError)
    async def problem_from_validation(request: Request, error: RequestValidationError):
        return JSONResponse(
            status_code=422,
            media_type="application/problem+json",
            content={
                "type": "urn:proofpath:problems:validation-error",
                "title": "Invalid request",
                "status": 422,
                "detail": "One or more request fields are invalid.",
                "instance": request.url.path,
                "request_id": getattr(request.state, "request_id", None),
                "code": "VALIDATION_ERROR",
                "field_errors": [
                    {
                        "field": ".".join(str(part) for part in item["loc"] if part not in ("body", "query", "path")),
                        "message": item["msg"],
                        "code": item["type"],
                    }
                    for item in error.errors()
                ],
            },
        )

    @app.exception_handler(Exception)
    async def problem_from_unexpected(request: Request, error: Exception):
        logger.error(
            "Unhandled request failure; request_id=%s; exception_type=%s",
            getattr(request.state, "request_id", None),
            type(error).__name__,
        )
        return JSONResponse(
            status_code=500,
            media_type="application/problem+json",
            content={
                "type": "urn:proofpath:problems:internal-error",
                "title": "Internal server error",
                "status": 500,
                "detail": "The request could not be completed.",
                "instance": request.url.path,
                "request_id": getattr(request.state, "request_id", None),
                "code": "INTERNAL_ERROR",
            },
        )

    def get_connection() -> Iterator[psycopg.Connection]:
        if not dsn:
            raise api_error(503, "DATABASE_UNAVAILABLE", "Service unavailable", "The account service is not configured.")
        with connect_database(dsn) as connection:
            yield connection

    def current_session(
        raw_token: str | None = Security(session_cookie_scheme),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> SessionContext:
        if not raw_token:
            raise api_error(401, "AUTHENTICATION_REQUIRED", "Authentication required", "Sign in to continue.")
        row = connection.execute(
            """SELECT s.id AS session_id, s.csrf_token_hash, u.id, u.email,
                      u.email_verified_at, u.initial_purpose
               FROM auth_sessions AS s JOIN users AS u ON u.id = s.user_id
               WHERE s.session_token_hash = %s AND s.revoked_at IS NULL AND s.expires_at > now()""",
            (hash_token(raw_token),),
        ).fetchone()
        if not row:
            raise api_error(401, "AUTHENTICATION_REQUIRED", "Authentication required", "Sign in to continue.")
        return SessionContext(
            session_id=row["session_id"],
            csrf_token_hash=bytes(row["csrf_token_hash"]),
            user=AuthUser(
                id=row["id"],
                email=row["email"],
                email_verified=row["email_verified_at"] is not None,
                initial_purpose=row["initial_purpose"],
            ),
        )

    def require_csrf(
        request: Request,
        session: SessionContext = Depends(current_session),
        header_token: str | None = Security(csrf_header_scheme),
    ) -> SessionContext:
        cookie_token = request.cookies.get(CSRF_COOKIE, "")
        if (
            not cookie_token
            or not header_token
            or not hmac.compare_digest(cookie_token, header_token)
            or not hmac.compare_digest(hash_token(cookie_token), session.csrf_token_hash)
        ):
            raise api_error(403, "CSRF_VALIDATION_FAILED", "Request rejected", "The request could not be verified.")
        return session

    @app.get("/", include_in_schema=False)
    def landing_page() -> FileResponse:
        return FileResponse(FRONTEND / "landing.html")

    @app.get("/signup", include_in_schema=False)
    def signup_page() -> FileResponse:
        return FileResponse(FRONTEND / "signup.html")

    @app.get("/auth.css", include_in_schema=False)
    def auth_styles() -> FileResponse:
        return FileResponse(FRONTEND / "auth.css", media_type="text/css")

    @app.get("/auth-ui.js", include_in_schema=False)
    def auth_ui() -> FileResponse:
        return FileResponse(FRONTEND / "auth-ui.js", media_type="text/javascript")

    @app.get("/login", include_in_schema=False)
    def login_page() -> FileResponse:
        return FileResponse(FRONTEND / "login.html")

    @app.get("/security/sessions", include_in_schema=False)
    def security_sessions_page() -> FileResponse:
        return FileResponse(
            FRONTEND / "security-sessions.html",
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    @app.get("/account/privacy", include_in_schema=False)
    def account_privacy_page() -> FileResponse:
        return FileResponse(
            FRONTEND / "account-privacy.html",
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    @app.get("/verify-email", include_in_schema=False)
    def verify_page() -> FileResponse:
        return FileResponse(
            FRONTEND / "verify-email.html",
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    @app.get("/candidate/onboarding", include_in_schema=False)
    def candidate_onboarding_page() -> FileResponse:
        return FileResponse(FRONTEND / "candidate-onboarding.html")

    @app.get("/candidate/dashboard", include_in_schema=False)
    def candidate_dashboard_page() -> FileResponse:
        return FileResponse(FRONTEND / "candidate-dashboard.html", headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    @app.get("/projects", include_in_schema=False)
    def projects_page() -> FileResponse:
        return FileResponse(FRONTEND / "projects.html")

    @app.get("/app/portfolio/builder", include_in_schema=False)
    def portfolio_builder_page() -> FileResponse:
        return FileResponse(
            FRONTEND / "portfolio-builder.html",
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    @app.get("/portfolio-renderer.js", include_in_schema=False)
    def portfolio_renderer_script() -> FileResponse:
        return FileResponse(FRONTEND / "portfolio-renderer.js", media_type="text/javascript")

    @app.get("/jobs", include_in_schema=False)
    def jobs_page() -> FileResponse:
        return FileResponse(FRONTEND / "jobs.html")

    @app.get("/jobs/{job_id}", include_in_schema=False)
    def job_detail_page(job_id: uuid.UUID) -> FileResponse:
        return FileResponse(FRONTEND / "job-detail.html", headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    @app.get("/applications", include_in_schema=False)
    def applications_page() -> FileResponse:
        return FileResponse(FRONTEND / "applications.html", headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    @app.get("/assessment-workspace", include_in_schema=False)
    def assessment_workspace_page() -> FileResponse:
        return FileResponse(FRONTEND / "assessment-workspace.html", headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    @app.get("/organization/applications", include_in_schema=False)
    def organization_applications_page() -> FileResponse:
        return FileResponse(FRONTEND / "organization-applications.html", headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    @app.get("/organization/jobs", include_in_schema=False)
    def organization_jobs_page() -> FileResponse:
        return FileResponse(FRONTEND / "organization-jobs.html", headers={"Cache-Control": "no-store"})

    @app.get("/organization/assessments", include_in_schema=False)
    def organization_assessments_page() -> FileResponse:
        return FileResponse(FRONTEND / "organization-assessments.html", headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    @app.get("/organization/candidates", include_in_schema=False)
    def organization_candidates_page() -> FileResponse:
        return FileResponse(FRONTEND / "organization-candidates.html", headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    @app.get("/u/{handle}", include_in_schema=False)
    @app.get("/portfolio/{handle}", include_in_schema=False)
    def public_portfolio_page(handle: str) -> FileResponse:
        return FileResponse(
            FRONTEND / "public-portfolio.html",
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex, nofollow"},
        )

    @app.get("/organization/onboarding", include_in_schema=False)
    def organization_onboarding_page() -> FileResponse:
        return FileResponse(FRONTEND / "organization-onboarding.html")

    @app.get("/organization/invitation", include_in_schema=False)
    def organization_invitation_page() -> FileResponse:
        return FileResponse(
            FRONTEND / "organization-invitation.html",
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    @app.get("/reset-password", include_in_schema=False)
    def reset_password_page() -> FileResponse:
        return FileResponse(
            FRONTEND / "reset-password.html",
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    @app.get(
        "/api/v1/meta/public",
        response_model=PublicMetadata,
        responses={500: problem_response("Unexpected service failure.")},
    )
    def public_metadata() -> PublicMetadata:
        return PublicMetadata(
            initial_purposes=["candidate", "employer"],
            enabled_auth_methods=["email_password"],
            registration_enabled=bool(active_terms_version and active_terms_url and delivery.available),
            terms_version=active_terms_version,
            terms_url=active_terms_url,
        )

    @app.get(
        "/api/v1/competencies",
        response_model=CompetencyCatalogResponse,
        responses={
            422: problem_response("The requested competency track is not supported."),
            500: problem_response("Unexpected service failure."),
        },
        description="Returns active, stable competency identifiers for the initial Python/FastAPI backend track.",
    )
    def list_competencies(
        track: Literal["python-fastapi-backend"] = "python-fastapi-backend",
        connection: psycopg.Connection = Depends(get_connection),
    ) -> CompetencyCatalogResponse:
        rows = connection.execute(
            """SELECT code, track, name, description FROM competencies
               WHERE track = %s AND active ORDER BY sort_order""",
            (track,),
        ).fetchall()
        return CompetencyCatalogResponse(track=track, competencies=rows)

    @app.post(
        "/api/v1/auth/register",
        response_model=RegistrationResponse,
        status_code=202,
        responses={
            409: problem_response("The terms version is stale."),
            422: problem_response("The request is invalid."),
            429: problem_response("Too many requests."),
            500: problem_response("Unexpected service failure."),
            503: problem_response("Registration is unavailable."),
        },
    )
    def register(
        body: RegistrationRequest,
        request: Request,
        connection: psycopg.Connection = Depends(get_connection),
    ) -> RegistrationResponse:
        email = str(body.email).casefold()
        client_ip = request.client.host if request.client else "unknown"
        if (
            not app.state.rate_limiter.allow(f"register:ip:{client_ip}", 20, 900)
            or not app.state.rate_limiter.allow(f"register:email:{email}", 5, 900)
        ):
            raise api_error(429, "RATE_LIMITED", "Too many requests", "Try again later.")
        if not active_terms_version or not active_terms_url or not delivery.available:
            raise api_error(503, "REGISTRATION_DISABLED", "Registration unavailable", "Account creation is not configured.")
        if body.terms_version != active_terms_version:
            raise api_error(409, "TERMS_VERSION_STALE", "Terms changed", "Review the current terms and submit again.")

        verification_link = None
        with connection.transaction():
            user = connection.execute(
                """INSERT INTO users (id, full_name, email, password_hash, initial_purpose, terms_version, terms_accepted_at)
                   VALUES (%s, %s, %s, %s, %s, %s, now())
                   ON CONFLICT (email) DO NOTHING
                   RETURNING id, full_name, email_verified_at""",
                (uuid.uuid4(), body.full_name, email, hash_password(body.password), body.initial_purpose, active_terms_version),
            ).fetchone()
            is_new_user = user is not None
            if user is None:
                user = connection.execute(
                    "SELECT id, full_name, email_verified_at FROM users WHERE email = %s",
                    (email,),
                ).fetchone()
            if user is None:
                raise api_error(503, "REGISTRATION_UNAVAILABLE", "Registration unavailable", "Account creation is not configured.")
            if user["email_verified_at"] is None:
                raw_token = secrets.token_urlsafe(32)
                connection.execute(
                    """INSERT INTO email_verification_tokens (id, user_id, token_hash, expires_at)
                       VALUES (%s, %s, %s, now() + INTERVAL '24 hours')""",
                    (uuid.uuid4(), user["id"], hash_token(raw_token)),
                )
                verification_link = f"{public_url}/verify-email#token={raw_token}"
            connection.execute(
                """INSERT INTO audit_events
                   (id, actor_type, resource_type, resource_id, action, correlation_id, outcome)
                   VALUES (%s, 'public', 'user', %s, %s, %s, 'success')""",
                (
                    uuid.uuid4(),
                    user["id"],
                    "auth.user_registered" if is_new_user else "auth.registration_requested",
                    request.state.request_id,
                ),
            )
        connection.commit()
        try:
            delivery.send_registration(email, user["full_name"], verification_link)
        except Exception:
            logger.warning("Registration email delivery failed; request_id=%s", request.state.request_id)
            connection.execute(
                """INSERT INTO audit_events
                   (id, actor_type, resource_type, resource_id, action, correlation_id, outcome)
                   VALUES (%s, 'system', 'user', %s, 'auth.verification_delivery_failed', %s, 'failure')""",
                (uuid.uuid4(), user["id"], request.state.request_id),
            )
            connection.commit()
            raise api_error(503, "EMAIL_DELIVERY_UNAVAILABLE", "Registration unavailable", "Try again later.")
        return RegistrationResponse()

    @app.post(
        "/api/v1/auth/email-verifications/confirm",
        response_model=EmailVerificationResponse,
        responses={
            400: problem_response("The verification token is invalid, expired, or already used."),
            422: problem_response("The request is invalid."),
            500: problem_response("Unexpected service failure."),
        },
    )
    def confirm_email(
        body: VerificationRequest,
        request: Request,
        connection: psycopg.Connection = Depends(get_connection),
    ) -> EmailVerificationResponse:
        with connection.transaction():
            token = connection.execute(
                """UPDATE email_verification_tokens SET consumed_at = now()
                   WHERE token_hash = %s AND consumed_at IS NULL AND expires_at > now()
                   RETURNING user_id""",
                (hash_token(body.token),),
            ).fetchone()
            if token is None:
                raise api_error(400, "VERIFICATION_TOKEN_INVALID", "Verification link invalid", "Request a new verification link.")
            connection.execute(
                "UPDATE users SET email_verified_at = COALESCE(email_verified_at, now()) WHERE id = %s",
                (token["user_id"],),
            )
            connection.execute(
                "UPDATE email_verification_tokens SET consumed_at = now() WHERE user_id = %s AND consumed_at IS NULL",
                (token["user_id"],),
            )
            connection.execute(
                """INSERT INTO audit_events
                   (id, actor_id, actor_type, resource_type, resource_id, action, correlation_id, outcome)
                   VALUES (%s, %s, 'user', 'user', %s, 'auth.email_verified', %s, 'success')""",
                (uuid.uuid4(), token["user_id"], token["user_id"], request.state.request_id),
            )
        return EmailVerificationResponse(email_verified=True)

    @app.post(
        "/api/v1/auth/login",
        response_model=LoginResponse,
        responses={
            401: problem_response("The credentials are invalid or the email is unverified."),
            422: problem_response("The request is invalid."),
            429: problem_response("Too many requests."),
            500: problem_response("Unexpected service failure."),
        },
    )
    def login(
        body: LoginRequest,
        request: Request,
        response: Response,
        connection: psycopg.Connection = Depends(get_connection),
    ) -> LoginResponse:
        email = str(body.email).casefold()
        client_ip = request.client.host if request.client else "unknown"
        if (
            not app.state.rate_limiter.allow(f"login:ip:{client_ip}", 30, 900)
            or not app.state.rate_limiter.allow(f"login:email:{email}", 10, 900)
        ):
            raise api_error(429, "RATE_LIMITED", "Too many requests", "Try again later.")
        user = connection.execute(
            """SELECT id, email, password_hash, email_verified_at, initial_purpose
               FROM users WHERE email = %s""",
            (email,),
        ).fetchone()
        password_ok = verify_password(user["password_hash"] if user else DUMMY_PASSWORD_HASH, body.password)
        if not user or not password_ok or user["email_verified_at"] is None:
            raise api_error(401, "INVALID_CREDENTIALS", "Sign-in failed", "Invalid email or password.")

        session_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        session_id = uuid.uuid4()
        connection.execute(
            """INSERT INTO auth_sessions (id, user_id, session_token_hash, csrf_token_hash, expires_at)
               VALUES (%s, %s, %s, %s, now() + INTERVAL '12 hours')""",
            (session_id, user["id"], hash_token(session_token), hash_token(csrf_token)),
        )
        connection.execute(
            """INSERT INTO audit_events
               (id, actor_id, actor_type, resource_type, resource_id, action, correlation_id, outcome)
               VALUES (%s, %s, 'user', 'auth_session', %s, 'auth.login', %s, 'success')""",
            (uuid.uuid4(), user["id"], session_id, request.state.request_id),
        )
        connection.commit()
        response.set_cookie(
            SESSION_COOKIE,
            session_token,
            max_age=SESSION_SECONDS,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        response.set_cookie(
            CSRF_COOKIE,
            csrf_token,
            max_age=SESSION_SECONDS,
            path="/",
            secure=True,
            httponly=False,
            samesite="lax",
        )
        purpose = user["initial_purpose"]
        return LoginResponse(
            user=AuthUser(
                id=user["id"],
                email=user["email"],
                email_verified=True,
                initial_purpose=purpose,
            ),
            next_action="candidate_onboarding" if purpose == "candidate" else "employer_onboarding",
        )

    @app.post(
        "/api/v1/auth/password-resets",
        response_model=PasswordResetRequestedResponse,
        status_code=202,
        responses={
            422: problem_response("The request is invalid."),
            429: problem_response("Too many requests."),
            500: problem_response("Unexpected service failure."),
        },
    )
    def request_password_reset(
        body: PasswordResetRequest,
        request: Request,
        connection: psycopg.Connection = Depends(get_connection),
    ) -> PasswordResetRequestedResponse:
        email = str(body.email).casefold()
        client_ip = request.client.host if request.client else "unknown"
        if (
            not app.state.rate_limiter.allow(f"password-reset:ip:{client_ip}", 20, 900)
            or not app.state.rate_limiter.allow(f"password-reset:email:{email}", 5, 900)
        ):
            raise api_error(429, "RATE_LIMITED", "Too many requests", "Try again later.")

        user = connection.execute(
            "SELECT id, full_name FROM users WHERE email = %s AND email_verified_at IS NOT NULL",
            (email,),
        ).fetchone()
        if user:
            raw_token = secrets.token_urlsafe(32)
            with connection.transaction():
                connection.execute(
                    "UPDATE password_reset_tokens SET consumed_at = now() WHERE user_id = %s AND consumed_at IS NULL",
                    (user["id"],),
                )
                connection.execute(
                    """INSERT INTO password_reset_tokens (id, user_id, token_hash, expires_at)
                       VALUES (%s, %s, %s, now() + INTERVAL '1 hour')""",
                    (uuid.uuid4(), user["id"], hash_token(raw_token)),
                )
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_type, resource_type, resource_id, action, correlation_id, outcome)
                       VALUES (%s, 'public', 'user', %s, 'auth.password_reset_requested', %s, 'success')""",
                    (uuid.uuid4(), user["id"], request.state.request_id),
                )
            connection.commit()
            reset_link = f"{public_url}/reset-password#token={raw_token}"
            try:
                delivery.send_password_reset(email, user["full_name"], reset_link)
            except Exception:
                logger.warning("Password reset delivery failed; request_id=%s", request.state.request_id)
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_type, resource_type, resource_id, action, correlation_id, outcome)
                       VALUES (%s, 'system', 'user', %s, 'auth.password_reset_delivery_failed', %s, 'failure')""",
                    (uuid.uuid4(), user["id"], request.state.request_id),
                )
                connection.commit()
        return PasswordResetRequestedResponse()

    @app.post(
        "/api/v1/auth/password-resets/confirm",
        response_model=PasswordResetConfirmationResponse,
        responses={
            400: problem_response("The password reset token is invalid, expired, or already used."),
            422: problem_response("The request is invalid."),
            429: problem_response("Too many requests."),
            500: problem_response("Unexpected service failure."),
        },
    )
    def confirm_password_reset(
        body: PasswordResetConfirmation,
        request: Request,
        connection: psycopg.Connection = Depends(get_connection),
    ) -> PasswordResetConfirmationResponse:
        client_ip = request.client.host if request.client else "unknown"
        if not app.state.rate_limiter.allow(f"password-reset-confirm:ip:{client_ip}", 10, 900):
            raise api_error(429, "RATE_LIMITED", "Too many requests", "Try again later.")
        password_hash = hash_password(body.new_password)
        with connection.transaction():
            token = connection.execute(
                """UPDATE password_reset_tokens SET consumed_at = now()
                   WHERE token_hash = %s AND consumed_at IS NULL AND expires_at > now()
                   RETURNING user_id""",
                (hash_token(body.token),),
            ).fetchone()
            if token is None:
                raise api_error(400, "PASSWORD_RESET_TOKEN_INVALID", "Reset link invalid", "Request a new password reset link.")
            connection.execute("UPDATE users SET password_hash = %s WHERE id = %s", (password_hash, token["user_id"]))
            connection.execute(
                "UPDATE auth_sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL",
                (token["user_id"],),
            )
            connection.execute(
                """UPDATE password_reset_tokens SET consumed_at = now()
                   WHERE user_id = %s AND consumed_at IS NULL""",
                (token["user_id"],),
            )
            connection.execute(
                """INSERT INTO audit_events
                   (id, actor_id, actor_type, resource_type, resource_id, action, correlation_id, outcome)
                   VALUES (%s, %s, 'user', 'user', %s, 'auth.password_reset_completed', %s, 'success')""",
                (uuid.uuid4(), token["user_id"], token["user_id"], request.state.request_id),
            )
        return PasswordResetConfirmationResponse(password_reset=True)

    @app.get(
        "/api/v1/auth/session",
        response_model=SessionResponse,
        responses={401: problem_response("Authentication is required."), 500: problem_response("Unexpected service failure.")},
    )
    def get_session(session: SessionContext = Depends(current_session)) -> SessionResponse:
        purpose = session.user.initial_purpose
        return SessionResponse(
            user=session.user,
            next_action="candidate_onboarding" if purpose == "candidate" else "employer_onboarding",
        )

    @app.get(
        "/api/v1/me/security/sessions",
        response_model=AccountSessionListResponse,
        responses={401: problem_response("Authentication is required."), 500: problem_response("Unexpected service failure.")},
    )
    def list_account_sessions(
        response: Response,
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> AccountSessionListResponse:
        rows = connection.execute(
            """SELECT id AS session_id, created_at, expires_at, id = %s AS is_current
               FROM auth_sessions
               WHERE user_id = %s AND revoked_at IS NULL AND expires_at > now()
               ORDER BY created_at DESC, id""",
            (session.session_id, session.user.id),
        ).fetchall()
        response.headers["Cache-Control"] = "no-store"
        return AccountSessionListResponse(sessions=[AccountSessionItem(**row) for row in rows])

    @app.delete(
        "/api/v1/me/security/sessions/{session_id}",
        status_code=204,
        responses={
            401: problem_response("Authentication is required."),
            403: problem_response("The CSRF token is missing or invalid."),
            404: problem_response("The session was not found on this account."),
            500: problem_response("Unexpected service failure."),
        },
    )
    def revoke_account_session(
        session_id: uuid.UUID,
        response: Response,
        request: Request,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> Response:
        with connection.transaction():
            target = connection.execute(
                "SELECT id, revoked_at FROM auth_sessions WHERE id = %s AND user_id = %s FOR UPDATE",
                (session_id, session.user.id),
            ).fetchone()
            if target is None:
                raise api_error(404, "AUTH_SESSION_NOT_FOUND", "Session not found", "The session was not found on this account.")
            if target["revoked_at"] is None:
                connection.execute("UPDATE auth_sessions SET revoked_at = now() WHERE id = %s", (session_id,))
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_id, actor_type, resource_type, resource_id, action, correlation_id, outcome)
                       VALUES (%s, %s, 'user', 'auth_session', %s, 'auth.session_revoked', %s, 'success')""",
                    (uuid.uuid4(), session.user.id, session_id, request.state.request_id),
                )
        response.headers["Cache-Control"] = "no-store"
        if session_id == session.session_id:
            response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
            response.delete_cookie(CSRF_COOKIE, path="/", secure=True, httponly=False, samesite="lax")
        response.status_code = 204
        return response

    @app.post(
        "/api/v1/me/security/sessions/revoke-others",
        response_model=RevokeOtherSessionsResponse,
        responses={
            401: problem_response("Authentication is required."),
            403: problem_response("The CSRF token is missing or the current password is invalid."),
            422: problem_response("The request is invalid."),
            429: problem_response("Too many reauthentication attempts."),
            500: problem_response("Unexpected service failure."),
        },
    )
    def revoke_other_account_sessions(
        body: RevokeOtherSessionsRequest,
        request: Request,
        response: Response,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> RevokeOtherSessionsResponse:
        if not app.state.rate_limiter.allow(f"session-revoke-others:user:{session.user.id}", 5, 900):
            raise api_error(429, "RATE_LIMITED", "Too many requests", "Try again later.")
        revoked_count = 0
        password_valid = False
        with connection.transaction():
            account = connection.execute(
                "SELECT password_hash FROM users WHERE id = %s", (session.user.id,)
            ).fetchone()
            password_valid = bool(account and verify_password(account["password_hash"], body.current_password))
            if password_valid:
                revoked = connection.execute(
                    """UPDATE auth_sessions SET revoked_at = now()
                       WHERE user_id = %s AND id <> %s AND revoked_at IS NULL AND expires_at > now()
                       RETURNING id""",
                    (session.user.id, session.session_id),
                ).fetchall()
                revoked_count = len(revoked)
                action = "auth.other_sessions_revoked"
                outcome = "success"
            else:
                action = "auth.session_revoke_reauthentication_failed"
                outcome = "denied"
            connection.execute(
                """INSERT INTO audit_events
                   (id, actor_id, actor_type, resource_type, resource_id, action, correlation_id, outcome, metadata)
                   VALUES (%s, %s, 'user', 'auth_session', %s, %s, %s, %s, %s)""",
                (uuid.uuid4(), session.user.id, session.session_id, action, request.state.request_id, outcome,
                 Jsonb({"revoked_session_count": revoked_count})),
            )
        if not password_valid:
            connection.commit()
            raise api_error(403, "REAUTHENTICATION_FAILED", "Reauthentication failed", "The current password is invalid.")
        response.headers["Cache-Control"] = "no-store"
        return RevokeOtherSessionsResponse(revoked_session_count=revoked_count)

    @app.post(
        "/api/v1/me/data-exports",
        response_model=AccountDataExportResponse,
        responses={
            401: problem_response("Authentication is required."),
            403: problem_response("The CSRF token, email verification, or current password is invalid."),
            422: problem_response("The request is invalid."),
            429: problem_response("Too many export requests."),
            500: problem_response("Unexpected service failure."),
        },
    )
    def export_account_data(
        body: AccountDataExportRequest,
        request: Request,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> Response:
        if not app.state.rate_limiter.allow(f"account-data-export:user:{session.user.id}", 3, 3600):
            raise api_error(429, "RATE_LIMITED", "Too many requests", "Try again in an hour.")
        account = connection.execute(
            "SELECT password_hash, email_verified_at FROM users WHERE id = %s",
            (session.user.id,),
        ).fetchone()
        password_valid = bool(account and verify_password(account["password_hash"], body.current_password))
        verified = bool(account and account["email_verified_at"])
        if not password_valid or not verified:
            action = "account.data_export_reauthentication_failed" if not password_valid else "account.data_export_unverified_denied"
            connection.execute(
                """INSERT INTO audit_events
                   (id, actor_id, actor_type, resource_type, resource_id, action, correlation_id, outcome)
                   VALUES (%s, %s, 'user', 'user', %s, %s, %s, 'denied')""",
                (uuid.uuid4(), session.user.id, session.user.id, action, request.state.request_id),
            )
            connection.commit()
            if not verified:
                raise api_error(403, "EMAIL_VERIFICATION_REQUIRED", "Verified email required", "Verify your email before exporting account data.")
            raise api_error(403, "REAUTHENTICATION_FAILED", "Reauthentication failed", "The current password is invalid.")
        connection.commit()
        generated_at = datetime.now(timezone.utc)
        with connection.transaction():
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            exported = build_account_data_export(connection, session.user.id, generated_at)
        with connection.transaction():
            connection.execute(
                """INSERT INTO audit_events
                   (id, actor_id, actor_type, resource_type, resource_id, action, correlation_id, outcome)
                   VALUES (%s, %s, 'user', 'user', %s, 'account.data_exported', %s, 'success')""",
                (uuid.uuid4(), session.user.id, session.user.id, request.state.request_id),
            )
        return JSONResponse(
            content=exported.model_dump(mode="json"),
            headers={
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": f'attachment; filename="proofpath-account-data-{generated_at:%Y-%m-%d}.json"',
            },
        )

    @app.post(
        "/api/v1/auth/logout",
        status_code=204,
        responses={
            401: problem_response("Authentication is required."),
            403: problem_response("The CSRF token is missing or invalid."),
            500: problem_response("Unexpected service failure."),
        },
    )
    def logout(
        response: Response,
        request: Request,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> Response:
        connection.execute("UPDATE auth_sessions SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL", (session.session_id,))
        connection.execute(
            """INSERT INTO audit_events
               (id, actor_id, actor_type, resource_type, resource_id, action, correlation_id, outcome)
               VALUES (%s, %s, 'user', 'auth_session', %s, 'auth.logout', %s, 'success')""",
            (uuid.uuid4(), session.user.id, session.session_id, request.state.request_id),
        )
        connection.commit()
        response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
        response.delete_cookie(CSRF_COOKIE, path="/", secure=True, httponly=False, samesite="lax")
        response.status_code = 204
        return response

    @app.get(
        "/api/v1/me/candidate/onboarding",
        response_model=CandidateOnboardingResponse,
        responses={
            401: problem_response("Authentication is required."),
            500: problem_response("Unexpected service failure."),
        },
    )
    def get_candidate_onboarding(
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> CandidateOnboardingResponse:
        # Candidate workspaces are additive; initial_purpose is not a global role.
        return load_candidate_onboarding(connection, session.user.id)

    @app.get(
        "/api/v1/me/dashboard",
        response_model=CandidateDashboardResponse,
        summary="Get the candidate workspace summary",
        description="Returns persisted, candidate-owned counts and public portfolio state; does not infer schedules, scores, or evidence.",
        responses={
            401: problem_response("Authentication is required."),
            403: problem_response("A candidate workspace is required."),
            500: problem_response("Unexpected service failure."),
        },
    )
    def get_candidate_dashboard(
        response: Response,
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> CandidateDashboardResponse:
        if session.user.initial_purpose != "candidate" and not connection.execute(
            "SELECT 1 FROM candidate_onboardings WHERE user_id = %s", (session.user.id,)
        ).fetchone():
            raise api_error(403, "CANDIDATE_WORKSPACE_REQUIRED", "Candidate workspace required", "Open your candidate workspace to view its dashboard.")
        row = connection.execute(
            """SELECT account.full_name AS candidate_name,
                      COALESCE(onboarding.completed_at IS NOT NULL, false) AS candidate_setup_complete,
                      COALESCE(portfolio.published_at IS NOT NULL, false) AS portfolio_public,
                      CASE WHEN portfolio.published_at IS NOT NULL THEN portfolio.public_handle END AS public_handle,
                      (SELECT count(*) FROM candidate_projects AS project
                       WHERE project.user_id = account.id AND project.published_at IS NOT NULL
                         AND portfolio.published_at IS NOT NULL) AS public_project_count,
                      (SELECT count(*) FROM job_applications AS application
                       WHERE application.user_id = account.id AND application.status <> 'withdrawn') AS application_count,
                      (SELECT count(*) FROM saved_jobs AS saved WHERE saved.user_id = account.id) AS saved_job_count,
                      (SELECT count(*) FROM assessment_invitations AS invitation
                       WHERE invitation.candidate_user_id = account.id AND invitation.status = 'issued'
                         AND invitation.expires_at > now()) AS pending_assessment_invitation_count,
                      (SELECT count(*) FROM assessment_attempts AS attempt WHERE attempt.candidate_user_id = account.id
                       AND attempt.status IN ('created', 'consented', 'ready', 'active', 'paused')) AS active_assessment_attempt_count
               FROM users AS account
               LEFT JOIN candidate_onboardings AS onboarding ON onboarding.user_id = account.id
               LEFT JOIN candidate_portfolios AS portfolio ON portfolio.user_id = account.id
               WHERE account.id = %s""",
            (session.user.id,),
        ).fetchone()
        response.headers["Cache-Control"] = "no-store"
        return CandidateDashboardResponse(**row)

    @app.patch(
        "/api/v1/me/candidate/onboarding",
        response_model=CandidateOnboardingResponse,
        responses={
            401: problem_response("Authentication is required."),
            403: problem_response("The CSRF token is missing or invalid."),
            409: problem_response("The onboarding draft changed or is already complete."),
            422: problem_response("The request is invalid."),
            500: problem_response("Unexpected service failure."),
        },
    )
    def save_candidate_onboarding(
        body: CandidateOnboardingPatch,
        request: Request,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> CandidateOnboardingResponse:
        fields = body.model_fields_set
        roles = body.target_roles or []
        skills = body.skills or []
        with connection.transaction():
            connection.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (session.user.id,))
            current = connection.execute(
                "SELECT target_roles, skills, version, completed_at FROM candidate_onboardings WHERE user_id = %s",
                (session.user.id,),
            ).fetchone()
            if current and current["completed_at"]:
                raise api_error(409, "ONBOARDING_ALREADY_COMPLETE", "Onboarding complete", "This onboarding draft is already complete.")
            if (current["version"] if current else 0) != body.expected_version:
                raise api_error(409, "ONBOARDING_VERSION_CONFLICT", "Draft changed", "Reload the draft and try again.")
            target_roles = roles if "target_roles" in fields else (current["target_roles"] if current else [])
            saved_skills = skills if "skills" in fields else (current["skills"] if current else [])
            if current:
                connection.execute(
                    """UPDATE candidate_onboardings SET target_roles = %s, skills = %s,
                              version = version + 1, updated_at = now()
                       WHERE user_id = %s""",
                    (target_roles, saved_skills, session.user.id),
                )
            else:
                connection.execute(
                    """INSERT INTO candidate_onboardings (user_id, target_roles, skills, version)
                       VALUES (%s, %s, %s, 1)""",
                    (session.user.id, target_roles, saved_skills),
                )
            if "full_name" in fields:
                connection.execute(
                    "UPDATE users SET full_name = %s WHERE id = %s",
                    (body.full_name, session.user.id),
                )
            connection.execute(
                """INSERT INTO audit_events
                   (id, actor_id, actor_type, resource_type, resource_id, action, correlation_id, outcome)
                   VALUES (%s, %s, 'user', 'candidate_onboarding', %s, 'candidate.onboarding_saved', %s, 'success')""",
                (uuid.uuid4(), session.user.id, session.user.id, request.state.request_id),
            )
        return load_candidate_onboarding(connection, session.user.id)

    @app.post(
        "/api/v1/me/candidate/onboarding/complete",
        response_model=CandidateOnboardingResponse,
        responses={
            401: problem_response("Authentication is required."),
            403: problem_response("The CSRF token is missing or invalid."),
            409: problem_response("The onboarding draft changed."),
            422: problem_response("Required onboarding fields are missing."),
            500: problem_response("Unexpected service failure."),
        },
    )
    def complete_candidate_onboarding(
        body: CandidateOnboardingCompletion,
        request: Request,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> CandidateOnboardingResponse:
        with connection.transaction():
            current = connection.execute(
                """SELECT version, completed_at, target_roles
                   FROM candidate_onboardings WHERE user_id = %s FOR UPDATE""",
                (session.user.id,),
            ).fetchone()
            if current is None:
                raise api_error(422, "ONBOARDING_INCOMPLETE", "Onboarding incomplete", "Add your name and at least one target role before completing.")
            if current["completed_at"] is None:
                if current["version"] != body.expected_version:
                    raise api_error(409, "ONBOARDING_VERSION_CONFLICT", "Draft changed", "Reload the draft and try again.")
                if not current["target_roles"]:
                    raise api_error(422, "ONBOARDING_INCOMPLETE", "Onboarding incomplete", "Add at least one target role before completing.")
                completed = connection.execute(
                    """UPDATE candidate_onboardings SET completed_at = now(), version = version + 1,
                              updated_at = now()
                       WHERE user_id = %s AND version = %s AND completed_at IS NULL
                       RETURNING version""",
                    (session.user.id, body.expected_version),
                ).fetchone()
                if completed is None:
                    raise api_error(409, "ONBOARDING_VERSION_CONFLICT", "Draft changed", "Reload the draft and try again.")
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_id, actor_type, resource_type, resource_id, action, correlation_id, outcome)
                       VALUES (%s, %s, 'user', 'candidate_onboarding', %s, 'candidate.onboarding_completed', %s, 'success')""",
                    (uuid.uuid4(), session.user.id, session.user.id, request.state.request_id),
                )
        return load_candidate_onboarding(connection, session.user.id)

    @app.get(
        "/api/v1/me/projects",
        response_model=ProjectListResponse,
        responses={401: problem_response("Authentication is required."), 500: problem_response("Unexpected service failure.")},
    )
    def list_projects(
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> ProjectListResponse:
        rows = connection.execute(
            f"""SELECT p.id, p.name, p.problem_statement, p.contribution, p.technologies,
                      p.version, p.created_at, p.updated_at, p.published_at, {PROJECT_SOURCE_LINKS_SELECT}, {PROJECT_COMPETENCY_EVIDENCE_SELECT}
               FROM candidate_projects AS p WHERE p.user_id = %s ORDER BY p.sort_order, p.id""",
            (session.user.id,),
        ).fetchall()
        return ProjectListResponse(projects=[project_response(row) for row in rows])

    @app.post(
        "/api/v1/me/projects",
        response_model=ProjectResponse,
        status_code=201,
        responses={
            401: problem_response("Authentication is required."),
            403: problem_response("The CSRF token is missing or invalid."),
            409: problem_response("The idempotency key was reused with different input."),
            422: problem_response("The request is invalid."),
            429: problem_response("Too many requests."),
            500: problem_response("Unexpected service failure."),
        },
    )
    def create_project(
        body: ProjectCreateRequest,
        request: Request,
        response: Response,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> ProjectResponse:
        client_ip = request.client.host if request.client else "unknown"
        if (
            not app.state.rate_limiter.allow(f"project-create:ip:{client_ip}", 30, 900)
            or not app.state.rate_limiter.allow(f"project-create:user:{session.user.id}", 20, 900)
        ):
            raise api_error(429, "RATE_LIMITED", "Too many requests", "Try again later.")
        payload_hash = hash_token(
            "\0".join(("project:create:v1", body.name, body.problem_statement, body.contribution, "\x01".join(body.technologies)))
            + (
                "\0project-claims:v1\0" + "\x01".join(
                    f"{claim.competency_code}\0{claim.statement}" for claim in body.competency_claims
                )
                if body.competency_claims else ""
            )
        )
        with connection.transaction():
            connection.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (session.user.id,))
            validate_project_competency_codes(connection, body.competency_claims)
            next_order = connection.execute(
                "SELECT COALESCE(MAX(sort_order), 0) + 1 AS value FROM candidate_projects WHERE user_id = %s",
                (session.user.id,),
            ).fetchone()["value"]
            project = connection.execute(
                """INSERT INTO candidate_projects
                   (id, user_id, request_key, creation_payload_hash, name, problem_statement, contribution, technologies, sort_order)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (user_id, request_key) DO NOTHING
                   RETURNING id, name, problem_statement, contribution, technologies, version, created_at, updated_at""",
                (uuid.uuid4(), session.user.id, body.request_key, payload_hash, body.name,
                 body.problem_statement, body.contribution, body.technologies, next_order),
            ).fetchone()
            if project is None:
                project = connection.execute(
                    """SELECT id, name, problem_statement, contribution, technologies, version, created_at, updated_at,
                              creation_payload_hash
                       FROM candidate_projects WHERE user_id = %s AND request_key = %s""",
                    (session.user.id, body.request_key),
                ).fetchone()
                if project is None:
                    raise api_error(409, "PROJECT_CREATE_CONFLICT", "Request conflict", "Retry with a new request key.")
                if bytes(project["creation_payload_hash"]) != payload_hash:
                    raise api_error(409, "IDEMPOTENCY_KEY_REUSED", "Request conflict", "Use a new key for different project details.")
            else:
                sync_project_competency_claims(connection, session.user.id, project["id"], body.competency_claims)
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_id, actor_type, resource_type, resource_id, action, correlation_id, outcome)
                       VALUES (%s, %s, 'user', 'candidate_project', %s, 'candidate.project_created', %s, 'success')""",
                    (uuid.uuid4(), session.user.id, project["id"], request.state.request_id),
                )
                if body.competency_claims:
                    connection.execute(
                        """INSERT INTO audit_events
                           (id, actor_id, actor_type, resource_type, resource_id, action, correlation_id, outcome, metadata)
                           VALUES (%s, %s, 'user', 'candidate_competency_evidence', %s,
                                   'candidate.project_competency_claims_recorded', %s, 'success', %s)""",
                        (uuid.uuid4(), session.user.id, project["id"], request.state.request_id,
                         Jsonb({"claim_count": len(body.competency_claims)})),
                    )
                connection.execute(
                    """INSERT INTO candidate_portfolios (user_id, version)
                       VALUES (%s, 1) ON CONFLICT (user_id) DO UPDATE
                       SET version = candidate_portfolios.version + 1, updated_at = now()""",
                    (session.user.id,),
                )
        project = connection.execute(
            f"""SELECT p.id, p.name, p.problem_statement, p.contribution, p.technologies,
                      p.version, p.created_at, p.updated_at, p.published_at, {PROJECT_SOURCE_LINKS_SELECT}, {PROJECT_COMPETENCY_EVIDENCE_SELECT}
               FROM candidate_projects AS p WHERE p.id = %s AND p.user_id = %s""",
            (project["id"], session.user.id),
        ).fetchone()
        response.headers["Location"] = f"/api/v1/me/projects/{project['id']}"
        return project_response(project)

    @app.get(
        "/api/v1/me/projects/{project_id}",
        response_model=ProjectResponse,
        responses={
            401: problem_response("Authentication is required."),
            404: problem_response("The project was not found."),
            500: problem_response("Unexpected service failure."),
        },
    )
    def get_project(
        project_id: uuid.UUID,
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> ProjectResponse:
        row = connection.execute(
            f"""SELECT p.id, p.name, p.problem_statement, p.contribution, p.technologies,
                      p.version, p.created_at, p.updated_at, p.published_at, {PROJECT_SOURCE_LINKS_SELECT}, {PROJECT_COMPETENCY_EVIDENCE_SELECT}
               FROM candidate_projects AS p WHERE p.id = %s AND p.user_id = %s""",
            (project_id, session.user.id),
        ).fetchone()
        if row is None:
            raise api_error(404, "PROJECT_NOT_FOUND", "Project not found", "The project was not found.")
        return project_response(row)

    @app.patch(
        "/api/v1/me/projects/{project_id}",
        response_model=ProjectResponse,
        responses={
            401: problem_response("Authentication is required."),
            403: problem_response("The CSRF token is missing or invalid."),
            404: problem_response("The project was not found."),
            409: problem_response("The project version changed."),
            422: problem_response("The request is invalid."),
            500: problem_response("Unexpected service failure."),
        },
    )
    def update_project(
        project_id: uuid.UUID,
        body: ProjectPatchRequest,
        request: Request,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> ProjectResponse:
        with connection.transaction():
            connection.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (session.user.id,))
            current = connection.execute(
                """SELECT id, name, problem_statement, contribution, technologies, version, created_at, updated_at, published_at
                   FROM candidate_projects WHERE id = %s AND user_id = %s FOR UPDATE""",
                (project_id, session.user.id),
            ).fetchone()
            if current is None:
                raise api_error(404, "PROJECT_NOT_FOUND", "Project not found", "The project was not found.")
            if current["version"] != body.expected_version:
                raise api_error(409, "PROJECT_VERSION_CONFLICT", "Project changed", "Reload the project and try again.")
            if current["published_at"] is not None:
                raise api_error(409, "PROJECT_MUST_BE_UNPUBLISHED", "Project is public", "Unpublish this project before changing its content.")
            fields = body.model_fields_set - {"expected_version"}
            if "competency_claims" in fields:
                validate_project_competency_codes(connection, body.competency_claims)
                sync_project_competency_claims(connection, session.user.id, project_id, body.competency_claims)
            project = connection.execute(
                """UPDATE candidate_projects SET name = %s, problem_statement = %s, contribution = %s,
                          technologies = %s, version = version + 1, updated_at = now()
                   WHERE id = %s AND user_id = %s AND version = %s
                   RETURNING id, name, problem_statement, contribution, technologies, version, created_at, updated_at""",
                (
                    body.name if "name" in fields else current["name"],
                    body.problem_statement if "problem_statement" in fields else current["problem_statement"],
                    body.contribution if "contribution" in fields else current["contribution"],
                    body.technologies if "technologies" in fields else current["technologies"],
                    project_id, session.user.id, body.expected_version,
                ),
            ).fetchone()
            if project is None:
                raise api_error(409, "PROJECT_VERSION_CONFLICT", "Project changed", "Reload the project and try again.")
            project = connection.execute(
                f"""SELECT p.id, p.name, p.problem_statement, p.contribution, p.technologies,
                      p.version, p.created_at, p.updated_at, p.published_at, {PROJECT_SOURCE_LINKS_SELECT}, {PROJECT_COMPETENCY_EVIDENCE_SELECT}
                   FROM candidate_projects AS p WHERE p.id = %s AND p.user_id = %s""",
                (project_id, session.user.id),
            ).fetchone()
            connection.execute(
                """INSERT INTO audit_events
                   (id, actor_id, actor_type, resource_type, resource_id, action, correlation_id, outcome)
                   VALUES (%s, %s, 'user', 'candidate_project', %s, 'candidate.project_updated', %s, 'success')""",
                (uuid.uuid4(), session.user.id, project_id, request.state.request_id),
            )
            if "competency_claims" in fields:
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_id, actor_type, resource_type, resource_id, action, correlation_id, outcome, metadata)
                       VALUES (%s, %s, 'user', 'candidate_competency_evidence', %s,
                               'candidate.project_competency_claims_updated', %s, 'success', %s)""",
                    (uuid.uuid4(), session.user.id, project_id, request.state.request_id,
                     Jsonb({"active_claim_count": len(body.competency_claims)})),
                )
        return project_response(project)

    @app.delete(
        "/api/v1/me/projects/{project_id}",
        status_code=204,
        responses={
            401: problem_response("Authentication is required."),
            403: problem_response("The CSRF token is missing or invalid."),
            404: problem_response("The project was not found."),
            409: problem_response("Project proof records prevent deletion."),
            500: problem_response("Unexpected service failure."),
        },
    )
    def delete_project(
        project_id: uuid.UUID,
        request: Request,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> Response:
        try:
            with connection.transaction():
                connection.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (session.user.id,))
                project = connection.execute(
                    "DELETE FROM candidate_projects WHERE id = %s AND user_id = %s RETURNING id",
                    (project_id, session.user.id),
                ).fetchone()
                if project is None:
                    raise api_error(404, "PROJECT_NOT_FOUND", "Project not found", "The project was not found.")
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_id, actor_type, resource_type, resource_id, action, correlation_id, outcome)
                       VALUES (%s, %s, 'user', 'candidate_project', %s, 'candidate.project_deleted', %s, 'success')""",
                    (uuid.uuid4(), session.user.id, project_id, request.state.request_id),
                )
                connection.execute(
                    """INSERT INTO candidate_portfolios (user_id, version)
                       VALUES (%s, 1) ON CONFLICT (user_id) DO UPDATE
                       SET version = candidate_portfolios.version + 1, updated_at = now()""",
                    (session.user.id,),
                )
        except psycopg.errors.ForeignKeyViolation:
            raise api_error(409, "PROJECT_HAS_PROOF_RECORDS", "Project has proof records", "Remove linked proof records before deleting the project.")
        return Response(status_code=204)

    @app.post(
        "/api/v1/me/projects/{project_id}/source-links",
        response_model=ProjectSourceLinkResponse,
        status_code=201,
        responses={
            401: problem_response("Authentication is required."),
            403: problem_response("The CSRF token is missing or invalid."),
            404: problem_response("The project was not found."),
            409: problem_response("Unpublish the project before changing its links."),
            422: problem_response("A valid GitHub repository URL is required."),
            500: problem_response("Unexpected service failure."),
        },
    )
    def create_project_source_link(
        project_id: uuid.UUID,
        body: ProjectSourceLinkCreateRequest,
        request: Request,
        response: Response,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> ProjectSourceLinkResponse:
        with connection.transaction():
            connection.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (session.user.id,))
            project = connection.execute(
                "SELECT id, published_at FROM candidate_projects WHERE id = %s AND user_id = %s FOR UPDATE",
                (project_id, session.user.id),
            ).fetchone()
            if project is None:
                raise api_error(404, "PROJECT_NOT_FOUND", "Project not found", "The project was not found.")
            if project["published_at"] is not None:
                raise api_error(409, "PROJECT_MUST_BE_UNPUBLISHED", "Project is public", "Unpublish this project before changing its links.")
            link = connection.execute(
                """INSERT INTO candidate_project_source_links (id, project_id, url)
                   VALUES (%s, %s, %s) ON CONFLICT (project_id, url) DO NOTHING
                   RETURNING id, url, created_at""",
                (uuid.uuid4(), project_id, body.url),
            ).fetchone()
            created = link is not None
            if not created:
                link = connection.execute(
                    """SELECT id, url, created_at FROM candidate_project_source_links
                       WHERE project_id = %s AND url = %s""",
                    (project_id, body.url),
                ).fetchone()
            if created:
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_id, actor_type, resource_type, resource_id, correlation_id, action, outcome)
                       VALUES (%s, %s, 'user', 'candidate_project_source_link', %s, %s, 'candidate.project_source_linked', 'success')""",
                    (uuid.uuid4(), session.user.id, link["id"], request.state.request_id),
                )
            response.status_code = 201 if created else 200
        response.headers["Location"] = f"/api/v1/me/projects/{project_id}/source-links/{link['id']}"
        return ProjectSourceLinkResponse(**link)

    @app.delete(
        "/api/v1/me/projects/{project_id}/source-links/{link_id}",
        status_code=204,
        responses={
            401: problem_response("Authentication is required."),
            403: problem_response("The CSRF token is missing or invalid."),
            404: problem_response("The project source link was not found."),
            409: problem_response("Unpublish the project before changing its links."),
            500: problem_response("Unexpected service failure."),
        },
    )
    def delete_project_source_link(
        project_id: uuid.UUID,
        link_id: uuid.UUID,
        request: Request,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> Response:
        with connection.transaction():
            connection.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (session.user.id,))
            project = connection.execute(
                "SELECT published_at FROM candidate_projects WHERE id = %s AND user_id = %s FOR UPDATE",
                (project_id, session.user.id),
            ).fetchone()
            if project is None:
                raise api_error(404, "PROJECT_NOT_FOUND", "Project not found", "The project was not found.")
            if project["published_at"] is not None:
                raise api_error(409, "PROJECT_MUST_BE_UNPUBLISHED", "Project is public", "Unpublish this project before changing its links.")
            link = connection.execute(
                """DELETE FROM candidate_project_source_links AS source
                   USING candidate_projects AS project
                   WHERE source.id = %s AND source.project_id = %s
                     AND project.id = source.project_id AND project.user_id = %s
                   RETURNING source.id""",
                (link_id, project_id, session.user.id),
            ).fetchone()
            if link is None:
                raise api_error(404, "PROJECT_SOURCE_LINK_NOT_FOUND", "Source link not found", "The project source link was not found.")
            connection.execute(
                """INSERT INTO audit_events
                   (id, actor_id, actor_type, resource_type, resource_id, correlation_id, action, outcome)
                   VALUES (%s, %s, 'user', 'candidate_project_source_link', %s, %s, 'candidate.project_source_unlinked', 'success')""",
                (uuid.uuid4(), session.user.id, link_id, request.state.request_id),
            )
        return Response(status_code=204)

    @app.get(
        "/api/v1/me/entitlements",
        response_model=CandidateEntitlementsResponse,
        responses={401: problem_response("Authentication is required.")},
    )
    def get_candidate_entitlements(
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> CandidateEntitlementsResponse:
        portfolio_customization = has_portfolio_customization(connection, session.user.id)
        return CandidateEntitlementsResponse(
            candidate_plan="pro" if portfolio_customization else "free",
            portfolio_customization=portfolio_customization,
        )

    @app.get(
        "/api/v1/me/portfolio/versions",
        response_model=PortfolioVersionsResponse,
        responses={401: problem_response("Authentication is required.")},
    )
    def get_portfolio_versions(
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> PortfolioVersionsResponse:
        return load_portfolio_versions(connection, session.user.id)

    @app.post(
        "/api/v1/me/portfolio/versions",
        response_model=PortfolioVersionResponse,
        status_code=201,
        responses={
            401: problem_response("Authentication is required."),
            403: problem_response("A verified identity, CSRF validation, and active Pro entitlement are required."),
            409: problem_response("The portfolio draft changed. Reload and try again."),
            422: problem_response("The portfolio design contains an unsupported template, theme, or section."),
            429: problem_response("Too many portfolio versions were saved."),
        },
    )
    def save_portfolio_version(
        body: PortfolioVersionCreateRequest,
        request: Request,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> PortfolioVersionResponse:
        if not session.user.email_verified:
            raise api_error(403, "EMAIL_VERIFICATION_REQUIRED", "Verified email required", "Verify your email before customizing your portfolio.")
        if not app.state.rate_limiter.allow(f"portfolio-version:user:{session.user.id}", 60, 60):
            raise api_error(429, "RATE_LIMITED", "Too many requests", "Try again later.")
        version_id = uuid.uuid4()
        with connection.transaction():
            connection.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (session.user.id,))
            if not lock_portfolio_customization(connection, session.user.id):
                raise api_error(403, "PRO_ENTITLEMENT_REQUIRED", "Pro portfolio required", "Premium portfolio customization requires an active Candidate Pro entitlement.")
            connection.execute(
                "INSERT INTO candidate_portfolios (user_id, version) VALUES (%s, 0) ON CONFLICT (user_id) DO NOTHING",
                (session.user.id,),
            )
            portfolio = connection.execute(
                "SELECT draft_version_id FROM candidate_portfolios WHERE user_id = %s FOR UPDATE",
                (session.user.id,),
            ).fetchone()
            current = connection.execute(
                "SELECT version_number FROM candidate_portfolio_versions WHERE user_id = %s AND id = %s",
                (session.user.id, portfolio["draft_version_id"]),
            ).fetchone() if portfolio["draft_version_id"] else None
            current_version = current["version_number"] if current else 0
            if current_version != body.expected_draft_version:
                raise api_error(409, "PORTFOLIO_VERSION_CONFLICT", "Portfolio changed", "Reload the latest portfolio draft and try again.")
            version_number = connection.execute(
                "SELECT COALESCE(max(version_number), 0) + 1 AS next_version FROM candidate_portfolio_versions WHERE user_id = %s",
                (session.user.id,),
            ).fetchone()["next_version"]
            row = connection.execute(
                """INSERT INTO candidate_portfolio_versions
                       (id, user_id, version_number, template_key, theme_key, sections)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   RETURNING id, version_number, template_key, theme_key, sections, created_at, published_at""",
                (version_id, session.user.id, version_number, body.design.template_key, body.design.theme_key, Jsonb(body.design.sections)),
            ).fetchone()
            connection.execute(
                "UPDATE candidate_portfolios SET draft_version_id = %s, updated_at = now() WHERE user_id = %s",
                (version_id, session.user.id),
            )
            connection.execute(
                """INSERT INTO audit_events
                   (id, actor_id, actor_type, resource_type, resource_id, correlation_id, action, outcome, metadata)
                   VALUES (%s, %s, 'user', 'candidate_portfolio_version', %s, %s, 'candidate.portfolio_draft_saved', 'success', %s)""",
                (uuid.uuid4(), session.user.id, version_id, request.state.request_id,
                 Jsonb({"version_number": version_number, "template_key": body.design.template_key, "theme_key": body.design.theme_key})),
            )
        return PortfolioVersionResponse(**row, is_draft=True, is_published=False)

    @app.get(
        "/api/v1/me/portfolio",
        response_model=PortfolioResponse,
        responses={401: problem_response("Authentication is required."), 500: problem_response("Unexpected service failure.")},
    )
    def get_private_portfolio(
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> PortfolioResponse:
        return load_private_portfolio(connection, session.user.id)

    @app.patch(
        "/api/v1/me/portfolio",
        response_model=PortfolioResponse,
        responses={
            401: problem_response("Authentication is required."),
            403: problem_response("The CSRF token is missing or invalid."),
            409: problem_response("The portfolio order changed."),
            422: problem_response("The project list must contain every owned project exactly once."),
            500: problem_response("Unexpected service failure."),
        },
    )
    def reorder_private_portfolio(
        body: PortfolioReorderRequest,
        request: Request,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> PortfolioResponse:
        with connection.transaction():
            connection.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (session.user.id,))
            portfolio = connection.execute(
                "SELECT version FROM candidate_portfolios WHERE user_id = %s FOR UPDATE", (session.user.id,)
            ).fetchone()
            current_version = portfolio["version"] if portfolio else 0
            if current_version != body.expected_version:
                raise api_error(409, "PORTFOLIO_VERSION_CONFLICT", "Portfolio changed", "Reload the portfolio and try again.")
            owned_ids = [row["id"] for row in connection.execute(
                "SELECT id FROM candidate_projects WHERE user_id = %s ORDER BY sort_order, id FOR UPDATE",
                (session.user.id,),
            ).fetchall()]
            if len(body.project_ids) != len(owned_ids) or set(body.project_ids) != set(owned_ids):
                raise api_error(422, "PORTFOLIO_PROJECT_SET_MISMATCH", "Invalid project order", "Include every owned project exactly once.")
            if body.project_ids:
                connection.execute(
                    """UPDATE candidate_projects AS project SET sort_order = requested.ordinal
                       FROM unnest(%s::uuid[]) WITH ORDINALITY AS requested(project_id, ordinal)
                       WHERE project.id = requested.project_id AND project.user_id = %s""",
                    (body.project_ids, session.user.id),
                )
            if portfolio:
                connection.execute(
                    "UPDATE candidate_portfolios SET version = version + 1, updated_at = now() WHERE user_id = %s",
                    (session.user.id,),
                )
            else:
                connection.execute(
                    "INSERT INTO candidate_portfolios (user_id, version) VALUES (%s, 1)", (session.user.id,)
                )
            connection.execute(
                """INSERT INTO audit_events
                   (id, actor_id, actor_type, resource_type, resource_id, action, correlation_id, outcome)
                   VALUES (%s, %s, 'user', 'candidate_portfolio', %s, 'candidate.portfolio_reordered', %s, 'success')""",
                (uuid.uuid4(), session.user.id, session.user.id, request.state.request_id),
            )
        return load_private_portfolio(connection, session.user.id)

    @app.get(
        "/api/v1/me/candidate-profile/discovery",
        response_model=CandidateProfileDiscoveryResponse,
        responses={401: problem_response("Authentication is required.")},
    )
    def get_candidate_profile_discovery(
        response: Response,
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> CandidateProfileDiscoveryResponse:
        response.headers["Cache-Control"] = "no-store"
        return load_candidate_discovery(connection, session.user.id)

    @app.patch(
        "/api/v1/me/candidate-profile/discovery",
        response_model=CandidateProfileDiscoveryResponse,
        responses={
            401: problem_response("Authentication is required."),
            403: problem_response("A verified identity and valid CSRF token are required."),
            409: problem_response("Publish a completed candidate profile before opting into discovery."),
            429: problem_response("Too many discovery preference changes."),
        },
    )
    def update_candidate_profile_discovery(
        body: CandidateProfileDiscoveryRequest,
        request: Request,
        response: Response,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> CandidateProfileDiscoveryResponse:
        if not session.user.email_verified:
            raise api_error(403, "EMAIL_VERIFICATION_REQUIRED", "Verified email required", "Verify your email before changing discovery preferences.")
        if not app.state.rate_limiter.allow(f"candidate-discovery:user:{session.user.id}", 20, 3600):
            raise api_error(429, "RATE_LIMITED", "Too many requests", "Try again later.")
        with connection.transaction():
            connection.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (session.user.id,))
            portfolio = connection.execute(
                "SELECT discoverable_at, published_at FROM candidate_portfolios WHERE user_id = %s FOR UPDATE",
                (session.user.id,),
            ).fetchone()
            if body.discoverable and (
                portfolio is None
                or portfolio["published_at"] is None
                or connection.execute(
                    "SELECT 1 FROM candidate_onboardings WHERE user_id = %s AND completed_at IS NOT NULL",
                    (session.user.id,),
                ).fetchone() is None
            ):
                raise api_error(409, "PUBLIC_PROFILE_REQUIRED", "Public profile required", "Complete and publish your Free profile before opting into employer discovery.")
            current = bool(portfolio and portfolio["discoverable_at"])
            if portfolio and current != body.discoverable:
                if body.discoverable:
                    connection.execute(
                        "UPDATE candidate_portfolios SET discoverable_at = now(), discovery_consent_at = now(), discovery_consent_version = %s, updated_at = now() WHERE user_id = %s",
                        (body.consent_version, session.user.id),
                    )
                else:
                    connection.execute(
                        "UPDATE candidate_portfolios SET discoverable_at = NULL, discovery_consent_at = NULL, discovery_consent_version = NULL, updated_at = now() WHERE user_id = %s",
                        (session.user.id,),
                    )
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_id, actor_type, resource_type, resource_id, action, correlation_id, outcome, metadata)
                       VALUES (%s, %s, 'user', 'candidate_profile_discovery', %s, %s, %s, 'success', %s)""",
                    (uuid.uuid4(), session.user.id, session.user.id,
                     "candidate.discovery_enabled" if body.discoverable else "candidate.discovery_disabled",
                     request.state.request_id, Jsonb({"consent_version": body.consent_version})),
                )
        response.headers["Cache-Control"] = "no-store"
        return load_candidate_discovery(connection, session.user.id)

    @app.post(
        "/api/v1/me/candidate-profile/handle",
        response_model=CandidateProfileHandleResponse,
        responses={
            401: problem_response("Authentication is required."),
            403: problem_response("A verified candidate account is required."),
            409: problem_response("The handle is already in use, the candidate profile is unavailable, or the published handle is locked."),
            422: problem_response("The handle is invalid or reserved."),
            429: problem_response("Too many handle changes."),
        },
    )
    def set_candidate_profile_handle(
        body: CandidateProfileHandleRequest,
        request: Request,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> CandidateProfileHandleResponse:
        if not session.user.email_verified:
            raise api_error(403, "EMAIL_VERIFICATION_REQUIRED", "Verified email required", "Verify your email before setting a profile address.")
        client_ip = request.client.host if request.client else "unknown"
        if not app.state.rate_limiter.allow(f"profile-handle:user:{session.user.id}", 10, 3600) or not app.state.rate_limiter.allow(f"profile-handle:ip:{client_ip}", 40, 3600):
            raise api_error(429, "RATE_LIMITED", "Too many requests", "Try again later.")
        try:
            with connection.transaction():
                connection.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (session.user.id,))
                candidate = connection.execute(
                    "SELECT 1 FROM candidate_onboardings WHERE user_id = %s", (session.user.id,)
                ).fetchone()
                if candidate is None:
                    raise api_error(409, "CANDIDATE_PROFILE_REQUIRED", "Candidate profile unavailable", "Save your candidate profile before choosing a public address.")
                portfolio = connection.execute(
                    "SELECT public_handle, public_handle_locked_at, published_at FROM candidate_portfolios WHERE user_id = %s FOR UPDATE",
                    (session.user.id,),
                ).fetchone()
                if portfolio is None:
                    connection.execute(
                        "INSERT INTO candidate_portfolios (user_id, version) VALUES (%s, 0)", (session.user.id,)
                    )
                    portfolio = {"public_handle": None, "public_handle_locked_at": None, "published_at": None}
                if portfolio["public_handle_locked_at"] is not None and portfolio["public_handle"] != body.handle:
                    raise api_error(409, "PROFILE_HANDLE_LOCKED", "Profile address is permanent", "A published profile address cannot be changed.")
                connection.execute(
                    "UPDATE candidate_portfolios SET public_handle = %s, updated_at = now() WHERE user_id = %s",
                    (body.handle, session.user.id),
                )
                if portfolio["public_handle"] != body.handle:
                    connection.execute(
                        """INSERT INTO audit_events
                           (id, actor_id, actor_type, resource_type, resource_id, action, correlation_id, outcome)
                           VALUES (%s, %s, 'user', 'candidate_portfolio', %s, 'candidate.portfolio_handle_changed', %s, 'success')""",
                        (uuid.uuid4(), session.user.id, session.user.id, request.state.request_id),
                    )
        except psycopg.errors.UniqueViolation:
            raise api_error(409, "PROFILE_HANDLE_UNAVAILABLE", "Handle unavailable", "Choose another profile address.")
        return CandidateProfileHandleResponse(handle=body.handle, published=portfolio["published_at"] is not None)

    @app.post(
        "/api/v1/me/projects/{project_id}/publish",
        response_model=ProjectResponse,
        responses={
            401: problem_response("Authentication is required."),
            403: problem_response("Verified identity and CSRF validation are required."),
            404: problem_response("The project was not found."),
            409: problem_response("The project or candidate profile changed."),
            422: problem_response("The consent request is invalid."),
        },
    )
    def publish_project(
        project_id: uuid.UUID,
        body: ProjectPublishRequest,
        request: Request,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> ProjectResponse:
        if not session.user.email_verified:
            raise api_error(403, "EMAIL_VERIFICATION_REQUIRED", "Verified email required", "Verify your email before publishing work.")
        with connection.transaction():
            connection.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (session.user.id,))
            project = connection.execute(
                "SELECT version, published_at FROM candidate_projects WHERE id = %s AND user_id = %s FOR UPDATE",
                (project_id, session.user.id),
            ).fetchone()
            if project is None:
                raise api_error(404, "PROJECT_NOT_FOUND", "Project not found", "The project was not found.")
            candidate = connection.execute(
                "SELECT completed_at FROM candidate_onboardings WHERE user_id = %s FOR SHARE", (session.user.id,)
            ).fetchone()
            portfolio = connection.execute(
                "SELECT public_handle FROM candidate_portfolios WHERE user_id = %s FOR SHARE", (session.user.id,)
            ).fetchone()
            if candidate is None or candidate["completed_at"] is None or portfolio is None or not portfolio["public_handle"]:
                raise api_error(409, "PUBLIC_PROFILE_REQUIRED", "Public profile required", "Complete your profile and choose its public address first.")
            if project["version"] != body.expected_version:
                raise api_error(409, "PROJECT_VERSION_CONFLICT", "Project changed", "Reload the project and try again.")
            if project["published_at"] is None:
                connection.execute(
                    "UPDATE candidate_projects SET published_at = now() WHERE id = %s AND user_id = %s",
                    (project_id, session.user.id),
                )
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_id, actor_type, resource_type, resource_id, action, correlation_id, outcome, metadata)
                       VALUES (%s, %s, 'user', 'candidate_project', %s, 'candidate.project_published', %s, 'success', %s)""",
                    (uuid.uuid4(), session.user.id, project_id, request.state.request_id, Jsonb({"consent_version": body.consent_version})),
                )
        row = connection.execute(
            f"""SELECT p.id, p.name, p.problem_statement, p.contribution, p.technologies,
                      p.version, p.created_at, p.updated_at, p.published_at, {PROJECT_SOURCE_LINKS_SELECT}, {PROJECT_COMPETENCY_EVIDENCE_SELECT}
               FROM candidate_projects AS p WHERE p.id = %s AND p.user_id = %s""",
            (project_id, session.user.id),
        ).fetchone()
        return project_response(row)

    @app.post(
        "/api/v1/me/projects/{project_id}/unpublish",
        status_code=204,
        responses={401: problem_response("Authentication is required."), 403: problem_response("CSRF validation is required."), 404: problem_response("The project was not found.")},
    )
    def unpublish_project(
        project_id: uuid.UUID,
        request: Request,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> Response:
        with connection.transaction():
            connection.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (session.user.id,))
            project = connection.execute(
                "SELECT published_at FROM candidate_projects WHERE id = %s AND user_id = %s FOR UPDATE",
                (project_id, session.user.id),
            ).fetchone()
            if project is None:
                raise api_error(404, "PROJECT_NOT_FOUND", "Project not found", "The project was not found.")
            if project["published_at"] is not None:
                connection.execute(
                    "UPDATE candidate_projects SET published_at = NULL WHERE id = %s AND user_id = %s",
                    (project_id, session.user.id),
                )
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_id, actor_type, resource_type, resource_id, action, correlation_id, outcome)
                       VALUES (%s, %s, 'user', 'candidate_project', %s, 'candidate.project_unpublished', %s, 'success')""",
                    (uuid.uuid4(), session.user.id, project_id, request.state.request_id),
                )
        return Response(status_code=204)

    @app.post(
        "/api/v1/me/portfolio/publish",
        response_model=PortfolioResponse,
        responses={
            401: problem_response("Authentication is required."),
            403: problem_response("Verified identity and CSRF validation are required."),
            404: problem_response("The portfolio version was not found."),
            409: problem_response("Complete the candidate profile and choose a public address first."),
        },
    )
    def publish_portfolio(
        body: PortfolioPublishRequest,
        request: Request,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> PortfolioResponse:
        if not session.user.email_verified:
            raise api_error(403, "EMAIL_VERIFICATION_REQUIRED", "Verified email required", "Verify your email before publishing your profile.")
        with connection.transaction():
            connection.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (session.user.id,))
            if body.draft_version_id and not lock_portfolio_customization(connection, session.user.id):
                raise api_error(403, "PRO_ENTITLEMENT_REQUIRED", "Pro portfolio required", "Publishing a premium design requires an active Candidate Pro entitlement.")
            candidate = connection.execute(
                "SELECT completed_at FROM candidate_onboardings WHERE user_id = %s FOR SHARE", (session.user.id,)
            ).fetchone()
            portfolio = connection.execute(
                "SELECT public_handle, public_handle_locked_at, published_at FROM candidate_portfolios WHERE user_id = %s FOR UPDATE",
                (session.user.id,),
            ).fetchone()
            if candidate is None or candidate["completed_at"] is None or portfolio is None or not portfolio["public_handle"]:
                raise api_error(409, "PUBLIC_PROFILE_REQUIRED", "Public profile required", "Complete your profile and choose its public address first.")
            if body.draft_version_id and portfolio["published_at"] is None:
                raise api_error(409, "PUBLIC_PROFILE_REQUIRED", "Public profile required", "Publish your free profile before publishing a premium design.")
            design_version = None
            if body.draft_version_id:
                design_version = connection.execute(
                    """SELECT id, version_number, template_key, theme_key FROM candidate_portfolio_versions
                       WHERE id = %s AND user_id = %s FOR SHARE""",
                    (body.draft_version_id, session.user.id),
                ).fetchone()
                if design_version is None:
                    raise api_error(404, "PORTFOLIO_VERSION_NOT_FOUND", "Portfolio version not found", "The portfolio version was not found.")
            if portfolio["published_at"] is None:
                connection.execute(
                    """UPDATE candidate_portfolios SET published_at = now(), publication_consent_at = now(),
                              publication_consent_version = %s, public_handle_locked_at = COALESCE(public_handle_locked_at, now()),
                              updated_at = now() WHERE user_id = %s""",
                    (body.consent_version, session.user.id),
                )
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_id, actor_type, resource_type, resource_id, action, correlation_id, outcome, metadata)
                       VALUES (%s, %s, 'user', 'candidate_portfolio', %s, 'candidate.portfolio_published', %s, 'success', %s)""",
                    (uuid.uuid4(), session.user.id, session.user.id, request.state.request_id, Jsonb({"consent_version": body.consent_version})),
                )
            if design_version:
                connection.execute(
                    "UPDATE candidate_portfolio_versions SET published_at = COALESCE(published_at, now()) WHERE id = %s AND user_id = %s",
                    (design_version["id"], session.user.id),
                )
                connection.execute(
                    "UPDATE candidate_portfolios SET published_version_id = %s, updated_at = now() WHERE user_id = %s",
                    (design_version["id"], session.user.id),
                )
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_id, actor_type, resource_type, resource_id, correlation_id, action, outcome, metadata)
                       VALUES (%s, %s, 'user', 'candidate_portfolio_version', %s, %s, 'candidate.portfolio_design_published', 'success', %s)""",
                    (uuid.uuid4(), session.user.id, design_version["id"], request.state.request_id,
                     Jsonb({"version_number": design_version["version_number"], "template_key": design_version["template_key"], "theme_key": design_version["theme_key"]})),
                )
        return load_private_portfolio(connection, session.user.id)

    @app.post(
        "/api/v1/me/portfolio/unpublish",
        status_code=204,
        responses={401: problem_response("Authentication is required."), 403: problem_response("CSRF validation is required.")},
    )
    def unpublish_portfolio(
        request: Request,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> Response:
        with connection.transaction():
            connection.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (session.user.id,))
            portfolio = connection.execute(
                "SELECT published_at, discoverable_at FROM candidate_portfolios WHERE user_id = %s FOR UPDATE", (session.user.id,)
            ).fetchone()
            changed_projects = connection.execute(
                "UPDATE candidate_projects SET published_at = NULL WHERE user_id = %s AND published_at IS NOT NULL RETURNING id",
                (session.user.id,),
            ).fetchall()
            if portfolio and (portfolio["published_at"] is not None or portfolio["discoverable_at"] is not None):
                connection.execute(
                    "UPDATE candidate_portfolios SET published_at = NULL, discoverable_at = NULL, discovery_consent_at = NULL, discovery_consent_version = NULL, updated_at = now() WHERE user_id = %s",
                    (session.user.id,),
                )
                if portfolio["discoverable_at"] is not None:
                    connection.execute(
                        """INSERT INTO audit_events
                           (id, actor_id, actor_type, resource_type, resource_id, action, correlation_id, outcome, metadata)
                           VALUES (%s, %s, 'user', 'candidate_profile_discovery', %s, 'candidate.discovery_disabled', %s, 'success', %s)""",
                        (uuid.uuid4(), session.user.id, session.user.id, request.state.request_id,
                         Jsonb({"reason": "portfolio_unpublished", "consent_version": "candidate-discovery-v1"})),
                    )
            if portfolio and (portfolio["published_at"] is not None or changed_projects):
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_id, actor_type, resource_type, resource_id, action, correlation_id, outcome)
                       VALUES (%s, %s, 'user', 'candidate_portfolio', %s, 'candidate.portfolio_unpublished', %s, 'success')""",
                    (uuid.uuid4(), session.user.id, session.user.id, request.state.request_id),
                )
        return Response(status_code=204)

    @app.get(
        "/api/v1/public/portfolios/{handle}",
        response_model=PublicPortfolioResponse,
        responses={404: problem_response("The published portfolio was not found."), 429: problem_response("Too many requests.")},
    )
    def get_public_portfolio(
        handle: str,
        request: Request,
        response: Response,
        connection: psycopg.Connection = Depends(get_connection),
    ) -> PublicPortfolioResponse:
        client_ip = request.client.host if request.client else "unknown"
        if not app.state.rate_limiter.allow(f"public-portfolio:ip:{client_ip}", 120, 60):
            raise api_error(429, "RATE_LIMITED", "Too many requests", "Try again later.")
        profile = connection.execute(
            """SELECT account.full_name, onboarding.target_roles, onboarding.skills, portfolio.user_id
               FROM candidate_portfolios AS portfolio
               JOIN users AS account ON account.id = portfolio.user_id
               JOIN candidate_onboardings AS onboarding ON onboarding.user_id = account.id
               WHERE lower(portfolio.public_handle) = lower(%s) AND portfolio.published_at IS NOT NULL
                 AND onboarding.completed_at IS NOT NULL""",
            (handle,),
        ).fetchone()
        if profile is None:
            raise api_error(404, "PUBLIC_PORTFOLIO_NOT_FOUND", "Portfolio not found", "The published portfolio was not found.")
        design = None
        if has_portfolio_customization(connection, profile["user_id"]):
            design_row = connection.execute(
                """SELECT version.template_key, version.theme_key, version.sections
                   FROM candidate_portfolios AS portfolio
                   JOIN candidate_portfolio_versions AS version
                     ON version.id = portfolio.published_version_id AND version.user_id = portfolio.user_id
                   WHERE portfolio.user_id = %s""",
                (profile["user_id"],),
            ).fetchone()
            if design_row:
                design = PortfolioDesign(**design_row)
        projects = connection.execute(
            f"""SELECT p.name, p.problem_statement, p.contribution, p.technologies,
                      COALESCE((SELECT jsonb_agg(source.url ORDER BY source.created_at, source.id)
                                FROM candidate_project_source_links AS source WHERE source.project_id = p.id), '[]'::jsonb) AS source_links
               FROM candidate_projects AS p WHERE p.user_id = %s AND p.published_at IS NOT NULL
               ORDER BY p.sort_order, p.id""",
            (profile["user_id"],),
        ).fetchall()
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return PublicPortfolioResponse(
            display_name=profile["full_name"], target_roles=profile["target_roles"], skills=profile["skills"],
            projects=[PublicProjectResponse(**project) for project in projects], design=design,
        )

    @app.get(
        "/api/v1/jobs",
        response_model=PublicJobListResponse,
        responses={429: problem_response("Too many requests.")},
    )
    def list_public_jobs(
        request: Request,
        response: Response,
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
        offset: Annotated[int, Query(ge=0, le=10000)] = 0,
        connection: psycopg.Connection = Depends(get_connection),
    ) -> PublicJobListResponse:
        client_ip = request.client.host if request.client else "unknown"
        if not app.state.rate_limiter.allow(f"public-jobs:ip:{client_ip}", 120, 60):
            raise api_error(429, "RATE_LIMITED", "Too many requests", "Try again later.")
        rows = connection.execute(
            f"""SELECT job.id, organization.name AS organization_name, job.title, job.description, job.published_at,
                      {JOB_COMPETENCY_REQUIREMENTS_SELECT}
               FROM organization_jobs AS job JOIN organizations AS organization
                 ON organization.id = job.organization_id
               WHERE job.status = 'published' ORDER BY job.published_at DESC, job.id LIMIT %s OFFSET %s""",
            (limit, offset),
        ).fetchall()
        response.headers["Cache-Control"] = "no-store"
        return PublicJobListResponse(
            jobs=[PublicJobResponse(**row) for row in rows], limit=limit, offset=offset
        )

    @app.get(
        "/api/v1/jobs/{job_id}",
        response_model=PublicJobResponse,
        responses={404: problem_response("The published job was not found."), 429: problem_response("Too many requests.")},
    )
    def get_public_job(
        job_id: uuid.UUID,
        request: Request,
        response: Response,
        connection: psycopg.Connection = Depends(get_connection),
    ) -> PublicJobResponse:
        client_ip = request.client.host if request.client else "unknown"
        if not app.state.rate_limiter.allow(f"public-job:ip:{client_ip}", 240, 60):
            raise api_error(429, "RATE_LIMITED", "Too many requests", "Try again later.")
        row = connection.execute(
            f"""SELECT job.id, organization.name AS organization_name, job.title, job.description, job.published_at,
                      {JOB_COMPETENCY_REQUIREMENTS_SELECT}
               FROM organization_jobs AS job JOIN organizations AS organization
                 ON organization.id = job.organization_id
               WHERE job.id = %s AND job.status = 'published'""",
            (job_id,),
        ).fetchone()
        if row is None:
            raise api_error(404, "JOB_NOT_FOUND", "Job not found", "The published job was not found.")
        response.headers["Cache-Control"] = "no-store"
        return PublicJobResponse(**row)

    @app.get(
        "/api/v1/organizations/{org_id}/jobs",
        response_model=JobListResponse,
        responses={401: problem_response("Authentication is required."), 404: problem_response("The organization was not found.")},
    )
    def list_organization_jobs(
        org_id: uuid.UUID,
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> JobListResponse:
        organization_access(connection, session.user.id, org_id)
        rows = connection.execute(
            f"{JOB_SELECT} WHERE job.organization_id = %s ORDER BY job.created_at DESC, job.id",
            (org_id,),
        ).fetchall()
        return JobListResponse(jobs=[job_response(row) for row in rows])

    @app.post(
        "/api/v1/organizations/{org_id}/jobs",
        response_model=JobResponse,
        status_code=201,
        responses={
            401: problem_response("Authentication is required."),
            403: problem_response("A verified owner, admin, or recruiter is required."),
            404: problem_response("The organization was not found."),
            409: problem_response("Organization setup is incomplete or the request key conflicts."),
            422: problem_response("The request is invalid."),
            429: problem_response("Too many requests."),
        },
    )
    def create_organization_job(
        org_id: uuid.UUID,
        body: JobCreateRequest,
        request: Request,
        response: Response,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> JobResponse:
        if not session.user.email_verified:
            raise api_error(403, "EMAIL_VERIFICATION_REQUIRED", "Verified email required", "Verify your email before creating a job.")
        client_ip = request.client.host if request.client else "unknown"
        if not app.state.rate_limiter.allow(f"job-create:user:{session.user.id}", 20, 900) or not app.state.rate_limiter.allow(f"job-create:ip:{client_ip}", 60, 900):
            raise api_error(429, "RATE_LIMITED", "Too many requests", "Try again later.")
        payload_hash = hash_token(
            "\0".join(("job:create:v1", body.title, body.description))
            + (
                "\0job-competencies:v1\0" + "\x01".join(
                    "\0".join((
                        item.competency_code, item.target_level,
                        "\x02".join(item.accepted_verification_methods), str(item.max_age_days),
                    ))
                    for item in body.competency_requirements
                )
                if body.competency_requirements else ""
            )
        )
        with connection.transaction():
            organization = organization_access(connection, session.user.id, org_id, for_update=True)
            if organization["member_role"] not in ("owner", "admin", "recruiter"):
                raise api_error(403, "JOB_AUTHORING_FORBIDDEN", "Access denied", "An organization recruiter role is required.")
            if organization["onboarding_completed_at"] is None:
                raise api_error(409, "ORGANIZATION_ONBOARDING_REQUIRED", "Organization setup incomplete", "Complete organization setup before creating jobs.")
            validate_job_competency_codes(connection, body.competency_requirements)
            created = connection.execute(
                """INSERT INTO organization_jobs
                   (id, organization_id, created_by_user_id, request_key, creation_payload_hash, title, description)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (organization_id, request_key) DO NOTHING RETURNING id""",
                (uuid.uuid4(), org_id, session.user.id, body.request_key, payload_hash, body.title, body.description),
            ).fetchone()
            if created is None:
                existing = connection.execute(
                    "SELECT id, creation_payload_hash FROM organization_jobs WHERE organization_id = %s AND request_key = %s",
                    (org_id, body.request_key),
                ).fetchone()
                if existing is None or bytes(existing["creation_payload_hash"]) != payload_hash:
                    raise api_error(409, "JOB_CREATE_CONFLICT", "Request conflict", "Use a new request key for different job details.")
                job_id = existing["id"]
                response.status_code = 200
            else:
                job_id = created["id"]
                sync_job_competency_requirements(connection, org_id, job_id, body.competency_requirements)
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome)
                       VALUES (%s, %s, 'user', %s, 'organization_job', %s, 'organization.job_created', %s, 'success')""",
                    (uuid.uuid4(), session.user.id, org_id, job_id, request.state.request_id),
                )
                if body.competency_requirements:
                    connection.execute(
                        """INSERT INTO audit_events
                           (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome, metadata)
                           VALUES (%s, %s, 'user', %s, 'organization_job', %s,
                                   'organization.job_competency_requirements_updated', %s, 'success', %s)""",
                        (uuid.uuid4(), session.user.id, org_id, job_id, request.state.request_id,
                         Jsonb({"requirement_count": len(body.competency_requirements)})),
                    )
        row = connection.execute(f"{JOB_SELECT} WHERE job.id = %s AND job.organization_id = %s", (job_id, org_id)).fetchone()
        response.headers["Location"] = f"/api/v1/organizations/{org_id}/jobs/{job_id}"
        return job_response(row)

    @app.get(
        "/api/v1/organizations/{org_id}/jobs/{job_id}",
        response_model=JobResponse,
        responses={401: problem_response("Authentication is required."), 404: problem_response("The organization or job was not found.")},
    )
    def get_organization_job(
        org_id: uuid.UUID,
        job_id: uuid.UUID,
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> JobResponse:
        organization_access(connection, session.user.id, org_id)
        row = connection.execute(f"{JOB_SELECT} WHERE job.id = %s AND job.organization_id = %s", (job_id, org_id)).fetchone()
        if row is None:
            raise api_error(404, "JOB_NOT_FOUND", "Job not found", "The job was not found.")
        return job_response(row)

    @app.patch(
        "/api/v1/organizations/{org_id}/jobs/{job_id}",
        response_model=JobResponse,
        responses={
            401: problem_response("Authentication is required."),
            403: problem_response("An organization recruiter role is required."),
            404: problem_response("The organization or job was not found."),
            409: problem_response("The job changed or is closed."),
            422: problem_response("The request is invalid."),
        },
    )
    def update_organization_job(
        org_id: uuid.UUID,
        job_id: uuid.UUID,
        body: JobPatchRequest,
        request: Request,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> JobResponse:
        with connection.transaction():
            organization = organization_access(connection, session.user.id, org_id, for_update=True)
            if organization["member_role"] not in ("owner", "admin", "recruiter"):
                raise api_error(403, "JOB_AUTHORING_FORBIDDEN", "Access denied", "An organization recruiter role is required.")
            job = connection.execute(
                "SELECT title, description, status, version FROM organization_jobs WHERE id = %s AND organization_id = %s FOR UPDATE",
                (job_id, org_id),
            ).fetchone()
            if job is None:
                raise api_error(404, "JOB_NOT_FOUND", "Job not found", "The job was not found.")
            if job["status"] == "closed":
                raise api_error(409, "JOB_CLOSED", "Job is closed", "A closed job cannot be edited.")
            if job["version"] != body.expected_version:
                raise api_error(409, "JOB_VERSION_CONFLICT", "Job changed", "Reload the job and try again.")
            fields = body.model_fields_set - {"expected_version"}
            if "competency_requirements" in fields:
                validate_job_competency_codes(connection, body.competency_requirements)
                sync_job_competency_requirements(connection, org_id, job_id, body.competency_requirements)
            connection.execute(
                """UPDATE organization_jobs SET title = %s, description = %s, version = version + 1, updated_at = now()
                   WHERE id = %s AND organization_id = %s""",
                (body.title if "title" in fields else job["title"], body.description if "description" in fields else job["description"], job_id, org_id),
            )
            connection.execute(
                """INSERT INTO audit_events
                   (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome)
                   VALUES (%s, %s, 'user', %s, 'organization_job', %s, 'organization.job_updated', %s, 'success')""",
                (uuid.uuid4(), session.user.id, org_id, job_id, request.state.request_id),
            )
            if "competency_requirements" in fields:
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome, metadata)
                       VALUES (%s, %s, 'user', %s, 'organization_job', %s,
                               'organization.job_competency_requirements_updated', %s, 'success', %s)""",
                    (uuid.uuid4(), session.user.id, org_id, job_id, request.state.request_id,
                     Jsonb({"requirement_count": len(body.competency_requirements)})),
                )
        return job_response(connection.execute(f"{JOB_SELECT} WHERE job.id = %s AND job.organization_id = %s", (job_id, org_id)).fetchone())

    @app.post(
        "/api/v1/organizations/{org_id}/jobs/{job_id}/publish",
        response_model=JobResponse,
        responses={401: problem_response("Authentication is required."), 403: problem_response("An organization recruiter role is required."), 404: problem_response("The organization or job was not found."), 409: problem_response("The job cannot be published from its current state or version.")},
    )
    def publish_organization_job(
        org_id: uuid.UUID,
        job_id: uuid.UUID,
        body: JobTransitionRequest,
        request: Request,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> JobResponse:
        with connection.transaction():
            organization = organization_access(connection, session.user.id, org_id, for_update=True)
            if organization["member_role"] not in ("owner", "admin", "recruiter"):
                raise api_error(403, "JOB_AUTHORING_FORBIDDEN", "Access denied", "An organization recruiter role is required.")
            if organization["onboarding_completed_at"] is None:
                raise api_error(409, "ORGANIZATION_ONBOARDING_REQUIRED", "Organization setup incomplete", "Complete organization setup before publishing jobs.")
            job = connection.execute(
                "SELECT status, version FROM organization_jobs WHERE id = %s AND organization_id = %s FOR UPDATE",
                (job_id, org_id),
            ).fetchone()
            if job is None:
                raise api_error(404, "JOB_NOT_FOUND", "Job not found", "The job was not found.")
            if job["status"] == "published":
                pass
            elif job["status"] != "draft":
                raise api_error(409, "JOB_INVALID_TRANSITION", "Job cannot be published", "Only a draft job can be published.")
            elif job["version"] != body.expected_version:
                raise api_error(409, "JOB_VERSION_CONFLICT", "Job changed", "Reload the job and try again.")
            else:
                connection.execute(
                    """UPDATE organization_jobs SET status = 'published', published_at = now(), version = version + 1, updated_at = now()
                       WHERE id = %s AND organization_id = %s""",
                    (job_id, org_id),
                )
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome)
                       VALUES (%s, %s, 'user', %s, 'organization_job', %s, 'organization.job_published', %s, 'success')""",
                    (uuid.uuid4(), session.user.id, org_id, job_id, request.state.request_id),
                )
        return job_response(connection.execute(f"{JOB_SELECT} WHERE job.id = %s AND job.organization_id = %s", (job_id, org_id)).fetchone())

    @app.post(
        "/api/v1/organizations/{org_id}/jobs/{job_id}/close",
        response_model=JobResponse,
        responses={401: problem_response("Authentication is required."), 403: problem_response("An organization recruiter role is required."), 404: problem_response("The organization or job was not found."), 409: problem_response("The job cannot be closed from its current state or version.")},
    )
    def close_organization_job(
        org_id: uuid.UUID,
        job_id: uuid.UUID,
        body: JobTransitionRequest,
        request: Request,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> JobResponse:
        with connection.transaction():
            organization = organization_access(connection, session.user.id, org_id, for_update=True)
            if organization["member_role"] not in ("owner", "admin", "recruiter"):
                raise api_error(403, "JOB_AUTHORING_FORBIDDEN", "Access denied", "An organization recruiter role is required.")
            job = connection.execute(
                "SELECT status, version FROM organization_jobs WHERE id = %s AND organization_id = %s FOR UPDATE",
                (job_id, org_id),
            ).fetchone()
            if job is None:
                raise api_error(404, "JOB_NOT_FOUND", "Job not found", "The job was not found.")
            if job["status"] == "closed":
                pass
            elif job["status"] != "published":
                raise api_error(409, "JOB_INVALID_TRANSITION", "Job cannot be closed", "Only a published job can be closed.")
            elif job["version"] != body.expected_version:
                raise api_error(409, "JOB_VERSION_CONFLICT", "Job changed", "Reload the job and try again.")
            else:
                connection.execute(
                    """UPDATE organization_jobs SET status = 'closed', closed_at = now(), version = version + 1, updated_at = now()
                       WHERE id = %s AND organization_id = %s""",
                    (job_id, org_id),
                )
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome)
                       VALUES (%s, %s, 'user', %s, 'organization_job', %s, 'organization.job_closed', %s, 'success')""",
                    (uuid.uuid4(), session.user.id, org_id, job_id, request.state.request_id),
                )
        return job_response(connection.execute(f"{JOB_SELECT} WHERE job.id = %s AND job.organization_id = %s", (job_id, org_id)).fetchone())

    @app.get(
        "/api/v1/organizations/{org_id}/assessments",
        response_model=AssessmentListResponse,
        responses={401: problem_response("Authentication is required."), 403: problem_response("Hiring team access is required."), 404: problem_response("The organization was not found.")},
    )
    def list_organization_assessments(
        org_id: uuid.UUID,
        response: Response,
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
        offset: Annotated[int, Query(ge=0, le=10000)] = 0,
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> AssessmentListResponse:
        organization = organization_access(connection, session.user.id, org_id)
        if organization["member_role"] not in {"owner", "admin", "recruiter"}:
            raise api_error(403, "ASSESSMENT_ACCESS_FORBIDDEN", "Access denied", "A hiring team role is required to view assessment content.")
        rows = connection.execute(
            """SELECT assessment.id, assessment.definition->>'title' AS title,
                      assessment.definition->>'role_label' AS role_label,
                      assessment.definition->>'assessment_type' AS assessment_type,
                      (assessment.definition->>'duration_minutes')::integer AS duration_minutes,
                      assessment.status, assessment.version, published.version_number AS published_version,
                      assessment.created_at, assessment.updated_at
               FROM organization_assessments AS assessment
               LEFT JOIN LATERAL (
                   SELECT version_number FROM organization_assessment_versions
                   WHERE assessment_id = assessment.id ORDER BY version_number DESC LIMIT 1
               ) AS published ON TRUE
               WHERE assessment.organization_id = %s
               ORDER BY assessment.created_at DESC, assessment.id LIMIT %s OFFSET %s""",
            (org_id, limit, offset),
        ).fetchall()
        response.headers["Cache-Control"] = "no-store"
        return AssessmentListResponse(
            assessments=[AssessmentListItem(**row) for row in rows], limit=limit, offset=offset
        )

    @app.post(
        "/api/v1/organizations/{org_id}/assessments",
        response_model=AssessmentResponse,
        status_code=201,
        responses={
            200: {"model": AssessmentResponse, "description": "The matching idempotent draft already exists."},
            401: problem_response("Authentication is required."),
            403: problem_response("A verified organization recruiter role is required."),
            404: problem_response("The organization was not found."),
            409: problem_response("Organization setup is incomplete or the request key conflicts."),
            422: problem_response("The request is invalid."),
            429: problem_response("Too many requests."),
        },
    )
    def create_organization_assessment(
        org_id: uuid.UUID,
        body: AssessmentCreateRequest,
        request: Request,
        response: Response,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> AssessmentResponse:
        if not session.user.email_verified:
            raise api_error(403, "EMAIL_VERIFICATION_REQUIRED", "Verified email required", "Verify your email before authoring an assessment.")
        if not app.state.rate_limiter.allow(f"assessment-create:user:{session.user.id}", 20, 900):
            raise api_error(429, "RATE_LIMITED", "Too many requests", "Try again later.")
        payload_hash = hash_token(f"assessment:create:v1:{body.definition.model_dump_json()}")
        definition = body.definition.model_dump(mode="json")
        with connection.transaction():
            organization = organization_access(connection, session.user.id, org_id, for_update=True)
            if organization["member_role"] not in {"owner", "admin", "recruiter"}:
                raise api_error(403, "ASSESSMENT_AUTHORING_FORBIDDEN", "Access denied", "An organization recruiter role is required.")
            if organization["onboarding_completed_at"] is None:
                raise api_error(409, "ORGANIZATION_ONBOARDING_REQUIRED", "Organization setup incomplete", "Complete organization setup before authoring assessments.")
            validate_assessment_competency_codes(connection, body.definition)
            created = connection.execute(
                """INSERT INTO organization_assessments
                   (id, organization_id, created_by_user_id, request_key, creation_payload_hash, definition)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (organization_id, request_key) DO NOTHING RETURNING id""",
                (uuid.uuid4(), org_id, session.user.id, body.request_key, payload_hash, Jsonb(definition)),
            ).fetchone()
            if created is None:
                existing = connection.execute(
                    """SELECT id, creation_payload_hash FROM organization_assessments
                       WHERE organization_id = %s AND request_key = %s""",
                    (org_id, body.request_key),
                ).fetchone()
                if existing is None or bytes(existing["creation_payload_hash"]) != payload_hash:
                    raise api_error(409, "ASSESSMENT_CREATE_CONFLICT", "Request conflict", "Use a new request key for different assessment details.")
                assessment_id = existing["id"]
                response.status_code = 200
            else:
                assessment_id = created["id"]
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome)
                       VALUES (%s, %s, 'user', %s, 'organization_assessment', %s, 'organization.assessment_created', %s, 'success')""",
                    (uuid.uuid4(), session.user.id, org_id, assessment_id, request.state.request_id),
                )
        row = connection.execute(
            f"{ASSESSMENT_SELECT} WHERE assessment.id = %s AND assessment.organization_id = %s",
            (assessment_id, org_id),
        ).fetchone()
        response.headers["Location"] = f"/api/v1/organizations/{org_id}/assessments/{assessment_id}"
        response.headers["Cache-Control"] = "no-store"
        return assessment_response(row)

    @app.get(
        "/api/v1/organizations/{org_id}/assessments/{assessment_id}",
        response_model=AssessmentResponse,
        responses={401: problem_response("Authentication is required."), 403: problem_response("Hiring team access is required."), 404: problem_response("The assessment was not found in this organization.")},
    )
    def get_organization_assessment(
        org_id: uuid.UUID,
        assessment_id: uuid.UUID,
        response: Response,
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> AssessmentResponse:
        organization = organization_access(connection, session.user.id, org_id)
        if organization["member_role"] not in {"owner", "admin", "recruiter"}:
            raise api_error(403, "ASSESSMENT_ACCESS_FORBIDDEN", "Access denied", "A hiring team role is required to view assessment content.")
        row = connection.execute(
            f"{ASSESSMENT_SELECT} WHERE assessment.id = %s AND assessment.organization_id = %s",
            (assessment_id, org_id),
        ).fetchone()
        if row is None:
            raise api_error(404, "ASSESSMENT_NOT_FOUND", "Assessment not found", "The assessment was not found in this organization.")
        response.headers["Cache-Control"] = "no-store"
        return assessment_response(row)

    @app.patch(
        "/api/v1/organizations/{org_id}/assessments/{assessment_id}",
        response_model=AssessmentResponse,
        responses={
            401: problem_response("Authentication is required."),
            403: problem_response("A verified organization owner or admin is required to review and publish."),
            404: problem_response("The assessment was not found in this organization."),
            409: problem_response("The assessment is published or its version changed."),
            422: problem_response("The request is invalid."),
        },
    )
    def update_organization_assessment(
        org_id: uuid.UUID,
        assessment_id: uuid.UUID,
        body: AssessmentDraftUpdateRequest,
        request: Request,
        response: Response,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> AssessmentResponse:
        if not session.user.email_verified:
            raise api_error(403, "EMAIL_VERIFICATION_REQUIRED", "Verified email required", "Verify your email before authoring an assessment.")
        with connection.transaction():
            organization = organization_access(connection, session.user.id, org_id, for_update=True)
            if organization["member_role"] not in {"owner", "admin", "recruiter"}:
                raise api_error(403, "ASSESSMENT_AUTHORING_FORBIDDEN", "Access denied", "An organization recruiter role is required.")
            assessment = connection.execute(
                """SELECT status, version FROM organization_assessments
                   WHERE id = %s AND organization_id = %s FOR UPDATE""",
                (assessment_id, org_id),
            ).fetchone()
            if assessment is None:
                raise api_error(404, "ASSESSMENT_NOT_FOUND", "Assessment not found", "The assessment was not found in this organization.")
            if assessment["status"] != "draft":
                raise api_error(409, "ASSESSMENT_NOT_EDITABLE", "Assessment is immutable", "Published assessments cannot be edited.")
            if assessment["version"] != body.expected_version:
                raise api_error(409, "ASSESSMENT_VERSION_CONFLICT", "Assessment changed", "Reload the assessment and try again.")
            validate_assessment_competency_codes(connection, body.definition)
            connection.execute(
                """UPDATE organization_assessments
                   SET definition = %s, version = version + 1, updated_at = now()
                   WHERE id = %s AND organization_id = %s""",
                (Jsonb(body.definition.model_dump(mode="json")), assessment_id, org_id),
            )
            connection.execute(
                """INSERT INTO audit_events
                   (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome)
                   VALUES (%s, %s, 'user', %s, 'organization_assessment', %s, 'organization.assessment_updated', %s, 'success')""",
                (uuid.uuid4(), session.user.id, org_id, assessment_id, request.state.request_id),
            )
        row = connection.execute(
            f"{ASSESSMENT_SELECT} WHERE assessment.id = %s AND assessment.organization_id = %s",
            (assessment_id, org_id),
        ).fetchone()
        response.headers["Cache-Control"] = "no-store"
        return assessment_response(row)

    @app.post(
        "/api/v1/organizations/{org_id}/assessments/{assessment_id}/publish",
        response_model=AssessmentResponse,
        responses={
            401: problem_response("Authentication is required."),
            403: problem_response("A verified organization owner or admin is required to review and publish."),
            404: problem_response("The assessment was not found in this organization."),
            409: problem_response("The assessment cannot be published in its current state or version."),
        },
    )
    def publish_organization_assessment(
        org_id: uuid.UUID,
        assessment_id: uuid.UUID,
        body: AssessmentPublishRequest,
        request: Request,
        response: Response,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> AssessmentResponse:
        if not session.user.email_verified:
            raise api_error(403, "EMAIL_VERIFICATION_REQUIRED", "Verified email required", "Verify your email before publishing an assessment.")
        with connection.transaction():
            organization = organization_access(connection, session.user.id, org_id, for_update=True)
            if organization["member_role"] not in {"owner", "admin"}:
                raise api_error(403, "ASSESSMENT_REVIEW_FORBIDDEN", "Assessment review required", "An organization owner or admin must approve this assessment version.")
            if organization["onboarding_completed_at"] is None:
                raise api_error(409, "ORGANIZATION_ONBOARDING_REQUIRED", "Organization setup incomplete", "Complete organization setup before publishing assessments.")
            assessment = connection.execute(
                """SELECT status, version, definition FROM organization_assessments
                   WHERE id = %s AND organization_id = %s FOR UPDATE""",
                (assessment_id, org_id),
            ).fetchone()
            if assessment is None:
                raise api_error(404, "ASSESSMENT_NOT_FOUND", "Assessment not found", "The assessment was not found in this organization.")
            if assessment["status"] == "published":
                if body.expected_version != assessment["version"] - 1:
                    raise api_error(409, "ASSESSMENT_VERSION_CONFLICT", "Assessment changed", "Reload the assessment and try again.")
            elif assessment["status"] != "draft":
                raise api_error(409, "ASSESSMENT_INVALID_TRANSITION", "Assessment cannot be published", "Only a draft assessment can be published.")
            elif assessment["version"] != body.expected_version:
                raise api_error(409, "ASSESSMENT_VERSION_CONFLICT", "Assessment changed", "Reload the assessment and try again.")
            else:
                connection.execute(
                    """INSERT INTO organization_assessment_versions
                       (id, organization_id, assessment_id, version_number, definition, published_by_user_id)
                       VALUES (%s, %s, %s, 1, %s, %s)""",
                    (uuid.uuid4(), org_id, assessment_id, Jsonb(assessment["definition"]), session.user.id),
                )
                connection.execute(
                    """UPDATE organization_assessments SET status = 'published', published_at = now(),
                              version = version + 1, updated_at = now()
                       WHERE id = %s AND organization_id = %s""",
                    (assessment_id, org_id),
                )
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome, metadata)
                       VALUES (%s, %s, 'user', %s, 'organization_assessment', %s, 'organization.assessment_published', %s, 'success', %s)""",
                    (
                        uuid.uuid4(), session.user.id, org_id, assessment_id, request.state.request_id,
                        Jsonb({"review_confirmation": body.review_confirmation, "version_number": 1}),
                    ),
                )
        row = connection.execute(
            f"{ASSESSMENT_SELECT} WHERE assessment.id = %s AND assessment.organization_id = %s",
            (assessment_id, org_id),
        ).fetchone()
        response.headers["Cache-Control"] = "no-store"
        return assessment_response(row)

    @app.get(
        "/api/v1/organizations/{org_id}/assessment-invitations",
        response_model=OrganizationAssessmentInvitationListResponse,
        responses={401: problem_response("Authentication is required."), 403: problem_response("Hiring team access is required."), 404: problem_response("The organization was not found.")},
    )
    def list_organization_assessment_invitations(
        org_id: uuid.UUID,
        request: Request,
        response: Response,
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
        offset: Annotated[int, Query(ge=0, le=10000)] = 0,
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> OrganizationAssessmentInvitationListResponse:
        organization = organization_access(connection, session.user.id, org_id)
        if organization["member_role"] not in {"owner", "admin", "recruiter"}:
            raise api_error(403, "ASSESSMENT_ACCESS_FORBIDDEN", "Access denied", "A hiring team role is required to view invitations.")
        expire_assessment_invitations(connection, scope="organization_id", scope_id=org_id, request_id=request.state.request_id)
        rows = connection.execute(
            """SELECT invitation.id, invitation.application_id,
                      application.portfolio_snapshot->>'display_name' AS applicant_name,
                      application.job_snapshot->>'title' AS job_title,
                      version.definition->>'title' AS assessment_title,
                      invitation.assessment_version, invitation.status, invitation.version,
                      invitation.expires_at, invitation.created_at, invitation.responded_at
               FROM assessment_invitations AS invitation
               JOIN job_applications AS application ON application.id = invitation.application_id
               JOIN organization_assessment_versions AS version
                 ON version.assessment_id = invitation.assessment_id
                AND version.version_number = invitation.assessment_version
               WHERE invitation.organization_id = %s AND application.status <> 'withdrawn'
                 AND (%s OR EXISTS (SELECT 1 FROM application_reviewer_assignments AS assignment
                                    WHERE assignment.application_id = application.id AND assignment.reviewer_user_id = %s))
               ORDER BY invitation.created_at DESC, invitation.id LIMIT %s OFFSET %s""",
            (org_id, organization["member_role"] in {"owner", "admin"}, session.user.id, limit, offset),
        ).fetchall()
        response.headers["Cache-Control"] = "no-store"
        return OrganizationAssessmentInvitationListResponse(
            invitations=[OrganizationAssessmentInvitationItem(**row) for row in rows], limit=limit, offset=offset
        )

    @app.post(
        "/api/v1/organizations/{org_id}/assessment-invitations",
        response_model=OrganizationAssessmentInvitationItem,
        status_code=201,
        responses={
            200: {"model": OrganizationAssessmentInvitationItem, "description": "The matching idempotent invitation already exists."},
            401: problem_response("Authentication is required."),
            403: problem_response("A verified organization recruiter role is required."),
            404: problem_response("The organization, active application, or published assessment version was not found."),
            409: problem_response("The application changed, is ineligible, or already has this assessment version."),
            422: problem_response("The request is invalid."),
            429: problem_response("Too many requests."),
        },
    )
    def create_organization_assessment_invitation(
        org_id: uuid.UUID,
        body: AssessmentInvitationCreateRequest,
        request: Request,
        response: Response,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> OrganizationAssessmentInvitationItem:
        if not session.user.email_verified:
            raise api_error(403, "EMAIL_VERIFICATION_REQUIRED", "Verified email required", "Verify your email before inviting a candidate.")
        if not app.state.rate_limiter.allow(f"assessment-invite:user:{session.user.id}", 30, 900):
            raise api_error(429, "RATE_LIMITED", "Too many requests", "Try again later.")
        payload_hash = hash_token(
            "assessment-invitation:create:v1:" + ":".join((str(body.application_id), str(body.expected_application_version), str(body.assessment_id), str(body.assessment_version)))
        )
        with connection.transaction():
            organization = organization_access(connection, session.user.id, org_id, for_update=True)
            if organization["member_role"] not in {"owner", "admin", "recruiter"}:
                raise api_error(403, "ASSESSMENT_INVITE_FORBIDDEN", "Access denied", "An organization recruiter role is required.")
            if organization["onboarding_completed_at"] is None:
                raise api_error(409, "ORGANIZATION_ONBOARDING_REQUIRED", "Organization setup incomplete", "Complete organization setup before inviting candidates.")
            existing_request = connection.execute(
                """SELECT id, request_payload_hash FROM assessment_invitations
                   WHERE organization_id = %s AND request_key = %s""",
                (org_id, body.request_key),
            ).fetchone()
            if existing_request:
                if bytes(existing_request["request_payload_hash"]) != payload_hash:
                    raise api_error(409, "ASSESSMENT_INVITATION_KEY_REUSED", "Request conflict", "Use a new request key for a different invitation.")
                application = connection.execute(
                    "SELECT status FROM job_applications WHERE id = %s AND organization_id = %s",
                    (body.application_id, org_id),
                ).fetchone()
                if application is None or application["status"] == "withdrawn":
                    raise api_error(404, "APPLICATION_NOT_FOUND", "Application not found", "The active application was not found in this organization.")
                require_application_reviewer_access(
                    connection, org_id, body.application_id, session.user.id, organization["member_role"]
                )
                invitation_id = existing_request["id"]
                response.status_code = 200
            else:
                application = connection.execute(
                    """SELECT * FROM job_applications
                       WHERE id = %s AND organization_id = %s AND status <> 'withdrawn' FOR UPDATE""",
                    (body.application_id, org_id),
                ).fetchone()
                if application is None:
                    raise api_error(404, "APPLICATION_NOT_FOUND", "Application not found", "The active application was not found in this organization.")
                require_application_reviewer_access(
                    connection, org_id, body.application_id, session.user.id, organization["member_role"], lock=True
                )
                if application["version"] != body.expected_application_version:
                    raise api_error(409, "APPLICATION_VERSION_CONFLICT", "Application changed", "Reload the application and try again.")
                if application["status"] not in {"in_review", "assessment"}:
                    raise api_error(409, "APPLICATION_NOT_READY", "Application is not ready", "Move the application to review before sending an assessment invitation.")
                if not connection.execute(
                    """SELECT 1 FROM organization_assessment_versions AS version
                       JOIN organization_assessments AS assessment
                         ON assessment.id = version.assessment_id AND assessment.organization_id = version.organization_id
                       WHERE version.organization_id = %s AND version.assessment_id = %s
                         AND version.version_number = %s AND assessment.status = 'published'""",
                    (org_id, body.assessment_id, body.assessment_version),
                ).fetchone():
                    raise api_error(404, "ASSESSMENT_VERSION_NOT_FOUND", "Assessment version not found", "A published assessment version was not found in this organization.")
                duplicate = connection.execute(
                    """SELECT 1 FROM assessment_invitations
                       WHERE application_id = %s AND assessment_id = %s AND assessment_version = %s""",
                    (body.application_id, body.assessment_id, body.assessment_version),
                ).fetchone()
                if duplicate:
                    raise api_error(409, "ASSESSMENT_ALREADY_ASSIGNED", "Assessment already assigned", "This published assessment version was already assigned to the application.")
                inserted = connection.execute(
                    """INSERT INTO assessment_invitations
                       (id, organization_id, application_id, candidate_user_id, assessment_id,
                        assessment_version, invited_by_user_id, request_key, request_payload_hash, expires_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now() + interval '14 days')
                       ON CONFLICT DO NOTHING RETURNING id""",
                    (
                        uuid.uuid4(), org_id, body.application_id, application["user_id"], body.assessment_id,
                        body.assessment_version, session.user.id, body.request_key, payload_hash,
                    ),
                ).fetchone()
                if inserted is None:
                    existing_request = connection.execute(
                        """SELECT id, request_payload_hash FROM assessment_invitations
                           WHERE organization_id = %s AND request_key = %s""",
                        (org_id, body.request_key),
                    ).fetchone()
                    if existing_request and bytes(existing_request["request_payload_hash"]) == payload_hash:
                        invitation_id = existing_request["id"]
                        response.status_code = 200
                    elif connection.execute(
                        """SELECT 1 FROM assessment_invitations
                           WHERE application_id = %s AND assessment_id = %s AND assessment_version = %s""",
                        (body.application_id, body.assessment_id, body.assessment_version),
                    ).fetchone():
                        raise api_error(409, "ASSESSMENT_ALREADY_ASSIGNED", "Assessment already assigned", "This published assessment version was already assigned to the application.")
                    else:
                        raise api_error(409, "ASSESSMENT_INVITATION_KEY_REUSED", "Request conflict", "Use a new request key for a different invitation.")
                else:
                    invitation_id = inserted["id"]
                    if application["status"] == "in_review":
                        connection.execute(
                            "UPDATE job_applications SET status = 'assessment', version = version + 1, updated_at = now() WHERE id = %s",
                            (body.application_id,),
                        )
                        connection.execute(
                            """INSERT INTO application_events (id, application_id, actor_id, actor_type, action, from_status, to_status)
                               VALUES (%s, %s, %s, 'organization', 'stage_changed', 'in_review', 'assessment')""",
                            (uuid.uuid4(), body.application_id, session.user.id),
                        )
                        connection.execute(
                            """INSERT INTO audit_events
                               (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome)
                               VALUES (%s, %s, 'user', %s, 'job_application', %s, 'organization.application_stage_changed', %s, 'success')""",
                            (uuid.uuid4(), session.user.id, org_id, body.application_id, request.state.request_id),
                        )
                    connection.execute(
                        """INSERT INTO audit_events
                           (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome, metadata)
                           VALUES (%s, %s, 'user', %s, 'assessment_invitation', %s, 'organization.assessment_invitation_created', %s, 'success', %s)""",
                        (
                            uuid.uuid4(), session.user.id, org_id, invitation_id, request.state.request_id,
                            Jsonb({"application_id": str(body.application_id), "assessment_id": str(body.assessment_id), "assessment_version": body.assessment_version}),
                        ),
                    )
        row = connection.execute(
            """SELECT invitation.id, invitation.application_id,
                      application.portfolio_snapshot->>'display_name' AS applicant_name,
                      application.job_snapshot->>'title' AS job_title,
                      version.definition->>'title' AS assessment_title,
                      invitation.assessment_version, invitation.status, invitation.version,
                      invitation.expires_at, invitation.created_at, invitation.responded_at
               FROM assessment_invitations AS invitation
               JOIN job_applications AS application ON application.id = invitation.application_id
               JOIN organization_assessment_versions AS version
                 ON version.assessment_id = invitation.assessment_id AND version.version_number = invitation.assessment_version
               WHERE invitation.id = %s AND invitation.organization_id = %s AND application.status <> 'withdrawn'""",
            (invitation_id, org_id),
        ).fetchone()
        response.headers["Cache-Control"] = "no-store"
        return OrganizationAssessmentInvitationItem(**row)

    @app.post(
        "/api/v1/organizations/{org_id}/assessment-invitations/{invite_id}/cancel",
        response_model=OrganizationAssessmentInvitationItem,
        responses={401: problem_response("Authentication is required."), 403: problem_response("Recruiter access is required."), 404: problem_response("The active invitation was not found in this organization."), 409: problem_response("The invitation changed or is no longer cancellable."), 422: problem_response("The request is invalid.")},
    )
    def cancel_organization_assessment_invitation(
        org_id: uuid.UUID,
        invite_id: uuid.UUID,
        body: AssessmentInvitationCancelRequest,
        request: Request,
        response: Response,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> OrganizationAssessmentInvitationItem:
        organization = organization_access(connection, session.user.id, org_id)
        if organization["member_role"] not in {"owner", "admin", "recruiter"}:
            raise api_error(403, "ASSESSMENT_INVITE_FORBIDDEN", "Access denied", "An organization recruiter role is required.")
        with connection.transaction():
            invitation = connection.execute(
                """SELECT invitation.*, application.status AS application_status,
                          invitation.expires_at <= now() AS is_expired
                   FROM assessment_invitations AS invitation
                   JOIN job_applications AS application ON application.id = invitation.application_id
                   WHERE invitation.id = %s AND invitation.organization_id = %s
                     AND application.status <> 'withdrawn' FOR UPDATE OF invitation""",
                (invite_id, org_id),
            ).fetchone()
            if invitation is None:
                raise api_error(404, "ASSESSMENT_INVITATION_NOT_FOUND", "Invitation not found", "The active invitation was not found in this organization.")
            require_application_reviewer_access(
                connection, org_id, invitation["application_id"], session.user.id, organization["member_role"], lock=True
            )
            if invitation["status"] == "issued" and invitation["is_expired"]:
                raise api_error(409, "ASSESSMENT_INVITATION_EXPIRED", "Invitation expired", "This assessment invitation is no longer available.")
            if invitation["status"] == "cancelled" and invitation["version"] == body.expected_version + 1:
                pass
            elif invitation["status"] != "issued" or invitation["version"] != body.expected_version:
                raise api_error(409, "ASSESSMENT_INVITATION_STATE_CONFLICT", "Invitation changed", "Only a current, pending invitation can be cancelled.")
            else:
                connection.execute(
                    """UPDATE assessment_invitations SET status = 'cancelled', cancelled_by_user_id = %s,
                              cancelled_at = now(), cancellation_reason = %s, version = version + 1, updated_at = now()
                       WHERE id = %s""",
                    (session.user.id, body.reason, invite_id),
                )
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome, metadata)
                       VALUES (%s, %s, 'user', %s, 'assessment_invitation', %s, 'organization.assessment_invitation_cancelled', %s, 'success', %s)""",
                    (uuid.uuid4(), session.user.id, org_id, invite_id, request.state.request_id, Jsonb({"reason": body.reason})),
                )
        row = connection.execute(
            """SELECT invitation.id, invitation.application_id,
                      application.portfolio_snapshot->>'display_name' AS applicant_name,
                      application.job_snapshot->>'title' AS job_title,
                      version.definition->>'title' AS assessment_title,
                      invitation.assessment_version, invitation.status, invitation.version,
                      invitation.expires_at, invitation.created_at, invitation.responded_at
               FROM assessment_invitations AS invitation
               JOIN job_applications AS application ON application.id = invitation.application_id
               JOIN organization_assessment_versions AS version
                 ON version.assessment_id = invitation.assessment_id AND version.version_number = invitation.assessment_version
               WHERE invitation.id = %s AND invitation.organization_id = %s""",
            (invite_id, org_id),
        ).fetchone()
        response.headers["Cache-Control"] = "no-store"
        return OrganizationAssessmentInvitationItem(**row)

    @app.get(
        "/api/v1/me/assessment-invitations",
        response_model=CandidateAssessmentInvitationListResponse,
        responses={401: problem_response("Authentication is required."), 403: problem_response("A candidate workspace is required.")},
    )
    def list_my_assessment_invitations(
        request: Request,
        response: Response,
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
        offset: Annotated[int, Query(ge=0, le=10000)] = 0,
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> CandidateAssessmentInvitationListResponse:
        if session.user.initial_purpose != "candidate" and not connection.execute(
            "SELECT 1 FROM candidate_onboardings WHERE user_id = %s", (session.user.id,)
        ).fetchone():
            raise api_error(403, "CANDIDATE_WORKSPACE_REQUIRED", "Candidate workspace required", "Open your candidate workspace to view assessment invitations.")
        expire_assessment_invitations(connection, scope="candidate_user_id", scope_id=session.user.id, request_id=request.state.request_id)
        rows = connection.execute(
            """SELECT invitation.id, invitation.application_id,
                      organization.name AS organization_name, application.job_snapshot->>'title' AS job_title,
                      version.definition->>'title' AS assessment_title, invitation.assessment_version,
                      invitation.status, invitation.version, invitation.expires_at, invitation.created_at,
                      invitation.responded_at
               FROM assessment_invitations AS invitation
               JOIN organizations AS organization ON organization.id = invitation.organization_id
               JOIN job_applications AS application ON application.id = invitation.application_id
               JOIN organization_assessment_versions AS version
                 ON version.assessment_id = invitation.assessment_id AND version.version_number = invitation.assessment_version
               WHERE invitation.candidate_user_id = %s
               ORDER BY invitation.created_at DESC, invitation.id LIMIT %s OFFSET %s""",
            (session.user.id, limit, offset),
        ).fetchall()
        response.headers["Cache-Control"] = "no-store"
        return CandidateAssessmentInvitationListResponse(
            invitations=[CandidateAssessmentInvitationListItem(**row) for row in rows], limit=limit, offset=offset
        )

    @app.get(
        "/api/v1/me/assessment-invitations/{invite_id}",
        response_model=CandidateAssessmentInvitationResponse,
        responses={401: problem_response("Authentication is required."), 403: problem_response("A candidate workspace is required."), 404: problem_response("The invitation was not found.")},
    )
    def get_my_assessment_invitation(
        invite_id: uuid.UUID,
        request: Request,
        response: Response,
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> CandidateAssessmentInvitationResponse:
        if session.user.initial_purpose != "candidate" and not connection.execute(
            "SELECT 1 FROM candidate_onboardings WHERE user_id = %s", (session.user.id,)
        ).fetchone():
            raise api_error(403, "CANDIDATE_WORKSPACE_REQUIRED", "Candidate workspace required", "Open your candidate workspace to view assessment invitations.")
        expire_assessment_invitations(connection, scope="candidate_user_id", scope_id=session.user.id, request_id=request.state.request_id)
        row = connection.execute(
            """SELECT invitation.id, invitation.application_id,
                      organization.name AS organization_name, application.job_snapshot->>'title' AS job_title,
                      version.definition->>'title' AS assessment_title, invitation.assessment_version,
                      invitation.status, invitation.version, invitation.expires_at, invitation.created_at,
                      invitation.responded_at, invitation.consent_version, invitation.consented_at,
                      version.definition AS assessment_definition
               FROM assessment_invitations AS invitation
               JOIN organizations AS organization ON organization.id = invitation.organization_id
               JOIN job_applications AS application ON application.id = invitation.application_id
               JOIN organization_assessment_versions AS version
                 ON version.assessment_id = invitation.assessment_id AND version.version_number = invitation.assessment_version
               WHERE invitation.id = %s AND invitation.candidate_user_id = %s""",
            (invite_id, session.user.id),
        ).fetchone()
        if row is None:
            raise api_error(404, "ASSESSMENT_INVITATION_NOT_FOUND", "Invitation not found", "The invitation was not found.")
        response.headers["Cache-Control"] = "no-store"
        return candidate_assessment_invitation_response(row)

    @app.post(
        "/api/v1/me/assessment-invitations/{invite_id}/respond",
        response_model=CandidateAssessmentInvitationResponse,
        responses={401: problem_response("Authentication is required."), 403: problem_response("Candidate consent and CSRF validation are required."), 404: problem_response("The invitation was not found."), 409: problem_response("The invitation is expired or its version changed."), 422: problem_response("The request is invalid.")},
    )
    def respond_to_assessment_invitation(
        invite_id: uuid.UUID,
        body: AssessmentInvitationRespondRequest,
        request: Request,
        response: Response,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> CandidateAssessmentInvitationResponse:
        if session.user.initial_purpose != "candidate" and not connection.execute(
            "SELECT 1 FROM candidate_onboardings WHERE user_id = %s", (session.user.id,)
        ).fetchone():
            raise api_error(403, "CANDIDATE_WORKSPACE_REQUIRED", "Candidate workspace required", "Open your candidate workspace to respond to invitations.")
        with connection.transaction():
            invitation = connection.execute(
                """SELECT *, expires_at <= now() AS is_expired FROM assessment_invitations
                   WHERE id = %s AND candidate_user_id = %s FOR UPDATE""",
                (invite_id, session.user.id),
            ).fetchone()
            if invitation is None:
                raise api_error(404, "ASSESSMENT_INVITATION_NOT_FOUND", "Invitation not found", "The invitation was not found.")
            if invitation["status"] in {"accepted", "declined"} and invitation["response_from_version"] == body.expected_version:
                if invitation["status"] == ("accepted" if body.action == "accept" else "declined"):
                    pass
                else:
                    raise api_error(409, "ASSESSMENT_INVITATION_STATE_CONFLICT", "Invitation changed", "This invitation already has a different response.")
            elif invitation["status"] == "expired":
                raise api_error(409, "ASSESSMENT_INVITATION_EXPIRED", "Invitation expired", "This assessment invitation is no longer available.")
            elif invitation["status"] != "issued":
                raise api_error(409, "ASSESSMENT_INVITATION_STATE_CONFLICT", "Invitation changed", "This invitation is no longer awaiting a response.")
            elif invitation["is_expired"]:
                raise api_error(409, "ASSESSMENT_INVITATION_EXPIRED", "Invitation expired", "This assessment invitation is no longer available.")
            elif invitation["version"] != body.expected_version:
                raise api_error(409, "ASSESSMENT_INVITATION_VERSION_CONFLICT", "Invitation changed", "Reload the invitation and try again.")
            else:
                new_status = "accepted" if body.action == "accept" else "declined"
                connection.execute(
                    """UPDATE assessment_invitations
                       SET status = %s, response_from_version = %s, responded_at = now(),
                           consent_version = %s, consented_at = CASE WHEN %s = 'accepted' THEN now() ELSE NULL END,
                           version = version + 1, updated_at = now()
                       WHERE id = %s""",
                    (new_status, body.expected_version, body.consent_version, new_status, invite_id),
                )
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome, metadata)
                       VALUES (%s, %s, 'user', %s, 'assessment_invitation', %s, %s, %s, 'success', %s)""",
                    (
                        uuid.uuid4(), session.user.id, invitation["organization_id"], invite_id,
                        f"candidate.assessment_invitation_{new_status}", request.state.request_id,
                        Jsonb({"consent_version": body.consent_version}),
                    ),
                )
        row = connection.execute(
            """SELECT invitation.id, invitation.application_id,
                      organization.name AS organization_name, application.job_snapshot->>'title' AS job_title,
                      version.definition->>'title' AS assessment_title, invitation.assessment_version,
                      invitation.status, invitation.version, invitation.expires_at, invitation.created_at,
                      invitation.responded_at, invitation.consent_version, invitation.consented_at,
                      version.definition AS assessment_definition
               FROM assessment_invitations AS invitation
               JOIN organizations AS organization ON organization.id = invitation.organization_id
               JOIN job_applications AS application ON application.id = invitation.application_id
               JOIN organization_assessment_versions AS version
                 ON version.assessment_id = invitation.assessment_id AND version.version_number = invitation.assessment_version
               WHERE invitation.id = %s AND invitation.candidate_user_id = %s""",
            (invite_id, session.user.id),
        ).fetchone()
        response.headers["Cache-Control"] = "no-store"
        return candidate_assessment_invitation_response(row)

    @app.post(
        "/api/v1/assessment-invitations/{invite_id}/attempts",
        status_code=201,
        response_model=AssessmentAttemptItem,
        responses={200: {"model": AssessmentAttemptItem, "description": "The matching idempotent attempt already exists."}, 401: problem_response("Authentication is required."), 403: problem_response("A candidate workspace and CSRF validation are required."), 404: problem_response("The accepted invitation was not found."), 409: problem_response("The invitation or application is not ready, or an active attempt already exists."), 422: problem_response("The request is invalid."), 429: problem_response("Too many requests.")},
    )
    def create_assessment_attempt(
        invite_id: uuid.UUID,
        body: AssessmentAttemptCreateRequest,
        request: Request,
        response: Response,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> AssessmentAttemptItem:
        if session.user.initial_purpose != "candidate" and not connection.execute(
            "SELECT 1 FROM candidate_onboardings WHERE user_id = %s", (session.user.id,)
        ).fetchone():
            raise api_error(403, "CANDIDATE_WORKSPACE_REQUIRED", "Candidate workspace required", "Open your candidate workspace to create an assessment attempt.")
        if not app.state.rate_limiter.allow(f"assessment-attempt:user:{session.user.id}", 10, 900):
            raise api_error(429, "RATE_LIMITED", "Too many requests", "Try again later.")
        payload_hash = hash_token(f"assessment-attempt:create:v1:{invite_id}:{body.expected_invitation_version}")
        attempt_id = None
        with connection.transaction():
            previous = connection.execute(
                """SELECT id, invitation_id, request_payload_hash FROM assessment_attempts
                   WHERE candidate_user_id = %s AND request_key = %s FOR UPDATE""",
                (session.user.id, body.request_key),
            ).fetchone()
            if previous:
                if previous["invitation_id"] != invite_id or bytes(previous["request_payload_hash"]) != payload_hash:
                    raise api_error(409, "ASSESSMENT_ATTEMPT_KEY_REUSED", "Request conflict", "Use a new request key for a different attempt request.")
                attempt_id = previous["id"]
                response.status_code = 200
            else:
                invitation = connection.execute(
                    """SELECT invitation.organization_id, invitation.candidate_user_id,
                              invitation.assessment_id, invitation.assessment_version,
                              invitation.status AS invitation_status, invitation.version AS invitation_version,
                              application.status AS application_status
                       FROM assessment_invitations AS invitation
                       JOIN job_applications AS application ON application.id = invitation.application_id
                       WHERE invitation.id = %s AND invitation.candidate_user_id = %s
                       FOR UPDATE OF application""",
                    (invite_id, session.user.id),
                ).fetchone()
                if invitation is None:
                    raise api_error(404, "ASSESSMENT_INVITATION_NOT_FOUND", "Invitation not found", "The accepted invitation was not found.")
                previous = connection.execute(
                    """SELECT id, invitation_id, request_payload_hash FROM assessment_attempts
                       WHERE candidate_user_id = %s AND request_key = %s FOR UPDATE""",
                    (session.user.id, body.request_key),
                ).fetchone()
                if previous:
                    if previous["invitation_id"] != invite_id or bytes(previous["request_payload_hash"]) != payload_hash:
                        raise api_error(409, "ASSESSMENT_ATTEMPT_KEY_REUSED", "Request conflict", "Use a new request key for a different attempt request.")
                    attempt_id = previous["id"]
                    response.status_code = 200
                else:
                    if invitation["invitation_status"] != "accepted" or invitation["application_status"] != "assessment":
                        raise api_error(409, "ASSESSMENT_ATTEMPT_NOT_READY", "Attempt unavailable", "An attempt requires an accepted invitation on an application in the assessment stage.")
                    if invitation["invitation_version"] != body.expected_invitation_version:
                        raise api_error(409, "ASSESSMENT_INVITATION_VERSION_CONFLICT", "Invitation changed", "Reload the invitation and try again.")
                    active = connection.execute(
                        """SELECT id FROM assessment_attempts WHERE invitation_id = %s
                           AND status IN ('created', 'consented', 'ready', 'active', 'paused', 'submitted', 'processing')""",
                        (invite_id,),
                    ).fetchone()
                    if active:
                        raise api_error(409, "ASSESSMENT_ATTEMPT_ALREADY_ACTIVE", "Attempt already exists", "This invitation already has an active attempt.")
                    inserted = connection.execute(
                        """INSERT INTO assessment_attempts
                           (id, organization_id, invitation_id, candidate_user_id, assessment_id,
                            assessment_version, request_key, request_payload_hash)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT DO NOTHING RETURNING id""",
                        (
                            uuid.uuid4(), invitation["organization_id"], invite_id, session.user.id,
                            invitation["assessment_id"], invitation["assessment_version"], body.request_key, payload_hash,
                        ),
                    ).fetchone()
                    if inserted is None:
                        previous = connection.execute(
                            """SELECT id, invitation_id, request_payload_hash FROM assessment_attempts
                               WHERE candidate_user_id = %s AND request_key = %s""",
                            (session.user.id, body.request_key),
                        ).fetchone()
                        if previous and previous["invitation_id"] == invite_id and bytes(previous["request_payload_hash"]) == payload_hash:
                            attempt_id = previous["id"]
                            response.status_code = 200
                        else:
                            raise api_error(409, "ASSESSMENT_ATTEMPT_ALREADY_ACTIVE", "Attempt already exists", "This invitation already has an active attempt or the request key was reused.")
                    else:
                        attempt_id = inserted["id"]
                        connection.execute(
                            """INSERT INTO audit_events
                               (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome)
                               VALUES (%s, %s, 'user', %s, 'assessment_attempt', %s, 'candidate.assessment_attempt_created', %s, 'success')""",
                            (uuid.uuid4(), session.user.id, invitation["organization_id"], attempt_id, request.state.request_id),
                        )
        row = connection.execute(
            """SELECT attempt.id, attempt.invitation_id, organization.name AS organization_name,
                      application.job_snapshot->>'title' AS job_title,
                      version.definition->>'title' AS assessment_title, attempt.assessment_version,
                      attempt.status, attempt.version,
                      (attempt.status = 'created') AS requires_consent,
                      CASE WHEN attempt.status = 'created' THEN 'prepare' ELSE 'none' END AS next_action,
                      attempt.created_at, attempt.updated_at
               FROM assessment_attempts AS attempt
               JOIN assessment_invitations AS invitation ON invitation.id = attempt.invitation_id
               JOIN organizations AS organization ON organization.id = attempt.organization_id
               JOIN job_applications AS application ON application.id = invitation.application_id
               JOIN organization_assessment_versions AS version
                 ON version.assessment_id = attempt.assessment_id AND version.version_number = attempt.assessment_version
               WHERE attempt.id = %s AND attempt.candidate_user_id = %s""",
            (attempt_id, session.user.id),
        ).fetchone()
        response.headers["Cache-Control"] = "no-store"
        return AssessmentAttemptItem(**row)

    @app.get(
        "/api/v1/me/attempts",
        response_model=AssessmentAttemptListResponse,
        responses={401: problem_response("Authentication is required."), 403: problem_response("A candidate workspace is required.")},
    )
    def list_my_assessment_attempts(
        response: Response,
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
        offset: Annotated[int, Query(ge=0, le=10000)] = 0,
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> AssessmentAttemptListResponse:
        if session.user.initial_purpose != "candidate" and not connection.execute(
            "SELECT 1 FROM candidate_onboardings WHERE user_id = %s", (session.user.id,)
        ).fetchone():
            raise api_error(403, "CANDIDATE_WORKSPACE_REQUIRED", "Candidate workspace required", "Open your candidate workspace to view assessment attempts.")
        rows = connection.execute(
            """SELECT attempt.id, attempt.invitation_id, organization.name AS organization_name,
                      application.job_snapshot->>'title' AS job_title,
                      version.definition->>'title' AS assessment_title, attempt.assessment_version,
                      attempt.status, attempt.version,
                      (attempt.status = 'created') AS requires_consent,
                      CASE WHEN attempt.status = 'created' THEN 'prepare' ELSE 'none' END AS next_action,
                      attempt.created_at, attempt.updated_at
               FROM assessment_attempts AS attempt
               JOIN assessment_invitations AS invitation ON invitation.id = attempt.invitation_id
               JOIN organizations AS organization ON organization.id = attempt.organization_id
               JOIN job_applications AS application ON application.id = invitation.application_id
               JOIN organization_assessment_versions AS version
                 ON version.assessment_id = attempt.assessment_id AND version.version_number = attempt.assessment_version
               WHERE attempt.candidate_user_id = %s
               ORDER BY attempt.created_at DESC, attempt.id LIMIT %s OFFSET %s""",
            (session.user.id, limit, offset),
        ).fetchall()
        response.headers["Cache-Control"] = "no-store"
        return AssessmentAttemptListResponse(attempts=[AssessmentAttemptItem(**row) for row in rows], limit=limit, offset=offset)

    @app.get(
        "/api/v1/attempts/{attempt_id}",
        response_model=AssessmentAttemptItem,
        responses={401: problem_response("Authentication is required."), 404: problem_response("The attempt was not found.")},
    )
    def get_assessment_attempt(
        attempt_id: uuid.UUID,
        response: Response,
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> AssessmentAttemptItem:
        row = connection.execute(
            """SELECT attempt.id, attempt.invitation_id, organization.name AS organization_name,
                      application.job_snapshot->>'title' AS job_title,
                      version.definition->>'title' AS assessment_title, attempt.assessment_version,
                      attempt.status, attempt.version,
                      (attempt.status = 'created') AS requires_consent,
                      CASE WHEN attempt.status = 'created' THEN 'prepare' ELSE 'none' END AS next_action,
                      attempt.created_at, attempt.updated_at
               FROM assessment_attempts AS attempt
               JOIN assessment_invitations AS invitation ON invitation.id = attempt.invitation_id
               JOIN organizations AS organization ON organization.id = attempt.organization_id
               JOIN job_applications AS application ON application.id = invitation.application_id
               JOIN organization_assessment_versions AS version
                 ON version.assessment_id = attempt.assessment_id AND version.version_number = attempt.assessment_version
               WHERE attempt.id = %s AND attempt.candidate_user_id = %s""",
            (attempt_id, session.user.id),
        ).fetchone()
        if row is None:
            raise api_error(404, "ASSESSMENT_ATTEMPT_NOT_FOUND", "Attempt not found", "The attempt was not found.")
        response.headers["Cache-Control"] = "no-store"
        return AssessmentAttemptItem(**row)

    @app.get(
        "/api/v1/attempts/{attempt_id}/readiness",
        response_model=AssessmentAttemptReadinessResponse,
        summary="Read consent and isolated-runtime readiness for a candidate attempt",
        description="Only the candidate who owns the attempt may read this private status. Attempt consent is versioned; start remains blocked until a separately isolated execution runtime is available.",
        responses={401: problem_response("Authentication is required."), 404: problem_response("The attempt was not found.")},
    )
    def get_assessment_attempt_readiness(
        attempt_id: uuid.UUID,
        response: Response,
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> AssessmentAttemptReadinessResponse:
        attempt = connection.execute(
            """SELECT attempt.status, version.definition
               FROM assessment_attempts AS attempt
               JOIN organization_assessment_versions AS version
                 ON version.assessment_id = attempt.assessment_id
                AND version.version_number = attempt.assessment_version
               WHERE attempt.id = %s AND attempt.candidate_user_id = %s""",
            (attempt_id, session.user.id),
        ).fetchone()
        if attempt is None:
            raise api_error(404, "ASSESSMENT_ATTEMPT_NOT_FOUND", "Attempt not found", "The attempt was not found.")
        blockers = []
        suite = AssessmentDefinition.model_validate(attempt["definition"]).python_test_suite
        if suite is None:
            blockers.append("ASSESSMENT_TEST_SUITE_UNAVAILABLE")
        runner = app.state.assessment_runner
        execution_available = bool(runner and runner.available())
        if not execution_available:
            blockers.append("ISOLATED_RUNTIME_UNAVAILABLE")
        if attempt["status"] == "created":
            blockers.insert(0, "ASSESSMENT_CONSENT_REQUIRED")
        if attempt["status"] == "cancelled":
            blockers.insert(0, "ATTEMPT_CANCELLED")
        response.headers["Cache-Control"] = "no-store"
        return AssessmentAttemptReadinessResponse(
            attempt_id=attempt_id,
            status=attempt["status"],
            can_start=attempt["status"] == "consented" and not blockers,
            requires_consent=attempt["status"] == "created",
            execution_available=execution_available,
            recording_enabled=False,
            blockers=blockers,
        )

    def assessment_workspace_payload(attempt, workspace=None) -> AssessmentWorkspaceResponse:
        definition = AssessmentDefinition.model_validate(attempt["definition"])
        return AssessmentWorkspaceResponse(
            attempt_id=attempt["id"], status=attempt["status"],
            version=workspace["version"] if workspace else 0,
            source_code=workspace["source_code"] if workspace else "",
            assessment_title=definition.title, task_instructions=definition.task_instructions,
            python_entrypoint=definition.python_test_suite.entrypoint if definition.python_test_suite else None,
        )

    @app.get(
        "/api/v1/attempts/{attempt_id}/workspace",
        response_model=AssessmentWorkspaceResponse,
        summary="Read the candidate's private assessment workspace",
        responses={401: problem_response("Authentication is required."), 404: problem_response("The attempt was not found."), 409: problem_response("The attempt is not ready for a workspace.")},
    )
    def get_assessment_workspace(
        attempt_id: uuid.UUID,
        response: Response,
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> AssessmentWorkspaceResponse:
        attempt = connection.execute(
            """SELECT attempt.id, attempt.status, version.definition
               FROM assessment_attempts AS attempt
               JOIN organization_assessment_versions AS version
                 ON version.assessment_id = attempt.assessment_id
                AND version.version_number = attempt.assessment_version
               WHERE attempt.id = %s AND attempt.candidate_user_id = %s""",
            (attempt_id, session.user.id),
        ).fetchone()
        if attempt is None:
            raise api_error(404, "ASSESSMENT_ATTEMPT_NOT_FOUND", "Attempt not found", "The attempt was not found.")
        if attempt["status"] not in {"consented", "active"}:
            raise api_error(409, "ASSESSMENT_WORKSPACE_UNAVAILABLE", "Workspace unavailable", "Accept the assessment consent and check readiness before opening the workspace.")
        if AssessmentDefinition.model_validate(attempt["definition"]).python_test_suite is None:
            raise api_error(409, "ASSESSMENT_TEST_SUITE_UNAVAILABLE", "Test suite unavailable", "This assessment does not have a deterministic Python test suite.")
        workspace = connection.execute(
            "SELECT version, source_code FROM assessment_attempt_workspaces WHERE attempt_id = %s AND candidate_user_id = %s",
            (attempt_id, session.user.id),
        ).fetchone()
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return assessment_workspace_payload(attempt, workspace)

    @app.put(
        "/api/v1/attempts/{attempt_id}/workspace",
        response_model=AssessmentWorkspaceResponse,
        summary="Save the candidate's private Python assessment solution",
        responses={401: problem_response("Authentication is required."), 403: problem_response("CSRF validation is required."), 404: problem_response("The attempt was not found."), 409: problem_response("The workspace changed or is unavailable."), 422: problem_response("The request is invalid."), 503: problem_response("The isolated runtime is unavailable.")},
    )
    def save_assessment_workspace(
        attempt_id: uuid.UUID,
        body: AssessmentWorkspaceUpdateRequest,
        request: Request,
        response: Response,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> AssessmentWorkspaceResponse:
        runner = app.state.assessment_runner
        with connection.transaction():
            attempt = connection.execute(
                """SELECT attempt.id, attempt.status, attempt.organization_id, version.definition
                   FROM assessment_attempts AS attempt
                   JOIN organization_assessment_versions AS version
                     ON version.assessment_id = attempt.assessment_id
                    AND version.version_number = attempt.assessment_version
                   WHERE attempt.id = %s AND attempt.candidate_user_id = %s FOR UPDATE OF attempt""",
                (attempt_id, session.user.id),
            ).fetchone()
            if attempt is None:
                raise api_error(404, "ASSESSMENT_ATTEMPT_NOT_FOUND", "Attempt not found", "The attempt was not found.")
            if attempt["status"] not in {"consented", "active"}:
                raise api_error(409, "ASSESSMENT_WORKSPACE_UNAVAILABLE", "Workspace unavailable", "This attempt cannot be changed.")
            if AssessmentDefinition.model_validate(attempt["definition"]).python_test_suite is None:
                raise api_error(409, "ASSESSMENT_TEST_SUITE_UNAVAILABLE", "Test suite unavailable", "This assessment does not have a deterministic Python test suite.")
            if not runner or not runner.available():
                raise api_error(503, "ASSESSMENT_RUNTIME_UNAVAILABLE", "Runtime unavailable", "The isolated code execution runtime is not available.")
            workspace = connection.execute(
                "SELECT version, source_code FROM assessment_attempt_workspaces WHERE attempt_id = %s FOR UPDATE",
                (attempt_id,),
            ).fetchone()
            current_version = workspace["version"] if workspace else 0
            if current_version != body.expected_version:
                raise api_error(409, "ASSESSMENT_WORKSPACE_VERSION_CONFLICT", "Workspace changed", "Reload the workspace before saving your changes.")
            if workspace is None:
                saved = connection.execute(
                    """INSERT INTO assessment_attempt_workspaces (attempt_id, candidate_user_id, source_code)
                       VALUES (%s, %s, %s) RETURNING version, source_code""",
                    (attempt_id, session.user.id, body.source_code),
                ).fetchone()
            elif workspace["source_code"] == body.source_code:
                saved = workspace
            else:
                saved = connection.execute(
                    """UPDATE assessment_attempt_workspaces SET source_code = %s, version = version + 1, updated_at = now()
                       WHERE attempt_id = %s RETURNING version, source_code""",
                    (body.source_code, attempt_id),
                ).fetchone()
            if attempt["status"] == "consented":
                connection.execute(
                    "UPDATE assessment_attempts SET status = 'active', version = version + 1, updated_at = now() WHERE id = %s",
                    (attempt_id,),
                )
            if workspace is None or workspace["source_code"] != body.source_code:
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome)
                       VALUES (%s, %s, 'user', %s, 'assessment_attempt_workspace', %s, 'candidate.assessment_workspace_saved', %s, 'success')""",
                    (uuid.uuid4(), session.user.id, attempt["organization_id"], attempt_id, request.state.request_id),
                )
            attempt["status"] = "active" if attempt["status"] == "consented" else attempt["status"]
            result = assessment_workspace_payload(attempt, saved)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return result

    @app.post(
        "/api/v1/attempts/{attempt_id}/test-runs",
        response_model=AssessmentTestRunResponse,
        status_code=201,
        summary="Execute a saved assessment solution against its private deterministic suite",
        description="The candidate never supplies commands or test expectations. Each test case runs in a fresh rootless Podman container without network, host mounts, credentials, or writeable root; responses contain per-competency counts only.",
        responses={401: problem_response("Authentication is required."), 403: problem_response("CSRF validation is required."), 404: problem_response("The attempt was not found."), 409: problem_response("The attempt or workspace changed."), 422: problem_response("The request is invalid."), 429: problem_response("The assessment run limit was reached."), 503: problem_response("The isolated runtime is unavailable.")},
    )
    def create_assessment_test_run(
        attempt_id: uuid.UUID,
        body: AssessmentTestRunRequest,
        request: Request,
        response: Response,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> AssessmentTestRunResponse:
        runner = app.state.assessment_runner
        payload_hash = hashlib.sha256(str(body.expected_workspace_version).encode()).digest()
        run_id = None
        code = None
        suite = None
        proficiency_levels = None
        with connection.transaction():
            attempt = connection.execute(
                """SELECT attempt.status, attempt.organization_id, version.definition
                   FROM assessment_attempts AS attempt
                   JOIN organization_assessment_versions AS version
                     ON version.assessment_id = attempt.assessment_id
                    AND version.version_number = attempt.assessment_version
                   WHERE attempt.id = %s AND attempt.candidate_user_id = %s FOR UPDATE OF attempt""",
                (attempt_id, session.user.id),
            ).fetchone()
            if attempt is None:
                raise api_error(404, "ASSESSMENT_ATTEMPT_NOT_FOUND", "Attempt not found", "The attempt was not found.")
            connection.execute(
                """UPDATE assessment_test_runs SET status = 'infrastructure_failed', completed_at = now()
                   WHERE attempt_id = %s AND status = 'running' AND created_at < now() - interval '2 minutes'""",
                (attempt_id,),
            )
            existing = connection.execute(
                "SELECT * FROM assessment_test_runs WHERE attempt_id = %s AND request_key = %s",
                (attempt_id, body.request_key),
            ).fetchone()
            if existing:
                if bytes(existing["request_payload_hash"]) != payload_hash:
                    raise api_error(409, "ASSESSMENT_TEST_RUN_IDEMPOTENCY_CONFLICT", "Request key reused", "Reload the workspace and use a new request key.")
                response.status_code = 200
                result = AssessmentTestRunResponse(**existing)
            else:
                if attempt["status"] not in {"consented", "active"}:
                    raise api_error(409, "ASSESSMENT_ATTEMPT_NOT_RUNNABLE", "Attempt unavailable", "Accept consent and save your assessment solution before running tests.")
                definition = AssessmentDefinition.model_validate(attempt["definition"])
                suite = definition.python_test_suite
                if suite is None:
                    raise api_error(409, "ASSESSMENT_TEST_SUITE_UNAVAILABLE", "Test suite unavailable", "This assessment does not have a deterministic Python test suite.")
                proficiency_levels = {item.competency_code: item.target_level for item in definition.competencies if item.competency_code}
                workspace = connection.execute(
                    "SELECT version, source_code FROM assessment_attempt_workspaces WHERE attempt_id = %s AND candidate_user_id = %s FOR UPDATE",
                    (attempt_id, session.user.id),
                ).fetchone()
                if workspace is None or workspace["version"] != body.expected_workspace_version:
                    raise api_error(409, "ASSESSMENT_WORKSPACE_VERSION_CONFLICT", "Workspace changed", "Save or reload the workspace before running tests.")
                if not runner or not runner.available():
                    raise api_error(503, "ASSESSMENT_RUNTIME_UNAVAILABLE", "Runtime unavailable", "The isolated code execution runtime is not available.")
                run_count = connection.execute(
                    "SELECT count(*) AS count FROM assessment_test_runs WHERE attempt_id = %s",
                    (attempt_id,),
                ).fetchone()["count"]
                if run_count >= 10:
                    raise api_error(429, "ASSESSMENT_TEST_RUN_LIMIT", "Run limit reached", "This assessment has reached its ten-run limit.")
                code = workspace["source_code"]
                run_id = uuid.uuid4()
                source_hash = hashlib.sha256(code.encode("utf-8")).digest()
                inserted = connection.execute(
                    """INSERT INTO assessment_test_runs
                       (id, attempt_id, candidate_user_id, workspace_version, request_key, request_payload_hash,
                        source_sha256, source_snapshot, status)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'running') RETURNING *""",
                    (run_id, attempt_id, session.user.id, workspace["version"], body.request_key,
                     payload_hash, source_hash, code),
                ).fetchone()
                result = AssessmentTestRunResponse(**inserted)
                if attempt["status"] == "consented":
                    connection.execute(
                        "UPDATE assessment_attempts SET status = 'active', version = version + 1, updated_at = now() WHERE id = %s",
                        (attempt_id,),
                    )
        if run_id is not None:
            counts = {}
            run_status = "passed"
            for case in suite.cases:
                bucket = counts.setdefault(case.competency_code, {"passed_cases": 0, "total_cases": 0})
                bucket["total_cases"] += 1
                try:
                    observed = runner.run_case(code, suite.entrypoint, case.arguments)
                except Exception:
                    run_status = "infrastructure_failed"
                    break
                if observed.status == "infrastructure_failed":
                    run_status = "infrastructure_failed"
                    break
                if observed.status == "timed_out":
                    run_status = "timed_out"
                elif observed.status == "completed":
                    try:
                        is_match = json.dumps(observed.value, sort_keys=True, separators=(",", ":"), allow_nan=False) == json.dumps(case.expected, sort_keys=True, separators=(",", ":"), allow_nan=False)
                    except (TypeError, ValueError):
                        is_match = False
                    if is_match:
                        bucket["passed_cases"] += 1
                if run_status == "passed" and bucket["passed_cases"] < bucket["total_cases"]:
                    run_status = "failed"
            competency_results = [
                {"competency_code": code_key,
                 "status": "demonstrated" if item["passed_cases"] == item["total_cases"] else "insufficient",
                 **item}
                for code_key, item in sorted(counts.items())
            ]
            if run_status == "infrastructure_failed":
                competency_results = []
            # shortcut: execution is synchronous; move it to a durable worker before larger suites or multi-instance scale.
            with connection.transaction():
                final_attempt = connection.execute(
                    "SELECT status FROM assessment_attempts WHERE id = %s FOR UPDATE",
                    (attempt_id,),
                ).fetchone()
                if final_attempt["status"] == "cancelled":
                    run_status = "cancelled"
                    competency_results = []
                connection.execute(
                    """UPDATE assessment_test_runs SET status = %s, competency_results = %s, completed_at = now()
                       WHERE id = %s""",
                    (run_status, Jsonb(competency_results), run_id),
                )
                if run_status not in {"infrastructure_failed", "cancelled"}:
                    for item in competency_results:
                        connection.execute(
                            """INSERT INTO candidate_competency_evidence
                               (id, candidate_user_id, competency_code, source_type, assessment_attempt_id,
                                observed_outcome, verification_method, proficiency_level, review_status, evidence_limitations,
                                sharing_scope, source_test_run_id)
                               VALUES (%s, %s, %s, 'assessment_result', %s, %s,
                                       'deterministic_test', %s, 'unreviewed', %s, 'candidate_only', %s)""",
                            (uuid.uuid4(), session.user.id, item["competency_code"], attempt_id,
                             f"Run {run_id}: {item['passed_cases']} of {item['total_cases']} server-authored test cases passed.",
                             proficiency_levels.get(item["competency_code"]),
                             "Confirms only the private deterministic test cases for this assessment version; it is not a hiring decision.",
                             run_id),
                        )
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome)
                       VALUES (%s, %s, 'user', %s, 'assessment_test_run', %s, 'candidate.assessment_test_run_completed', %s, %s)""",
                    (uuid.uuid4(), session.user.id, attempt["organization_id"], run_id,
                     request.state.request_id, "success" if run_status == "passed" else "failure"),
                )
                finished = connection.execute("SELECT * FROM assessment_test_runs WHERE id = %s", (run_id,)).fetchone()
                result = AssessmentTestRunResponse(**finished)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return result

    @app.get(
        "/api/v1/attempts/{attempt_id}/test-runs/{run_id}",
        response_model=AssessmentTestRunResponse,
        summary="Read the candidate's sanitized assessment test result",
        responses={401: problem_response("Authentication is required."), 404: problem_response("The test run was not found.")},
    )
    def get_assessment_test_run(
        attempt_id: uuid.UUID,
        run_id: uuid.UUID,
        response: Response,
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> AssessmentTestRunResponse:
        connection.execute(
            """UPDATE assessment_test_runs SET status = 'infrastructure_failed', completed_at = now()
               WHERE id = %s AND attempt_id = %s AND candidate_user_id = %s
                 AND status = 'running' AND created_at < now() - interval '2 minutes'""",
            (run_id, attempt_id, session.user.id),
        )
        row = connection.execute(
            "SELECT * FROM assessment_test_runs WHERE id = %s AND attempt_id = %s AND candidate_user_id = %s",
            (run_id, attempt_id, session.user.id),
        ).fetchone()
        if row is None:
            raise api_error(404, "ASSESSMENT_TEST_RUN_NOT_FOUND", "Test run not found", "The test run was not found.")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return AssessmentTestRunResponse(**row)

    @app.put(
        "/api/v1/attempts/{attempt_id}/consents",
        response_model=AssessmentAttemptConsentResponse,
        summary="Record or revoke versioned consent for an assessment attempt",
        description="Only the candidate who owns an unstarted attempt may accept or revoke consent. Recording channels are disabled in this preview.",
        responses={401: problem_response("Authentication is required."), 403: problem_response("Candidate workspace and CSRF validation are required."), 404: problem_response("The attempt was not found."), 409: problem_response("The attempt changed or cannot transition consent."), 422: problem_response("The request is invalid.")},
    )
    def update_assessment_attempt_consent(
        attempt_id: uuid.UUID,
        body: AssessmentAttemptConsentRequest,
        request: Request,
        response: Response,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> AssessmentAttemptConsentResponse:
        action = "accepted" if body.action == "accept" else "revoked"
        desired_status = "consented" if action == "accepted" else "created"
        with connection.transaction():
            attempt = connection.execute(
                """SELECT status, version, organization_id FROM assessment_attempts
                   WHERE id = %s AND candidate_user_id = %s FOR UPDATE""",
                (attempt_id, session.user.id),
            ).fetchone()
            if attempt is None:
                raise api_error(404, "ASSESSMENT_ATTEMPT_NOT_FOUND", "Attempt not found", "The attempt was not found.")
            latest = connection.execute(
                """SELECT action, consent_version, recording_preferences, attempt_version, created_at
                   FROM assessment_attempt_consent_events WHERE attempt_id = %s
                   ORDER BY attempt_version DESC LIMIT 1""",
                (attempt_id,),
            ).fetchone()
            if attempt["status"] == desired_status and latest and latest["action"] == action:
                result = AssessmentAttemptConsentResponse(
                    attempt_id=attempt_id,
                    attempt_status=attempt["status"],
                    attempt_version=attempt["version"],
                    consent_status=action,
                    consent_version=latest["consent_version"],
                    consent_event_at=latest["created_at"],
                    recording_preferences=latest["recording_preferences"],
                )
            else:
                if attempt["version"] != body.expected_version:
                    raise api_error(409, "ASSESSMENT_ATTEMPT_VERSION_CONFLICT", "Attempt changed", "Reload this attempt and try again.")
                if body.action == "accept" and attempt["status"] != "created":
                    raise api_error(409, "ASSESSMENT_ATTEMPT_CONSENT_UNAVAILABLE", "Consent unavailable", "Consent can only be accepted before the attempt starts.")
                if body.action == "revoke" and (attempt["status"] != "consented" or latest is None or latest["action"] != "accepted"):
                    raise api_error(409, "ASSESSMENT_ATTEMPT_CONSENT_UNAVAILABLE", "Consent unavailable", "Consent can only be revoked before the attempt starts.")

                consent_version = body.consent_version if body.action == "accept" else latest["consent_version"]
                preferences = body.recording_preferences.model_dump() if body.action == "accept" else latest["recording_preferences"]
                updated = connection.execute(
                    """UPDATE assessment_attempts SET status = %s, version = version + 1, updated_at = now()
                       WHERE id = %s RETURNING version""",
                    (desired_status, attempt_id),
                ).fetchone()
                event = connection.execute(
                    """INSERT INTO assessment_attempt_consent_events
                       (id, attempt_id, candidate_user_id, actor_id, attempt_version, action,
                        consent_version, recording_preferences)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING created_at""",
                    (
                        uuid.uuid4(), attempt_id, session.user.id, session.user.id, updated["version"],
                        action, consent_version, Jsonb(preferences),
                    ),
                ).fetchone()
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action,
                        correlation_id, outcome, metadata)
                       VALUES (%s, %s, 'user', %s, 'assessment_attempt', %s, %s, %s, 'success', %s)""",
                    (
                        uuid.uuid4(), session.user.id, attempt["organization_id"], attempt_id,
                        f"candidate.assessment_attempt_consent_{action}", request.state.request_id,
                        Jsonb({"attempt_version": updated["version"], "consent_version": consent_version}),
                    ),
                )
                result = AssessmentAttemptConsentResponse(
                    attempt_id=attempt_id,
                    attempt_status=desired_status,
                    attempt_version=updated["version"],
                    consent_status=action,
                    consent_version=consent_version,
                    consent_event_at=event["created_at"],
                    recording_preferences=preferences,
                )
        response.headers["Cache-Control"] = "no-store"
        return result

    @app.get(
        "/api/v1/me/saved-jobs",
        response_model=SavedJobListResponse,
        responses={401: problem_response("Authentication is required."), 403: problem_response("A candidate workspace is required.")},
    )
    def list_saved_jobs(
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> SavedJobListResponse:
        if session.user.initial_purpose != "candidate" and not connection.execute(
            "SELECT 1 FROM candidate_onboardings WHERE user_id = %s", (session.user.id,)
        ).fetchone():
            raise api_error(403, "CANDIDATE_WORKSPACE_REQUIRED", "Candidate workspace required", "Open your candidate workspace to save jobs.")
        rows = connection.execute(
            """SELECT job.id, organization.name AS organization_name, job.title, job.description,
                      job.status, job.published_at, saved.created_at AS saved_at
               FROM saved_jobs AS saved JOIN organization_jobs AS job ON job.id = saved.job_id
               JOIN organizations AS organization ON organization.id = job.organization_id
               WHERE saved.user_id = %s ORDER BY saved.created_at DESC, job.id""",
            (session.user.id,),
        ).fetchall()
        return SavedJobListResponse(jobs=[SavedJobItem(**row) for row in rows])

    @app.put(
        "/api/v1/me/saved-jobs/{job_id}",
        status_code=204,
        responses={401: problem_response("Authentication is required."), 403: problem_response("A candidate workspace and CSRF validation are required."), 404: problem_response("The published job was not found.")},
    )
    def save_job(
        job_id: uuid.UUID,
        request: Request,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> Response:
        if session.user.initial_purpose != "candidate" and not connection.execute(
            "SELECT 1 FROM candidate_onboardings WHERE user_id = %s", (session.user.id,)
        ).fetchone():
            raise api_error(403, "CANDIDATE_WORKSPACE_REQUIRED", "Candidate workspace required", "Open your candidate workspace to save jobs.")
        with connection.transaction():
            job = connection.execute(
                "SELECT id FROM organization_jobs WHERE id = %s AND status = 'published' FOR SHARE", (job_id,)
            ).fetchone()
            if job is None:
                raise api_error(404, "JOB_NOT_FOUND", "Job not found", "The published job was not found.")
            saved = connection.execute(
                "INSERT INTO saved_jobs (user_id, job_id) VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING job_id",
                (session.user.id, job_id),
            ).fetchone()
            if saved:
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_id, actor_type, resource_type, resource_id, action, correlation_id, outcome)
                       VALUES (%s, %s, 'user', 'saved_job', %s, 'candidate.job_saved', %s, 'success')""",
                    (uuid.uuid4(), session.user.id, job_id, request.state.request_id),
                )
        return Response(status_code=204)

    @app.delete(
        "/api/v1/me/saved-jobs/{job_id}",
        status_code=204,
        responses={401: problem_response("Authentication is required."), 403: problem_response("A candidate workspace and CSRF validation are required.")},
    )
    def unsave_job(
        job_id: uuid.UUID,
        request: Request,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> Response:
        if session.user.initial_purpose != "candidate" and not connection.execute(
            "SELECT 1 FROM candidate_onboardings WHERE user_id = %s", (session.user.id,)
        ).fetchone():
            raise api_error(403, "CANDIDATE_WORKSPACE_REQUIRED", "Candidate workspace required", "Open your candidate workspace to save jobs.")
        with connection.transaction():
            removed = connection.execute(
                "DELETE FROM saved_jobs WHERE user_id = %s AND job_id = %s RETURNING job_id",
                (session.user.id, job_id),
            ).fetchone()
            if removed:
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_id, actor_type, resource_type, resource_id, action, correlation_id, outcome)
                       VALUES (%s, %s, 'user', 'saved_job', %s, 'candidate.job_unsaved', %s, 'success')""",
                    (uuid.uuid4(), session.user.id, job_id, request.state.request_id),
                )
        return Response(status_code=204)

    @app.post(
        "/api/v1/jobs/{job_id}/applications",
        response_model=CandidateApplicationResponse,
        status_code=201,
        responses={
            200: {"model": CandidateApplicationResponse, "description": "Returns the original application for an identical idempotent retry."},
            401: problem_response("Authentication is required."),
            403: problem_response("A verified candidate workspace is required."),
            404: problem_response("The published job was not found."),
            409: problem_response("The candidate portfolio, job, or application state is not eligible."),
            422: problem_response("The request is invalid."),
            429: problem_response("Too many applications were submitted."),
        },
    )
    def create_job_application(
        job_id: uuid.UUID,
        body: ApplicationCreateRequest,
        request: Request,
        response: Response,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> CandidateApplicationResponse:
        if not session.user.email_verified:
            raise api_error(403, "EMAIL_VERIFICATION_REQUIRED", "Verified email required", "Verify your email before applying.")
        if session.user.initial_purpose != "candidate" and not connection.execute(
            "SELECT 1 FROM candidate_onboardings WHERE user_id = %s", (session.user.id,)
        ).fetchone():
            raise api_error(403, "CANDIDATE_WORKSPACE_REQUIRED", "Candidate workspace required", "Open your candidate workspace before applying.")
        payload_hash = hash_token(
            f"job-application:v1:{job_id}:{body.portfolio_consent_version}:{body.cover_note}"
        )
        with connection.transaction():
            previous = connection.execute(
                "SELECT * FROM job_applications WHERE user_id = %s AND request_key = %s FOR UPDATE",
                (session.user.id, body.request_key),
            ).fetchone()
            if previous:
                if bytes(previous["request_payload_hash"]) != payload_hash:
                    raise api_error(409, "IDEMPOTENCY_KEY_REUSED", "Request key conflict", "Use a new request key for different application content.")
                response.status_code = 200
                response.headers["Cache-Control"] = "no-store"
                return candidate_application_response(previous)
            if not app.state.rate_limiter.allow(f"application-create:user:{session.user.id}", 20, 3600):
                raise api_error(429, "RATE_LIMITED", "Too many applications", "Try again later.")

            job = connection.execute(
                f"""SELECT job.id, job.organization_id, job.title, job.description,
                          organization.name AS organization_name, {JOB_COMPETENCY_REQUIREMENTS_SELECT}
                   FROM organization_jobs AS job JOIN organizations AS organization
                     ON organization.id = job.organization_id
                   WHERE job.id = %s AND job.status = 'published' FOR SHARE OF job""",
                (job_id,),
            ).fetchone()
            if job is None:
                raise api_error(404, "JOB_NOT_FOUND", "Job not found", "The published job was not found.")
            if connection.execute(
                "SELECT 1 FROM organization_memberships WHERE organization_id = %s AND user_id = %s",
                (job["organization_id"], session.user.id),
            ).fetchone():
                raise api_error(403, "ORGANIZATION_CONFLICT_OF_INTEREST", "Application not allowed", "Organization members cannot apply to their own organization's jobs.")

            connection.execute("SELECT id FROM users WHERE id = %s FOR SHARE", (session.user.id,))
            profile = connection.execute(
                """SELECT users.full_name, onboarding.target_roles, onboarding.skills
                   FROM candidate_onboardings AS onboarding
                   JOIN users ON users.id = onboarding.user_id
                   JOIN candidate_portfolios AS portfolio ON portfolio.user_id = onboarding.user_id
                   WHERE onboarding.user_id = %s AND onboarding.completed_at IS NOT NULL
                     AND portfolio.published_at IS NOT NULL""",
                (session.user.id,),
            ).fetchone()
            if profile is None:
                raise api_error(409, "PUBLIC_PORTFOLIO_REQUIRED", "Portfolio required", "Complete and publish your candidate portfolio before applying.")
            projects = connection.execute(
                """SELECT project.name, project.problem_statement, project.contribution, project.technologies,
                          COALESCE((SELECT jsonb_agg(source.url ORDER BY source.created_at, source.id)
                                    FROM candidate_project_source_links AS source WHERE source.project_id = project.id), '[]'::jsonb) AS source_links
                   FROM candidate_projects AS project
                   WHERE project.user_id = %s AND project.published_at IS NOT NULL
                   ORDER BY project.sort_order, project.id""",
                (session.user.id,),
            ).fetchall()
            portfolio_snapshot = {
                "display_name": profile["full_name"],
                "target_roles": profile["target_roles"],
                "skills": profile["skills"],
                "projects": [dict(project) for project in projects],
            }
            job_snapshot = {
                "title": job["title"],
                "description": job["description"],
                "organization_name": job["organization_name"],
                "competency_requirements": job["competency_requirements"],
            }
            application = connection.execute(
                """INSERT INTO job_applications
                   (id, organization_id, job_id, user_id, request_key, request_payload_hash,
                    portfolio_snapshot, job_snapshot, cover_note, portfolio_consent_version)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT DO NOTHING RETURNING *""",
                (
                    uuid.uuid4(), job["organization_id"], job_id, session.user.id, body.request_key,
                    payload_hash, Jsonb(portfolio_snapshot), Jsonb(job_snapshot), body.cover_note,
                    body.portfolio_consent_version,
                ),
            ).fetchone()
            if application is None:
                previous = connection.execute(
                    "SELECT * FROM job_applications WHERE user_id = %s AND request_key = %s FOR UPDATE",
                    (session.user.id, body.request_key),
                ).fetchone()
                if previous:
                    if bytes(previous["request_payload_hash"]) != payload_hash:
                        raise api_error(409, "IDEMPOTENCY_KEY_REUSED", "Request key conflict", "Use a new request key for different application content.")
                    response.status_code = 200
                    response.headers["Cache-Control"] = "no-store"
                    return candidate_application_response(previous)
                raise api_error(409, "APPLICATION_ALREADY_EXISTS", "Application already exists", "You already have an active application for this job.")
            connection.execute(
                """INSERT INTO application_events (id, application_id, actor_id, actor_type, action, to_status)
                   VALUES (%s, %s, %s, 'candidate', 'submitted', 'submitted')""",
                (uuid.uuid4(), application["id"], session.user.id),
            )
            connection.execute(
                """INSERT INTO audit_events
                   (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome)
                   VALUES (%s, %s, 'user', %s, 'job_application', %s, 'candidate.application_submitted', %s, 'success')""",
                (uuid.uuid4(), session.user.id, job["organization_id"], application["id"], request.state.request_id),
            )
        response.headers["Cache-Control"] = "no-store"
        return candidate_application_response(application)

    @app.get(
        "/api/v1/me/applications",
        response_model=CandidateApplicationListResponse,
        responses={401: problem_response("Authentication is required."), 403: problem_response("A candidate workspace is required.")},
    )
    def list_my_applications(
        response: Response,
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
        offset: Annotated[int, Query(ge=0, le=10000)] = 0,
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> CandidateApplicationListResponse:
        if session.user.initial_purpose != "candidate" and not connection.execute(
            "SELECT 1 FROM candidate_onboardings WHERE user_id = %s", (session.user.id,)
        ).fetchone():
            raise api_error(403, "CANDIDATE_WORKSPACE_REQUIRED", "Candidate workspace required", "Open your candidate workspace to view applications.")
        rows = connection.execute(
            """SELECT application.id, application.job_id, application.job_snapshot->>'title' AS job_title,
                      application.job_snapshot->>'organization_name' AS organization_name,
                      application.status, application.submitted_at, application.version
               FROM job_applications AS application
               WHERE application.user_id = %s
               ORDER BY application.submitted_at DESC, application.id LIMIT %s OFFSET %s""",
            (session.user.id, limit, offset),
        ).fetchall()
        response.headers["Cache-Control"] = "no-store"
        return CandidateApplicationListResponse(applications=[CandidateApplicationListItem(**row) for row in rows])

    @app.get(
        "/api/v1/me/applications/{application_id}",
        response_model=CandidateApplicationResponse,
        responses={401: problem_response("Authentication is required."), 404: problem_response("The application was not found.")},
    )
    def get_my_application(
        application_id: uuid.UUID,
        response: Response,
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> CandidateApplicationResponse:
        row = connection.execute(
            "SELECT * FROM job_applications WHERE id = %s AND user_id = %s",
            (application_id, session.user.id),
        ).fetchone()
        if row is None:
            raise api_error(404, "APPLICATION_NOT_FOUND", "Application not found", "The application was not found.")
        response.headers["Cache-Control"] = "no-store"
        return candidate_application_response(row)

    @app.get(
        "/api/v1/me/applications/{application_id}/evidence-sharing",
        response_model=CandidateApplicationEvidenceResponse,
        summary="Review private competency evidence and the evidence-sharing state for an application",
        responses={401: problem_response("Authentication is required."), 404: problem_response("The application was not found.")},
    )
    def get_candidate_application_evidence(
        application_id: uuid.UUID,
        response: Response,
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> CandidateApplicationEvidenceResponse:
        application = connection.execute(
            "SELECT id, status, job_snapshot FROM job_applications WHERE id = %s AND user_id = %s",
            (application_id, session.user.id),
        ).fetchone()
        if application is None:
            raise api_error(404, "APPLICATION_NOT_FOUND", "Application not found", "The application was not found.")
        response.headers["Cache-Control"] = "no-store"
        return candidate_application_evidence_response(connection, application, session.user.id)

    @app.put(
        "/api/v1/me/applications/{application_id}/evidence-sharing",
        response_model=CandidateApplicationEvidenceResponse,
        summary="Grant or revoke application-scoped evidence disclosure",
        description="Only the candidate may share their own active, non-withdrawn evidence items for the competencies in this application. Each change appends a consent event; sharing is not inherited by other applications.",
        responses={401: problem_response("Authentication is required."), 403: problem_response("CSRF validation or candidate workspace is required."), 404: problem_response("The application or evidence was not found.",), 409: problem_response("The application changed or the consent version is stale."), 422: problem_response("The request is invalid.")},
    )
    def update_candidate_application_evidence_share(
        application_id: uuid.UUID,
        body: ApplicationEvidenceShareRequest,
        request: Request,
        response: Response,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> CandidateApplicationEvidenceResponse:
        if session.user.initial_purpose != "candidate" and not connection.execute(
            "SELECT 1 FROM candidate_onboardings WHERE user_id = %s", (session.user.id,)
        ).fetchone():
            raise api_error(403, "CANDIDATE_WORKSPACE_REQUIRED", "Candidate workspace required", "Open your candidate workspace before sharing evidence.")
        requested_ids = sorted(body.evidence_ids, key=str)
        with connection.transaction():
            application = connection.execute(
                """SELECT id, organization_id, status, job_snapshot FROM job_applications
                   WHERE id = %s AND user_id = %s FOR UPDATE""",
                (application_id, session.user.id),
            ).fetchone()
            if application is None:
                raise api_error(404, "APPLICATION_NOT_FOUND", "Application not found", "The application was not found.")
            event = connection.execute(
                """SELECT id, version, action FROM candidate_application_evidence_share_events
                   WHERE application_id = %s ORDER BY version DESC LIMIT 1""",
                (application_id,),
            ).fetchone()
            current_version = event["version"] if event else 0
            current_ids = []
            if event and event["action"] == "shared":
                current_ids = [row["evidence_id"] for row in connection.execute(
                    "SELECT evidence_id FROM candidate_application_evidence_share_items WHERE share_event_id = %s ORDER BY evidence_id",
                    (event["id"],),
                ).fetchall()]
            expected_action = "shared" if body.action == "share" else "revoked"
            if event and event["action"] == expected_action and (body.action == "revoke" or current_ids == requested_ids):
                pass
            else:
                if body.expected_version != current_version:
                    raise api_error(409, "APPLICATION_EVIDENCE_SHARE_VERSION_CONFLICT", "Sharing changed", "Reload evidence sharing and try again.")
                if body.action == "share":
                    if application["status"] in {"hired", "rejected", "withdrawn"}:
                        raise api_error(409, "APPLICATION_EVIDENCE_SHARE_CLOSED", "Sharing unavailable", "Evidence can only be shared while the application is active.")
                    requirement_codes = {item["competency_code"] for item in (application["job_snapshot"] or {}).get("competency_requirements", [])}
                    evidence_rows = connection.execute(
                        """SELECT id, competency_code FROM candidate_competency_evidence
                           WHERE candidate_user_id = %s AND id = ANY(%s) AND withdrawn_at IS NULL""",
                        (session.user.id, requested_ids),
                    ).fetchall()
                    if len(evidence_rows) != len(requested_ids):
                        raise api_error(404, "APPLICATION_EVIDENCE_NOT_FOUND", "Evidence not found", "One or more evidence items are not available.")
                    if any(row["competency_code"] not in requirement_codes for row in evidence_rows):
                        raise api_error(422, "APPLICATION_EVIDENCE_NOT_RELEVANT", "Evidence not relevant", "Share evidence linked to a competency required by this job.")
                elif current_version == 0 or not current_ids:
                    raise api_error(409, "APPLICATION_EVIDENCE_NOT_SHARED", "Evidence not shared", "There is no active evidence grant to revoke.")
                new_version = current_version + 1
                event_id = uuid.uuid4()
                connection.execute(
                    """INSERT INTO candidate_application_evidence_share_events
                       (id, organization_id, application_id, candidate_user_id, version, action, consent_version)
                       VALUES (%s, %s, %s, %s, %s, %s, 'candidate-application-evidence-v1')""",
                    (event_id, application["organization_id"], application_id, session.user.id, new_version,
                     "shared" if body.action == "share" else "revoked"),
                )
                for evidence_id in requested_ids if body.action == "share" else []:
                    connection.execute(
                        """INSERT INTO candidate_application_evidence_share_items
                           (share_event_id, application_id, candidate_user_id, evidence_id)
                           VALUES (%s, %s, %s, %s)""",
                        (event_id, application_id, session.user.id, evidence_id),
                    )
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome, metadata)
                       VALUES (%s, %s, 'user', %s, 'job_application', %s, %s, %s, 'success', %s)""",
                    (uuid.uuid4(), session.user.id, application["organization_id"], application_id,
                     f"candidate.application_evidence_{body.action}", request.state.request_id,
                     Jsonb({"evidence_count": len(requested_ids) if body.action == "share" else 0, "share_version": new_version})),
                )
        response.headers["Cache-Control"] = "no-store"
        return candidate_application_evidence_response(connection, application, session.user.id)

    @app.post(
        "/api/v1/me/applications/{application_id}/withdraw",
        response_model=CandidateApplicationResponse,
        responses={401: problem_response("Authentication is required."), 403: problem_response("The CSRF token is missing or invalid."), 404: problem_response("The application was not found."), 409: problem_response("The application changed or is final."), 422: problem_response("The request is invalid.")},
    )
    def withdraw_my_application(
        application_id: uuid.UUID,
        body: ApplicationWithdrawRequest,
        request: Request,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> CandidateApplicationResponse:
        with connection.transaction():
            application = connection.execute(
                "SELECT * FROM job_applications WHERE id = %s AND user_id = %s FOR UPDATE",
                (application_id, session.user.id),
            ).fetchone()
            if application is None:
                raise api_error(404, "APPLICATION_NOT_FOUND", "Application not found", "The application was not found.")
            if application["status"] != "withdrawn":
                if application["status"] in {"hired", "rejected"} or application["version"] != body.expected_version:
                    raise api_error(409, "APPLICATION_STATE_CONFLICT", "Application changed", "Reload the application before withdrawing it.")
                connection.execute(
                    "UPDATE job_applications SET status = 'withdrawn', withdrawn_at = now(), version = version + 1, updated_at = now() WHERE id = %s",
                    (application_id,),
                )
                cancel_application_interviews(
                    connection, application_id, application["organization_id"], session.user.id,
                    request.state.request_id, "candidate.interview_cancelled_on_withdrawal",
                    "The candidate withdrew the application.",
                )
                connection.execute(
                    """INSERT INTO application_events (id, application_id, actor_id, actor_type, action, from_status, to_status)
                       VALUES (%s, %s, %s, 'candidate', 'withdrawn', %s, 'withdrawn')""",
                    (uuid.uuid4(), application_id, session.user.id, application["status"]),
                )
                cancelled_invitations = connection.execute(
                    """UPDATE assessment_invitations
                       SET status = 'cancelled', cancelled_by_user_id = %s, cancelled_at = now(),
                           cancellation_reason = 'The candidate withdrew the application.',
                           version = version + 1, updated_at = now()
                       WHERE application_id = %s AND status = 'issued'
                       RETURNING id, organization_id""",
                    (session.user.id, application_id),
                ).fetchall()
                for invitation in cancelled_invitations:
                    connection.execute(
                        """INSERT INTO audit_events
                           (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome)
                           VALUES (%s, %s, 'user', %s, 'assessment_invitation', %s, 'candidate.invitation_cancelled_on_withdrawal', %s, 'success')""",
                        (uuid.uuid4(), session.user.id, invitation["organization_id"], invitation["id"], request.state.request_id),
                    )
                cancelled_attempts = connection.execute(
                    """UPDATE assessment_attempts SET status = 'cancelled', version = version + 1, updated_at = now()
                       WHERE invitation_id IN (SELECT id FROM assessment_invitations WHERE application_id = %s)
                         AND status IN ('created', 'consented', 'active')
                       RETURNING id, organization_id""",
                    (application_id,),
                ).fetchall()
                for attempt in cancelled_attempts:
                    connection.execute(
                        """INSERT INTO audit_events
                           (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome)
                           VALUES (%s, %s, 'user', %s, 'assessment_attempt', %s, 'candidate.assessment_attempt_cancelled_on_withdrawal', %s, 'success')""",
                        (uuid.uuid4(), session.user.id, attempt["organization_id"], attempt["id"], request.state.request_id),
                    )
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome)
                       VALUES (%s, %s, 'user', %s, 'job_application', %s, 'candidate.application_withdrawn', %s, 'success')""",
                    (uuid.uuid4(), session.user.id, application["organization_id"], application_id, request.state.request_id),
                )
            application = connection.execute("SELECT * FROM job_applications WHERE id = %s", (application_id,)).fetchone()
        return candidate_application_response(application)

    @app.get("/api/v1/me/interviews", response_model=InterviewListResponse)
    def list_my_interviews(
        response: Response,
        limit: int = Query(default=50, ge=1, le=100),
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> InterviewListResponse:
        rows = connection.execute(
            INTERVIEW_SELECT + """ WHERE interview.candidate_user_id = %s AND application.status <> 'withdrawn'
                                  ORDER BY interview.scheduled_at DESC, interview.id LIMIT %s""",
            (session.user.id, limit),
        ).fetchall()
        response.headers["Cache-Control"] = "no-store"
        return InterviewListResponse(interviews=[candidate_interview_item(row) for row in rows])

    @app.post(
        "/api/v1/me/interviews/{interview_id}/respond",
        response_model=InterviewItem,
        responses={401: problem_response("Authentication is required."), 403: problem_response("CSRF validation failed."), 404: problem_response("The interview was not found."), 409: problem_response("The interview changed or cannot accept a response."), 422: problem_response("The request is invalid.")},
    )
    def respond_to_interview(
        interview_id: uuid.UUID,
        body: InterviewResponseRequest,
        request: Request,
        response: Response,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> InterviewItem:
        if not app.state.rate_limiter.allow(f"interview-response:user:{session.user.id}", 40, 3600):
            raise api_error(429, "RATE_LIMITED", "Too many responses", "Try again later.")
        response_name = {"accept": "accepted", "decline": "declined", "request_reschedule": "reschedule_requested"}[body.action]
        with connection.transaction():
            current = connection.execute(
                """SELECT interview.*, application.status AS application_status
                   FROM application_interviews AS interview
                   JOIN job_applications AS application ON application.id = interview.application_id
                   WHERE interview.id = %s AND interview.candidate_user_id = %s AND application.status <> 'withdrawn'
                   FOR UPDATE OF interview, application""",
                (interview_id, session.user.id),
            ).fetchone()
            if current is None:
                raise api_error(404, "INTERVIEW_NOT_FOUND", "Interview not found", "The interview was not found.")
            if current["version"] != body.expected_version:
                raise api_error(409, "INTERVIEW_VERSION_CONFLICT", "Interview changed", "Reload the interview and try again.")
            if current["status"] != "scheduled" or current["scheduled_at"] <= datetime.now(timezone.utc) or current["application_status"] in {"hired", "rejected"} or current["candidate_response"] in {"declined", "reschedule_requested"}:
                raise api_error(409, "INTERVIEW_RESPONSE_INVALID", "Response unavailable", "This interview can no longer accept a response.")
            connection.execute(
                """UPDATE application_interviews SET candidate_response = %s,
                          response_message = NULLIF(%s, ''), responded_at = now(), version = version + 1, updated_at = now()
                   WHERE id = %s""",
                (response_name, body.message, interview_id),
            )
            create_user_notification(
                connection, current["created_by_user_id"], interview_id, "interview_response",
                current["version"] + 1, "Interview response received",
                "A candidate responded to an interview schedule. Review your assigned applications.",
            )
            connection.execute(
                """INSERT INTO audit_events
                   (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome, metadata)
                   VALUES (%s, %s, 'user', %s, 'application_interview', %s, %s, %s, 'success', %s)""",
                (uuid.uuid4(), session.user.id, current["organization_id"], interview_id,
                 f"candidate.interview_{response_name}", request.state.request_id,
                 Jsonb({"version": current["version"] + 1})),
            )
            updated = connection.execute(
                INTERVIEW_SELECT + " WHERE interview.id = %s", (interview_id,)
            ).fetchone()
        response.headers["Cache-Control"] = "no-store"
        return candidate_interview_item(updated)

    @app.get("/api/v1/organizations/{org_id}/interviews", response_model=InterviewListResponse)
    def list_organization_interviews(
        org_id: uuid.UUID,
        response: Response,
        application_id: uuid.UUID | None = None,
        limit: int = Query(default=50, ge=1, le=100),
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> InterviewListResponse:
        organization = organization_access(connection, session.user.id, org_id)
        if organization["member_role"] not in {"owner", "admin", "recruiter"}:
            raise api_error(403, "RECRUITER_ACCESS_REQUIRED", "Recruiter access required", "A recruiter workspace role is required.")
        query = INTERVIEW_SELECT + """ WHERE interview.organization_id = %s AND application.status <> 'withdrawn'
                                       AND (%s OR EXISTS (
                                           SELECT 1 FROM application_reviewer_assignments AS assignment
                                           WHERE assignment.organization_id = interview.organization_id
                                             AND assignment.application_id = interview.application_id
                                             AND assignment.reviewer_user_id = %s
                                       ))"""
        params: tuple = (org_id, organization["member_role"] in {"owner", "admin"}, session.user.id)
        if application_id is not None:
            query += " AND interview.application_id = %s"
            params += (application_id,)
        rows = connection.execute(
            query + " ORDER BY interview.scheduled_at DESC, interview.id LIMIT %s",
            params + (limit,),
        ).fetchall()
        response.headers["Cache-Control"] = "no-store"
        return InterviewListResponse(interviews=[interview_item(row) for row in rows])

    @app.post(
        "/api/v1/organizations/{org_id}/interviews",
        response_model=InterviewItem,
        status_code=201,
        responses={200: {"model": InterviewItem, "description": "The idempotent schedule request was replayed."}, 401: problem_response("Authentication is required."), 403: problem_response("Verified recruiter access is required."), 404: problem_response("The active interview-stage application was not found."), 409: problem_response("The schedule conflicts or the request key was reused."), 422: problem_response("The request is invalid.")},
    )
    def create_interview(
        org_id: uuid.UUID,
        body: InterviewCreateRequest,
        request: Request,
        response: Response,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> InterviewItem:
        if not session.user.email_verified:
            raise api_error(403, "EMAIL_VERIFICATION_REQUIRED", "Verified email required", "Verify your email before scheduling interviews.")
        if not app.state.rate_limiter.allow(f"interview-create:user:{session.user.id}", 20, 3600):
            raise api_error(429, "RATE_LIMITED", "Too many schedules", "Try again later.")
        payload_hash = hash_token("interview-create:v1:" + body.model_dump_json())
        organization = organization_access(connection, session.user.id, org_id)
        if organization["member_role"] not in {"owner", "admin", "recruiter"}:
            raise api_error(403, "RECRUITER_ACCESS_REQUIRED", "Recruiter access required", "A recruiter workspace role is required.")
        with connection.transaction():
            application = connection.execute(
                """SELECT id, user_id, status FROM job_applications
                   WHERE id = %s AND organization_id = %s AND status <> 'withdrawn' FOR UPDATE""",
                (body.application_id, org_id),
            ).fetchone()
            if application is None:
                raise api_error(404, "APPLICATION_NOT_FOUND", "Application not found", "The active application was not found in this organization.")
            require_application_reviewer_access(
                connection, org_id, body.application_id, session.user.id, organization["member_role"], lock=True
            )
            existing = connection.execute(
                INTERVIEW_SELECT + " WHERE interview.organization_id = %s AND interview.request_key = %s",
                (org_id, body.request_key),
            ).fetchone()
            if existing:
                if bytes(existing["request_payload_hash"]) != payload_hash:
                    raise api_error(409, "INTERVIEW_REQUEST_KEY_REUSED", "Request conflict", "Use a new request key for different interview details.")
                response.status_code = 200
                created = existing
            else:
                if application["status"] != "interview":
                    raise api_error(409, "APPLICATION_NOT_AT_INTERVIEW_STAGE", "Interview stage required", "Move the application to the interview stage before scheduling.")
                ensure_interviewer_slot(connection, session.user.id, body.scheduled_at, body.duration_minutes)
                interview_id = uuid.uuid4()
                connection.execute(
                    """INSERT INTO application_interviews
                       (id, organization_id, application_id, candidate_user_id, interviewer_user_id,
                        created_by_user_id, request_key, request_payload_hash, scheduled_at,
                        duration_minutes, display_timezone, meeting_url, candidate_note)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (interview_id, org_id, body.application_id, application["user_id"], session.user.id,
                     session.user.id, body.request_key, payload_hash, body.scheduled_at,
                     body.duration_minutes, body.display_timezone, body.meeting_url, body.candidate_note),
                )
                create_user_notification(
                    connection, application["user_id"], interview_id, "interview_scheduled", 1,
                    "Interview scheduled", "A hiring team scheduled an interview. Review the time and meeting details in application tracking.",
                )
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome)
                       VALUES (%s, %s, 'user', %s, 'application_interview', %s, 'organization.interview_scheduled', %s, 'success')""",
                    (uuid.uuid4(), session.user.id, org_id, interview_id, request.state.request_id),
                )
                created = connection.execute(INTERVIEW_SELECT + " WHERE interview.id = %s", (interview_id,)).fetchone()
        response.headers["Cache-Control"] = "no-store"
        return interview_item(created)

    @app.patch(
        "/api/v1/organizations/{org_id}/interviews/{interview_id}",
        response_model=InterviewItem,
        responses={401: problem_response("Authentication is required."), 403: problem_response("Assigned recruiter access is required."), 404: problem_response("The active interview was not found."), 409: problem_response("The interview changed or the new time conflicts."), 422: problem_response("The request is invalid.")},
    )
    def update_interview(
        org_id: uuid.UUID,
        interview_id: uuid.UUID,
        body: InterviewUpdateRequest,
        request: Request,
        response: Response,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> InterviewItem:
        if not session.user.email_verified:
            raise api_error(403, "EMAIL_VERIFICATION_REQUIRED", "Verified email required", "Verify your email before changing interviews.")
        organization = organization_access(connection, session.user.id, org_id)
        if organization["member_role"] not in {"owner", "admin", "recruiter"}:
            raise api_error(403, "RECRUITER_ACCESS_REQUIRED", "Recruiter access required", "A recruiter workspace role is required.")
        with connection.transaction():
            current = connection.execute(
                """SELECT interview.*, application.status AS application_status
                   FROM application_interviews AS interview
                   JOIN job_applications AS application ON application.id = interview.application_id
                   WHERE interview.id = %s AND interview.organization_id = %s AND application.status <> 'withdrawn'
                   FOR UPDATE OF interview, application""",
                (interview_id, org_id),
            ).fetchone()
            if current is None:
                raise api_error(404, "INTERVIEW_NOT_FOUND", "Interview not found", "The active interview was not found in this organization.")
            require_application_reviewer_access(
                connection, org_id, current["application_id"], session.user.id, organization["member_role"], lock=True
            )
            if current["version"] != body.expected_version or current["status"] != "scheduled":
                raise api_error(409, "INTERVIEW_VERSION_CONFLICT", "Interview changed", "Reload the scheduled interview and try again.")
            ensure_interviewer_slot(
                connection, current["interviewer_user_id"], body.scheduled_at, body.duration_minutes,
                exclude_interview_id=interview_id,
            )
            connection.execute(
                """UPDATE application_interviews SET scheduled_at = %s, duration_minutes = %s,
                          display_timezone = %s, meeting_url = %s, candidate_note = %s,
                          candidate_response = 'pending', response_message = NULL, responded_at = NULL,
                          version = version + 1, updated_at = now() WHERE id = %s""",
                (body.scheduled_at, body.duration_minutes, body.display_timezone, body.meeting_url, body.candidate_note, interview_id),
            )
            create_user_notification(
                connection, current["candidate_user_id"], interview_id, "interview_rescheduled",
                current["version"] + 1, "Interview time updated",
                "The hiring team changed the interview schedule. Review the updated time in application tracking.",
            )
            connection.execute(
                """INSERT INTO audit_events
                   (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome, metadata)
                   VALUES (%s, %s, 'user', %s, 'application_interview', %s, 'organization.interview_rescheduled', %s, 'success', %s)""",
                (uuid.uuid4(), session.user.id, org_id, interview_id, request.state.request_id,
                 Jsonb({"version": current["version"] + 1})),
            )
            updated = connection.execute(INTERVIEW_SELECT + " WHERE interview.id = %s", (interview_id,)).fetchone()
        response.headers["Cache-Control"] = "no-store"
        return interview_item(updated)

    @app.post(
        "/api/v1/organizations/{org_id}/interviews/{interview_id}/cancel",
        response_model=InterviewItem,
        responses={401: problem_response("Authentication is required."), 403: problem_response("Assigned recruiter access is required."), 404: problem_response("The active interview was not found."), 409: problem_response("The interview changed or is already cancelled."), 422: problem_response("The request is invalid.")},
    )
    def cancel_interview(
        org_id: uuid.UUID,
        interview_id: uuid.UUID,
        body: InterviewCancelRequest,
        request: Request,
        response: Response,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> InterviewItem:
        if not session.user.email_verified:
            raise api_error(403, "EMAIL_VERIFICATION_REQUIRED", "Verified email required", "Verify your email before cancelling interviews.")
        organization = organization_access(connection, session.user.id, org_id)
        if organization["member_role"] not in {"owner", "admin", "recruiter"}:
            raise api_error(403, "RECRUITER_ACCESS_REQUIRED", "Recruiter access required", "A recruiter workspace role is required.")
        with connection.transaction():
            current = connection.execute(
                """SELECT interview.* FROM application_interviews AS interview
                   JOIN job_applications AS application ON application.id = interview.application_id
                   WHERE interview.id = %s AND interview.organization_id = %s AND application.status <> 'withdrawn'
                   FOR UPDATE OF interview, application""",
                (interview_id, org_id),
            ).fetchone()
            if current is None:
                raise api_error(404, "INTERVIEW_NOT_FOUND", "Interview not found", "The active interview was not found in this organization.")
            require_application_reviewer_access(
                connection, org_id, current["application_id"], session.user.id, organization["member_role"], lock=True
            )
            if current["version"] != body.expected_version or current["status"] != "scheduled":
                raise api_error(409, "INTERVIEW_VERSION_CONFLICT", "Interview changed", "Reload the scheduled interview and try again.")
            connection.execute(
                """UPDATE application_interviews SET status = 'cancelled', cancelled_by_user_id = %s,
                          cancelled_at = now(), cancellation_reason = %s, version = version + 1, updated_at = now()
                   WHERE id = %s""",
                (session.user.id, body.reason, interview_id),
            )
            create_user_notification(
                connection, current["candidate_user_id"], interview_id, "interview_cancelled",
                current["version"] + 1, "Interview cancelled",
                "The hiring team cancelled the interview. Check application tracking for details.",
            )
            connection.execute(
                """INSERT INTO audit_events
                   (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome)
                   VALUES (%s, %s, 'user', %s, 'application_interview', %s, 'organization.interview_cancelled', %s, 'success')""",
                (uuid.uuid4(), session.user.id, org_id, interview_id, request.state.request_id),
            )
            updated = connection.execute(INTERVIEW_SELECT + " WHERE interview.id = %s", (interview_id,)).fetchone()
        response.headers["Cache-Control"] = "no-store"
        return interview_item(updated)

    @app.get("/api/v1/me/notifications", response_model=NotificationListResponse)
    def list_my_notifications(
        response: Response,
        limit: int = Query(default=50, ge=1, le=100),
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> NotificationListResponse:
        access = """FROM user_notifications AS notification
                    JOIN application_interviews AS interview ON interview.id = notification.interview_id
                    JOIN job_applications AS application ON application.id = interview.application_id
                    WHERE notification.user_id = %s AND application.status <> 'withdrawn'
                      AND (
                          (notification.notification_type <> 'interview_response'
                           AND interview.candidate_user_id = %s)
                          OR (notification.notification_type = 'interview_response' AND EXISTS (
                              SELECT 1 FROM organization_memberships AS membership
                              WHERE membership.organization_id = interview.organization_id
                                AND membership.user_id = %s
                                AND membership.role IN ('owner', 'admin', 'recruiter')
                                AND (membership.role IN ('owner', 'admin') OR EXISTS (
                                    SELECT 1 FROM application_reviewer_assignments AS assignment
                                    WHERE assignment.organization_id = interview.organization_id
                                      AND assignment.application_id = interview.application_id
                                      AND assignment.reviewer_user_id = %s
                                ))
                          ))
                      )"""
        access_params = (session.user.id, session.user.id, session.user.id, session.user.id)
        unread_count = connection.execute(
            "SELECT count(*) AS unread_count " + access + " AND notification.read_at IS NULL",
            access_params,
        ).fetchone()["unread_count"]
        rows = connection.execute(
            """SELECT notification.id, notification.notification_type, notification.title,
                      notification.message, notification.created_at,
                      notification.read_at IS NOT NULL AS is_read """ + access +
            " ORDER BY notification.created_at DESC, notification.id LIMIT %s",
            access_params + (limit,),
        ).fetchall()
        response.headers["Cache-Control"] = "no-store"
        return NotificationListResponse(
            notifications=[NotificationItem(**row) for row in rows], unread_count=unread_count
        )

    @app.patch(
        "/api/v1/me/notifications/{notification_id}",
        response_model=NotificationItem,
        responses={401: problem_response("Authentication is required."), 403: problem_response("CSRF validation failed."), 404: problem_response("The notification was not found."), 422: problem_response("The request is invalid.")},
    )
    def update_notification(
        notification_id: uuid.UUID,
        body: NotificationPatchRequest,
        response: Response,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> NotificationItem:
        row = connection.execute(
            """UPDATE user_notifications
               SET read_at = CASE WHEN %s THEN COALESCE(read_at, now()) ELSE NULL END
               WHERE id = %s AND user_id = %s
               RETURNING id, notification_type, title, message, created_at, read_at IS NOT NULL AS is_read""",
            (body.is_read, notification_id, session.user.id),
        ).fetchone()
        if row is None:
            raise api_error(404, "NOTIFICATION_NOT_FOUND", "Notification not found", "The notification was not found.")
        response.headers["Cache-Control"] = "no-store"
        return NotificationItem(**row)

    @app.post(
        "/api/v1/me/notifications/read-all",
        status_code=204,
        responses={401: problem_response("Authentication is required."), 403: problem_response("CSRF validation failed.")},
    )
    def mark_notifications_read(
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> Response:
        connection.execute(
            "UPDATE user_notifications SET read_at = now() WHERE user_id = %s AND read_at IS NULL",
            (session.user.id,),
        )
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    @app.get(
        "/api/v1/organizations/{org_id}/jobs/{job_id}/applications",
        response_model=OrganizationApplicationListResponse,
        responses={401: problem_response("Authentication is required."), 403: problem_response("Recruiter access is required."), 404: problem_response("The job was not found in this organization.")},
    )
    def list_job_applications(
        org_id: uuid.UUID,
        job_id: uuid.UUID,
        response: Response,
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
        offset: Annotated[int, Query(ge=0, le=10000)] = 0,
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> OrganizationApplicationListResponse:
        organization = organization_access(connection, session.user.id, org_id)
        if organization["member_role"] not in {"owner", "admin", "recruiter"}:
            raise api_error(403, "RECRUITER_ACCESS_REQUIRED", "Recruiter access required", "A recruiter workspace role is required.")
        if not connection.execute("SELECT 1 FROM organization_jobs WHERE id = %s AND organization_id = %s", (job_id, org_id)).fetchone():
            raise api_error(404, "JOB_NOT_FOUND", "Job not found", "The job was not found in this organization.")
        rows = connection.execute(
            """SELECT application.id, application.portfolio_snapshot->>'display_name' AS applicant_name,
                      application.status, application.submitted_at, application.version,
                      assignment.reviewer_user_id AS assigned_reviewer_user_id,
                      reviewer.full_name AS assigned_reviewer_name
               FROM job_applications AS application
               LEFT JOIN application_reviewer_assignments AS assignment
                 ON assignment.organization_id = application.organization_id AND assignment.application_id = application.id
               LEFT JOIN users AS reviewer ON reviewer.id = assignment.reviewer_user_id
               WHERE application.organization_id = %s AND application.job_id = %s AND application.status <> 'withdrawn'
                 AND (%s OR assignment.reviewer_user_id = %s)
               ORDER BY application.submitted_at DESC, application.id LIMIT %s OFFSET %s""",
            (org_id, job_id, organization["member_role"] in {"owner", "admin"}, session.user.id, limit, offset),
        ).fetchall()
        response.headers["Cache-Control"] = "no-store"
        return OrganizationApplicationListResponse(applications=[OrganizationApplicationListItem(**row) for row in rows])

    @app.post(
        "/api/v1/organizations/{org_id}/applications/{application_id}/assign-reviewer",
        response_model=ApplicationReviewerAssignmentResponse,
        status_code=201,
        responses={
            200: {"model": ApplicationReviewerAssignmentResponse, "description": "The assignment was updated or the idempotent request was replayed."},
            401: problem_response("Authentication is required."),
            403: problem_response("A verified organization owner or admin is required."),
            404: problem_response("The active application or organization reviewer was not found."),
            409: problem_response("The application assignment changed or the request key was reused."),
            422: problem_response("The request is invalid."),
        },
    )
    def assign_application_reviewer(
        org_id: uuid.UUID,
        application_id: uuid.UUID,
        body: ApplicationReviewerAssignmentRequest,
        request: Request,
        response: Response,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> ApplicationReviewerAssignmentResponse:
        if not session.user.email_verified:
            raise api_error(403, "EMAIL_VERIFICATION_REQUIRED", "Verified email required", "Verify your email before assigning an application reviewer.")
        payload_hash = hash_token(
            f"application-reviewer-assignment:v1:{application_id}:{body.reviewer_user_id}:{body.expected_version}"
        )
        with connection.transaction():
            organization = organization_access(connection, session.user.id, org_id, for_update=True)
            if organization["member_role"] not in {"owner", "admin"}:
                raise api_error(403, "HIRING_MANAGER_REQUIRED", "Hiring manager required", "An organization owner or admin must assign application reviewers.")
            application = connection.execute(
                "SELECT id FROM job_applications WHERE id = %s AND organization_id = %s AND status <> 'withdrawn' FOR UPDATE",
                (application_id, org_id),
            ).fetchone()
            if application is None:
                raise api_error(404, "APPLICATION_NOT_FOUND", "Application not found", "The active application was not found in this organization.")
            reviewer = connection.execute(
                """SELECT account.full_name FROM organization_memberships AS membership
                   JOIN users AS account ON account.id = membership.user_id
                   WHERE membership.organization_id = %s AND membership.user_id = %s
                     AND membership.role IN ('owner', 'admin', 'recruiter')
                   FOR SHARE OF membership""",
                (org_id, body.reviewer_user_id),
            ).fetchone()
            if reviewer is None:
                raise api_error(404, "APPLICATION_REVIEWER_NOT_FOUND", "Reviewer not found", "An eligible hiring reviewer was not found in this organization.")
            current = connection.execute(
                "SELECT * FROM application_reviewer_assignments WHERE application_id = %s FOR UPDATE",
                (application_id,),
            ).fetchone()
            if current and current["request_key"] == body.request_key:
                if bytes(current["request_payload_hash"]) != payload_hash:
                    raise api_error(409, "APPLICATION_REVIEWER_KEY_REUSED", "Request conflict", "Use a new request key for a different reviewer assignment.")
                response.status_code = 200
            else:
                current_version = current["version"] if current else 0
                if current_version != body.expected_version:
                    raise api_error(409, "APPLICATION_REVIEWER_VERSION_CONFLICT", "Assignment changed", "Reload the reviewer assignment and try again.")
                if current and current["reviewer_user_id"] == body.reviewer_user_id:
                    connection.execute(
                        "UPDATE application_reviewer_assignments SET request_key = %s, request_payload_hash = %s WHERE application_id = %s",
                        (body.request_key, payload_hash, application_id),
                    )
                    response.status_code = 200
                elif current:
                    connection.execute(
                        """UPDATE application_reviewer_assignments
                           SET reviewer_user_id = %s, assigned_by_user_id = %s, request_key = %s,
                               request_payload_hash = %s, version = version + 1, assigned_at = now(), updated_at = now()
                           WHERE application_id = %s""",
                        (body.reviewer_user_id, session.user.id, body.request_key, payload_hash, application_id),
                    )
                    response.status_code = 200
                else:
                    connection.execute(
                        """INSERT INTO application_reviewer_assignments
                           (organization_id, application_id, reviewer_user_id, assigned_by_user_id, request_key, request_payload_hash)
                           VALUES (%s, %s, %s, %s, %s, %s)""",
                        (org_id, application_id, body.reviewer_user_id, session.user.id, body.request_key, payload_hash),
                    )
                if not current or current["reviewer_user_id"] != body.reviewer_user_id:
                    connection.execute(
                        """INSERT INTO audit_events
                           (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome, metadata)
                           VALUES (%s, %s, 'user', %s, 'application_reviewer_assignment', %s, 'organization.application_reviewer_assigned', %s, 'success', %s)""",
                        (uuid.uuid4(), session.user.id, org_id, application_id, request.state.request_id,
                         Jsonb({"reviewer_user_id": str(body.reviewer_user_id)})),
                    )
            assigned = connection.execute(
                """SELECT assignment.application_id, assignment.reviewer_user_id,
                          reviewer.full_name AS reviewer_name, assignment.assigned_by_user_id,
                          assignment.version, assignment.assigned_at, assignment.updated_at
                   FROM application_reviewer_assignments AS assignment
                   JOIN users AS reviewer ON reviewer.id = assignment.reviewer_user_id
                   WHERE assignment.application_id = %s""",
                (application_id,),
            ).fetchone()
        response.headers["Cache-Control"] = "no-store"
        return ApplicationReviewerAssignmentResponse(**assigned)

    @app.get(
        "/api/v1/organizations/{org_id}/applications/{application_id}",
        response_model=OrganizationApplicationResponse,
        responses={401: problem_response("Authentication is required."), 403: problem_response("Recruiter access is required."), 404: problem_response("The application was not found in this organization.")},
    )
    def get_organization_application(
        org_id: uuid.UUID,
        application_id: uuid.UUID,
        response: Response,
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> OrganizationApplicationResponse:
        organization = organization_access(connection, session.user.id, org_id)
        if organization["member_role"] not in {"owner", "admin", "recruiter"}:
            raise api_error(403, "RECRUITER_ACCESS_REQUIRED", "Recruiter access required", "A recruiter workspace role is required.")
        row = connection.execute(
            """SELECT application.*, application.portfolio_snapshot->>'display_name' AS applicant_name,
                      assignment.reviewer_user_id AS assigned_reviewer_user_id,
                      reviewer.full_name AS assigned_reviewer_name,
                      COALESCE(assignment.version, 0) AS reviewer_assignment_version,
                      assignment.assigned_at AS reviewer_assigned_at
               FROM job_applications AS application
               LEFT JOIN application_reviewer_assignments AS assignment
                 ON assignment.organization_id = application.organization_id AND assignment.application_id = application.id
               LEFT JOIN users AS reviewer ON reviewer.id = assignment.reviewer_user_id
               WHERE application.id = %s AND application.organization_id = %s AND application.status <> 'withdrawn'""",
            (application_id, org_id),
        ).fetchone()
        if row is None:
            raise api_error(404, "APPLICATION_NOT_FOUND", "Application not found", "The application was not found in this organization.")
        require_application_reviewer_access(connection, org_id, application_id, session.user.id, organization["member_role"])
        response.headers["Cache-Control"] = "no-store"
        return organization_application_response(row)

    @app.get(
        "/api/v1/organizations/{org_id}/applications/{application_id}/evidence-report",
        response_model=OrganizationApplicationEvidenceReport,
        summary="Read a candidate-consented, deterministic competency evidence report",
        description="Only organization owners/admins and the assigned application reviewer may read evidence explicitly shared for this application. Self-reported claims never satisfy a verified job requirement.",
        responses={401: problem_response("Authentication is required."), 403: problem_response("Recruiter access is required."), 404: problem_response("The application or reviewer assignment was not found.")},
    )
    def get_organization_application_evidence_report(
        org_id: uuid.UUID,
        application_id: uuid.UUID,
        response: Response,
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> OrganizationApplicationEvidenceReport:
        organization = organization_access(connection, session.user.id, org_id)
        if organization["member_role"] not in {"owner", "admin", "recruiter"}:
            raise api_error(403, "RECRUITER_ACCESS_REQUIRED", "Recruiter access required", "A recruiter workspace role is required.")
        application = connection.execute(
            """SELECT id, user_id, status, job_snapshot FROM job_applications
               WHERE id = %s AND organization_id = %s AND status <> 'withdrawn'""",
            (application_id, org_id),
        ).fetchone()
        if application is None:
            raise api_error(404, "APPLICATION_NOT_FOUND", "Application not found", "The application was not found in this organization.")
        require_application_reviewer_access(connection, org_id, application_id, session.user.id, organization["member_role"])
        event = connection.execute(
            """SELECT id, action FROM candidate_application_evidence_share_events
               WHERE application_id = %s ORDER BY version DESC LIMIT 1""",
            (application_id,),
        ).fetchone()
        share_status = event["action"] if event else "not_shared"
        evidence_ids = []
        if event and event["action"] == "shared":
            evidence_ids = [row["evidence_id"] for row in connection.execute(
                "SELECT evidence_id FROM candidate_application_evidence_share_items WHERE share_event_id = %s ORDER BY evidence_id",
                (event["id"],),
            ).fetchall()]
        shared = bool(event and event["action"] == "shared")
        evidence = load_application_evidence(connection, application["user_id"], evidence_ids) if shared else []
        requirements = (application["job_snapshot"] or {}).get("competency_requirements", [])
        gap_report = build_application_gap_report(requirements, evidence, evidence_shared=shared)
        recommendation = None
        if application["status"] not in {"hired", "rejected", "withdrawn"}:
            recommendation = recommend_focused_assessment(
                connection, org_id, application_id, requirements, gap_report
            )
        response.headers["Cache-Control"] = "no-store"
        return OrganizationApplicationEvidenceReport(
            application_id=application_id, share_status=share_status,
            requirements=gap_report, recommended_assessment=recommendation,
        )

    @app.patch(
        "/api/v1/organizations/{org_id}/applications/{application_id}/stage",
        response_model=OrganizationApplicationResponse,
        responses={401: problem_response("Authentication is required."), 403: problem_response("Recruiter access is required."), 404: problem_response("The application was not found in this organization."), 409: problem_response("The stage transition is invalid or the application changed."), 422: problem_response("The request is invalid.")},
    )
    def update_application_stage(
        org_id: uuid.UUID,
        application_id: uuid.UUID,
        body: ApplicationStageRequest,
        request: Request,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> OrganizationApplicationResponse:
        if not session.user.email_verified:
            raise api_error(403, "EMAIL_VERIFICATION_REQUIRED", "Verified email required", "Verify your email before reviewing applications.")
        organization = organization_access(connection, session.user.id, org_id)
        if organization["member_role"] not in {"owner", "admin", "recruiter"}:
            raise api_error(403, "RECRUITER_ACCESS_REQUIRED", "Recruiter access required", "A recruiter workspace role is required.")
        transitions = {
            "submitted": {"in_review"},
            "in_review": {"assessment", "interview", "offer"},
            "assessment": {"interview", "offer"},
            "interview": {"offer"},
        }
        with connection.transaction():
            application = connection.execute(
                """SELECT * FROM job_applications
                   WHERE id = %s AND organization_id = %s AND status <> 'withdrawn' FOR UPDATE""",
                (application_id, org_id),
            ).fetchone()
            if application is None:
                raise api_error(404, "APPLICATION_NOT_FOUND", "Application not found", "The application was not found in this organization.")
            require_application_reviewer_access(
                connection, org_id, application_id, session.user.id, organization["member_role"], lock=True
            )
            if application["version"] != body.expected_version:
                raise api_error(409, "APPLICATION_VERSION_CONFLICT", "Application changed", "Reload the application and try again.")
            if body.stage != application["status"]:
                if body.stage not in transitions.get(application["status"], set()):
                    raise api_error(409, "APPLICATION_STAGE_TRANSITION_INVALID", "Stage transition rejected", "This application cannot move to that stage.")
                connection.execute(
                    "UPDATE job_applications SET status = %s, version = version + 1, updated_at = now() WHERE id = %s",
                    (body.stage, application_id),
                )
                connection.execute(
                    """INSERT INTO application_events (id, application_id, actor_id, actor_type, action, from_status, to_status)
                       VALUES (%s, %s, %s, 'organization', 'stage_changed', %s, %s)""",
                    (uuid.uuid4(), application_id, session.user.id, application["status"], body.stage),
                )
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome)
                       VALUES (%s, %s, 'user', %s, 'job_application', %s, 'organization.application_stage_changed', %s, 'success')""",
                    (uuid.uuid4(), session.user.id, org_id, application_id, request.state.request_id),
                )
            updated = connection.execute(
                """SELECT application.*, application.portfolio_snapshot->>'display_name' AS applicant_name,
                          assignment.reviewer_user_id AS assigned_reviewer_user_id,
                          reviewer.full_name AS assigned_reviewer_name,
                          COALESCE(assignment.version, 0) AS reviewer_assignment_version,
                          assignment.assigned_at AS reviewer_assigned_at
                   FROM job_applications AS application
                   LEFT JOIN application_reviewer_assignments AS assignment
                     ON assignment.organization_id = application.organization_id AND assignment.application_id = application.id
                   LEFT JOIN users AS reviewer ON reviewer.id = assignment.reviewer_user_id
                   WHERE application.id = %s""",
                (application_id,),
            ).fetchone()
        return organization_application_response(updated)

    @app.post(
        "/api/v1/organizations/{org_id}/applications/{application_id}/decision",
        response_model=OrganizationApplicationResponse,
        responses={401: problem_response("Authentication is required."), 403: problem_response("A verified owner or admin is required."), 404: problem_response("The application was not found in this organization."), 409: problem_response("The decision is invalid or the application changed."), 422: problem_response("The request is invalid.")},
    )
    def record_application_decision(
        org_id: uuid.UUID,
        application_id: uuid.UUID,
        body: ApplicationDecisionRequest,
        request: Request,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> OrganizationApplicationResponse:
        if not session.user.email_verified:
            raise api_error(403, "EMAIL_VERIFICATION_REQUIRED", "Verified email required", "Verify your email before making a hiring decision.")
        organization = organization_access(connection, session.user.id, org_id)
        if organization["member_role"] not in {"owner", "admin"}:
            raise api_error(403, "HIRING_MANAGER_REQUIRED", "Hiring manager required", "An organization owner or admin must record the final decision.")
        with connection.transaction():
            application = connection.execute(
                """SELECT * FROM job_applications
                   WHERE id = %s AND organization_id = %s AND status <> 'withdrawn' FOR UPDATE""",
                (application_id, org_id),
            ).fetchone()
            if application is None:
                raise api_error(404, "APPLICATION_NOT_FOUND", "Application not found", "The application was not found in this organization.")
            if application["version"] != body.expected_version:
                raise api_error(409, "APPLICATION_VERSION_CONFLICT", "Application changed", "Reload the application and try again.")
            if application["status"] in {"hired", "rejected"} or (body.decision == "hired" and application["status"] != "offer"):
                raise api_error(409, "APPLICATION_DECISION_INVALID", "Decision rejected", "A hire decision is available only after the offer stage; final decisions cannot be changed.")
            connection.execute(
                """UPDATE job_applications SET status = %s, decision_reason = %s, decided_by_user_id = %s,
                          decided_at = now(), version = version + 1, updated_at = now() WHERE id = %s""",
                (body.decision, body.reason, session.user.id, application_id),
            )
            connection.execute(
                """INSERT INTO application_events (id, application_id, actor_id, actor_type, action, from_status, to_status)
                   VALUES (%s, %s, %s, 'organization', 'decision_recorded', %s, %s)""",
                (uuid.uuid4(), application_id, session.user.id, application["status"], body.decision),
            )
            connection.execute(
                """INSERT INTO audit_events
                   (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome)
                   VALUES (%s, %s, 'user', %s, 'job_application', %s, 'organization.application_decision_recorded', %s, 'success')""",
                (uuid.uuid4(), session.user.id, org_id, application_id, request.state.request_id),
            )
            cancel_application_interviews(
                connection, application_id, org_id, session.user.id, request.state.request_id,
                "organization.interview_cancelled_on_application_decision",
                "The application reached a final hiring decision.",
            )
            cancelled_attempts = connection.execute(
                """UPDATE assessment_attempts SET status = 'cancelled', version = version + 1, updated_at = now()
                   WHERE invitation_id IN (SELECT id FROM assessment_invitations WHERE application_id = %s)
                     AND status IN ('created', 'consented', 'active')
                   RETURNING id""",
                (application_id,),
            ).fetchall()
            for attempt in cancelled_attempts:
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome)
                       VALUES (%s, %s, 'user', %s, 'assessment_attempt', %s, 'organization.assessment_attempt_cancelled_on_application_decision', %s, 'success')""",
                    (uuid.uuid4(), session.user.id, org_id, attempt["id"], request.state.request_id),
                )
            updated = connection.execute(
                """SELECT application.*, application.portfolio_snapshot->>'display_name' AS applicant_name,
                          assignment.reviewer_user_id AS assigned_reviewer_user_id,
                          reviewer.full_name AS assigned_reviewer_name,
                          COALESCE(assignment.version, 0) AS reviewer_assignment_version,
                          assignment.assigned_at AS reviewer_assigned_at
                   FROM job_applications AS application
                   LEFT JOIN application_reviewer_assignments AS assignment
                     ON assignment.organization_id = application.organization_id AND assignment.application_id = application.id
                   LEFT JOIN users AS reviewer ON reviewer.id = assignment.reviewer_user_id
                   WHERE application.id = %s""",
                (application_id,),
            ).fetchone()
        return organization_application_response(updated)

    @app.get(
        "/api/v1/organizations/{org_id}/applications/{application_id}/history",
        response_model=ApplicationHistoryResponse,
        responses={401: problem_response("Authentication is required."), 403: problem_response("Recruiter access is required."), 404: problem_response("The application was not found in this organization.")},
    )
    def get_application_history(
        org_id: uuid.UUID,
        application_id: uuid.UUID,
        response: Response,
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> ApplicationHistoryResponse:
        organization = organization_access(connection, session.user.id, org_id)
        if organization["member_role"] not in {"owner", "admin", "recruiter"}:
            raise api_error(403, "RECRUITER_ACCESS_REQUIRED", "Recruiter access required", "A recruiter workspace role is required.")
        if not connection.execute(
            """SELECT 1 FROM job_applications AS application
               WHERE application.id = %s AND application.organization_id = %s AND application.status <> 'withdrawn'
                 AND (%s OR EXISTS (SELECT 1 FROM application_reviewer_assignments AS assignment
                                    WHERE assignment.application_id = application.id AND assignment.reviewer_user_id = %s))""",
            (application_id, org_id, organization["member_role"] in {"owner", "admin"}, session.user.id),
        ).fetchone():
            raise api_error(404, "APPLICATION_NOT_FOUND", "Application not found", "The application was not found in this organization.")
        rows = connection.execute(
            """SELECT id, actor_id, actor_type, action, from_status, to_status, occurred_at
               FROM application_events WHERE application_id = %s ORDER BY event_number""",
            (application_id,),
        ).fetchall()
        response.headers["Cache-Control"] = "no-store"
        return ApplicationHistoryResponse(events=[ApplicationEventItem(**row) for row in rows])

    @app.get(
        "/api/v1/me/workspaces",
        response_model=WorkspaceListResponse,
        responses={401: problem_response("Authentication is required."), 500: problem_response("Unexpected service failure.")},
    )
    def get_workspaces(
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> WorkspaceListResponse:
        workspaces = []
        candidate = connection.execute(
            """SELECT u.id, u.full_name, u.initial_purpose, c.completed_at
               FROM users AS u LEFT JOIN candidate_onboardings AS c ON c.user_id = u.id
               WHERE u.id = %s AND (u.initial_purpose = 'candidate' OR c.user_id IS NOT NULL)""",
            (session.user.id,),
        ).fetchone()
        if candidate:
            workspaces.append(
                WorkspaceSummary(
                    id=candidate["id"],
                    type="candidate",
                    name=candidate["full_name"],
                    role="candidate",
                    onboarding_status="complete" if candidate["completed_at"] else "draft",
                )
            )
        for row in connection.execute(
            """SELECT o.id, o.name, o.onboarding_completed_at, m.role
               FROM organization_memberships AS m
               JOIN organizations AS o ON o.id = m.organization_id
               WHERE m.user_id = %s ORDER BY o.created_at, o.id""",
            (session.user.id,),
        ).fetchall():
            workspaces.append(
                WorkspaceSummary(
                    id=row["id"],
                    type="organization",
                    name=row["name"],
                    role=row["role"],
                    onboarding_status="complete" if row["onboarding_completed_at"] else "draft",
                )
            )
        return WorkspaceListResponse(workspaces=workspaces)

    @app.post(
        "/api/v1/organizations",
        response_model=OrganizationResponse,
        status_code=201,
        responses={
            401: problem_response("Authentication is required."),
            403: problem_response("A verified email address is required."),
            409: problem_response("The idempotency key was reused with different input."),
            422: problem_response("The request is invalid."),
            429: problem_response("Too many requests."),
            500: problem_response("Unexpected service failure."),
        },
    )
    def create_organization(
        body: OrganizationCreateRequest,
        request: Request,
        response: Response,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> OrganizationResponse:
        if not session.user.email_verified:
            raise api_error(403, "EMAIL_VERIFICATION_REQUIRED", "Verified email required", "Verify your email before creating an organization.")
        client_ip = request.client.host if request.client else "unknown"
        if (
            not app.state.rate_limiter.allow(f"organization-create:ip:{client_ip}", 10, 900)
            or not app.state.rate_limiter.allow(f"organization-create:user:{session.user.id}", 3, 3600)
        ):
            raise api_error(429, "RATE_LIMITED", "Too many requests", "Try again later.")
        payload_hash = hash_token(f"organization-create:v1:{body.name}")
        with connection.transaction():
            organization = connection.execute(
                """INSERT INTO organizations
                   (id, created_by_user_id, creation_key, creation_payload_hash, name)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (created_by_user_id, creation_key) DO NOTHING
                   RETURNING id, name, created_at, onboarding_completed_at""",
                (uuid.uuid4(), session.user.id, body.request_key, payload_hash, body.name),
            ).fetchone()
            if organization:
                connection.execute(
                    """INSERT INTO organization_memberships (organization_id, user_id, role)
                       VALUES (%s, %s, 'owner')""",
                    (organization["id"], session.user.id),
                )
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome)
                       VALUES (%s, %s, 'user', %s, 'organization', %s, 'organization.created', %s, 'success')""",
                    (uuid.uuid4(), session.user.id, organization["id"], organization["id"], request.state.request_id),
                )
                role = "owner"
            else:
                organization = connection.execute(
                    """SELECT o.id, o.name, o.created_at, o.onboarding_completed_at,
                              o.creation_payload_hash, m.role AS member_role
                       FROM organizations AS o
                       LEFT JOIN organization_memberships AS m
                         ON m.organization_id = o.id AND m.user_id = %s
                       WHERE o.created_by_user_id = %s AND o.creation_key = %s""",
                    (session.user.id, session.user.id, body.request_key),
                ).fetchone()
                if organization is None or organization["member_role"] is None:
                    raise api_error(404, "ORGANIZATION_NOT_FOUND", "Organization not found", "The organization was not found.")
                if bytes(organization["creation_payload_hash"]) != payload_hash:
                    raise api_error(409, "IDEMPOTENCY_KEY_REUSED", "Request conflict", "Use a new key for a different organization name.")
                role = organization["member_role"]
        response.headers["Location"] = f"/api/v1/organizations/{organization['id']}"
        return OrganizationResponse(
            id=organization["id"],
            name=organization["name"],
            member_role=role,
            onboarding_status="complete" if organization["onboarding_completed_at"] else "draft",
            created_at=organization["created_at"],
        )

    @app.get(
        "/api/v1/organizations/{org_id}",
        response_model=OrganizationResponse,
        responses={404: problem_response("The organization was not found."), 500: problem_response("Unexpected service failure.")},
    )
    def get_organization(
        org_id: uuid.UUID,
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> OrganizationResponse:
        row = organization_access(connection, session.user.id, org_id)
        return OrganizationResponse(
            id=row["id"],
            name=row["name"],
            member_role=row["member_role"],
            onboarding_status="complete" if row["onboarding_completed_at"] else "draft",
            created_at=row["created_at"],
        )

    @app.get(
        "/api/v1/organizations/{org_id}/candidates",
        response_model=DiscoverableCandidateListResponse,
        responses={
            401: problem_response("Authentication is required."),
            403: problem_response("An organization owner, admin, or recruiter role is required."),
            404: problem_response("The organization was not found."),
            409: problem_response("Complete organization setup before searching for candidates."),
            429: problem_response("Too many candidate searches."),
        },
    )
    def list_discoverable_candidates(
        org_id: uuid.UUID,
        request: Request,
        response: Response,
        q: Annotated[str | None, Query(min_length=2, max_length=80)] = None,
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
        offset: Annotated[int, Query(ge=0, le=10000)] = 0,
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> DiscoverableCandidateListResponse:
        organization = organization_access(connection, session.user.id, org_id)
        if organization["member_role"] not in {"owner", "admin", "recruiter"}:
            raise api_error(403, "RECRUITER_ACCESS_REQUIRED", "Hiring team access required", "An organization owner, admin, or recruiter role is required.")
        if organization["onboarding_completed_at"] is None:
            raise api_error(409, "ORGANIZATION_ONBOARDING_REQUIRED", "Organization setup incomplete", "Complete organization setup before searching for candidates.")
        if not app.state.rate_limiter.allow(f"candidate-discovery:org:{org_id}:user:{session.user.id}", 60, 60):
            raise api_error(429, "RATE_LIMITED", "Too many searches", "Try again later.")
        rows = connection.execute(
            """SELECT account.full_name AS display_name, onboarding.target_roles, onboarding.skills,
                      portfolio.public_handle
               FROM candidate_portfolios AS portfolio
               JOIN users AS account ON account.id = portfolio.user_id
               JOIN candidate_onboardings AS onboarding ON onboarding.user_id = account.id
               WHERE portfolio.discoverable_at IS NOT NULL AND portfolio.published_at IS NOT NULL
                 AND onboarding.completed_at IS NOT NULL
                 AND NOT EXISTS (
                     SELECT 1 FROM organization_memberships AS membership
                     WHERE membership.organization_id = %s AND membership.user_id = portfolio.user_id
                 )
                 AND (%s::text IS NULL OR strpos(lower(account.full_name), lower(%s::text)) > 0
                      OR strpos(lower(portfolio.public_handle), lower(%s::text)) > 0
                      OR EXISTS (
                          SELECT 1 FROM unnest(
                              COALESCE(onboarding.target_roles, '{}'::text[]) || COALESCE(onboarding.skills, '{}'::text[])
                          ) AS label WHERE strpos(lower(label), lower(%s::text)) > 0
                      ))
               ORDER BY portfolio.discoverable_at DESC, lower(account.full_name), portfolio.user_id
               LIMIT %s OFFSET %s""",
            (org_id, q, q, q, q, limit + 1, offset),
        ).fetchall()
        has_more = len(rows) > limit
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return DiscoverableCandidateListResponse(
            candidates=[
                DiscoverableCandidateItem(
                    display_name=row["display_name"],
                    target_roles=row["target_roles"],
                    skills=row["skills"],
                    profile_path=f"/u/{row['public_handle']}",
                )
                for row in rows[:limit]
            ],
            limit=limit,
            offset=offset,
            has_more=has_more,
        )

    @app.get(
        "/api/v1/organizations/{org_id}/onboarding",
        response_model=OrganizationOnboardingResponse,
        responses={404: problem_response("The organization was not found."), 500: problem_response("Unexpected service failure.")},
    )
    def get_organization_onboarding(
        org_id: uuid.UUID,
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> OrganizationOnboardingResponse:
        return organization_onboarding_response(organization_access(connection, session.user.id, org_id))

    @app.patch(
        "/api/v1/organizations/{org_id}/onboarding",
        response_model=OrganizationOnboardingResponse,
        responses={
            401: problem_response("Authentication is required."),
            403: problem_response("An organization owner or admin role is required."),
            404: problem_response("The organization was not found."),
            409: problem_response("The onboarding version changed or onboarding is complete."),
            422: problem_response("The request is invalid."),
            500: problem_response("Unexpected service failure."),
        },
    )
    def save_organization_onboarding(
        org_id: uuid.UUID,
        body: OrganizationOnboardingPatch,
        request: Request,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> OrganizationOnboardingResponse:
        with connection.transaction():
            current = organization_access(connection, session.user.id, org_id, for_update=True)
            if current["member_role"] not in ("owner", "admin"):
                raise api_error(403, "ORGANIZATION_ADMIN_REQUIRED", "Access denied", "An organization owner or admin role is required.")
            if current["onboarding_completed_at"]:
                raise api_error(409, "ONBOARDING_ALREADY_COMPLETE", "Onboarding complete", "This onboarding is already complete.")
            if current["version"] != body.expected_version:
                raise api_error(409, "ONBOARDING_VERSION_CONFLICT", "Draft changed", "Reload the organization and try again.")
            connection.execute(
                """UPDATE organizations SET name = %s, version = version + 1, updated_at = now()
                   WHERE id = %s AND version = %s AND onboarding_completed_at IS NULL""",
                (body.name, org_id, body.expected_version),
            )
            connection.execute(
                """INSERT INTO audit_events
                   (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome)
                   VALUES (%s, %s, 'user', %s, 'organization', %s, 'organization.onboarding_saved', %s, 'success')""",
                (uuid.uuid4(), session.user.id, org_id, org_id, request.state.request_id),
            )
        return organization_onboarding_response(organization_access(connection, session.user.id, org_id))

    @app.post(
        "/api/v1/organizations/{org_id}/onboarding/complete",
        response_model=OrganizationOnboardingResponse,
        responses={
            401: problem_response("Authentication is required."),
            403: problem_response("An organization owner or admin role is required."),
            404: problem_response("The organization was not found."),
            409: problem_response("The onboarding version changed."),
            422: problem_response("Required onboarding fields are missing."),
            500: problem_response("Unexpected service failure."),
        },
    )
    def complete_organization_onboarding(
        org_id: uuid.UUID,
        body: OrganizationOnboardingCompletion,
        request: Request,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> OrganizationOnboardingResponse:
        with connection.transaction():
            current = organization_access(connection, session.user.id, org_id, for_update=True)
            if current["member_role"] not in ("owner", "admin"):
                raise api_error(403, "ORGANIZATION_ADMIN_REQUIRED", "Access denied", "An organization owner or admin role is required.")
            if current["onboarding_completed_at"] is None:
                if current["version"] != body.expected_version:
                    raise api_error(409, "ONBOARDING_VERSION_CONFLICT", "Draft changed", "Reload the organization and try again.")
                if len(current["name"].strip()) < 2:
                    raise api_error(422, "ONBOARDING_INCOMPLETE", "Onboarding incomplete", "Enter the organization name before completing.")
                connection.execute(
                    """UPDATE organizations SET onboarding_completed_at = now(), version = version + 1,
                              updated_at = now()
                       WHERE id = %s AND version = %s AND onboarding_completed_at IS NULL""",
                    (org_id, body.expected_version),
                )
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome)
                       VALUES (%s, %s, 'user', %s, 'organization', %s, 'organization.onboarding_completed', %s, 'success')""",
                    (uuid.uuid4(), session.user.id, org_id, org_id, request.state.request_id),
                )
        return organization_onboarding_response(organization_access(connection, session.user.id, org_id))

    @app.post(
        "/api/v1/organizations/{org_id}/invitations",
        response_model=OrganizationInvitationResponse,
        status_code=201,
        responses={
            401: problem_response("Authentication is required."),
            403: problem_response("An organization owner or admin role is required."),
            404: problem_response("The organization was not found."),
            409: problem_response("The person is already a member or has a pending invitation."),
            422: problem_response("The request is invalid."),
            429: problem_response("Too many invitations were requested."),
            503: problem_response("Invitation delivery failed."),
            500: problem_response("Unexpected service failure."),
        },
    )
    def create_organization_invitation(
        org_id: uuid.UUID,
        body: OrganizationInvitationCreateRequest,
        request: Request,
        response: Response,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> OrganizationInvitationResponse:
        client_ip = request.client.host if request.client else "unknown"
        if (
            not app.state.rate_limiter.allow(f"org-invitation:ip:{client_ip}", 20, 900)
            or not app.state.rate_limiter.allow(f"org-invitation:user:{session.user.id}", 10, 3600)
        ):
            raise api_error(429, "RATE_LIMITED", "Too many invitations", "Try again later.")
        token = secrets.token_urlsafe(32)
        invitation_id = uuid.uuid4()
        with connection.transaction():
            organization = organization_access(connection, session.user.id, org_id, for_update=True)
            if organization["member_role"] not in ("owner", "admin"):
                raise api_error(403, "ORGANIZATION_ADMIN_REQUIRED", "Access denied", "An organization owner or admin role is required.")
            if organization["member_role"] == "admin" and body.role == "admin":
                raise api_error(403, "ORGANIZATION_OWNER_REQUIRED", "Access denied", "Only an organization owner can invite an admin.")
            existing_member = connection.execute(
                """SELECT 1 FROM organization_memberships AS membership
                   JOIN users AS account ON account.id = membership.user_id
                   WHERE membership.organization_id = %s AND account.email = %s""",
                (org_id, body.email),
            ).fetchone()
            if existing_member:
                raise api_error(409, "ALREADY_ORGANIZATION_MEMBER", "Already a member", "This person already belongs to the organization.")
            connection.execute(
                """UPDATE organization_invitations SET cancelled_at = now()
                   WHERE organization_id = %s AND email = %s AND accepted_at IS NULL
                     AND cancelled_at IS NULL AND expires_at <= now()""",
                (org_id, body.email),
            )
            pending = connection.execute(
                """SELECT 1 FROM organization_invitations
                   WHERE organization_id = %s AND email = %s AND accepted_at IS NULL AND cancelled_at IS NULL""",
                (org_id, body.email),
            ).fetchone()
            if pending:
                raise api_error(409, "INVITATION_ALREADY_PENDING", "Invitation already pending", "Cancel the existing invitation before sending another.")
            invitation = connection.execute(
                """INSERT INTO organization_invitations
                   (id, organization_id, invited_by_user_id, email, role, token_hash, expires_at)
                   VALUES (%s, %s, %s, %s, %s, %s, now() + interval '7 days')
                   RETURNING id, email, role, expires_at, created_at""",
                (invitation_id, org_id, session.user.id, body.email, body.role, hash_token(token)),
            ).fetchone()
            connection.execute(
                """INSERT INTO audit_events
                   (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome)
                   VALUES (%s, %s, 'user', %s, 'organization_invitation', %s, 'organization.invitation_created', %s, 'success')""",
                (uuid.uuid4(), session.user.id, org_id, invitation_id, request.state.request_id),
            )
        invitation_link = f"{public_url}/organization/invitation#token={token}"
        try:
            delivery.send_organization_invitation(body.email, organization["name"], invitation_link)
        except Exception as error:
            with connection.transaction():
                connection.execute(
                    "UPDATE organization_invitations SET cancelled_at = now() WHERE id = %s AND cancelled_at IS NULL AND accepted_at IS NULL",
                    (invitation_id,),
                )
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome)
                       VALUES (%s, %s, 'user', %s, 'organization_invitation', %s, 'organization.invitation_delivery_failed', %s, 'failure')""",
                    (uuid.uuid4(), session.user.id, org_id, invitation_id, request.state.request_id),
                )
            logger.warning("Organization invitation delivery failed for invitation %s", invitation_id)
            raise api_error(503, "INVITATION_DELIVERY_FAILED", "Invitation delivery failed", "Retry after checking the email delivery configuration.") from error
        response.headers["Location"] = f"/api/v1/organizations/{org_id}/invitations/{invitation_id}"
        return organization_invitation_response(invitation)

    @app.get(
        "/api/v1/organizations/{org_id}/invitations",
        response_model=OrganizationInvitationListResponse,
        responses={
            401: problem_response("Authentication is required."),
            403: problem_response("An organization owner or admin role is required."),
            404: problem_response("The organization was not found."),
            500: problem_response("Unexpected service failure."),
        },
    )
    def list_organization_invitations(
        org_id: uuid.UUID,
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> OrganizationInvitationListResponse:
        organization = organization_access(connection, session.user.id, org_id)
        if organization["member_role"] not in ("owner", "admin"):
            raise api_error(403, "ORGANIZATION_ADMIN_REQUIRED", "Access denied", "An organization owner or admin role is required.")
        rows = connection.execute(
            """SELECT id, email, role, expires_at, created_at,
                      CASE WHEN accepted_at IS NOT NULL THEN 'accepted'
                           WHEN cancelled_at IS NOT NULL THEN 'cancelled'
                           WHEN expires_at <= now() THEN 'expired' ELSE 'pending' END AS status
               FROM organization_invitations WHERE organization_id = %s ORDER BY created_at DESC, id""",
            (org_id,),
        ).fetchall()
        return OrganizationInvitationListResponse(
            invitations=[organization_invitation_response(row) for row in rows]
        )

    @app.delete(
        "/api/v1/organizations/{org_id}/invitations/{invite_id}",
        status_code=204,
        responses={
            401: problem_response("Authentication is required."),
            403: problem_response("An organization owner or admin role is required."),
            404: problem_response("The invitation was not found."),
            409: problem_response("The invitation has already been accepted."),
            500: problem_response("Unexpected service failure."),
        },
    )
    def cancel_organization_invitation(
        org_id: uuid.UUID,
        invite_id: uuid.UUID,
        request: Request,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> Response:
        with connection.transaction():
            organization = organization_access(connection, session.user.id, org_id, for_update=True)
            if organization["member_role"] not in ("owner", "admin"):
                raise api_error(403, "ORGANIZATION_ADMIN_REQUIRED", "Access denied", "An organization owner or admin role is required.")
            invitation = connection.execute(
                """SELECT accepted_at, cancelled_at FROM organization_invitations
                   WHERE id = %s AND organization_id = %s FOR UPDATE""",
                (invite_id, org_id),
            ).fetchone()
            if invitation is None:
                raise api_error(404, "INVITATION_NOT_FOUND", "Invitation not found", "The invitation was not found.")
            if invitation["accepted_at"]:
                raise api_error(409, "INVITATION_ALREADY_ACCEPTED", "Invitation accepted", "An accepted invitation cannot be cancelled.")
            if invitation["cancelled_at"] is None:
                connection.execute(
                    "UPDATE organization_invitations SET cancelled_at = now() WHERE id = %s",
                    (invite_id,),
                )
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome)
                       VALUES (%s, %s, 'user', %s, 'organization_invitation', %s, 'organization.invitation_cancelled', %s, 'success')""",
                    (uuid.uuid4(), session.user.id, org_id, invite_id, request.state.request_id),
                )
        return Response(status_code=204)

    @app.post(
        "/api/v1/organization-invitations/accept",
        response_model=OrganizationInvitationAcceptanceResponse,
        responses={
            401: problem_response("Authentication is required."),
            403: problem_response("A verified account matching the invitation is required."),
            400: problem_response("The invitation is invalid, expired, cancelled or already used."),
            409: problem_response("The user is already an organization member."),
            422: problem_response("The request is invalid."),
            500: problem_response("Unexpected service failure."),
        },
    )
    def accept_organization_invitation(
        body: OrganizationInvitationAcceptanceRequest,
        request: Request,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> OrganizationInvitationAcceptanceResponse:
        if not session.user.email_verified:
            raise api_error(403, "EMAIL_VERIFICATION_REQUIRED", "Verified email required", "Verify your email before accepting an invitation.")
        token_hash = hash_token(body.token)
        found = connection.execute(
            "SELECT organization_id FROM organization_invitations WHERE token_hash = %s", (token_hash,)
        ).fetchone()
        if found is None:
            raise api_error(400, "INVITATION_INVALID", "Invitation unavailable", "The invitation is invalid, expired, cancelled or already used.")
        with connection.transaction():
            organization = connection.execute(
                "SELECT id, name FROM organizations WHERE id = %s FOR UPDATE", (found["organization_id"],)
            ).fetchone()
            invitation = connection.execute(
                """SELECT id, organization_id, email, role, expires_at, accepted_at, accepted_by_user_id, cancelled_at
                   FROM organization_invitations WHERE token_hash = %s AND organization_id = %s FOR UPDATE""",
                (token_hash, found["organization_id"]),
            ).fetchone()
            if organization is None or invitation is None or invitation["accepted_at"] or invitation["cancelled_at"] or invitation["expires_at"] <= datetime.now(invitation["expires_at"].tzinfo):
                raise api_error(400, "INVITATION_INVALID", "Invitation unavailable", "The invitation is invalid, expired, cancelled or already used.")
            if invitation["email"] != session.user.email.lower():
                raise api_error(403, "INVITATION_EMAIL_MISMATCH", "Invitation email mismatch", "Sign in with the verified email address that received the invitation.")
            existing = connection.execute(
                "SELECT 1 FROM organization_memberships WHERE organization_id = %s AND user_id = %s",
                (organization["id"], session.user.id),
            ).fetchone()
            if existing:
                raise api_error(409, "ALREADY_ORGANIZATION_MEMBER", "Already a member", "This user already belongs to the organization.")
            connection.execute(
                "INSERT INTO organization_memberships (organization_id, user_id, role) VALUES (%s, %s, %s)",
                (organization["id"], session.user.id, invitation["role"]),
            )
            connection.execute(
                "UPDATE organization_invitations SET accepted_at = now(), accepted_by_user_id = %s WHERE id = %s",
                (session.user.id, invitation["id"]),
            )
            connection.execute(
                """INSERT INTO audit_events
                   (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome)
                   VALUES (%s, %s, 'user', %s, 'organization_invitation', %s, 'organization.invitation_accepted', %s, 'success')""",
                (uuid.uuid4(), session.user.id, organization["id"], invitation["id"], request.state.request_id),
            )
        return OrganizationInvitationAcceptanceResponse(
            organization_id=organization["id"], organization_name=organization["name"], member_role=invitation["role"]
        )

    @app.get(
        "/api/v1/organizations/{org_id}/members",
        response_model=OrganizationMemberListResponse,
        responses={
            401: problem_response("Authentication is required."),
            404: problem_response("The organization was not found."),
            500: problem_response("Unexpected service failure."),
        },
    )
    def list_organization_members(
        org_id: uuid.UUID,
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> OrganizationMemberListResponse:
        organization_access(connection, session.user.id, org_id)
        rows = connection.execute(
            """SELECT account.id AS user_id, account.full_name AS display_name, membership.role, membership.created_at AS joined_at
               FROM organization_memberships AS membership JOIN users AS account ON account.id = membership.user_id
               WHERE membership.organization_id = %s ORDER BY membership.created_at, account.id""",
            (org_id,),
        ).fetchall()
        return OrganizationMemberListResponse(members=[OrganizationMemberResponse(**row) for row in rows])

    @app.patch(
        "/api/v1/organizations/{org_id}/members/{member_id}/role",
        response_model=OrganizationMemberResponse,
        responses={
            401: problem_response("Authentication is required."),
            403: problem_response("An organization owner or admin role is required."),
            404: problem_response("The organization member was not found."),
            409: problem_response("The last owner cannot be removed."),
            422: problem_response("The request is invalid."),
            500: problem_response("Unexpected service failure."),
        },
    )
    def update_organization_member_role(
        org_id: uuid.UUID,
        member_id: uuid.UUID,
        body: OrganizationMemberRolePatch,
        request: Request,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> OrganizationMemberResponse:
        with connection.transaction():
            organization = organization_access(connection, session.user.id, org_id, for_update=True)
            if organization["member_role"] not in ("owner", "admin"):
                raise api_error(403, "ORGANIZATION_ADMIN_REQUIRED", "Access denied", "An organization owner or admin role is required.")
            member = connection.execute(
                """SELECT account.id AS user_id, account.full_name AS display_name, membership.role, membership.created_at AS joined_at
                   FROM organization_memberships AS membership JOIN users AS account ON account.id = membership.user_id
                   WHERE membership.organization_id = %s AND membership.user_id = %s FOR UPDATE OF membership""",
                (org_id, member_id),
            ).fetchone()
            if member is None:
                raise api_error(404, "ORGANIZATION_MEMBER_NOT_FOUND", "Member not found", "The organization member was not found.")
            if organization["member_role"] == "admin" and (member["role"] in ("owner", "admin") or body.role == "admin"):
                raise api_error(403, "ORGANIZATION_OWNER_REQUIRED", "Access denied", "Only an organization owner can manage admin roles.")
            if member["role"] == "owner":
                owner_count = connection.execute(
                    "SELECT count(*) AS count FROM organization_memberships WHERE organization_id = %s AND role = 'owner'",
                    (org_id,),
                ).fetchone()["count"]
                if owner_count <= 1:
                    raise api_error(409, "LAST_ORGANIZATION_OWNER", "Last owner protected", "An organization must keep at least one owner.")
            if member["role"] != body.role:
                if body.role == "member":
                    assignments = connection.execute(
                        """DELETE FROM application_reviewer_assignments
                           WHERE organization_id = %s AND reviewer_user_id = %s RETURNING application_id""",
                        (org_id, member_id),
                    ).fetchall()
                    for assignment in assignments:
                        connection.execute(
                            """INSERT INTO audit_events
                               (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome, metadata)
                               VALUES (%s, %s, 'user', %s, 'application_reviewer_assignment', %s, 'organization.application_reviewer_revoked_on_role_change', %s, 'success', %s)""",
                            (uuid.uuid4(), session.user.id, org_id, assignment["application_id"], request.state.request_id,
                             Jsonb({"reviewer_user_id": str(member_id)})),
                        )
                connection.execute(
                    "UPDATE organization_memberships SET role = %s WHERE organization_id = %s AND user_id = %s",
                    (body.role, org_id, member_id),
                )
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome, metadata)
                       VALUES (%s, %s, 'user', %s, 'organization_membership', %s, 'organization.member_role_changed', %s, 'success', %s)""",
                    (uuid.uuid4(), session.user.id, org_id, member_id, request.state.request_id, Jsonb({"role": body.role})),
                )
                member["role"] = body.role
        return OrganizationMemberResponse(**member)

    @app.delete(
        "/api/v1/organizations/{org_id}/members/{member_id}",
        status_code=204,
        responses={
            401: problem_response("Authentication is required."),
            403: problem_response("An organization owner or admin role is required."),
            404: problem_response("The organization member was not found."),
            409: problem_response("The last owner cannot be removed."),
            500: problem_response("Unexpected service failure."),
        },
    )
    def remove_organization_member(
        org_id: uuid.UUID,
        member_id: uuid.UUID,
        request: Request,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> Response:
        with connection.transaction():
            organization = organization_access(connection, session.user.id, org_id, for_update=True)
            if organization["member_role"] not in ("owner", "admin"):
                raise api_error(403, "ORGANIZATION_ADMIN_REQUIRED", "Access denied", "An organization owner or admin role is required.")
            member = connection.execute(
                "SELECT role FROM organization_memberships WHERE organization_id = %s AND user_id = %s FOR UPDATE",
                (org_id, member_id),
            ).fetchone()
            if member is None:
                raise api_error(404, "ORGANIZATION_MEMBER_NOT_FOUND", "Member not found", "The organization member was not found.")
            if organization["member_role"] == "admin" and member["role"] in ("owner", "admin"):
                raise api_error(403, "ORGANIZATION_OWNER_REQUIRED", "Access denied", "Only an organization owner can remove an admin or owner.")
            if member["role"] == "owner":
                owner_count = connection.execute(
                    "SELECT count(*) AS count FROM organization_memberships WHERE organization_id = %s AND role = 'owner'",
                    (org_id,),
                ).fetchone()["count"]
                if owner_count <= 1:
                    raise api_error(409, "LAST_ORGANIZATION_OWNER", "Last owner protected", "An organization must keep at least one owner.")
            connection.execute(
                "DELETE FROM organization_memberships WHERE organization_id = %s AND user_id = %s",
                (org_id, member_id),
            )
            connection.execute(
                """INSERT INTO audit_events
                   (id, actor_id, actor_type, tenant_id, resource_type, resource_id, action, correlation_id, outcome)
                   VALUES (%s, %s, 'user', %s, 'organization_membership', %s, 'organization.member_removed', %s, 'success')""",
                (uuid.uuid4(), session.user.id, org_id, member_id, request.state.request_id),
            )
        return Response(status_code=204)

    return app


app = create_app()
