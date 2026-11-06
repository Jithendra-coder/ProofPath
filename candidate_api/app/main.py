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
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg
from psycopg.types.json import Jsonb
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Query, Request, Response, Security, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import APIKeyCookie, APIKeyHeader
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from candidate_api.app.db import connect_database, migrate_database
from candidate_api.app.email_delivery import Mailer, create_mailer
from candidate_api.app.rate_limit import PostgresRateLimiter
from candidate_api.app.security import DUMMY_PASSWORD_HASH, hash_password, hash_token, verify_password
from candidate_api.app.resume import MAX_RESUME_BYTES, MAX_RESUME_TEXT, ResumeProcessingError, extract_resume_text, normalize_resume_text, suggest_resume_fields


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


class RegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: Annotated[str, Field(min_length=1, max_length=120)]
    email: Annotated[EmailStr, Field(max_length=254)]
    password: Annotated[str, Field(min_length=12, max_length=128)]
    initial_purpose: Literal["candidate"]
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
    next_action: Literal["candidate_onboarding"]


class SessionResponse(BaseModel):
    user: AuthUser
    next_action: Literal["candidate_onboarding"]


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
    resume: dict[str, Any] | None
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


class ResumeFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: Annotated[str, Field(max_length=120)] = ""
    email: Annotated[str, Field(max_length=254)] = ""
    phone: Annotated[str, Field(max_length=40)] = ""
    target_roles: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(default_factory=list, max_length=5)
    skills: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(default_factory=list, max_length=50)
    education: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(default_factory=list, max_length=20)
    experience: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(default_factory=list, max_length=20)
    projects: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(default_factory=list, max_length=20)
    certifications: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(default_factory=list, max_length=20)

    @field_validator("full_name", "email", "phone")
    @classmethod
    def clean_resume_value(cls, value: str) -> str:
        value = " ".join(value.split())
        if any(ord(character) < 32 for character in value):
            raise ValueError("Remove control characters from this value.")
        return value

    @field_validator("target_roles", "skills", "education", "experience", "projects", "certifications")
    @classmethod
    def clean_resume_lists(cls, values: list[str]) -> list[str]:
        clean = []
        seen = set()
        for value in values:
            value = " ".join(value.split())
            key = value.casefold()
            if not value or any(ord(character) < 32 for character in value):
                raise ValueError("Remove empty or invalid entries.")
            if key not in seen:
                seen.add(key)
                clean.append(value)
        return clean
        return clean


class ResumeTextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: Annotated[str, Field(min_length=40, max_length=MAX_RESUME_TEXT)]


class ResumeReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    fields: ResumeFields


class CandidateResumeResponse(BaseModel):
    id: uuid.UUID
    filename: str
    media_type: str
    byte_size: int
    sha256: str
    extraction_method: Literal["heuristic"]
    extracted_data: ResumeFields
    reviewed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class CurrentResumeResponse(BaseModel):
    resume: CandidateResumeResponse | None


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
























































































































class WorkspaceSummary(BaseModel):
    id: uuid.UUID
    type: Literal["candidate", "organization"]
    name: str
    role: Literal["candidate", "owner", "admin", "recruiter", "member"]
    onboarding_status: Literal["draft", "complete"]


class WorkspaceListResponse(BaseModel):
    workspaces: list[WorkspaceSummary]


class PublicMetadata(BaseModel):
    initial_purposes: list[Literal["candidate"]]
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


def candidate_resume_response(row: dict[str, Any] | None) -> CandidateResumeResponse | None:
    if row is None:
        return None
    return CandidateResumeResponse(
        id=row["id"],
        filename=row["filename"],
        media_type=row["media_type"],
        byte_size=row["byte_size"],
        sha256=row["sha256"].strip(),
        extraction_method="heuristic",
        extracted_data=ResumeFields.model_validate(row["extracted_data"]),
        reviewed_at=row["reviewed_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def get_candidate_resume(connection: psycopg.Connection, user_id: uuid.UUID) -> dict[str, Any] | None:
    return connection.execute(
        """SELECT id, filename, media_type, byte_size, sha256, extracted_data,
                  reviewed_at, created_at, updated_at
           FROM candidate_resumes WHERE user_id = %s""",
        (user_id,),
    ).fetchone()


def save_candidate_resume(
    connection: psycopg.Connection,
    user_id: uuid.UUID,
    *,
    filename: str,
    media_type: str,
    content: bytes,
    extracted_text: str,
    extracted_data: dict[str, Any],
) -> dict[str, Any]:
    digest = hashlib.sha256(content).hexdigest()
    current = get_candidate_resume(connection, user_id)
    if current and current["sha256"].strip() == digest:
        return current
    return connection.execute(
        """INSERT INTO candidate_resumes
               (id, user_id, filename, media_type, sha256, byte_size, content, extracted_text, extracted_data)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (user_id) DO UPDATE SET
               id = EXCLUDED.id, filename = EXCLUDED.filename, media_type = EXCLUDED.media_type,
               sha256 = EXCLUDED.sha256, byte_size = EXCLUDED.byte_size, content = EXCLUDED.content,
               extracted_text = EXCLUDED.extracted_text, extracted_data = EXCLUDED.extracted_data,
               reviewed_at = NULL, updated_at = now()
           RETURNING id, filename, media_type, byte_size, sha256, extracted_data,
                     reviewed_at, created_at, updated_at""",
        (
            uuid.uuid4(), user_id, filename, media_type, digest, len(content), content,
            extracted_text, Jsonb(extracted_data),
        ),
    ).fetchone()




















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
    resume = connection.execute(
        """SELECT filename, media_type, sha256, byte_size, encode(content, 'base64') AS content_base64,
                  extracted_text, extracted_data, reviewed_at, created_at, updated_at
           FROM candidate_resumes WHERE user_id = %s""",
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
        resume=resume,
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
            "The current resume file, extracted text, and reviewed fields are included in the account export.",
        ],
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
) -> FastAPI:
    dsn = database_url or os.getenv("DATABASE_URL")
    environment = app_env or os.getenv("APP_ENV", "development")
    active_terms_version = (terms_version if terms_version is not None else os.getenv("PROOFPATH_TERMS_VERSION")) or None
    active_terms_url = (terms_url if terms_url is not None else os.getenv("PROOFPATH_TERMS_URL")) or None
    rate_limit_secret = os.getenv("PROOFPATH_RATE_LIMIT_SECRET")
    public_url = os.getenv("PROOFPATH_PUBLIC_URL", "http://127.0.0.1:8000").rstrip("/")
    delivery = mailer or create_mailer(
        environment,
        mailbox_path=dev_mailbox or Path(os.getenv("PROOFPATH_DEV_MAILBOX", PROJECT_ROOT / "candidate_api" / "data" / "dev-mailbox.jsonl")),
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
            ("/api/v1/me/portfolio/versions", "post"),
            ("/api/v1/me/portfolio/publish", "post"),
            ("/api/v1/me/portfolio/unpublish", "post"),
            ("/api/v1/me/projects/{project_id}/publish", "post"),
            ("/api/v1/me/projects/{project_id}/unpublish", "post"),
            ("/api/v1/me/resume", "post"),
            ("/api/v1/me/resume/text", "post"),
            ("/api/v1/me/resume/review", "post"),
            ("/api/v1/me/resume", "delete"),
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









    @app.get("/u/{handle}", include_in_schema=False)
    @app.get("/portfolio/{handle}", include_in_schema=False)
    def public_portfolio_page(handle: str) -> FileResponse:
        return FileResponse(
            FRONTEND / "public-portfolio.html",
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex, nofollow"},
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
            initial_purposes=["candidate"],
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
            next_action="candidate_onboarding",
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
        return SessionResponse(
            user=session.user,
            next_action="candidate_onboarding",
        )

    @app.get(
        "/api/v1/me/resume",
        response_model=CurrentResumeResponse,
        responses={401: problem_response("Authentication is required."), 500: problem_response("Unexpected service failure.")},
    )
    def get_my_resume(
        response: Response,
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> CurrentResumeResponse:
        response.headers["Cache-Control"] = "no-store"
        return CurrentResumeResponse(resume=candidate_resume_response(get_candidate_resume(connection, session.user.id)))

    @app.get(
        "/api/v1/me/resume/file",
        response_class=Response,
        responses={401: problem_response("Authentication is required."), 404: problem_response("No resume is available."), 500: problem_response("Unexpected service failure.")},
    )
    def download_my_resume(
        session: SessionContext = Depends(current_session),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> Response:
        row = connection.execute(
            "SELECT filename, media_type, content FROM candidate_resumes WHERE user_id = %s",
            (session.user.id,),
        ).fetchone()
        if row is None:
            raise api_error(404, "RESUME_NOT_FOUND", "Resume not found", "Upload a resume before downloading it.")
        extension = "pdf" if row["media_type"] == "application/pdf" else "docx" if row["media_type"].endswith("wordprocessingml.document") else "txt"
        headers = {
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Content-Disposition": f"attachment; filename=resume.{extension}; filename*=UTF-8''{quote(row['filename'], safe='')}",
        }
        return Response(content=bytes(row["content"]), media_type="application/octet-stream", headers=headers)

    @app.post(
        "/api/v1/me/resume",
        response_model=CandidateResumeResponse,
        responses={401: problem_response("Authentication is required."), 403: problem_response("The CSRF token is missing or the email is not verified."), 413: problem_response("Resume files must be no larger than 5 MB."), 422: problem_response("The file type or document contents are not supported."), 429: problem_response("Too many resume uploads."), 500: problem_response("Unexpected service failure.")},
    )
    async def upload_my_resume(
        request: Request,
        file: UploadFile = File(...),
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> CandidateResumeResponse:
        if not session.user.email_verified:
            raise api_error(403, "EMAIL_VERIFICATION_REQUIRED", "Verified email required", "Verify your email before uploading a resume.")
        if not app.state.rate_limiter.allow(f"resume-upload:user:{session.user.id}", 5, 3600):
            raise api_error(429, "RESUME_UPLOAD_RATE_LIMITED", "Upload limit reached", "Try again later before uploading another resume.")
        content = await file.read(MAX_RESUME_BYTES + 1)
        if len(content) > MAX_RESUME_BYTES:
            raise api_error(413, "RESUME_TOO_LARGE", "Resume is too large", "Resume files must be no larger than 5 MB.")
        filename = Path(file.filename or "").name
        filename = re.sub(r"[^\w .()_-]", "", filename, flags=re.UNICODE).strip()[:180]
        if not filename:
            filename = "resume.pdf" if content.lstrip().startswith(b"%PDF-") else "resume.docx"
        try:
            extracted_text, media_type = extract_resume_text(filename, content)
        except ResumeProcessingError as error:
            raise api_error(422, "RESUME_UNREADABLE", "Resume couldn’t be read", str(error)) from error
        extracted_data = ResumeFields.model_validate(suggest_resume_fields(extracted_text))
        existing = get_candidate_resume(connection, session.user.id)
        if existing and existing["sha256"].strip() == hashlib.sha256(content).hexdigest():
            return candidate_resume_response(existing)
        with connection.transaction():
            saved = save_candidate_resume(
                connection, session.user.id, filename=filename, media_type=media_type,
                content=content, extracted_text=extracted_text,
                extracted_data=extracted_data.model_dump(),
            )
            connection.execute(
                """INSERT INTO audit_events
                   (id, actor_id, actor_type, resource_type, resource_id, action, correlation_id, outcome, metadata)
                   VALUES (%s, %s, 'user', 'candidate_resume', %s, 'candidate.resume_uploaded', %s, 'success', %s)""",
                (uuid.uuid4(), session.user.id, saved["id"], request.state.request_id,
                 Jsonb({"sha256": saved["sha256"].strip(), "byte_size": saved["byte_size"], "media_type": saved["media_type"]})),
            )
        return candidate_resume_response(saved)

    @app.post(
        "/api/v1/me/resume/text",
        response_model=CandidateResumeResponse,
        responses={401: problem_response("Authentication is required."), 403: problem_response("The CSRF token is missing or the email is not verified."), 422: problem_response("Resume text is not valid."), 429: problem_response("Too many resume uploads."), 500: problem_response("Unexpected service failure.")},
    )
    def save_resume_text(
        body: ResumeTextRequest,
        request: Request,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> CandidateResumeResponse:
        if not session.user.email_verified:
            raise api_error(403, "EMAIL_VERIFICATION_REQUIRED", "Verified email required", "Verify your email before saving resume information.")
        if not app.state.rate_limiter.allow(f"resume-upload:user:{session.user.id}", 5, 3600):
            raise api_error(429, "RESUME_UPLOAD_RATE_LIMITED", "Upload limit reached", "Try again later before saving another resume.")
        extracted_text = normalize_resume_text(body.text)
        if len(extracted_text) < 40:
            raise api_error(422, "RESUME_TEXT_TOO_SHORT", "Resume text is too short", "Add at least 40 readable characters.")
        content = extracted_text.encode("utf-8")
        extracted_data = ResumeFields.model_validate(suggest_resume_fields(extracted_text))
        existing = get_candidate_resume(connection, session.user.id)
        if existing and existing["sha256"].strip() == hashlib.sha256(content).hexdigest():
            return candidate_resume_response(existing)
        with connection.transaction():
            saved = save_candidate_resume(
                connection, session.user.id, filename="Pasted resume text.txt", media_type="text/plain",
                content=content, extracted_text=extracted_text,
                extracted_data=extracted_data.model_dump(),
            )
            connection.execute(
                """INSERT INTO audit_events
                   (id, actor_id, actor_type, resource_type, resource_id, action, correlation_id, outcome, metadata)
                   VALUES (%s, %s, 'user', 'candidate_resume', %s, 'candidate.resume_uploaded', %s, 'success', %s)""",
                (uuid.uuid4(), session.user.id, saved["id"], request.state.request_id,
                 Jsonb({"sha256": saved["sha256"].strip(), "byte_size": saved["byte_size"], "media_type": saved["media_type"]})),
            )
        return candidate_resume_response(saved)

    @app.post(
        "/api/v1/me/resume/review",
        response_model=CandidateResumeResponse,
        responses={401: problem_response("Authentication is required."), 403: problem_response("The CSRF token is missing or invalid."), 404: problem_response("No resume is available."), 409: problem_response("The resume changed while it was being reviewed."), 422: problem_response("The reviewed fields are invalid."), 500: problem_response("Unexpected service failure.")},
    )
    def review_my_resume(
        body: ResumeReviewRequest,
        request: Request,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> CandidateResumeResponse:
        with connection.transaction():
            saved = connection.execute(
                """UPDATE candidate_resumes SET extracted_data = %s, reviewed_at = now(), updated_at = now()
                   WHERE user_id = %s AND sha256 = %s
                   RETURNING id, filename, media_type, byte_size, sha256, extracted_data,
                             reviewed_at, created_at, updated_at""",
                (Jsonb(body.fields.model_dump()), session.user.id, body.expected_sha256),
            ).fetchone()
            if saved is None:
                exists = connection.execute("SELECT 1 FROM candidate_resumes WHERE user_id = %s", (session.user.id,)).fetchone()
                if not exists:
                    raise api_error(404, "RESUME_NOT_FOUND", "Resume not found", "Upload a resume before reviewing it.")
                raise api_error(409, "RESUME_CHANGED", "Resume changed", "Reload the current resume and review it again.")
            connection.execute(
                """INSERT INTO audit_events
                   (id, actor_id, actor_type, resource_type, resource_id, action, correlation_id, outcome)
                   VALUES (%s, %s, 'user', 'candidate_resume', %s, 'candidate.resume_reviewed', %s, 'success')""",
                (uuid.uuid4(), session.user.id, saved["id"], request.state.request_id),
            )
        return candidate_resume_response(saved)

    @app.delete(
        "/api/v1/me/resume",
        status_code=204,
        responses={401: problem_response("Authentication is required."), 403: problem_response("The CSRF token is missing or invalid."), 500: problem_response("Unexpected service failure.")},
    )
    def delete_my_resume(
        request: Request,
        session: SessionContext = Depends(require_csrf),
        connection: psycopg.Connection = Depends(get_connection),
    ) -> Response:
        with connection.transaction():
            resume = connection.execute(
                "DELETE FROM candidate_resumes WHERE user_id = %s RETURNING id", (session.user.id,),
            ).fetchone()
            if resume:
                connection.execute(
                    """INSERT INTO audit_events
                       (id, actor_id, actor_type, resource_type, resource_id, action, correlation_id, outcome)
                       VALUES (%s, %s, 'user', 'candidate_resume', %s, 'candidate.resume_deleted', %s, 'success')""",
                    (uuid.uuid4(), session.user.id, resume["id"], request.state.request_id),
                )
        return Response(status_code=204)

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





































































    return app


app = create_app()
