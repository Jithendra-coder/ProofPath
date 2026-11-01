from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
from psycopg.conninfo import conninfo_to_dict
from fastapi.testclient import TestClient

from backend.app.db import connect_database
from backend.app.main import CSRF_COOKIE, SESSION_COOKIE, create_app
from backend.app.rate_limit import PostgresRateLimiter
from backend.app.security import RateLimiter, hash_token, verify_password


TEST_DATABASE_URL = os.getenv(
    "PROOFPATH_TEST_DATABASE_URL",
    "postgresql://proofpath_codex_app@127.0.0.1:5432/proofpath_codex_test",
)
TERMS_VERSION = "test-terms-1"
TERMS_URL = "https://example.invalid/test-terms"


class MemoryMailer:
    available = True

    def __init__(self):
        self.messages = []
        self.password_reset_messages = []
        self.organization_invitation_messages = []
        self.fail_delivery = False

    def send_registration(self, email, full_name, verification_link):
        if self.fail_delivery:
            raise OSError("simulated SMTP outage")
        self.messages.append((email, full_name, verification_link))

    def send_password_reset(self, email, full_name, reset_link):
        if self.fail_delivery:
            raise OSError("simulated SMTP outage")
        self.password_reset_messages.append((email, full_name, reset_link))

    def send_organization_invitation(self, email, organization_name, invitation_link):
        if self.fail_delivery:
            raise OSError("simulated SMTP outage")
        self.organization_invitation_messages.append((email, organization_name, invitation_link))


@pytest.fixture
def auth_client(tmp_path):
    database_name = conninfo_to_dict(TEST_DATABASE_URL).get("dbname", "")
    if not database_name.endswith("_test"):
        pytest.fail("PROOFPATH_TEST_DATABASE_URL must target a database whose name ends in _test.")
    mailer = MemoryMailer()
    app = create_app(
        TEST_DATABASE_URL,
        app_env="test",
        terms_version=TERMS_VERSION,
        terms_url=TERMS_URL,
        mailer=mailer,
        dev_mailbox=tmp_path / "mailbox.jsonl",
    )
    with TestClient(app, base_url="https://proofpath.test") as client:
        with connect_database(TEST_DATABASE_URL) as connection:
            connection.execute(
                "TRUNCATE audit_events, auth_sessions, email_verification_tokens, rate_limit_buckets, users CASCADE"
            )
        yield client, mailer


def registration_body(email="alex@example.com", **overrides):
    body = {
        "full_name": " Alex   Johnson ",
        "email": email,
        "password": "a long secure password",
        "initial_purpose": "candidate",
        "terms_version": TERMS_VERSION,
    }
    return body | overrides


def get_link_token(mailer, index=0):
    link = mailer.messages[index][2]
    assert link is not None
    assert urlparse(link).query == ""  # Fragments keep bearer tokens out of HTTP access logs.
    return parse_qs(urlparse(link).fragment)["token"][0]


def register_and_login(client, mailer, email, purpose="candidate"):
    message_index = len(mailer.messages)
    response = client.post(
        "/api/v1/auth/register",
        json=registration_body(email=email, initial_purpose=purpose),
    )
    assert response.status_code == 202
    token = get_link_token(mailer, message_index)
    assert client.post("/api/v1/auth/email-verifications/confirm", json={"token": token}).status_code == 200
    login = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "a long secure password"}
    )
    assert login.status_code == 200
    return login.json()


def complete_and_publish_candidate_portfolio(client, *, full_name="Applicant Name", handle="application-test", project_name="Published Project"):
    csrf = client.cookies[CSRF_COOKIE]
    saved = client.patch(
        "/api/v1/me/candidate/onboarding",
        json={"expected_version": 0, "full_name": full_name, "target_roles": ["Backend Engineer"], "skills": ["Python"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert saved.status_code == 200, saved.text
    completed = client.post(
        "/api/v1/me/candidate/onboarding/complete", json={"expected_version": saved.json()["version"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert completed.status_code == 200, completed.text
    project = client.post(
        "/api/v1/me/projects",
        json={
            "request_key": str(uuid4()), "name": project_name,
            "problem_statement": "A public project problem.", "contribution": "My public contribution.",
            "technologies": ["Python", "PostgreSQL"],
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert project.status_code == 201, project.text
    private_project = client.post(
        "/api/v1/me/projects",
        json={"request_key": str(uuid4()), "name": "Private Draft Project"},
        headers={"X-CSRF-Token": csrf},
    )
    assert private_project.status_code == 201, private_project.text
    assert client.post(
        "/api/v1/me/candidate-profile/handle", json={"handle": handle}, headers={"X-CSRF-Token": csrf}
    ).status_code == 200
    assert client.post(
        "/api/v1/me/portfolio/publish", json={"consent_version": "public-portfolio-v1"},
        headers={"X-CSRF-Token": csrf},
    ).status_code == 200
    published = client.post(
        f"/api/v1/me/projects/{project.json()['id']}/publish",
        json={"expected_version": 0, "consent_version": "public-project-v1"},
        headers={"X-CSRF-Token": csrf},
    )
    assert published.status_code == 200, published.text
    return project.json()["id"]


def create_published_job(client, *, title="Backend Engineer", organization_name="Application Labs", competency_requirements=None):
    csrf = client.cookies[CSRF_COOKIE]
    organization = client.post(
        "/api/v1/organizations", json={"request_key": str(uuid4()), "name": organization_name},
        headers={"X-CSRF-Token": csrf},
    )
    assert organization.status_code == 201, organization.text
    organization_id = organization.json()["id"]
    saved = client.patch(
        f"/api/v1/organizations/{organization_id}/onboarding",
        json={"expected_version": 0, "name": organization_name}, headers={"X-CSRF-Token": csrf},
    )
    assert saved.status_code == 200, saved.text
    completed = client.post(
        f"/api/v1/organizations/{organization_id}/onboarding/complete",
        json={"expected_version": saved.json()["version"]}, headers={"X-CSRF-Token": csrf},
    )
    assert completed.status_code == 200, completed.text
    job_body = {"request_key": str(uuid4()), "title": title, "description": "Build and maintain reliable product systems."}
    if competency_requirements is not None:
        job_body["competency_requirements"] = competency_requirements
    job = client.post(
        f"/api/v1/organizations/{organization_id}/jobs",
        json=job_body,
        headers={"X-CSRF-Token": csrf},
    )
    assert job.status_code == 201, job.text
    published = client.post(
        f"/api/v1/organizations/{organization_id}/jobs/{job.json()['id']}/publish",
        json={"expected_version": 0}, headers={"X-CSRF-Token": csrf},
    )
    assert published.status_code == 200, published.text
    return organization_id, job.json()["id"]


def test_public_metadata_disables_signup_without_approved_terms():
    app = create_app(TEST_DATABASE_URL, app_env="test", mailer=MemoryMailer())
    with TestClient(app, base_url="https://proofpath.test") as client:
        metadata = client.get("/api/v1/meta/public")
        response = client.post("/api/v1/auth/register", json=registration_body())

    assert metadata.status_code == 200
    assert metadata.json()["registration_enabled"] is False
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")


def test_registration_is_non_enumerating_and_persists_only_hashes(auth_client):
    client, mailer = auth_client
    first = client.post("/api/v1/auth/register", json=registration_body())
    duplicate = client.post(
        "/api/v1/auth/register",
        json=registration_body("ALEX@example.com", full_name="Another Name", password="another secure password"),
    )

    assert first.status_code == duplicate.status_code == 202
    assert first.json() == duplicate.json() == {"next_action": "verify_email"}
    assert len(mailer.messages) == 2
    token = get_link_token(mailer)
    with connect_database(TEST_DATABASE_URL) as connection:
        user = connection.execute(
            "SELECT full_name, email, password_hash, terms_version FROM users"
        ).fetchone()
        token_row = connection.execute("SELECT token_hash FROM email_verification_tokens").fetchone()
        assert connection.execute("SELECT count(*) AS count FROM users").fetchone()["count"] == 1

    assert user["full_name"] == "Alex Johnson"
    assert user["email"] == "alex@example.com"
    assert user["terms_version"] == TERMS_VERSION
    assert verify_password(user["password_hash"], "a long secure password")
    assert "a long secure password" not in user["password_hash"]
    assert bytes(token_row["token_hash"]) == hash_token(token)


def test_registration_validation_and_terms_version_conflict(auth_client):
    client, _ = auth_client
    malformed = client.post("/api/v1/auth/register", json=registration_body(email="not-an-email"))
    extra_field = client.post(
        "/api/v1/auth/register", json=registration_body() | {"role": "admin"}
    )
    invalid_purpose = client.post(
        "/api/v1/auth/register", json=registration_body(initial_purpose="admin")
    )
    stale_terms = client.post(
        "/api/v1/auth/register", json=registration_body(terms_version="old-terms")
    )

    assert malformed.status_code == extra_field.status_code == invalid_purpose.status_code == 422
    assert malformed.headers["content-type"].startswith("application/problem+json")
    assert malformed.json()["status"] == 422
    assert stale_terms.status_code == 409
    assert stale_terms.json()["code"] == "TERMS_VERSION_STALE"


def test_verification_is_one_time_and_login_session_requires_csrf(auth_client):
    client, mailer = auth_client
    assert client.post("/api/v1/auth/register", json=registration_body()).status_code == 202
    token = get_link_token(mailer)

    unverified_login = client.post(
        "/api/v1/auth/login", json={"email": "alex@example.com", "password": "a long secure password"}
    )
    confirmed = client.post("/api/v1/auth/email-verifications/confirm", json={"token": token})
    replay = client.post("/api/v1/auth/email-verifications/confirm", json={"token": token})
    invalid_login = client.post(
        "/api/v1/auth/login", json={"email": "missing@example.com", "password": "a long secure password"}
    )
    assert unverified_login.status_code == invalid_login.status_code == 401
    assert unverified_login.json()["detail"] == invalid_login.json()["detail"]
    assert confirmed.status_code == 200 and confirmed.json() == {"email_verified": True}
    assert replay.status_code == 400

    login = client.post(
        "/api/v1/auth/login", json={"email": "alex@example.com", "password": "a long secure password"}
    )
    assert login.status_code == 200
    assert login.json()["next_action"] == "candidate_onboarding"
    cookie_headers = login.headers.get_list("set-cookie")
    session_header = next(cookie for cookie in cookie_headers if cookie.startswith(f"{SESSION_COOKIE}="))
    csrf_header = next(cookie for cookie in cookie_headers if cookie.startswith(f"{CSRF_COOKIE}="))
    assert "secure" in session_header.lower() and "httponly" in session_header.lower()
    assert "samesite=lax" in session_header.lower()
    assert "secure" in csrf_header.lower() and "httponly" not in csrf_header.lower()
    assert SESSION_COOKIE in client.cookies and CSRF_COOKIE in client.cookies
    session_token = client.cookies[SESSION_COOKIE]
    csrf_token = client.cookies[CSRF_COOKIE]
    assert client.get("/api/v1/auth/session").status_code == 200
    assert client.get("/api/v1/auth/session").json()["next_action"] == "candidate_onboarding"

    with connect_database(TEST_DATABASE_URL) as connection:
        session_row = connection.execute(
            "SELECT session_token_hash, csrf_token_hash FROM auth_sessions"
        ).fetchone()
    assert bytes(session_row["session_token_hash"]) == hash_token(session_token)
    assert bytes(session_row["csrf_token_hash"]) == hash_token(csrf_token)

    rejected_logout = client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": "invalid"})
    assert rejected_logout.status_code == 403
    assert client.get("/api/v1/auth/session").status_code == 200
    logout = client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf_token})
    assert logout.status_code == 204
    assert client.get("/api/v1/auth/session").status_code == 401


def test_expired_verification_token_is_rejected(auth_client):
    client, mailer = auth_client
    assert client.post("/api/v1/auth/register", json=registration_body()).status_code == 202
    token = get_link_token(mailer)
    with connect_database(TEST_DATABASE_URL) as connection:
        connection.execute(
            "UPDATE email_verification_tokens SET expires_at = now() - interval '1 second' WHERE token_hash = %s",
            (hash_token(token),),
        )

    response = client.post("/api/v1/auth/email-verifications/confirm", json={"token": token})
    assert response.status_code == 400
    with connect_database(TEST_DATABASE_URL) as connection:
        assert connection.execute("SELECT email_verified_at FROM users").fetchone()["email_verified_at"] is None


def test_mail_failure_keeps_pending_registration_retryable(auth_client):
    client, mailer = auth_client
    mailer.fail_delivery = True
    failed = client.post("/api/v1/auth/register", json=registration_body())
    assert failed.status_code == 503
    assert failed.json()["code"] == "EMAIL_DELIVERY_UNAVAILABLE"
    with connect_database(TEST_DATABASE_URL) as connection:
        assert connection.execute("SELECT count(*) AS count FROM users").fetchone()["count"] == 1
        assert connection.execute("SELECT count(*) AS count FROM email_verification_tokens").fetchone()["count"] == 1
        assert connection.execute(
            "SELECT count(*) AS count FROM audit_events WHERE action = 'auth.verification_delivery_failed'"
        ).fetchone()["count"] == 1

    mailer.fail_delivery = False
    retried = client.post("/api/v1/auth/register", json=registration_body())
    assert retried.status_code == 202
    assert len(mailer.messages) == 1


def test_session_expiry_and_login_rate_limit(auth_client):
    client, mailer = auth_client
    assert client.post("/api/v1/auth/register", json=registration_body()).status_code == 202
    token = get_link_token(mailer)
    client.post("/api/v1/auth/email-verifications/confirm", json={"token": token})
    login = client.post(
        "/api/v1/auth/login", json={"email": "alex@example.com", "password": "a long secure password"}
    )
    assert login.status_code == 200
    with connect_database(TEST_DATABASE_URL) as connection:
        connection.execute("UPDATE auth_sessions SET expires_at = now() - interval '1 second'")
    assert client.get("/api/v1/auth/session").status_code == 401

    client.app.state.rate_limiter = RateLimiter()
    for _ in range(10):
        response = client.post(
            "/api/v1/auth/login", json={"email": "missing@example.com", "password": "wrong password"}
        )
        assert response.status_code == 401
    limited = client.post(
        "/api/v1/auth/login", json={"email": "missing@example.com", "password": "wrong password"}
    )
    assert limited.status_code == 429


def test_account_sessions_are_private_and_revocable_after_reauthentication(auth_client):
    client, mailer = auth_client
    guest_session = client.get("/api/v1/me/security/sessions")
    guest_revoke = client.delete(f"/api/v1/me/security/sessions/{uuid4()}")
    guest_revoke_others = client.post("/api/v1/me/security/sessions/revoke-others", json={"current_password": "a long secure password"})
    assert guest_session.status_code == guest_revoke.status_code == guest_revoke_others.status_code == 401

    register_and_login(client, mailer, "sessions@example.com")
    csrf = client.cookies[CSRF_COOKIE]
    path = "/api/v1/me/security/sessions"
    with TestClient(client.app, base_url="https://proofpath.test") as second:
        second_login = second.post(
            "/api/v1/auth/login", json={"email": "sessions@example.com", "password": "a long secure password"}
        )
        assert second_login.status_code == 200
        listing = client.get(path)
        assert listing.status_code == 200 and listing.headers["cache-control"] == "no-store"
        sessions = listing.json()["sessions"]
        assert len(sessions) == 2 and sum(item["is_current"] for item in sessions) == 1
        assert all(set(item) == {"session_id", "created_at", "expires_at", "is_current"} for item in sessions)
        current = next(item for item in sessions if item["is_current"])
        other = next(item for item in sessions if not item["is_current"])
        assert "session_token_hash" not in listing.text and "csrf_token_hash" not in listing.text

        no_csrf = client.delete(f"{path}/{other['session_id']}")
        with TestClient(client.app, base_url="https://proofpath.test") as attacker:
            register_and_login(attacker, mailer, "other-session-owner@example.com")
            attacker_sessions = attacker.get(path).json()["sessions"]
            assert len(attacker_sessions) == 1 and attacker_sessions[0]["is_current"] is True
            assert current["session_id"] not in {item["session_id"] for item in attacker_sessions}
            cross_user = attacker.delete(
                f"{path}/{current['session_id']}", headers={"X-CSRF-Token": attacker.cookies[CSRF_COOKIE]}
            )
            attacker_revoke_others = attacker.post(
                f"{path}/revoke-others", json={"current_password": "a long secure password"},
                headers={"X-CSRF-Token": attacker.cookies[CSRF_COOKIE]},
            )
            assert attacker_revoke_others.status_code == 200 and attacker_revoke_others.json() == {"revoked_session_count": 0}
        bad_password = client.post(
            f"{path}/revoke-others", json={"current_password": "incorrect password"}, headers={"X-CSRF-Token": csrf}
        )
        assert no_csrf.status_code == 403 and cross_user.status_code == 404
        assert bad_password.status_code == 403 and client.get(path).json()["sessions"] == sessions
        assert client.post(
            f"{path}/revoke-others", json={"current_password": "a long secure password", "user_id": str(uuid4())},
            headers={"X-CSRF-Token": csrf},
        ).status_code == 422

        revoked = client.post(
            f"{path}/revoke-others", json={"current_password": "a long secure password"},
            headers={"X-CSRF-Token": csrf},
        )
        assert revoked.status_code == 200 and revoked.json() == {"revoked_session_count": 1}
        assert second.get("/api/v1/auth/session").status_code == 401
        assert [item["session_id"] for item in client.get(path).json()["sessions"]] == [current["session_id"]]
        repeat_revoke = client.post(
            f"{path}/revoke-others", json={"current_password": "a long secure password"},
            headers={"X-CSRF-Token": csrf},
        )
        assert repeat_revoke.status_code == 200 and repeat_revoke.json() == {"revoked_session_count": 0}

    current_revoke = client.delete(f"{path}/{current['session_id']}", headers={"X-CSRF-Token": csrf})
    assert current_revoke.status_code == 204 and client.get("/api/v1/auth/session").status_code == 401
    assert SESSION_COOKIE not in client.cookies and CSRF_COOKIE not in client.cookies
    with connect_database(TEST_DATABASE_URL) as connection:
        assert connection.execute(
            "SELECT count(*) AS count FROM audit_events WHERE action = 'auth.session_revoke_reauthentication_failed' AND outcome = 'denied'"
        ).fetchone()["count"] == 1
        assert connection.execute(
            """SELECT count(*) AS count FROM audit_events
               WHERE action = 'auth.other_sessions_revoked' AND outcome = 'success'
                 AND actor_id = (SELECT id FROM users WHERE email = 'sessions@example.com')"""
        ).fetchone()["count"] == 2
        assert connection.execute(
            """SELECT count(*) AS count FROM audit_events
               WHERE action = 'auth.other_sessions_revoked' AND outcome = 'success'
                 AND actor_id = (SELECT id FROM users WHERE email = 'other-session-owner@example.com')"""
        ).fetchone()["count"] == 1
        assert connection.execute(
            "SELECT count(*) AS count FROM audit_events WHERE action = 'auth.session_revoked' AND outcome = 'success'"
        ).fetchone()["count"] == 1
    openapi = client.get("/openapi.json").json()
    assert openapi["paths"][path]["get"]["security"] == [{"ProofpathSession": []}]
    assert openapi["paths"][f"{path}/{{session_id}}"]["delete"]["security"] == [
        {"ProofpathSession": [], "ProofpathCSRF": []}
    ]
    assert openapi["paths"][f"{path}/revoke-others"]["post"]["security"] == [
        {"ProofpathSession": [], "ProofpathCSRF": []}
    ]


def test_account_data_export_is_reauthenticated_scoped_and_not_cached(auth_client):
    client, mailer = auth_client
    export_path = "/api/v1/me/data-exports"
    body = {"current_password": "a long secure password"}
    assert client.post(export_path, json=body).status_code == 401

    register_and_login(client, mailer, "data-export@example.com")
    csrf = client.cookies[CSRF_COOKIE]
    assert client.post(export_path, json=body).status_code == 403
    assert client.post(
        export_path, json={"current_password": "incorrect password"}, headers={"X-CSRF-Token": csrf},
    ).status_code == 403
    complete_and_publish_candidate_portfolio(
        client, full_name="Export Candidate", handle="export-candidate", project_name="Published Export Project",
    )
    private_projects = client.get("/api/v1/me/projects").json()["projects"]
    private_project_id = next(project["id"] for project in private_projects if project["name"] == "Private Draft Project")
    assert client.post(
        f"/api/v1/me/projects/{private_project_id}/source-links",
        json={"url": "https://github.com/proofpath/export-project"}, headers={"X-CSRF-Token": csrf},
    ).status_code == 201
    with TestClient(client.app, base_url="https://proofpath.test") as other:
        register_and_login(other, mailer, "different-account@example.com", purpose="employer")
        _, job_id = create_published_job(
            other, title="Export Test Engineer", organization_name="Other Account Secret Organization",
        )
        application = client.post(
            f"/api/v1/jobs/{job_id}/applications",
            json={"request_key": str(uuid4()), "portfolio_consent_version": "job-application-public-portfolio-v1",
                  "cover_note": "I build reliable APIs for real users."},
            headers={"X-CSRF-Token": csrf},
        )
        assert application.status_code == 201, application.text

    exported = client.post(export_path, json=body, headers={"X-CSRF-Token": csrf})
    assert exported.status_code == 200, exported.text
    assert exported.headers["cache-control"] == "no-store"
    assert exported.headers["x-content-type-options"] == "nosniff"
    assert exported.headers["content-disposition"].startswith('attachment; filename="proofpath-account-data-')
    document = exported.json()
    assert document["schema_version"] == "proofpath-account-export-v1"
    assert document["account"]["email"] == "data-export@example.com"
    assert document["candidate_profile"]["target_roles"] == ["Backend Engineer"]
    assert {project["name"] for project in document["projects"]} == {"Published Export Project", "Private Draft Project"}
    private = next(project for project in document["projects"] if project["name"] == "Private Draft Project")
    assert private["source_links"][0]["url"] == "https://github.com/proofpath/export-project"
    assert document["portfolio"]["public_handle"] == "export-candidate"
    assert document["plan_entitlements"] == {"candidate_plan": "free", "portfolio_customization": False}
    assert len(document["security_sessions"]) == 1
    assert "different-account@example.com" not in exported.text
    assert len(document["applications"]) == 1
    assert document["applications"][0]["job_snapshot"]["title"] == "Export Test Engineer"
    assert document["applications"][0]["status_history"][0]["action"] == "submitted"
    for secret_key in ("password_hash", "session_token_hash", "csrf_token_hash", "token_hash", "request_payload_hash"):
        assert secret_key not in exported.text
    assert "scope_notes" in document and document["scope_notes"]
    with connect_database(TEST_DATABASE_URL) as connection:
        actions = connection.execute(
            "SELECT action, outcome FROM audit_events WHERE actor_id = (SELECT id FROM users WHERE email = %s) AND action LIKE 'account.data_export%%' ORDER BY action",
            ("data-export@example.com",),
        ).fetchall()
    assert [(row["action"], row["outcome"]) for row in actions] == [
        ("account.data_export_reauthentication_failed", "denied"),
        ("account.data_exported", "success"),
    ]
    openapi = client.get("/openapi.json").json()
    assert openapi["paths"][export_path]["post"]["security"] == [
        {"ProofpathSession": [], "ProofpathCSRF": []}
    ]


def test_frontend_routes_and_public_links_are_connected(auth_client):
    client, _ = auth_client
    assert client.get("/").status_code == 200
    signup = client.get("/signup?purpose=employer")
    login = client.get("/login")
    verification = client.get("/verify-email")
    onboarding = client.get("/candidate/onboarding")
    organization = client.get("/organization/onboarding")
    security_sessions = client.get("/security/sessions")
    privacy_data = client.get("/account/privacy")
    reset = client.get("/reset-password")
    public_portfolio = client.get("/u/sample-handle")
    legacy_public_portfolio = client.get("/portfolio/sample-handle")
    portfolio_builder = client.get("/app/portfolio/builder")
    portfolio_renderer = client.get("/portfolio-renderer.js")
    jobs_page = client.get("/jobs")
    job_detail_page = client.get(f"/jobs/{uuid4()}")
    organization_jobs_page = client.get(f"/organization/jobs?organization_id={uuid4()}")
    organization_assessments_page = client.get(f"/organization/assessments?organization_id={uuid4()}")
    organization_candidates_page = client.get("/organization/candidates")
    applications_page = client.get("/applications")
    organization_applications_page = client.get(f"/organization/applications?organization_id={uuid4()}&job_id={uuid4()}")
    css = client.get("/auth.css")
    assert signup.status_code == login.status_code == verification.status_code == onboarding.status_code == organization.status_code == security_sessions.status_code == privacy_data.status_code == reset.status_code == public_portfolio.status_code == legacy_public_portfolio.status_code == portfolio_builder.status_code == portfolio_renderer.status_code == organization_candidates_page.status_code == jobs_page.status_code == job_detail_page.status_code == organization_jobs_page.status_code == organization_assessments_page.status_code == applications_page.status_code == organization_applications_page.status_code == css.status_code == 200
    assert privacy_data.headers["cache-control"] == "no-store"
    assert "What the export contains" in privacy_data.text and "current password" in privacy_data.text.lower()
    assert organization_candidates_page.headers["cache-control"] == "no-store"
    assert "Find candidates" in organization_candidates_page.text and "consent-based discovery" in organization_candidates_page.text
    assert portfolio_builder.headers["cache-control"] == "no-store"
    assert "Live preview" in portfolio_builder.text and "Version history" in portfolio_builder.text
    assert "textContent" in portfolio_renderer.text and "innerHTML" not in portfolio_renderer.text
    assert organization_assessments_page.headers["cache-control"] == "no-store"
    assert security_sessions.headers["cache-control"] == "no-store"
    assert "rubric" in organization_assessments_page.text.lower()
    assert public_portfolio.headers["cache-control"] == "no-store"
    assert public_portfolio.headers["x-robots-tag"] == "noindex, nofollow"
    assert legacy_public_portfolio.headers["x-robots-tag"] == "noindex, nofollow"
    assert 'name="initial_purpose"' in signup.text
    assert "api/v1/auth/login" in login.text
    assert "/reset-password" in login.text
    assert "api/v1/auth/email-verifications/confirm" in verification.text
    assert "no-store" in verification.headers["cache-control"]
    assert "/signup?purpose=employer" in client.get("/").text
    openapi = client.get("/openapi.json").json()
    assert openapi["paths"]["/api/v1/auth/register"]["post"]["responses"]["422"]["content"].keys() == {"application/problem+json"}
    assert openapi["paths"]["/api/v1/auth/session"]["get"]["security"] == [{"ProofpathSession": []}]
    assert openapi["paths"]["/api/v1/auth/logout"]["post"]["security"] == [
        {"ProofpathSession": [], "ProofpathCSRF": []}
    ]
    assert openapi["paths"]["/api/v1/me/candidate/onboarding"]["patch"]["security"] == [
        {"ProofpathSession": [], "ProofpathCSRF": []}
    ]
    assert openapi["paths"]["/api/v1/me/candidate/onboarding"]["patch"]["responses"]["422"]["content"].keys() == {"application/problem+json"}
    assert openapi["paths"]["/api/v1/me/entitlements"]["get"]["security"] == [{"ProofpathSession": []}]
    assert openapi["paths"]["/api/v1/me/portfolio/versions"]["get"]["security"] == [{"ProofpathSession": []}]
    assert openapi["paths"]["/api/v1/me/portfolio/versions"]["post"]["security"] == [
        {"ProofpathSession": [], "ProofpathCSRF": []}
    ]
    assert openapi["paths"]["/api/v1/me/candidate-profile/discovery"]["get"]["security"] == [{"ProofpathSession": []}]
    assert openapi["paths"]["/api/v1/me/candidate-profile/discovery"]["patch"]["security"] == [
        {"ProofpathSession": [], "ProofpathCSRF": []}
    ]
    assert openapi["paths"]["/api/v1/me/data-exports"]["post"]["security"] == [
        {"ProofpathSession": [], "ProofpathCSRF": []}
    ]
    assert openapi["paths"]["/api/v1/organizations/{org_id}/candidates"]["get"]["security"] == [
        {"ProofpathSession": []}
    ]
    assert openapi["paths"]["/api/v1/auth/password-resets"]["post"]["responses"]["422"]["content"].keys() == {"application/problem+json"}
    assert openapi["paths"]["/api/v1/auth/password-resets/confirm"]["post"].get("security") is None
    assert openapi["paths"]["/api/v1/me/workspaces"]["get"]["security"] == [{"ProofpathSession": []}]
    assert openapi["paths"]["/api/v1/me/applications"]["get"]["security"] == [{"ProofpathSession": []}]
    assert openapi["paths"]["/api/v1/jobs/{job_id}/applications"]["post"]["security"] == [
        {"ProofpathSession": [], "ProofpathCSRF": []}
    ]
    assert openapi["paths"]["/api/v1/jobs/{job_id}/applications"]["post"]["responses"]["201"]
    assert openapi["paths"]["/api/v1/jobs/{job_id}/applications"]["post"]["responses"]["200"]
    assert openapi["paths"]["/api/v1/organizations/{org_id}/applications/{application_id}/stage"]["patch"]["security"] == [
        {"ProofpathSession": [], "ProofpathCSRF": []}
    ]
    assert openapi["paths"]["/api/v1/organizations/{org_id}/applications/{application_id}/decision"]["post"]["security"] == [
        {"ProofpathSession": [], "ProofpathCSRF": []}
    ]
    assert openapi["paths"]["/api/v1/me/projects"]["get"]["security"] == [{"ProofpathSession": []}]
    assert openapi["paths"]["/api/v1/me/projects"]["post"]["security"] == [
        {"ProofpathSession": [], "ProofpathCSRF": []}
    ]
    assert openapi["paths"]["/api/v1/me/projects/{project_id}"]["patch"]["security"] == [
        {"ProofpathSession": [], "ProofpathCSRF": []}
    ]
    assert openapi["paths"]["/api/v1/me/projects/{project_id}"]["delete"]["responses"]["204"]
    assert openapi["paths"]["/api/v1/me/projects/{project_id}/source-links"]["post"]["security"] == [
        {"ProofpathSession": [], "ProofpathCSRF": []}
    ]
    assert openapi["paths"]["/api/v1/me/projects/{project_id}/source-links/{link_id}"]["delete"]["security"] == [
        {"ProofpathSession": [], "ProofpathCSRF": []}
    ]
    assert openapi["paths"]["/api/v1/me/portfolio"]["get"]["security"] == [{"ProofpathSession": []}]
    assert "public_handle_locked" in openapi["components"]["schemas"]["PortfolioResponse"]["properties"]
    assert openapi["paths"]["/api/v1/me/portfolio"]["patch"]["security"] == [
        {"ProofpathSession": [], "ProofpathCSRF": []}
    ]
    for path, method in (
        ("/api/v1/me/candidate-profile/handle", "post"),
        ("/api/v1/me/portfolio/publish", "post"),
        ("/api/v1/me/portfolio/unpublish", "post"),
        ("/api/v1/me/projects/{project_id}/publish", "post"),
        ("/api/v1/me/projects/{project_id}/unpublish", "post"),
        ("/api/v1/organizations/{org_id}/jobs", "post"),
        ("/api/v1/organizations/{org_id}/jobs/{job_id}", "patch"),
        ("/api/v1/organizations/{org_id}/jobs/{job_id}/publish", "post"),
        ("/api/v1/organizations/{org_id}/jobs/{job_id}/close", "post"),
        ("/api/v1/me/saved-jobs/{job_id}", "put"),
        ("/api/v1/me/saved-jobs/{job_id}", "delete"),
    ):
        assert openapi["paths"][path][method]["security"] == [
            {"ProofpathSession": [], "ProofpathCSRF": []}
        ]
    assert openapi["paths"]["/api/v1/public/portfolios/{handle}"]["get"].get("security") is None
    assert openapi["paths"]["/api/v1/jobs"]["get"].get("security") is None
    assert openapi["paths"]["/api/v1/jobs/{job_id}"]["get"].get("security") is None
    assert openapi["paths"]["/api/v1/organizations/{org_id}/jobs"]["get"]["security"] == [
        {"ProofpathSession": []}
    ]
    assert openapi["paths"]["/api/v1/me/saved-jobs"]["get"]["security"] == [{"ProofpathSession": []}]
    assert openapi["paths"]["/api/v1/organizations"]["post"]["security"] == [
        {"ProofpathSession": [], "ProofpathCSRF": []}
    ]
    assert openapi["paths"]["/api/v1/organizations/{org_id}/onboarding"]["patch"]["security"] == [
        {"ProofpathSession": [], "ProofpathCSRF": []}
    ]
    assert openapi["paths"]["/api/v1/organizations/{org_id}/members"]["get"]["security"] == [
        {"ProofpathSession": []}
    ]
    assert openapi["paths"]["/api/v1/organizations/{org_id}/invitations"]["get"]["security"] == [
        {"ProofpathSession": []}
    ]
    assert openapi["paths"]["/api/v1/organizations/{org_id}/invitations"]["post"]["security"] == [
        {"ProofpathSession": [], "ProofpathCSRF": []}
    ]
    assert openapi["paths"]["/api/v1/organizations/{org_id}/invitations/{invite_id}"]["delete"]["security"] == [
        {"ProofpathSession": [], "ProofpathCSRF": []}
    ]
    assert openapi["paths"]["/api/v1/organization-invitations/accept"]["post"]["security"] == [
        {"ProofpathSession": [], "ProofpathCSRF": []}
    ]
    assert openapi["paths"]["/api/v1/organizations/{org_id}/members/{member_id}/role"]["patch"]["security"] == [
        {"ProofpathSession": [], "ProofpathCSRF": []}
    ]
    assert openapi["paths"]["/api/v1/organizations/{org_id}/members/{member_id}"]["delete"]["security"] == [
        {"ProofpathSession": [], "ProofpathCSRF": []}
    ]


def test_candidate_onboarding_validates_versions_completion_and_csrf(auth_client):
    client, mailer = auth_client
    guest = client.get("/api/v1/me/candidate/onboarding")
    guest_patch = client.patch("/api/v1/me/candidate/onboarding", json={"expected_version": 0, "target_roles": []})
    guest_complete = client.post("/api/v1/me/candidate/onboarding/complete", json={"expected_version": 0})
    assert guest.status_code == 401
    assert guest_patch.status_code == guest_complete.status_code == 401
    register_and_login(client, mailer, "candidate@example.com")
    csrf = client.cookies[CSRF_COOKIE]
    initial = client.get("/api/v1/me/candidate/onboarding")
    assert initial.status_code == 200
    assert initial.json() == {
        "full_name": "Alex Johnson",
        "target_roles": [],
        "skills": [],
        "status": "draft",
        "version": 0,
        "updated_at": None,
    }

    no_csrf = client.patch(
        "/api/v1/me/candidate/onboarding",
        json={"expected_version": 0, "target_roles": ["Backend Engineer"]},
        headers={"X-CSRF-Token": "wrong"},
    )
    extra_field = client.patch(
        "/api/v1/me/candidate/onboarding",
        json={"expected_version": 0, "target_roles": ["Backend Engineer"], "is_admin": True},
        headers={"X-CSRF-Token": csrf},
    )
    duplicate_label = client.patch(
        "/api/v1/me/candidate/onboarding",
        json={"expected_version": 0, "target_roles": ["Backend Engineer", " backend engineer "]},
        headers={"X-CSRF-Token": csrf},
    )
    assert no_csrf.status_code == 403
    assert extra_field.status_code == duplicate_label.status_code == 422

    empty = client.patch(
        "/api/v1/me/candidate/onboarding",
        json={"expected_version": 0, "target_roles": []},
        headers={"X-CSRF-Token": csrf},
    )
    assert empty.status_code == 200 and empty.json()["version"] == 1
    incomplete = client.post(
        "/api/v1/me/candidate/onboarding/complete",
        json={"expected_version": 1},
        headers={"X-CSRF-Token": csrf},
    )
    assert incomplete.status_code == 422

    saved = client.patch(
        "/api/v1/me/candidate/onboarding",
        json={
            "expected_version": 1,
            "full_name": "  Alex   Rivera ",
            "target_roles": ["Backend Engineer", "Product Engineer"],
            "skills": ["Python", "PostgreSQL"],
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["full_name"] == "Alex Rivera"
    assert saved.json()["target_roles"] == ["Backend Engineer", "Product Engineer"]
    assert saved.json()["skills"] == ["Python", "PostgreSQL"]
    assert saved.json()["version"] == 2

    conflict = client.patch(
        "/api/v1/me/candidate/onboarding",
        json={"expected_version": 1, "target_roles": ["Staff Engineer"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert conflict.status_code == 409
    completed = client.post(
        "/api/v1/me/candidate/onboarding/complete",
        json={"expected_version": 2},
        headers={"X-CSRF-Token": csrf},
    )
    repeated = client.post(
        "/api/v1/me/candidate/onboarding/complete",
        json={"expected_version": 0},
        headers={"X-CSRF-Token": csrf},
    )
    frozen = client.patch(
        "/api/v1/me/candidate/onboarding",
        json={"expected_version": 3, "target_roles": ["Staff Engineer"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert completed.status_code == repeated.status_code == 200
    assert completed.json()["status"] == "complete" and completed.json()["version"] == 3
    assert repeated.json() == completed.json()
    assert frozen.status_code == 409
    with connect_database(TEST_DATABASE_URL) as connection:
        assert connection.execute(
            "SELECT count(*) AS count FROM audit_events WHERE action = 'candidate.onboarding_completed'"
        ).fetchone()["count"] == 1
        assert connection.execute("SELECT full_name FROM users").fetchone()["full_name"] == "Alex Rivera"


def test_candidate_profiles_are_owner_scoped_and_additive_to_employer_identity(auth_client):
    client, mailer = auth_client
    first = register_and_login(client, mailer, "first@example.com", purpose="candidate")
    first_csrf = client.cookies[CSRF_COOKIE]
    first_saved = client.patch(
        "/api/v1/me/candidate/onboarding",
        json={"expected_version": 0, "target_roles": ["First User Role"]},
        headers={"X-CSRF-Token": first_csrf},
    )
    assert first_saved.status_code == 200

    with TestClient(client.app, base_url="https://proofpath.test") as second_client:
        second = register_and_login(second_client, mailer, "second@example.com", purpose="employer")
        assert second["next_action"] == "employer_onboarding"
        second_csrf = second_client.cookies[CSRF_COOKIE]
        second_saved = second_client.patch(
            "/api/v1/me/candidate/onboarding",
            json={"expected_version": 0, "target_roles": ["Second User Role"]},
            headers={"X-CSRF-Token": second_csrf},
        )
        assert second_saved.status_code == 200
        assert second_client.get("/api/v1/me/candidate/onboarding").json()["target_roles"] == ["Second User Role"]

    assert first["next_action"] == "candidate_onboarding"
    assert client.get("/api/v1/me/candidate/onboarding").json()["target_roles"] == ["First User Role"]


def test_password_reset_is_non_enumerating_one_time_and_revokes_sessions(auth_client):
    client, mailer = auth_client
    register_and_login(client, mailer, "reset@example.com")
    assert client.get("/api/v1/auth/session").status_code == 200

    malformed = client.post("/api/v1/auth/password-resets", json={"email": "not-an-email"})
    extra_field = client.post(
        "/api/v1/auth/password-resets", json={"email": "reset@example.com", "send_to": "attacker@example.com"}
    )
    assert malformed.status_code == extra_field.status_code == 422
    unknown = client.post("/api/v1/auth/password-resets", json={"email": "missing@example.com"})
    mailer.fail_delivery = True
    delivery_failed = client.post("/api/v1/auth/password-resets", json={"email": "RESET@example.com"})
    mailer.fail_delivery = False
    known = client.post("/api/v1/auth/password-resets", json={"email": "RESET@example.com"})
    assert unknown.status_code == delivery_failed.status_code == 202
    assert unknown.json() == delivery_failed.json()
    assert unknown.status_code == known.status_code == 202
    assert unknown.json() == known.json()
    assert len(mailer.password_reset_messages) == 1
    reset_link = mailer.password_reset_messages[0][2]
    parsed = urlparse(reset_link)
    assert parsed.query == ""
    token = parse_qs(parsed.fragment)["token"][0]
    with connect_database(TEST_DATABASE_URL) as connection:
        token_row = connection.execute(
            "SELECT token_hash FROM password_reset_tokens WHERE token_hash = %s", (hash_token(token),)
        ).fetchone()
    assert bytes(token_row["token_hash"]) == hash_token(token)

    response = client.post(
        "/api/v1/auth/password-resets/confirm",
        json={"token": token, "new_password": "a changed secure password"},
    )
    replay = client.post(
        "/api/v1/auth/password-resets/confirm",
        json={"token": token, "new_password": "a changed secure password"},
    )
    short = client.post(
        "/api/v1/auth/password-resets/confirm", json={"token": token, "new_password": "short"}
    )
    assert response.status_code == 200 and response.json() == {"password_reset": True}
    assert replay.status_code == 400
    assert short.status_code == 422
    assert client.get("/api/v1/auth/session").status_code == 401
    old_password = client.post(
        "/api/v1/auth/login", json={"email": "reset@example.com", "password": "a long secure password"}
    )
    new_password = client.post(
        "/api/v1/auth/login", json={"email": "reset@example.com", "password": "a changed secure password"}
    )
    assert old_password.status_code == 401
    assert new_password.status_code == 200
    with connect_database(TEST_DATABASE_URL) as connection:
        assert connection.execute(
            "SELECT count(*) AS count FROM audit_events WHERE action = 'auth.password_reset_completed'"
        ).fetchone()["count"] == 1
        assert connection.execute(
            "SELECT count(*) AS count FROM audit_events WHERE action = 'auth.password_reset_delivery_failed'"
        ).fetchone()["count"] == 1


def test_expired_password_reset_token_is_rejected(auth_client):
    client, mailer = auth_client
    register_and_login(client, mailer, "expired-reset@example.com")
    assert client.post(
        "/api/v1/auth/password-resets", json={"email": "expired-reset@example.com"}
    ).status_code == 202
    token = mailer.password_reset_messages[0][2].split("#token=", 1)[1]
    with connect_database(TEST_DATABASE_URL) as connection:
        connection.execute(
            "UPDATE password_reset_tokens SET expires_at = now() - interval '1 second' WHERE token_hash = %s",
            (hash_token(token),),
        )

    response = client.post(
        "/api/v1/auth/password-resets/confirm",
        json={"token": token, "new_password": "a changed secure password"},
    )
    assert response.status_code == 400
    assert client.post(
        "/api/v1/auth/login", json={"email": "expired-reset@example.com", "password": "a long secure password"}
    ).status_code == 200


def test_organization_create_is_idempotent_and_onboarding_is_versioned(auth_client):
    client, mailer = auth_client
    assert client.get("/api/v1/me/workspaces").status_code == 401
    assert client.get(f"/api/v1/organizations/{uuid4()}").status_code == 401
    assert client.get(f"/api/v1/organizations/{uuid4()}/onboarding").status_code == 401
    assert client.patch(
        f"/api/v1/organizations/{uuid4()}/onboarding", json={"expected_version": 0, "name": "Guest Update"}
    ).status_code == 401
    assert client.post(
        f"/api/v1/organizations/{uuid4()}/onboarding/complete", json={"expected_version": 0}
    ).status_code == 401
    guest_create = client.post(
        "/api/v1/organizations", json={"request_key": str(uuid4()), "name": "Guest Organization"}
    )
    assert guest_create.status_code == 401
    register_and_login(client, mailer, "org-owner@example.com", purpose="candidate")
    csrf = client.cookies[CSRF_COOKIE]
    guest_workspaces = client.get("/api/v1/me/workspaces")
    assert guest_workspaces.status_code == 200
    assert [workspace["type"] for workspace in guest_workspaces.json()["workspaces"]] == ["candidate"]
    request_key = str(uuid4())
    body = {"request_key": request_key, "name": "  Proofpath   Engineering  "}

    no_csrf = client.post("/api/v1/organizations", json=body)
    extra_field = client.post(
        "/api/v1/organizations", json=body | {"role": "owner"}, headers={"X-CSRF-Token": csrf}
    )
    invalid_name = client.post(
        "/api/v1/organizations", json={"request_key": str(uuid4()), "name": " "}, headers={"X-CSRF-Token": csrf}
    )
    assert no_csrf.status_code == 403
    assert extra_field.status_code == invalid_name.status_code == 422

    created = client.post("/api/v1/organizations", json=body, headers={"X-CSRF-Token": csrf})
    replay = client.post("/api/v1/organizations", json=body, headers={"X-CSRF-Token": csrf})
    changed_payload = client.post(
        "/api/v1/organizations",
        json={"request_key": request_key, "name": "Different Organization"},
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == replay.status_code == 201
    assert created.json() == replay.json()
    assert created.json()["name"] == "Proofpath Engineering"
    assert created.json()["member_role"] == "owner"
    assert created.headers["location"] == f"/api/v1/organizations/{created.json()['id']}"
    assert changed_payload.status_code == 409

    organization_id = created.json()["id"]
    onboarding = client.get(f"/api/v1/organizations/{organization_id}/onboarding")
    assert onboarding.status_code == 200
    assert onboarding.json()["status"] == "draft" and onboarding.json()["version"] == 0
    no_csrf_patch = client.patch(
        f"/api/v1/organizations/{organization_id}/onboarding",
        json={"expected_version": 0, "name": "No CSRF"},
    )
    no_csrf_complete = client.post(
        f"/api/v1/organizations/{organization_id}/onboarding/complete", json={"expected_version": 0}
    )
    assert no_csrf_patch.status_code == no_csrf_complete.status_code == 403
    invalid_patch = client.patch(
        f"/api/v1/organizations/{organization_id}/onboarding",
        json={"expected_version": -1, "name": "Invalid Version"},
        headers={"X-CSRF-Token": csrf},
    )
    invalid_complete = client.post(
        f"/api/v1/organizations/{organization_id}/onboarding/complete",
        json={"expected_version": -1},
        headers={"X-CSRF-Token": csrf},
    )
    assert invalid_patch.status_code == invalid_complete.status_code == 422
    saved = client.patch(
        f"/api/v1/organizations/{organization_id}/onboarding",
        json={"expected_version": 0, "name": "Proofpath Labs"},
        headers={"X-CSRF-Token": csrf},
    )
    conflict = client.patch(
        f"/api/v1/organizations/{organization_id}/onboarding",
        json={"expected_version": 0, "name": "Stale Name"},
        headers={"X-CSRF-Token": csrf},
    )
    assert saved.status_code == 200 and saved.json()["version"] == 1
    assert conflict.status_code == 409

    complete = client.post(
        f"/api/v1/organizations/{organization_id}/onboarding/complete",
        json={"expected_version": 1},
        headers={"X-CSRF-Token": csrf},
    )
    replay_complete = client.post(
        f"/api/v1/organizations/{organization_id}/onboarding/complete",
        json={"expected_version": 0},
        headers={"X-CSRF-Token": csrf},
    )
    assert complete.status_code == replay_complete.status_code == 200
    assert complete.json()["status"] == "complete" and complete.json()["version"] == 2
    assert complete.json() == replay_complete.json()
    assert client.get(f"/api/v1/organizations/{organization_id}").json()["member_role"] == "owner"
    workspaces = client.get("/api/v1/me/workspaces").json()["workspaces"]
    assert {(workspace["type"], workspace["role"]) for workspace in workspaces} == {
        ("candidate", "candidate"),
        ("organization", "owner"),
    }
    with connect_database(TEST_DATABASE_URL) as connection:
        assert connection.execute(
            "SELECT count(*) AS count FROM organizations WHERE created_by_user_id = (SELECT id FROM users WHERE email = 'org-owner@example.com')"
        ).fetchone()["count"] == 1
        audit = connection.execute(
            "SELECT tenant_id, count(*) AS count FROM audit_events WHERE action = 'organization.onboarding_completed' GROUP BY tenant_id"
        ).fetchone()
    assert str(audit["tenant_id"]) == organization_id and audit["count"] == 1


def test_organization_membership_is_tenant_scoped_and_roles_are_local(auth_client):
    client, mailer = auth_client
    register_and_login(client, mailer, "org-a@example.com", purpose="candidate")
    csrf_a = client.cookies[CSRF_COOKIE]
    org_a = client.post(
        "/api/v1/organizations", json={"request_key": str(uuid4()), "name": "Organization A"},
        headers={"X-CSRF-Token": csrf_a},
    ).json()["id"]

    with TestClient(client.app, base_url="https://proofpath.test") as second_client:
        register_and_login(second_client, mailer, "org-b@example.com", purpose="employer")
        csrf_b = second_client.cookies[CSRF_COOKIE]
        org_b = second_client.post(
            "/api/v1/organizations", json={"request_key": str(uuid4()), "name": "Organization B"},
            headers={"X-CSRF-Token": csrf_b},
        ).json()["id"]
        with connect_database(TEST_DATABASE_URL) as connection:
            user_b = connection.execute("SELECT id FROM users WHERE email = 'org-b@example.com'").fetchone()["id"]
            connection.execute(
                "INSERT INTO organization_memberships (organization_id, user_id, role) VALUES (%s, %s, 'member')",
                (org_a, user_b),
            )

        assert second_client.get(f"/api/v1/organizations/{org_a}").json()["member_role"] == "member"
        assert second_client.patch(
            f"/api/v1/organizations/{org_a}/onboarding",
            json={"expected_version": 0, "name": "Unauthorized Update"},
            headers={"X-CSRF-Token": csrf_b},
        ).status_code == 403
        assert second_client.post(
            f"/api/v1/organizations/{org_a}/onboarding/complete",
            json={"expected_version": 0},
            headers={"X-CSRF-Token": csrf_b},
        ).status_code == 403
        second_workspaces = second_client.get("/api/v1/me/workspaces").json()["workspaces"]
        assert {(item["id"], item["role"]) for item in second_workspaces if item["type"] == "organization"} == {
            (org_a, "member"),
            (org_b, "owner"),
        }

    assert client.get(f"/api/v1/organizations/{org_b}").status_code == 404
    assert client.get(f"/api/v1/organizations/{org_b}/onboarding").status_code == 404
    assert client.get(f"/api/v1/organizations/{org_b}/members").status_code == 404
    assert client.get(f"/api/v1/organizations/{org_b}/invitations").status_code == 404
    assert client.post(
        f"/api/v1/organizations/{org_b}/invitations",
        json={"email": "cross-tenant@example.com", "role": "member"},
        headers={"X-CSRF-Token": csrf_a},
    ).status_code == 404
    assert client.delete(
        f"/api/v1/organizations/{org_b}/invitations/{uuid4()}", headers={"X-CSRF-Token": csrf_a}
    ).status_code == 404
    assert client.patch(
        f"/api/v1/organizations/{org_b}/members/{user_b}/role", json={"role": "member"},
        headers={"X-CSRF-Token": csrf_a},
    ).status_code == 404
    assert client.delete(
        f"/api/v1/organizations/{org_b}/members/{user_b}", headers={"X-CSRF-Token": csrf_a}
    ).status_code == 404
    assert client.patch(
        f"/api/v1/organizations/{org_b}/onboarding",
        json={"expected_version": 0, "name": "Cross-tenant update"},
        headers={"X-CSRF-Token": csrf_a},
    ).status_code == 404
    assert client.post(
        f"/api/v1/organizations/{org_b}/onboarding/complete",
        json={"expected_version": 0},
        headers={"X-CSRF-Token": csrf_a},
    ).status_code == 404
    assert client.get(f"/api/v1/organizations/{org_a}/onboarding").status_code == 200


def test_organization_invitations_are_verified_single_use_and_role_scoped(auth_client):
    client, mailer = auth_client
    guest_org = uuid4()
    assert client.post(
        f"/api/v1/organizations/{guest_org}/invitations",
        json={"email": "invitee@example.com", "role": "recruiter"},
    ).status_code == 401
    assert client.get(f"/api/v1/organizations/{guest_org}/members").status_code == 401
    assert client.get(f"/api/v1/organizations/{guest_org}/invitations").status_code == 401
    assert client.delete(
        f"/api/v1/organizations/{guest_org}/invitations/{uuid4()}"
    ).status_code == 401
    assert client.patch(
        f"/api/v1/organizations/{guest_org}/members/{uuid4()}/role", json={"role": "member"}
    ).status_code == 401
    assert client.delete(
        f"/api/v1/organizations/{guest_org}/members/{uuid4()}"
    ).status_code == 401
    assert client.post(
        "/api/v1/organization-invitations/accept", json={"token": "a" * 43}
    ).status_code == 401
    register_and_login(client, mailer, "invite-owner@example.com")
    owner_id = client.get("/api/v1/auth/session").json()["user"]["id"]
    owner_csrf = client.cookies[CSRF_COOKIE]
    org = client.post(
        "/api/v1/organizations", json={"request_key": str(uuid4()), "name": "Invitation Org"},
        headers={"X-CSRF-Token": owner_csrf},
    ).json()["id"]

    no_csrf = client.post(
        f"/api/v1/organizations/{org}/invitations", json={"email": "invitee@example.com", "role": "recruiter"}
    )
    invalid = client.post(
        f"/api/v1/organizations/{org}/invitations",
        json={"email": "bad-address", "role": "owner"},
        headers={"X-CSRF-Token": owner_csrf},
    )
    self_invite = client.post(
        f"/api/v1/organizations/{org}/invitations",
        json={"email": "invite-owner@example.com", "role": "member"},
        headers={"X-CSRF-Token": owner_csrf},
    )
    assert no_csrf.status_code == 403
    assert invalid.status_code == 422
    assert self_invite.status_code == 409

    created = client.post(
        f"/api/v1/organizations/{org}/invitations",
        json={"email": "Invitee@example.com", "role": "recruiter"},
        headers={"X-CSRF-Token": owner_csrf},
    )
    assert created.status_code == 201
    assert created.json()["email_masked"] == "i***@example.com"
    assert created.json()["status"] == "pending"
    assert "invitee@example.com" not in created.text
    invitation_link = mailer.organization_invitation_messages[-1][2]
    assert urlparse(invitation_link).query == ""
    invitation_token = parse_qs(urlparse(invitation_link).fragment)["token"][0]
    duplicate = client.post(
        f"/api/v1/organizations/{org}/invitations",
        json={"email": "invitee@example.com", "role": "member"},
        headers={"X-CSRF-Token": owner_csrf},
    )
    listing = client.get(f"/api/v1/organizations/{org}/invitations")
    assert duplicate.status_code == 409
    assert listing.status_code == 200
    assert listing.json()["invitations"][0]["status"] == "pending"
    assert listing.json()["invitations"][0]["email_masked"] == "i***@example.com"
    assert invitation_token not in listing.text
    with connect_database(TEST_DATABASE_URL) as connection:
        stored = connection.execute(
            "SELECT token_hash FROM organization_invitations WHERE id = %s", (created.json()["id"],)
        ).fetchone()
    assert bytes(stored["token_hash"]) == hash_token(invitation_token)

    assert client.post(
        "/api/v1/organization-invitations/accept", json={"token": invitation_token},
        headers={"X-CSRF-Token": owner_csrf},
    ).status_code == 403

    with TestClient(client.app, base_url="https://proofpath.test") as invitee:
        register_and_login(invitee, mailer, "invitee@example.com", purpose="employer")
        invitee_csrf = invitee.cookies[CSRF_COOKIE]
        no_csrf_accept = invitee.post("/api/v1/organization-invitations/accept", json={"token": invitation_token})
        accepted = invitee.post(
            "/api/v1/organization-invitations/accept", json={"token": invitation_token},
            headers={"X-CSRF-Token": invitee_csrf},
        )
        replay = invitee.post(
            "/api/v1/organization-invitations/accept", json={"token": invitation_token},
            headers={"X-CSRF-Token": invitee_csrf},
        )
        assert no_csrf_accept.status_code == 403
        assert accepted.status_code == 200
        assert accepted.json() == {
            "organization_id": org, "organization_name": "Invitation Org", "member_role": "recruiter"
        }
        assert replay.status_code == 400
        assert invitee.get(f"/api/v1/organizations/{org}/invitations").status_code == 403
        assert invitee.delete(
            f"/api/v1/organizations/{org}/invitations/{created.json()['id']}",
            headers={"X-CSRF-Token": invitee_csrf},
        ).status_code == 403
        member = invitee.get(f"/api/v1/organizations/{org}/members")
        assert member.status_code == 200 and len(member.json()["members"]) == 2
        assert all("email" not in item for item in member.json()["members"])
        invitee_id = invitee.get("/api/v1/auth/session").json()["user"]["id"]

    role_update = client.patch(
        f"/api/v1/organizations/{org}/members/{invitee_id}/role", json={"role": "admin"},
        headers={"X-CSRF-Token": owner_csrf},
    )
    assert role_update.status_code == 200 and role_update.json()["role"] == "admin"
    invalid_role = client.patch(
        f"/api/v1/organizations/{org}/members/{invitee_id}/role", json={"role": "owner"},
        headers={"X-CSRF-Token": owner_csrf},
    )
    assert invalid_role.status_code == 422
    last_owner_role = client.patch(
        f"/api/v1/organizations/{org}/members/{owner_id}/role", json={"role": "member"},
        headers={"X-CSRF-Token": owner_csrf},
    )
    last_owner_remove = client.delete(
        f"/api/v1/organizations/{org}/members/{owner_id}", headers={"X-CSRF-Token": owner_csrf}
    )
    assert last_owner_role.status_code == last_owner_remove.status_code == 409

    with TestClient(client.app, base_url="https://proofpath.test") as invitee:
        login = invitee.post(
            "/api/v1/auth/login", json={"email": "invitee@example.com", "password": "a long secure password"}
        )
        assert login.status_code == 200
        invitee_csrf = invitee.cookies[CSRF_COOKIE]
        admin_invitation = invitee.post(
            f"/api/v1/organizations/{org}/invitations",
            json={"email": "another@example.com", "role": "admin"},
            headers={"X-CSRF-Token": invitee_csrf},
        )
        change_owner = invitee.patch(
            f"/api/v1/organizations/{org}/members/{owner_id}/role", json={"role": "member"},
            headers={"X-CSRF-Token": invitee_csrf},
        )
        remove_owner = invitee.delete(
            f"/api/v1/organizations/{org}/members/{owner_id}", headers={"X-CSRF-Token": invitee_csrf}
        )
        assert admin_invitation.status_code == change_owner.status_code == remove_owner.status_code == 403

    assert client.get(f"/api/v1/organizations/{org}/invitations").json()["invitations"][0]["status"] == "accepted"
    assert client.delete(
        f"/api/v1/organizations/{org}/members/{invitee_id}", headers={"X-CSRF-Token": owner_csrf}
    ).status_code == 204
    remaining = client.get(f"/api/v1/organizations/{org}/members").json()["members"]
    assert len(remaining) == 1
    assert remaining[0]["user_id"] == owner_id and remaining[0]["role"] == "owner"


def test_organization_invitation_cancel_and_delivery_failure(auth_client):
    client, mailer = auth_client
    register_and_login(client, mailer, "invite-canceller@example.com")
    csrf = client.cookies[CSRF_COOKIE]
    org = client.post(
        "/api/v1/organizations", json={"request_key": str(uuid4()), "name": "Cancel Org"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]
    pending = client.post(
        f"/api/v1/organizations/{org}/invitations",
        json={"email": "cancelled@example.com", "role": "member"},
        headers={"X-CSRF-Token": csrf},
    )
    invitation_link = mailer.organization_invitation_messages[-1][2]
    token = parse_qs(urlparse(invitation_link).fragment)["token"][0]
    cancelled = client.delete(
        f"/api/v1/organizations/{org}/invitations/{pending.json()['id']}", headers={"X-CSRF-Token": csrf}
    )
    replay_cancel = client.delete(
        f"/api/v1/organizations/{org}/invitations/{pending.json()['id']}", headers={"X-CSRF-Token": csrf}
    )
    assert cancelled.status_code == replay_cancel.status_code == 204
    assert client.get(f"/api/v1/organizations/{org}/invitations").json()["invitations"][0]["status"] == "cancelled"
    mailer.fail_delivery = True
    failed = client.post(
        f"/api/v1/organizations/{org}/invitations",
        json={"email": "retry@example.com", "role": "member"},
        headers={"X-CSRF-Token": csrf},
    )
    mailer.fail_delivery = False
    assert failed.status_code == 503
    assert client.get(f"/api/v1/organizations/{org}/invitations").json()["invitations"][0]["status"] == "cancelled"
    assert client.post(
        "/api/v1/organization-invitations/accept", json={"token": token}, headers={"X-CSRF-Token": csrf}
    ).status_code == 400
    expired = client.post(
        f"/api/v1/organizations/{org}/invitations",
        json={"email": "expired@example.com", "role": "member"},
        headers={"X-CSRF-Token": csrf},
    )
    expired_token = parse_qs(urlparse(mailer.organization_invitation_messages[-1][2]).fragment)["token"][0]
    with connect_database(TEST_DATABASE_URL) as connection:
        connection.execute(
            "UPDATE organization_invitations SET expires_at = now() - interval '1 second' WHERE id = %s",
            (expired.json()["id"],),
        )
    assert client.post(
        "/api/v1/organization-invitations/accept", json={"token": expired_token}, headers={"X-CSRF-Token": csrf}
    ).status_code == 400
    assert client.get(f"/api/v1/organizations/{org}/invitations").json()["invitations"][0]["status"] == "expired"


def test_job_drafts_publication_and_saved_jobs_are_tenant_scoped(auth_client):
    client, mailer = auth_client
    assert client.get("/api/v1/jobs").status_code == 200
    assert client.get("/api/v1/me/saved-jobs").status_code == 401
    guest_org, guest_job = uuid4(), uuid4()
    assert client.get(f"/api/v1/organizations/{guest_org}/jobs").status_code == 401
    assert client.get(f"/api/v1/organizations/{guest_org}/jobs/{guest_job}").status_code == 401
    assert client.post(
        f"/api/v1/organizations/{guest_org}/jobs",
        json={"request_key": str(uuid4()), "title": "Backend Engineer", "description": "Build and maintain our hiring platform services."},
    ).status_code == 401
    assert client.patch(
        f"/api/v1/organizations/{guest_org}/jobs/{guest_job}", json={"expected_version": 0, "title": "Backend Engineer"}
    ).status_code == 401
    assert client.post(
        f"/api/v1/organizations/{guest_org}/jobs/{guest_job}/publish", json={"expected_version": 0}
    ).status_code == 401
    assert client.post(
        f"/api/v1/organizations/{guest_org}/jobs/{guest_job}/close", json={"expected_version": 0}
    ).status_code == 401
    assert client.put(f"/api/v1/me/saved-jobs/{guest_job}").status_code == 401
    assert client.delete(f"/api/v1/me/saved-jobs/{guest_job}").status_code == 401
    register_and_login(client, mailer, "job-owner@example.com")
    csrf = client.cookies[CSRF_COOKIE]
    org = client.post(
        "/api/v1/organizations", json={"request_key": str(uuid4()), "name": "Hiring Labs"},
        headers={"X-CSRF-Token": csrf},
    ).json()["id"]
    onboarding = client.patch(
        f"/api/v1/organizations/{org}/onboarding", json={"expected_version": 0, "name": "Hiring Labs"},
        headers={"X-CSRF-Token": csrf},
    )
    assert onboarding.status_code == 200
    complete = client.post(
        f"/api/v1/organizations/{org}/onboarding/complete", json={"expected_version": onboarding.json()["version"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert complete.status_code == 200

    request_key = str(uuid4())
    body = {"request_key": request_key, "title": "Backend Engineer", "description": "Build and maintain our hiring platform services."}
    no_csrf = client.post(f"/api/v1/organizations/{org}/jobs", json=body)
    invalid = client.post(
        f"/api/v1/organizations/{org}/jobs", json={"request_key": str(uuid4()), "title": "  ", "description": "short"},
        headers={"X-CSRF-Token": csrf},
    )
    created = client.post(f"/api/v1/organizations/{org}/jobs", json=body, headers={"X-CSRF-Token": csrf})
    repeated = client.post(f"/api/v1/organizations/{org}/jobs", json=body, headers={"X-CSRF-Token": csrf})
    conflicting_key = client.post(
        f"/api/v1/organizations/{org}/jobs",
        json=body | {"title": "Product Engineer"}, headers={"X-CSRF-Token": csrf},
    )
    assert no_csrf.status_code == 403
    assert invalid.status_code == 422
    assert created.status_code == 201 and created.json()["status"] == "draft"
    assert repeated.status_code == 200 and repeated.json()["id"] == created.json()["id"]
    assert conflicting_key.status_code == 409
    job_id = created.json()["id"]
    assert client.get(f"/api/v1/organizations/{org}/jobs").json()["jobs"][0]["id"] == job_id
    assert client.get(f"/api/v1/organizations/{org}/jobs/{job_id}").status_code == 200
    assert client.get("/api/v1/jobs").json()["jobs"] == []
    assert client.get(f"/api/v1/jobs/{job_id}").status_code == 404
    assert client.get("/api/v1/jobs?limit=0").status_code == 422

    patch = client.patch(
        f"/api/v1/organizations/{org}/jobs/{job_id}",
        json={"expected_version": 0, "description": "Build reliable API services for the hiring platform."},
        headers={"X-CSRF-Token": csrf},
    )
    stale = client.patch(
        f"/api/v1/organizations/{org}/jobs/{job_id}",
        json={"expected_version": 0, "title": "Senior Backend Engineer"}, headers={"X-CSRF-Token": csrf},
    )
    extra = client.patch(
        f"/api/v1/organizations/{org}/jobs/{job_id}",
        json={"expected_version": 1, "title": "Backend Engineer", "admin": True}, headers={"X-CSRF-Token": csrf},
    )
    assert patch.status_code == 200 and patch.json()["version"] == 1
    assert stale.status_code == 409 and extra.status_code == 422
    publish = client.post(
        f"/api/v1/organizations/{org}/jobs/{job_id}/publish", json={"expected_version": 1},
        headers={"X-CSRF-Token": csrf},
    )
    repeat_publish = client.post(
        f"/api/v1/organizations/{org}/jobs/{job_id}/publish", json={"expected_version": 0},
        headers={"X-CSRF-Token": csrf},
    )
    assert publish.status_code == 200 and publish.json()["status"] == "published"
    assert repeat_publish.status_code == 200 and repeat_publish.json()["version"] == 2
    public = client.get(f"/api/v1/jobs/{job_id}")
    assert public.status_code == 200 and public.json()["organization_name"] == "Hiring Labs"
    assert public.headers["cache-control"] == "no-store"
    assert [job["id"] for job in client.get("/api/v1/jobs?limit=1&offset=0").json()["jobs"]] == [job_id]

    save = client.put(f"/api/v1/me/saved-jobs/{job_id}", headers={"X-CSRF-Token": csrf})
    repeat_save = client.put(f"/api/v1/me/saved-jobs/{job_id}", headers={"X-CSRF-Token": csrf})
    saved = client.get("/api/v1/me/saved-jobs")
    assert save.status_code == repeat_save.status_code == 204
    assert saved.status_code == 200 and [item["id"] for item in saved.json()["jobs"]] == [job_id]

    with TestClient(client.app, base_url="https://proofpath.test") as outsider:
        register_and_login(outsider, mailer, "job-outsider@example.com", purpose="employer")
        outsider_csrf = outsider.cookies[CSRF_COOKIE]
        assert outsider.get(f"/api/v1/organizations/{org}/jobs").status_code == 404
        assert outsider.get(f"/api/v1/organizations/{org}/jobs/{job_id}").status_code == 404
        assert outsider.post(
            f"/api/v1/organizations/{org}/jobs",
            json=body | {"request_key": str(uuid4())}, headers={"X-CSRF-Token": outsider_csrf},
        ).status_code == 404
        assert outsider.patch(
            f"/api/v1/organizations/{org}/jobs/{job_id}", json={"expected_version": 0, "title": "Other"},
            headers={"X-CSRF-Token": outsider_csrf},
        ).status_code == 404
        assert outsider.post(
            f"/api/v1/organizations/{org}/jobs/{job_id}/publish", json={"expected_version": 0},
            headers={"X-CSRF-Token": outsider_csrf},
        ).status_code == 404
        assert outsider.post(
            f"/api/v1/organizations/{org}/jobs/{job_id}/close", json={"expected_version": 0},
            headers={"X-CSRF-Token": outsider_csrf},
        ).status_code == 404
        assert outsider.put(f"/api/v1/me/saved-jobs/{job_id}", headers={"X-CSRF-Token": outsider_csrf}).status_code == 403
        with connect_database(TEST_DATABASE_URL) as connection:
            outsider_id = connection.execute("SELECT id FROM users WHERE email = 'job-outsider@example.com'").fetchone()["id"]
            connection.execute(
                "INSERT INTO organization_memberships (organization_id, user_id, role) VALUES (%s, %s, 'member')",
                (org, outsider_id),
            )
        assert outsider.get(f"/api/v1/organizations/{org}/jobs").status_code == 200
        forbidden_create = outsider.post(
            f"/api/v1/organizations/{org}/jobs", json=body | {"request_key": str(uuid4())},
            headers={"X-CSRF-Token": outsider_csrf},
        )
        forbidden_update = outsider.patch(
            f"/api/v1/organizations/{org}/jobs/{job_id}", json={"expected_version": 2, "title": "Changed"},
            headers={"X-CSRF-Token": outsider_csrf},
        )
        forbidden_publish = outsider.post(
            f"/api/v1/organizations/{org}/jobs/{job_id}/publish", json={"expected_version": 2},
            headers={"X-CSRF-Token": outsider_csrf},
        )
        forbidden_close = outsider.post(
            f"/api/v1/organizations/{org}/jobs/{job_id}/close", json={"expected_version": 2},
            headers={"X-CSRF-Token": outsider_csrf},
        )
        assert forbidden_create.status_code == forbidden_update.status_code == forbidden_publish.status_code == forbidden_close.status_code == 403

    close = client.post(
        f"/api/v1/organizations/{org}/jobs/{job_id}/close", json={"expected_version": 2},
        headers={"X-CSRF-Token": csrf},
    )
    repeat_close = client.post(
        f"/api/v1/organizations/{org}/jobs/{job_id}/close", json={"expected_version": 0},
        headers={"X-CSRF-Token": csrf},
    )
    assert close.status_code == 200 and close.json()["status"] == "closed"
    assert repeat_close.status_code == 200 and repeat_close.json()["version"] == 3
    assert client.get(f"/api/v1/jobs/{job_id}").status_code == 404
    assert client.get("/api/v1/me/saved-jobs").json()["jobs"][0]["status"] == "closed"
    assert client.delete(f"/api/v1/me/saved-jobs/{job_id}", headers={"X-CSRF-Token": csrf}).status_code == 204
    assert client.delete(f"/api/v1/me/saved-jobs/{job_id}", headers={"X-CSRF-Token": csrf}).status_code == 204
    with connect_database(TEST_DATABASE_URL) as connection:
        actions = connection.execute(
            "SELECT action FROM audit_events WHERE resource_id = %s ORDER BY action", (job_id,)
        ).fetchall()
    assert {row["action"] for row in actions} >= {
        "organization.job_created", "organization.job_updated", "organization.job_published", "organization.job_closed",
        "candidate.job_saved", "candidate.job_unsaved",
    }


def test_applications_snapshot_consent_withdrawal_pipeline_and_human_decision(auth_client):
    client, mailer = auth_client
    with TestClient(client.app, base_url="https://proofpath.test") as guest:
        guest_job = uuid4()
        assert guest.get("/api/v1/me/applications").status_code == 401
        assert guest.post(f"/api/v1/jobs/{guest_job}/applications", json={}).status_code == 401
        assert guest.get(f"/api/v1/me/applications/{uuid4()}").status_code == 401
        assert guest.post(
            f"/api/v1/me/applications/{uuid4()}/withdraw", json={"expected_version": 1}
        ).status_code == 401
        guest_org, guest_job, guest_application = uuid4(), uuid4(), uuid4()
        assert guest.get(f"/api/v1/organizations/{guest_org}/jobs/{guest_job}/applications").status_code == 401
        assert guest.get(f"/api/v1/organizations/{guest_org}/applications/{guest_application}").status_code == 401
        assert guest.patch(
            f"/api/v1/organizations/{guest_org}/applications/{guest_application}/stage",
            json={"expected_version": 1, "stage": "in_review"},
        ).status_code == 401
        assert guest.post(
            f"/api/v1/organizations/{guest_org}/applications/{guest_application}/decision",
            json={"expected_version": 1, "decision": "rejected", "reason": "Human decision reason."},
        ).status_code == 401
        assert guest.post(
            f"/api/v1/organizations/{guest_org}/applications/{guest_application}/assign-reviewer",
            json={"request_key": str(uuid4()), "expected_version": 0, "reviewer_user_id": str(uuid4())},
        ).status_code == 401
        assert guest.get(f"/api/v1/organizations/{guest_org}/applications/{guest_application}/history").status_code == 401

    owner = register_and_login(client, mailer, "application-owner@example.com", purpose="employer")
    owner_csrf = client.cookies[CSRF_COOKIE]
    org_id, job_id = create_published_job(client)
    owner_candidate = client.patch(
        "/api/v1/me/candidate/onboarding", json={"expected_version": 0, "target_roles": ["Platform Engineer"]},
        headers={"X-CSRF-Token": owner_csrf},
    )
    assert owner_candidate.status_code == 200
    assert client.post(
        "/api/v1/me/candidate/onboarding/complete", json={"expected_version": 1},
        headers={"X-CSRF-Token": owner_csrf},
    ).status_code == 200
    draft = client.post(
        f"/api/v1/organizations/{org_id}/jobs",
        json={"request_key": str(uuid4()), "title": "Private Draft Role", "description": "This job is not available to applicants."},
        headers={"X-CSRF-Token": owner_csrf},
    )
    assert draft.status_code == 201

    with TestClient(client.app, base_url="https://proofpath.test") as candidate:
        applicant = register_and_login(candidate, mailer, "applicant@example.com")
        project_id = complete_and_publish_candidate_portfolio(candidate)
        candidate_csrf = candidate.cookies[CSRF_COOKIE]
        request_key = str(uuid4())
        create_url = f"/api/v1/jobs/{job_id}/applications"
        application_body = {
            "request_key": request_key,
            "portfolio_consent_version": "job-application-public-portfolio-v1",
            "cover_note": "I have delivered reliable backend systems.",
        }
        assert candidate.post(
            f"/api/v1/jobs/{draft.json()['id']}/applications", json=application_body,
            headers={"X-CSRF-Token": candidate_csrf},
        ).status_code == 404
        assert candidate.post(create_url, json=application_body).status_code == 403
        invalid = candidate.post(
            create_url, json=application_body | {"portfolio_consent_version": "unchecked"},
            headers={"X-CSRF-Token": candidate_csrf},
        )
        extra = candidate.post(
            create_url, json=application_body | {"admin": True}, headers={"X-CSRF-Token": candidate_csrf}
        )
        assert invalid.status_code == extra.status_code == 422
        with connect_database(TEST_DATABASE_URL) as connection:
            connection.execute(
                "INSERT INTO organization_memberships (organization_id, user_id, role) VALUES (%s, %s, 'member')",
                (org_id, applicant["user"]["id"]),
            )
        conflict = candidate.post(create_url, json=application_body, headers={"X-CSRF-Token": candidate_csrf})
        assert conflict.status_code == 403
        with connect_database(TEST_DATABASE_URL) as connection:
            connection.execute(
                "DELETE FROM organization_memberships WHERE organization_id = %s AND user_id = %s",
                (org_id, applicant["user"]["id"]),
            )

        created = candidate.post(create_url, json=application_body, headers={"X-CSRF-Token": candidate_csrf})
        repeated = candidate.post(create_url, json=application_body, headers={"X-CSRF-Token": candidate_csrf})
        key_conflict = candidate.post(
            create_url, json=application_body | {"cover_note": "Different application content."},
            headers={"X-CSRF-Token": candidate_csrf},
        )
        duplicate = candidate.post(
            create_url, json=application_body | {"request_key": str(uuid4())},
            headers={"X-CSRF-Token": candidate_csrf},
        )
        assert created.status_code == 201, created.text
        assert created.headers["cache-control"] == "no-store"
        assert repeated.status_code == 200 and repeated.json()["id"] == created.json()["id"]
        assert key_conflict.status_code == duplicate.status_code == 409
        application_id = created.json()["id"]
        snapshot = created.json()["portfolio_snapshot"]
        assert [project["name"] for project in snapshot["projects"]] == ["Published Project"]
        assert "Private Draft Project" not in created.text and "applicant@example.com" not in created.text
        assert candidate.get("/api/v1/me/applications?limit=0").status_code == 422
        assert candidate.get("/api/v1/me/applications").json()["applications"][0]["id"] == application_id
        assert client.get("/api/v1/me/applications").json()["applications"] == []
        candidate_detail = candidate.get(f"/api/v1/me/applications/{application_id}")
        assert candidate_detail.json()["portfolio_snapshot"] == snapshot
        assert candidate_detail.headers["cache-control"] == "no-store"
        assert candidate.get(f"/api/v1/me/applications/{uuid4()}").status_code == 404
        assert client.get(f"/api/v1/me/applications/{application_id}").status_code == 404
        assert client.post(
            f"/api/v1/me/applications/{application_id}/withdraw", json={"expected_version": 1},
            headers={"X-CSRF-Token": owner_csrf},
        ).status_code == 404

        applications_url = f"/api/v1/organizations/{org_id}/jobs/{job_id}/applications"
        employer_list = client.get(applications_url)
        employer_detail_url = f"/api/v1/organizations/{org_id}/applications/{application_id}"
        employer_detail = client.get(employer_detail_url)
        assert employer_list.status_code == 200
        assert employer_list.headers["cache-control"] == "no-store"
        assert client.get(f"{applications_url}?limit=0").status_code == 422
        assert employer_list.json()["applications"][0]["applicant_name"] == "Applicant Name"
        assert "email" not in employer_list.text.lower()
        assert employer_detail.status_code == 200
        assert employer_detail.headers["cache-control"] == "no-store"
        assert employer_detail.json()["portfolio_snapshot"] == snapshot
        assert employer_detail.json()["decided_by_user_id"] is None
        assert client.get(f"/api/v1/organizations/{uuid4()}/applications/{application_id}").status_code == 404
        assert client.get(f"/api/v1/organizations/{uuid4()}/jobs/{job_id}/applications").status_code == 404

        # The immutable application snapshot remains available to its authorized employer after public revocation.
        assert candidate.post("/api/v1/me/portfolio/unpublish", headers={"X-CSRF-Token": candidate_csrf}).status_code == 204
        assert candidate.get(f"/api/v1/public/portfolios/application-test").status_code == 404
        assert candidate.get(f"/api/v1/me/applications/{application_id}").json()["portfolio_snapshot"] == snapshot
        assert client.get(employer_detail_url).json()["portfolio_snapshot"] == snapshot
        stale_withdraw = candidate.post(
            f"/api/v1/me/applications/{application_id}/withdraw", json={"expected_version": 2},
            headers={"X-CSRF-Token": candidate_csrf},
        )
        invalid_withdraw = candidate.post(
            f"/api/v1/me/applications/{application_id}/withdraw", json={"expected_version": 0},
            headers={"X-CSRF-Token": candidate_csrf},
        )
        no_csrf_withdraw = candidate.post(
            f"/api/v1/me/applications/{application_id}/withdraw", json={"expected_version": 1}
        )
        assert stale_withdraw.status_code == 409
        assert invalid_withdraw.status_code == 422 and no_csrf_withdraw.status_code == 403
        withdrawn = candidate.post(
            f"/api/v1/me/applications/{application_id}/withdraw", json={"expected_version": 1},
            headers={"X-CSRF-Token": candidate_csrf},
        )
        repeat_withdraw = candidate.post(
            f"/api/v1/me/applications/{application_id}/withdraw", json={"expected_version": 1},
            headers={"X-CSRF-Token": candidate_csrf},
        )
        assert withdrawn.status_code == 200 and withdrawn.json()["status"] == "withdrawn"
        assert repeat_withdraw.status_code == 200 and repeat_withdraw.json()["version"] == withdrawn.json()["version"]
        assert client.get(applications_url).json()["applications"] == []
        assert client.get(employer_detail_url).status_code == 404
        withdrawn_publication = candidate.post(
            create_url, json=application_body | {"request_key": str(uuid4())},
            headers={"X-CSRF-Token": candidate_csrf},
        )
        assert withdrawn_publication.status_code == 409

        # A candidate can reapply after withdrawal with a fresh request key and a new consented snapshot.
        assert candidate.post(
            "/api/v1/me/portfolio/publish", json={"consent_version": "public-portfolio-v1"},
            headers={"X-CSRF-Token": candidate_csrf},
        ).status_code == 200
        assert candidate.patch(
            f"/api/v1/me/projects/{project_id}",
            json={"expected_version": 0, "contribution": "Updated contribution for the next application."},
            headers={"X-CSRF-Token": candidate_csrf},
        ).status_code == 200
        assert candidate.post(
            f"/api/v1/me/projects/{project_id}/publish",
            json={"expected_version": 1, "consent_version": "public-project-v1"},
            headers={"X-CSRF-Token": candidate_csrf},
        ).status_code == 200
        assert candidate.get(f"/api/v1/me/applications/{application_id}").json()["portfolio_snapshot"] == snapshot
        second = candidate.post(
            create_url, json=application_body | {"request_key": str(uuid4())},
            headers={"X-CSRF-Token": candidate_csrf},
        )
        assert second.status_code == 201, second.text
        assert second.json()["portfolio_snapshot"]["projects"][0]["contribution"] == "Updated contribution for the next application."
        second_id = second.json()["id"]
        second_detail_url = f"/api/v1/organizations/{org_id}/applications/{second_id}"

        with TestClient(client.app, base_url="https://proofpath.test") as reviewer:
            reviewer_login = register_and_login(reviewer, mailer, "application-reviewer@example.com", purpose="employer")
            reviewer_id = reviewer_login["user"]["id"]
            reviewer_csrf = reviewer.cookies[CSRF_COOKIE]
            with connect_database(TEST_DATABASE_URL) as connection:
                connection.execute(
                    "INSERT INTO organization_memberships (organization_id, user_id, role) VALUES (%s, %s, 'recruiter')",
                    (org_id, reviewer_id),
                )
            assert reviewer.get(applications_url).json()["applications"] == []
            assert reviewer.get(second_detail_url).status_code == 404
            assert reviewer.get(f"/api/v1/organizations/{org_id}/applications/{second_id}/history").status_code == 404
            evidence_report_url = f"/api/v1/organizations/{org_id}/applications/{second_id}/evidence-report"
            assert reviewer.get(evidence_report_url).status_code == 404
            assert reviewer.patch(
                f"/api/v1/organizations/{org_id}/applications/{second_id}/stage",
                json={"expected_version": 1, "stage": "in_review"}, headers={"X-CSRF-Token": reviewer_csrf},
            ).status_code == 404
            assignment_url = f"/api/v1/organizations/{org_id}/applications/{second_id}/assign-reviewer"
            assignment_body = {"request_key": str(uuid4()), "expected_version": 0, "reviewer_user_id": reviewer_id}
            assert client.post(assignment_url, json=assignment_body).status_code == 403
            assert reviewer.post(
                assignment_url, json=assignment_body, headers={"X-CSRF-Token": reviewer_csrf}
            ).status_code == 403
            assert client.post(
                assignment_url, json=assignment_body | {"reviewer_user_id": str(uuid4())},
                headers={"X-CSRF-Token": owner_csrf},
            ).status_code == 404
            invalid_assignment = client.post(
                assignment_url, json=assignment_body | {"expected_version": -1},
                headers={"X-CSRF-Token": owner_csrf},
            )
            created_assignment = client.post(assignment_url, json=assignment_body, headers={"X-CSRF-Token": owner_csrf})
            repeated_assignment = client.post(assignment_url, json=assignment_body, headers={"X-CSRF-Token": owner_csrf})
            reused_assignment_key = client.post(
                assignment_url, json=assignment_body | {"reviewer_user_id": owner["user"]["id"]},
                headers={"X-CSRF-Token": owner_csrf},
            )
            stale_assignment = client.post(
                assignment_url, json=assignment_body | {"request_key": str(uuid4())},
                headers={"X-CSRF-Token": owner_csrf},
            )
            assert invalid_assignment.status_code == 422
            assert created_assignment.status_code == 201 and created_assignment.json()["version"] == 1
            assert repeated_assignment.status_code == 200 and repeated_assignment.json() == created_assignment.json()
            assert reused_assignment_key.status_code == stale_assignment.status_code == 409
            assert client.get(second_detail_url).json()["assigned_reviewer_user_id"] == reviewer_id
            assert client.get(second_detail_url).json()["reviewer_assignment_version"] == 1
            assert [item["id"] for item in reviewer.get(applications_url).json()["applications"]] == [second_id]
            reviewer_report = reviewer.get(evidence_report_url)
            assert reviewer_report.status_code == 200 and reviewer_report.json()["requirements"] == []
            reviewer_stage = reviewer.patch(
                f"/api/v1/organizations/{org_id}/applications/{second_id}/stage",
                json={"expected_version": 1, "stage": "in_review"}, headers={"X-CSRF-Token": reviewer_csrf},
            )
            assert reviewer_stage.status_code == 200 and reviewer_stage.json()["version"] == 2
            assert reviewer.post(
                f"/api/v1/organizations/{org_id}/applications/{second_id}/decision",
                json={"expected_version": 2, "decision": "rejected", "reason": "Human decision reason."},
                headers={"X-CSRF-Token": reviewer_csrf},
            ).status_code == 403
            with TestClient(client.app, base_url="https://proofpath.test") as unassigned:
                unassigned_login = register_and_login(unassigned, mailer, "unassigned-reviewer@example.com", purpose="employer")
                with connect_database(TEST_DATABASE_URL) as connection:
                    connection.execute(
                        "INSERT INTO organization_memberships (organization_id, user_id, role) VALUES (%s, %s, 'recruiter')",
                        (org_id, unassigned_login["user"]["id"]),
                    )
                assert unassigned.get(applications_url).json()["applications"] == []
                assert unassigned.get(second_detail_url).status_code == 404
                assert unassigned.get(evidence_report_url).status_code == 404
            demoted = client.patch(
                f"/api/v1/organizations/{org_id}/members/{reviewer_id}/role", json={"role": "member"},
                headers={"X-CSRF-Token": owner_csrf},
            )
            assert demoted.status_code == 200 and demoted.json()["role"] == "member"
            assert reviewer.get(applications_url).status_code == 403
            with connect_database(TEST_DATABASE_URL) as connection:
                assert connection.execute(
                    "SELECT 1 FROM application_reviewer_assignments WHERE application_id = %s", (second_id,)
                ).fetchone() is None
                assert connection.execute(
                    """SELECT 1 FROM audit_events WHERE resource_type = 'application_reviewer_assignment'
                       AND resource_id = %s AND action = 'organization.application_reviewer_revoked_on_role_change'""",
                    (second_id,),
                ).fetchone() is not None
            assignment_openapi = client.get("/openapi.json").json()
            assert assignment_openapi["paths"]["/api/v1/organizations/{org_id}/applications/{application_id}/assign-reviewer"]["post"]["security"] == [
                {"ProofpathSession": [], "ProofpathCSRF": []}
            ]
            assert "ApplicationReviewerAssignmentRequest" in assignment_openapi["components"]["schemas"]

        with TestClient(client.app, base_url="https://proofpath.test") as member:
            register_and_login(member, mailer, "application-member@example.com", purpose="employer")
            member_csrf = member.cookies[CSRF_COOKIE]
            with connect_database(TEST_DATABASE_URL) as connection:
                member_id = connection.execute(
                    "SELECT id FROM users WHERE email = 'application-member@example.com'"
                ).fetchone()["id"]
                connection.execute(
                    "INSERT INTO organization_memberships (organization_id, user_id, role) VALUES (%s, %s, 'member')",
                    (org_id, member_id),
                )
            assert member.get(applications_url).status_code == 403
            assert member.get(second_detail_url).status_code == 403
            assert member.patch(
                f"/api/v1/organizations/{org_id}/applications/{second_id}/stage",
                json={"expected_version": 1, "stage": "in_review"}, headers={"X-CSRF-Token": member_csrf},
            ).status_code == 403
            assert member.post(
                f"/api/v1/organizations/{org_id}/applications/{second_id}/decision",
                json={"expected_version": 1, "decision": "rejected", "reason": "Human decision reason."},
                headers={"X-CSRF-Token": member_csrf},
            ).status_code == 403
            assert member.get(f"/api/v1/organizations/{uuid4()}/applications/{second_id}").status_code == 404
            other_org_id, other_job_id = create_published_job(
                member, title="Another Organization Engineer", organization_name="Other Hiring Labs"
            )
            cross_tenant_reviewer = member.get("/api/v1/auth/session").json()["user"]["id"]
            cross_tenant_assignment = client.post(
                f"/api/v1/organizations/{org_id}/applications/{second_id}/assign-reviewer",
                json={"request_key": str(uuid4()), "expected_version": 0, "reviewer_user_id": cross_tenant_reviewer},
                headers={"X-CSRF-Token": owner_csrf},
            )
            assert cross_tenant_assignment.status_code == 404
            assert member.get(f"/api/v1/organizations/{other_org_id}/applications/{second_id}").status_code == 404
            assert member.get(
                f"/api/v1/organizations/{other_org_id}/jobs/{other_job_id}/applications"
            ).json()["applications"] == []
            assert member.get(
                f"/api/v1/organizations/{other_org_id}/applications/{second_id}/history"
            ).status_code == 404
            assert member.patch(
                f"/api/v1/organizations/{other_org_id}/applications/{second_id}/stage",
                json={"expected_version": 1, "stage": "in_review"}, headers={"X-CSRF-Token": member_csrf},
            ).status_code == 404
            assert member.post(
                f"/api/v1/organizations/{other_org_id}/applications/{second_id}/decision",
                json={"expected_version": 1, "decision": "rejected", "reason": "Human decision reason."},
                headers={"X-CSRF-Token": member_csrf},
            ).status_code == 404

        no_csrf_stage = client.patch(
            f"/api/v1/organizations/{org_id}/applications/{second_id}/stage",
            json={"expected_version": 2, "stage": "assessment"},
        )
        stale_stage = client.patch(
            f"/api/v1/organizations/{org_id}/applications/{second_id}/stage",
            json={"expected_version": 1, "stage": "assessment"}, headers={"X-CSRF-Token": owner_csrf},
        )
        assert no_csrf_stage.status_code == 403 and stale_stage.status_code == 409
        in_review = client.patch(
            f"/api/v1/organizations/{org_id}/applications/{second_id}/stage",
            json={"expected_version": 2, "stage": "in_review"}, headers={"X-CSRF-Token": owner_csrf},
        )
        premature = client.post(
            f"/api/v1/organizations/{org_id}/applications/{second_id}/decision",
            json={"expected_version": 2, "decision": "hired", "reason": "Approved by the hiring manager."},
            headers={"X-CSRF-Token": owner_csrf},
        )
        same_stage = client.patch(
            f"/api/v1/organizations/{org_id}/applications/{second_id}/stage",
            json={"expected_version": 2, "stage": "in_review"}, headers={"X-CSRF-Token": owner_csrf},
        )
        offer = client.patch(
            f"/api/v1/organizations/{org_id}/applications/{second_id}/stage",
            json={"expected_version": 2, "stage": "offer"}, headers={"X-CSRF-Token": owner_csrf},
        )
        assert in_review.status_code == 200 and in_review.json()["version"] == 2
        assert stale_stage.status_code == 409
        assert same_stage.status_code == 200 and same_stage.json()["version"] == 2
        assert premature.status_code == 409
        assert offer.status_code == 200 and offer.json()["status"] == "offer"
        short_reason = client.post(
            f"/api/v1/organizations/{org_id}/applications/{second_id}/decision",
            json={"expected_version": 3, "decision": "hired", "reason": "short"},
            headers={"X-CSRF-Token": owner_csrf},
        )
        hired = client.post(
            f"/api/v1/organizations/{org_id}/applications/{second_id}/decision",
            json={"expected_version": 3, "decision": "hired", "reason": "Approved by the hiring manager."},
            headers={"X-CSRF-Token": owner_csrf},
        )
        assert short_reason.status_code == 422
        assert hired.status_code == 200 and hired.json()["status"] == "hired"
        assert hired.json()["decided_by_user_id"] == owner["user"]["id"]
        candidate_result = candidate.get(f"/api/v1/me/applications/{second_id}")
        assert candidate_result.status_code == 200 and candidate_result.json()["status"] == "hired"
        assert "Approved by the hiring manager." not in candidate_result.text
        assert candidate.post(
            f"/api/v1/me/applications/{second_id}/withdraw", json={"expected_version": 4},
            headers={"X-CSRF-Token": candidate_csrf},
        ).status_code == 409
        assert client.post(
            f"/api/v1/organizations/{org_id}/applications/{second_id}/decision",
            json={"expected_version": 4, "decision": "rejected", "reason": "Attempt to revise the decision."},
            headers={"X-CSRF-Token": owner_csrf},
        ).status_code == 409

        history = client.get(f"{second_detail_url}/history")
        assert history.status_code == 200
        assert history.headers["cache-control"] == "no-store"
        assert [event["to_status"] for event in history.json()["events"]] == ["submitted", "in_review", "offer", "hired"]
        assert history.json()["events"][-1]["actor_id"] == owner["user"]["id"]
        with connect_database(TEST_DATABASE_URL) as connection:
            reason = connection.execute(
                "SELECT decision_reason, decided_by_user_id FROM job_applications WHERE id = %s", (second_id,)
            ).fetchone()
            audit_actions = connection.execute(
                "SELECT action FROM audit_events WHERE resource_id IN (%s, %s)", (application_id, second_id)
            ).fetchall()
        assert reason["decision_reason"] == "Approved by the hiring manager."
        assert str(reason["decided_by_user_id"]) == owner["user"]["id"]
        assert {event["action"] for event in audit_actions} >= {
            "candidate.application_submitted", "candidate.application_withdrawn",
            "organization.application_stage_changed", "organization.application_decision_recorded",
        }


def test_interview_scheduling_is_private_versioned_and_conflict_checked(auth_client):
    employer, mailer = auth_client
    with TestClient(employer.app, base_url="https://proofpath.test") as guest:
        assert guest.get("/api/v1/me/interviews").status_code == 401
        assert guest.get("/api/v1/me/notifications").status_code == 401
        assert guest.get(f"/api/v1/organizations/{uuid4()}/interviews").status_code == 401
        assert guest.post(
            f"/api/v1/me/interviews/{uuid4()}/respond", json={"expected_version": 1, "action": "accept"}
        ).status_code == 401
    register_and_login(employer, mailer, "interview-owner@example.com", purpose="employer")
    employer_csrf = employer.cookies[CSRF_COOKIE]
    organization_id, job_id = create_published_job(employer, title="Interview Backend Engineer")

    with TestClient(employer.app, base_url="https://proofpath.test") as candidate, TestClient(employer.app, base_url="https://proofpath.test") as other_candidate:
        register_and_login(candidate, mailer, "interview-candidate@example.com")
        complete_and_publish_candidate_portfolio(candidate, handle="interview-candidate")
        candidate_csrf = candidate.cookies[CSRF_COOKIE]
        application = candidate.post(
            f"/api/v1/jobs/{job_id}/applications",
            json={"request_key": str(uuid4()), "portfolio_consent_version": "job-application-public-portfolio-v1"},
            headers={"X-CSRF-Token": candidate_csrf},
        )
        assert application.status_code == 201, application.text
        application_id = application.json()["id"]
        for expected_version, stage in ((1, "in_review"), (2, "interview")):
            moved = employer.patch(
                f"/api/v1/organizations/{organization_id}/applications/{application_id}/stage",
                json={"expected_version": expected_version, "stage": stage},
                headers={"X-CSRF-Token": employer_csrf},
            )
            assert moved.status_code == 200, moved.text

        register_and_login(other_candidate, mailer, "other-interview-candidate@example.com")
        complete_and_publish_candidate_portfolio(other_candidate, handle="other-interview-candidate")
        other_candidate_csrf = other_candidate.cookies[CSRF_COOKIE]
        other_application = other_candidate.post(
            f"/api/v1/jobs/{job_id}/applications",
            json={"request_key": str(uuid4()), "portfolio_consent_version": "job-application-public-portfolio-v1"},
            headers={"X-CSRF-Token": other_candidate_csrf},
        )
        assert other_application.status_code == 201, other_application.text
        other_application_id = other_application.json()["id"]
        for expected_version, stage in ((1, "in_review"), (2, "interview")):
            moved = employer.patch(
                f"/api/v1/organizations/{organization_id}/applications/{other_application_id}/stage",
                json={"expected_version": expected_version, "stage": stage},
                headers={"X-CSRF-Token": employer_csrf},
            )
            assert moved.status_code == 200, moved.text
        assert other_candidate.get("/api/v1/me/interviews").json()["interviews"] == []

        interview_url = f"/api/v1/organizations/{organization_id}/interviews"
        scheduled_at = datetime.now(timezone.utc) + timedelta(days=2)
        schedule = {
            "application_id": application_id,
            "request_key": str(uuid4()),
            "scheduled_at": scheduled_at.isoformat(),
            "duration_minutes": 30,
            "display_timezone": "Asia/Kolkata",
            "meeting_url": "https://meet.example.com/proofpath-room",
            "candidate_note": "We will discuss your portfolio and project decisions.",
        }
        assert employer.post(interview_url, json=schedule).status_code == 403
        invalid_timezone = employer.post(
            interview_url, json=schedule | {"request_key": str(uuid4()), "display_timezone": "Not/AZone"},
            headers={"X-CSRF-Token": employer_csrf},
        )
        invalid_link = employer.post(
            interview_url, json=schedule | {"request_key": str(uuid4()), "meeting_url": "https://user:secret@meet.example.com"},
            headers={"X-CSRF-Token": employer_csrf},
        )
        assert invalid_timezone.status_code == invalid_link.status_code == 422
        created = employer.post(interview_url, json=schedule, headers={"X-CSRF-Token": employer_csrf})
        replay = employer.post(interview_url, json=schedule, headers={"X-CSRF-Token": employer_csrf})
        key_reuse = employer.post(
            interview_url, json=schedule | {"candidate_note": "Changed details under the same request key."},
            headers={"X-CSRF-Token": employer_csrf},
        )
        conflict = employer.post(
            interview_url,
            json=schedule | {"application_id": other_application_id, "request_key": str(uuid4()), "scheduled_at": (scheduled_at + timedelta(minutes=15)).isoformat()},
            headers={"X-CSRF-Token": employer_csrf},
        )
        assert created.status_code == 201, created.text
        assert "request_payload_hash" not in created.text
        assert replay.status_code == 200 and replay.json() == created.json()
        assert key_reuse.status_code == 409 and conflict.json()["code"] == "INTERVIEW_TIME_CONFLICT"
        wrong_org_create = employer.post(
            f"/api/v1/organizations/{uuid4()}/interviews",
            json=schedule | {"request_key": str(uuid4())}, headers={"X-CSRF-Token": employer_csrf},
        )
        assert wrong_org_create.status_code == 404
        interview_id = created.json()["id"]
        candidate_interviews = candidate.get("/api/v1/me/interviews")
        assert candidate_interviews.status_code == 200
        assert candidate_interviews.headers["cache-control"] == "no-store"
        assert len(candidate_interviews.json()["interviews"]) == 1
        assert candidate_interviews.json()["interviews"][0]["meeting_url"] == schedule["meeting_url"]
        assert candidate_interviews.json()["interviews"][0]["candidate_name"] is None
        notifications_url = "/api/v1/me/notifications"
        initial_notifications = candidate.get(notifications_url)
        assert initial_notifications.status_code == 200 and initial_notifications.json()["unread_count"] == 1
        scheduled_notification = initial_notifications.json()["notifications"][0]
        assert scheduled_notification["notification_type"] == "interview_scheduled"
        assert schedule["meeting_url"] not in initial_notifications.text
        assert employer.get(notifications_url).json()["notifications"] == []
        assert other_candidate.get(notifications_url).json()["notifications"] == []
        assert candidate.patch(
            f"{notifications_url}/{scheduled_notification['id']}", json={"is_read": True}
        ).status_code == 403
        assert other_candidate.patch(
            f"{notifications_url}/{scheduled_notification['id']}", json={"is_read": True},
            headers={"X-CSRF-Token": other_candidate_csrf},
        ).status_code == 404
        read_notification = candidate.patch(
            f"{notifications_url}/{scheduled_notification['id']}", json={"is_read": True},
            headers={"X-CSRF-Token": candidate_csrf},
        )
        assert read_notification.status_code == 200 and read_notification.json()["is_read"] is True

        with TestClient(employer.app, base_url="https://proofpath.test") as unassigned:
            reviewer = register_and_login(unassigned, mailer, "unassigned-interview-reviewer@example.com", purpose="employer")
            reviewer_csrf = unassigned.cookies[CSRF_COOKIE]
            with connect_database(TEST_DATABASE_URL) as connection:
                connection.execute(
                    "INSERT INTO organization_memberships (organization_id, user_id, role) VALUES (%s, %s, 'recruiter')",
                    (organization_id, reviewer["user"]["id"]),
                )
            assert unassigned.get(interview_url).json()["interviews"] == []
            forbidden = unassigned.post(
                interview_url, json=schedule | {"request_key": str(uuid4())},
                headers={"X-CSRF-Token": reviewer_csrf},
            )
            assert forbidden.status_code == 404

        response_url = f"/api/v1/me/interviews/{interview_id}/respond"
        no_csrf = candidate.post(response_url, json={"expected_version": 1, "action": "accept"})
        stale = candidate.post(
            response_url, json={"expected_version": 0, "action": "accept"},
            headers={"X-CSRF-Token": candidate_csrf},
        )
        accepted = candidate.post(
            response_url, json={"expected_version": 1, "action": "accept", "message": "That time works for me."},
            headers={"X-CSRF-Token": candidate_csrf},
        )
        assert no_csrf.status_code == 403 and stale.status_code == 422
        assert accepted.status_code == 200 and accepted.json()["candidate_response"] == "accepted"
        assert accepted.json()["response_message"] == "That time works for me."
        employer_notifications = employer.get(notifications_url)
        assert employer_notifications.status_code == 200 and employer_notifications.json()["unread_count"] == 1
        assert employer_notifications.json()["notifications"][0]["notification_type"] == "interview_response"
        assert "interview-candidate@example.com" not in employer_notifications.text
        assert employer.post(
            f"{notifications_url}/read-all", headers={"X-CSRF-Token": employer_csrf}
        ).status_code == 204
        stale_response = candidate.post(
            response_url, json={"expected_version": 1, "action": "decline"},
            headers={"X-CSRF-Token": candidate_csrf},
        )
        assert stale_response.status_code == 409
        org_list = employer.get(interview_url)
        assert org_list.status_code == 200 and org_list.json()["interviews"][0]["candidate_name"] == "Applicant Name"
        assert "interview-candidate@example.com" not in org_list.text
        wrong_tenant = employer.get(f"/api/v1/organizations/{uuid4()}/interviews")
        assert wrong_tenant.status_code == 404

        changed_time = scheduled_at + timedelta(days=1)
        update = employer.patch(
            f"{interview_url}/{interview_id}",
            json={"expected_version": 2, "scheduled_at": changed_time.isoformat(), "duration_minutes": 45,
                  "display_timezone": "UTC", "meeting_url": schedule["meeting_url"], "candidate_note": "Updated interview details."},
            headers={"X-CSRF-Token": employer_csrf},
        )
        assert update.status_code == 200 and update.json()["candidate_response"] == "pending"
        assert update.json()["version"] == 3
        stale_update = employer.patch(
            f"{interview_url}/{interview_id}",
            json={"expected_version": 2, "scheduled_at": (changed_time + timedelta(days=1)).isoformat(),
                  "duration_minutes": 30, "display_timezone": "UTC", "meeting_url": None, "candidate_note": "A stale change."},
            headers={"X-CSRF-Token": employer_csrf},
        )
        assert stale_update.status_code == 409
        assert candidate.get("/api/v1/me/interviews").json()["interviews"][0]["response_message"] is None
        accepted_again = candidate.post(
            response_url, json={"expected_version": 3, "action": "accept"},
            headers={"X-CSRF-Token": candidate_csrf},
        )
        assert accepted_again.status_code == 200 and accepted_again.json()["version"] == 4
        invalid_cancel = employer.post(
            f"{interview_url}/{interview_id}/cancel", json={"expected_version": 4, "reason": "No"},
            headers={"X-CSRF-Token": employer_csrf},
        )
        cancelled = employer.post(
            f"{interview_url}/{interview_id}/cancel",
            json={"expected_version": 4, "reason": "The interviewer is no longer available."},
            headers={"X-CSRF-Token": employer_csrf},
        )
        assert invalid_cancel.status_code == 422
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
        stale_cancel = employer.post(
            f"{interview_url}/{interview_id}/cancel",
            json={"expected_version": 4, "reason": "An out-of-date cancellation."},
            headers={"X-CSRF-Token": employer_csrf},
        )
        assert stale_cancel.status_code == 409
        assert candidate.get("/api/v1/me/interviews").json()["interviews"][0]["cancellation_reason"] == "The interviewer is no longer available."
        assert candidate.get(notifications_url).json()["unread_count"] == 2

        second_schedule = employer.post(
            interview_url, json=schedule | {"request_key": str(uuid4()), "scheduled_at": changed_time.isoformat()},
            headers={"X-CSRF-Token": employer_csrf},
        )
        assert second_schedule.status_code == 201, second_schedule.text
        withdrawn = candidate.post(
            f"/api/v1/me/applications/{application_id}/withdraw", json={"expected_version": 3},
            headers={"X-CSRF-Token": candidate_csrf},
        )
        assert withdrawn.status_code == 200
        assert candidate.get("/api/v1/me/interviews").json()["interviews"] == []
        with connect_database(TEST_DATABASE_URL) as connection:
            states = connection.execute(
                "SELECT status FROM application_interviews WHERE application_id = %s ORDER BY created_at", (application_id,)
            ).fetchall()
            audit = connection.execute(
                "SELECT action FROM audit_events WHERE resource_type = 'application_interview' AND resource_id = %s",
                (second_schedule.json()["id"],),
            ).fetchall()
        assert [row["status"] for row in states] == ["cancelled", "cancelled"]
        assert any(row["action"] == "candidate.interview_cancelled_on_withdrawal" for row in audit)

        export = candidate.post(
            "/api/v1/me/data-exports", json={"current_password": "a long secure password"},
            headers={"X-CSRF-Token": candidate_csrf},
        )
        assert export.status_code == 200
        assert len(export.json()["interviews"]) == 2
        assert len(export.json()["notifications"]) == 4
        openapi = employer.get("/openapi.json").json()
        assert openapi["paths"]["/api/v1/me/interviews"]["get"]["security"] == [{"ProofpathSession": []}]
        assert openapi["paths"]["/api/v1/me/interviews/{interview_id}/respond"]["post"]["security"] == [
            {"ProofpathSession": [], "ProofpathCSRF": []}
        ]
        assert openapi["paths"]["/api/v1/me/notifications"]["get"]["security"] == [{"ProofpathSession": []}]
        assert openapi["paths"]["/api/v1/me/notifications/{notification_id}"]["patch"]["security"] == [
            {"ProofpathSession": [], "ProofpathCSRF": []}
        ]


def test_job_competency_requirements_are_validated_public_and_frozen_for_applications(auth_client):
    employer, mailer = auth_client
    register_and_login(employer, mailer, "requirements-owner@example.com", purpose="employer")
    csrf = employer.cookies[CSRF_COOKIE]
    original = [{
        "competency_code": "fastapi.api_design", "target_level": "mid",
        "accepted_verification_methods": ["human_review"], "max_age_days": 180,
    }]
    organization_id, job_id = create_published_job(
        employer, title="Evidence-first API Engineer", competency_requirements=original,
    )
    created = employer.get(f"/api/v1/organizations/{organization_id}/jobs/{job_id}")
    assert created.status_code == 200
    assert created.json()["competency_requirements"][0] == {
        "competency_code": "fastapi.api_design", "competency_name": "FastAPI API design",
        "target_level": "mid", "accepted_verification_methods": ["human_review"], "max_age_days": 180,
    }
    public = employer.get(f"/api/v1/jobs/{job_id}")
    assert public.status_code == 200 and public.json()["competency_requirements"] == created.json()["competency_requirements"]

    invalid_code = employer.post(
        f"/api/v1/organizations/{organization_id}/jobs",
        json={"request_key": str(uuid4()), "title": "Invalid Requirement Job", "description": "A valid length description for this job.",
              "competency_requirements": [{**original[0], "competency_code": "java.bytecode"}]},
        headers={"X-CSRF-Token": csrf},
    )
    self_reported = employer.post(
        f"/api/v1/organizations/{organization_id}/jobs",
        json={"request_key": str(uuid4()), "title": "Invalid Evidence Policy", "description": "A valid length description for this job.",
              "competency_requirements": [{**original[0], "accepted_verification_methods": ["self_reported"]}]},
        headers={"X-CSRF-Token": csrf},
    )
    duplicate = employer.post(
        f"/api/v1/organizations/{organization_id}/jobs",
        json={"request_key": str(uuid4()), "title": "Duplicate Requirement", "description": "A valid length description for this job.",
              "competency_requirements": [original[0], original[0]]},
        headers={"X-CSRF-Token": csrf},
    )
    assert invalid_code.status_code == self_reported.status_code == duplicate.status_code == 422

    with TestClient(employer.app, base_url="https://proofpath.test") as candidate:
        register_and_login(candidate, mailer, "requirements-candidate@example.com")
        complete_and_publish_candidate_portfolio(candidate, handle="requirements-candidate")
        applied = candidate.post(
            f"/api/v1/jobs/{job_id}/applications",
            json={"request_key": str(uuid4()), "portfolio_consent_version": "job-application-public-portfolio-v1", "cover_note": "I build APIs with clear, validated contracts."},
            headers={"X-CSRF-Token": candidate.cookies[CSRF_COOKIE]},
        )
        assert applied.status_code == 201, applied.text

    changed = [{
        "competency_code": "fastapi.api_design", "target_level": "senior",
        "accepted_verification_methods": ["deterministic_test", "human_review"], "max_age_days": 90,
    }]
    updated = employer.patch(
        f"/api/v1/organizations/{organization_id}/jobs/{job_id}",
        json={"expected_version": 1, "competency_requirements": changed},
        headers={"X-CSRF-Token": csrf},
    )
    frozen = employer.get(f"/api/v1/organizations/{organization_id}/applications/{applied.json()['id']}")
    assert updated.status_code == 200 and updated.json()["version"] == 2
    assert updated.json()["competency_requirements"][0]["target_level"] == "senior"
    assert frozen.status_code == 200
    assert frozen.json()["competency_requirements"][0]["target_level"] == "mid"
    assert frozen.json()["competency_requirements"][0]["accepted_verification_methods"] == ["human_review"]


def test_projects_are_private_idempotent_versioned_and_owner_scoped(auth_client):
    client, mailer = auth_client
    assert client.get("/api/v1/me/projects").status_code == 401
    assert client.post("/api/v1/me/projects", json={"name": "Guest Project", "request_key": str(uuid4())}).status_code == 401
    guest_project_id = uuid4()
    assert client.get(f"/api/v1/me/projects/{guest_project_id}").status_code == 401
    assert client.post(
        f"/api/v1/me/projects/{guest_project_id}/source-links",
        json={"url": "https://github.com/example/repo"},
    ).status_code == 401
    assert client.delete(
        f"/api/v1/me/projects/{guest_project_id}/source-links/{uuid4()}"
    ).status_code == 401
    assert client.patch(
        f"/api/v1/me/projects/{guest_project_id}", json={"expected_version": 0, "name": "Guest Update"}
    ).status_code == 401
    assert client.delete(f"/api/v1/me/projects/{guest_project_id}").status_code == 401
    register_and_login(client, mailer, "project-owner@example.com")
    csrf = client.cookies[CSRF_COOKIE]

    no_csrf = client.post(
        "/api/v1/me/projects", json={"name": "Project One", "request_key": str(uuid4())}
    )
    invalid = client.post(
        "/api/v1/me/projects",
        json={"name": " ", "technologies": ["Python", " python "], "request_key": str(uuid4())},
        headers={"X-CSRF-Token": csrf},
    )
    assert no_csrf.status_code == 403
    assert invalid.status_code == 422
    assert client.get("/api/v1/me/projects").json() == {"projects": []}

    request_key = str(uuid4())
    body = {
        "request_key": request_key,
        "name": "  Consensus   Cache ",
        "problem_statement": "Replicas diverged after network partitions.",
        "contribution": "Designed deterministic leader election.",
        "technologies": ["Go", "Raft"],
    }
    created = client.post("/api/v1/me/projects", json=body, headers={"X-CSRF-Token": csrf})
    replay = client.post("/api/v1/me/projects", json=body, headers={"X-CSRF-Token": csrf})
    changed_key_payload = client.post(
        "/api/v1/me/projects", json=body | {"name": "Different Project"}, headers={"X-CSRF-Token": csrf}
    )
    assert created.status_code == replay.status_code == 201
    assert created.json() == replay.json()
    assert created.json()["name"] == "Consensus Cache"
    assert created.json()["status"] == "draft" and created.json()["visibility"] == "private"
    assert created.headers["location"] == f"/api/v1/me/projects/{created.json()['id']}"
    assert changed_key_payload.status_code == 409

    project_id = created.json()["id"]
    assert client.get(f"/api/v1/me/projects/{project_id}").json() == created.json()
    no_csrf_source = client.post(
        f"/api/v1/me/projects/{project_id}/source-links", json={"url": "https://github.com/example/repo"}
    )
    invalid_source = client.post(
        f"/api/v1/me/projects/{project_id}/source-links",
        json={"url": "https://github.com.evil.example/example/repo"},
        headers={"X-CSRF-Token": csrf},
    )
    assert no_csrf_source.status_code == 403
    assert invalid_source.status_code == 422
    source_body = {"url": "https://github.com/Proofpath/consensus-cache/"}
    linked = client.post(
        f"/api/v1/me/projects/{project_id}/source-links", json=source_body, headers={"X-CSRF-Token": csrf}
    )
    linked_replay = client.post(
        f"/api/v1/me/projects/{project_id}/source-links", json=source_body, headers={"X-CSRF-Token": csrf}
    )
    assert linked.status_code == 201 and linked_replay.status_code == 200
    assert linked.json() == linked_replay.json()
    assert linked.json()["url"] == "https://github.com/Proofpath/consensus-cache"
    assert client.get(f"/api/v1/me/projects/{project_id}").json()["source_links"] == [linked.json()]
    no_csrf_unlink = client.delete(
        f"/api/v1/me/projects/{project_id}/source-links/{linked.json()['id']}"
    )
    assert no_csrf_unlink.status_code == 403
    no_csrf_update = client.patch(
        f"/api/v1/me/projects/{project_id}", json={"expected_version": 0, "contribution": "Changed"}
    )
    updated = client.patch(
        f"/api/v1/me/projects/{project_id}",
        json={"expected_version": 0, "contribution": "Added failover tests.", "technologies": ["Go", "Raft", "PostgreSQL"]},
        headers={"X-CSRF-Token": csrf},
    )
    stale = client.patch(
        f"/api/v1/me/projects/{project_id}",
        json={"expected_version": 0, "name": "Stale update"},
        headers={"X-CSRF-Token": csrf},
    )
    null_patch = client.patch(
        f"/api/v1/me/projects/{project_id}",
        json={"expected_version": 1, "contribution": None},
        headers={"X-CSRF-Token": csrf},
    )
    assert no_csrf_update.status_code == 403
    assert updated.status_code == 200 and updated.json()["version"] == 1
    assert updated.json()["technologies"] == ["Go", "Raft", "PostgreSQL"]
    assert stale.status_code == 409
    assert null_patch.status_code == 422

    with TestClient(client.app, base_url="https://proofpath.test") as other_client:
        register_and_login(other_client, mailer, "other-project-owner@example.com")
        other_csrf = other_client.cookies[CSRF_COOKIE]
        assert other_client.get(f"/api/v1/me/projects/{project_id}").status_code == 404
        assert other_client.post(
            f"/api/v1/me/projects/{project_id}/source-links",
            json={"url": "https://github.com/other/repo"},
            headers={"X-CSRF-Token": other_csrf},
        ).status_code == 404
        assert other_client.delete(
            f"/api/v1/me/projects/{project_id}/source-links/{linked.json()['id']}",
            headers={"X-CSRF-Token": other_csrf},
        ).status_code == 404
        assert other_client.patch(
            f"/api/v1/me/projects/{project_id}",
            json={"expected_version": 1, "name": "Should stay private"},
            headers={"X-CSRF-Token": other_csrf},
        ).status_code == 404
        assert other_client.delete(
            f"/api/v1/me/projects/{project_id}", headers={"X-CSRF-Token": other_csrf}
        ).status_code == 404
        assert other_client.get("/api/v1/me/projects").json() == {"projects": []}

    unlinked = client.delete(
        f"/api/v1/me/projects/{project_id}/source-links/{linked.json()['id']}",
        headers={"X-CSRF-Token": csrf},
    )
    assert unlinked.status_code == 204
    assert client.delete(
        f"/api/v1/me/projects/{project_id}/source-links/{linked.json()['id']}",
        headers={"X-CSRF-Token": csrf},
    ).status_code == 404
    assert client.get(f"/api/v1/me/projects/{project_id}").json()["source_links"] == []
    no_csrf_delete = client.delete(f"/api/v1/me/projects/{project_id}")
    deleted = client.delete(f"/api/v1/me/projects/{project_id}", headers={"X-CSRF-Token": csrf})
    assert no_csrf_delete.status_code == 403
    assert deleted.status_code == 204
    assert client.delete(f"/api/v1/me/projects/{project_id}", headers={"X-CSRF-Token": csrf}).status_code == 404
    assert client.get("/api/v1/me/projects").json() == {"projects": []}
    with connect_database(TEST_DATABASE_URL) as connection:
        actions = connection.execute(
            "SELECT action FROM audit_events WHERE resource_id = %s ORDER BY action", (project_id,)
        ).fetchall()
    assert [row["action"] for row in actions] == [
        "candidate.project_created", "candidate.project_deleted", "candidate.project_updated"
    ]


def test_project_competency_claims_are_private_self_reported_and_versioned(auth_client):
    client, mailer = auth_client
    catalog = client.get("/api/v1/competencies?track=python-fastapi-backend")
    assert catalog.status_code == 200 and len(catalog.json()["competencies"]) == 8
    assert client.get("/api/v1/competencies?track=other").status_code == 422
    register_and_login(client, mailer, "competency-candidate@example.com")
    csrf = client.cookies[CSRF_COOKIE]
    saved = client.patch(
        "/api/v1/me/candidate/onboarding",
        json={"expected_version": 0, "full_name": "Competency Candidate", "target_roles": ["Backend Engineer"], "skills": ["Python"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert saved.status_code == 200
    assert client.post(
        "/api/v1/me/candidate/onboarding/complete", json={"expected_version": saved.json()["version"]},
        headers={"X-CSRF-Token": csrf},
    ).status_code == 200

    first_statement = "I designed transactional writes for the project data store."
    create_body = {
        "request_key": str(uuid4()), "name": "Transactional Service",
        "competency_claims": [{"competency_code": "postgres.transactions", "statement": first_statement}],
    }
    created = client.post("/api/v1/me/projects", json=create_body, headers={"X-CSRF-Token": csrf})
    replay = client.post("/api/v1/me/projects", json=create_body, headers={"X-CSRF-Token": csrf})
    assert created.status_code == replay.status_code == 201 and created.json() == replay.json()
    original = created.json()["competency_claims"][0]
    assert original["verification_method"] == "self_reported"
    assert original["verification_status"] == "unverified" and original["review_status"] == "unreviewed"
    assert original["sharing_scope"] == "candidate_only"
    assert "not independently verified" in original["evidence_limitations"]

    changed_statement = "I used PostgreSQL transactions to preserve consistent project updates."
    updated = client.patch(
        f"/api/v1/me/projects/{created.json()['id']}",
        json={"expected_version": 0, "competency_claims": [{"competency_code": "postgres.transactions", "statement": changed_statement}]},
        headers={"X-CSRF-Token": csrf},
    )
    assert updated.status_code == 200 and updated.json()["version"] == 1
    replacement = updated.json()["competency_claims"][0]
    assert replacement["id"] != original["id"] and replacement["statement"] == changed_statement
    invalid_code = client.patch(
        f"/api/v1/me/projects/{created.json()['id']}",
        json={"expected_version": 1, "competency_claims": [{"competency_code": "java.bytecode", "statement": changed_statement}]},
        headers={"X-CSRF-Token": csrf},
    )
    duplicate_code = client.patch(
        f"/api/v1/me/projects/{created.json()['id']}",
        json={"expected_version": 1, "competency_claims": [
            {"competency_code": "postgres.transactions", "statement": changed_statement},
            {"competency_code": "postgres.transactions", "statement": changed_statement},
        ]},
        headers={"X-CSRF-Token": csrf},
    )
    assert invalid_code.status_code == duplicate_code.status_code == 422
    with TestClient(client.app, base_url="https://proofpath.test") as other_client:
        register_and_login(other_client, mailer, "other-competency-candidate@example.com")
        assert other_client.get(f"/api/v1/me/projects/{created.json()['id']}").status_code == 404
        assert other_client.get("/api/v1/me/projects").json() == {"projects": []}

    handle = "competency-evidence-test"
    assert client.post("/api/v1/me/candidate-profile/handle", json={"handle": handle}, headers={"X-CSRF-Token": csrf}).status_code == 200
    assert client.post("/api/v1/me/portfolio/publish", json={"consent_version": "public-portfolio-v1"}, headers={"X-CSRF-Token": csrf}).status_code == 200
    published = client.post(
        f"/api/v1/me/projects/{created.json()['id']}/publish",
        json={"expected_version": 1, "consent_version": "public-project-v1"}, headers={"X-CSRF-Token": csrf},
    )
    assert published.status_code == 200
    public_portfolio = client.get(f"/api/v1/public/portfolios/{handle}")
    assert public_portfolio.status_code == 200
    assert changed_statement not in public_portfolio.text and "competency_claims" not in public_portfolio.text

    with TestClient(client.app, base_url="https://proofpath.test") as employer:
        register_and_login(employer, mailer, "competency-employer@example.com", purpose="employer")
        _, job_id = create_published_job(employer)
    application = client.post(
        f"/api/v1/jobs/{job_id}/applications",
        json={"request_key": str(uuid4()), "portfolio_consent_version": "job-application-public-portfolio-v1"},
        headers={"X-CSRF-Token": csrf},
    )
    assert application.status_code == 201, application.text
    assert changed_statement not in str(application.json()["portfolio_snapshot"])
    assert "competency_claims" not in application.json()["portfolio_snapshot"]["projects"][0]

    with connect_database(TEST_DATABASE_URL) as connection:
        history = connection.execute(
            """SELECT verification_method, review_status, sharing_scope, evidence_limitations, withdrawn_at
               FROM candidate_competency_evidence WHERE project_id = %s ORDER BY recorded_at""",
            (created.json()["id"],),
        ).fetchall()
        assert len(history) == 2 and history[0]["withdrawn_at"] is not None and history[1]["withdrawn_at"] is None
        assert all(item["verification_method"] == "self_reported" for item in history)
        assert all(item["review_status"] == "unreviewed" and item["sharing_scope"] == "candidate_only" for item in history)
        assert all("not independently verified" in item["evidence_limitations"] for item in history)
        audit_text = connection.execute(
            """SELECT COALESCE(string_agg(metadata::text, ' '), '') AS text
               FROM audit_events WHERE action LIKE 'candidate.project_competency_claims_%'"""
        ).fetchone()["text"]
        assert first_statement not in audit_text and changed_statement not in audit_text
    openapi = client.get("/openapi.json").json()
    assert openapi["paths"]["/api/v1/competencies"]["get"]["security"] == []
    assert openapi["paths"]["/api/v1/me/projects"]["post"]["security"] == [
        {"ProofpathSession": [], "ProofpathCSRF": []}
    ]


def test_private_portfolio_order_is_atomic_versioned_and_owner_scoped(auth_client):
    client, mailer = auth_client
    assert client.get("/api/v1/me/portfolio").status_code == 401
    assert client.patch(
        "/api/v1/me/portfolio", json={"expected_version": 0, "project_ids": []}
    ).status_code == 401
    register_and_login(client, mailer, "portfolio-owner@example.com")
    csrf = client.cookies[CSRF_COOKIE]
    assert client.get("/api/v1/me/portfolio").json() == {
        "visibility": "private", "version": 0, "projects": [], "updated_at": None,
        "public_handle": None, "public_handle_locked": False, "published_at": None,
    }
    project_ids = []
    for name in ("First Portfolio Project", "Second Portfolio Project"):
        created = client.post(
            "/api/v1/me/projects",
            json={"request_key": str(uuid4()), "name": name},
            headers={"X-CSRF-Token": csrf},
        )
        assert created.status_code == 201
        project_ids.append(created.json()["id"])

    initial = client.get("/api/v1/me/portfolio").json()
    assert initial["version"] == 2
    assert [project["id"] for project in initial["projects"]] == project_ids
    no_csrf = client.patch(
        "/api/v1/me/portfolio", json={"expected_version": 2, "project_ids": project_ids[::-1]}
    )
    duplicate = client.patch(
        "/api/v1/me/portfolio", json={"expected_version": 2, "project_ids": [project_ids[0], project_ids[0]]},
        headers={"X-CSRF-Token": csrf},
    )
    partial = client.patch(
        "/api/v1/me/portfolio", json={"expected_version": 2, "project_ids": project_ids[:1]},
        headers={"X-CSRF-Token": csrf},
    )
    assert no_csrf.status_code == 403
    assert duplicate.status_code == partial.status_code == 422

    reordered = client.patch(
        "/api/v1/me/portfolio", json={"expected_version": 2, "project_ids": project_ids[::-1]},
        headers={"X-CSRF-Token": csrf},
    )
    stale = client.patch(
        "/api/v1/me/portfolio", json={"expected_version": 2, "project_ids": project_ids},
        headers={"X-CSRF-Token": csrf},
    )
    assert reordered.status_code == 200 and reordered.json()["version"] == 3
    assert [project["id"] for project in reordered.json()["projects"]] == project_ids[::-1]
    assert stale.status_code == 409

    with TestClient(client.app, base_url="https://proofpath.test") as other_client:
        register_and_login(other_client, mailer, "portfolio-viewer@example.com")
        other_csrf = other_client.cookies[CSRF_COOKIE]
        other_portfolio = other_client.get("/api/v1/me/portfolio")
        assert other_portfolio.status_code == 200 and other_portfolio.json()["projects"] == []
        foreign_order = other_client.patch(
            "/api/v1/me/portfolio", json={"expected_version": 0, "project_ids": project_ids},
            headers={"X-CSRF-Token": other_csrf},
        )
        assert foreign_order.status_code == 422


def test_public_portfolio_requires_consent_and_only_exposes_selected_projects(auth_client):
    client, mailer = auth_client
    handle = "portfolio-qa-user"
    assert client.get(f"/api/v1/public/portfolios/{handle}").status_code == 404
    assert client.get("/portfolio/portfolio-qa-user").status_code == 200
    assert client.get("/u/portfolio-qa-user").status_code == 200
    assert client.post("/api/v1/me/candidate-profile/handle", json={"handle": handle}).status_code == 401
    assert client.post(
        "/api/v1/me/portfolio/publish", json={"consent_version": "public-portfolio-v1"}
    ).status_code == 401
    assert client.post("/api/v1/me/portfolio/unpublish").status_code == 401
    assert client.post(
        f"/api/v1/me/projects/{uuid4()}/publish",
        json={"expected_version": 0, "consent_version": "public-project-v1"},
    ).status_code == 401
    assert client.post(f"/api/v1/me/projects/{uuid4()}/unpublish").status_code == 401
    register_and_login(client, mailer, "portfolio-publisher@example.com")
    csrf = client.cookies[CSRF_COOKIE]
    no_csrf = client.post("/api/v1/me/candidate-profile/handle", json={"handle": handle})
    assert no_csrf.status_code == 403
    assert client.post(
        "/api/v1/me/portfolio/publish", json={"consent_version": "public-portfolio-v1"},
        headers={"X-CSRF-Token": csrf},
    ).status_code == 409

    saved = client.patch(
        "/api/v1/me/candidate/onboarding",
        json={"expected_version": 0, "target_roles": ["Platform Engineer"], "skills": ["Python", "PostgreSQL"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert saved.status_code == 200
    assert client.post(
        "/api/v1/me/candidate/onboarding/complete", json={"expected_version": saved.json()["version"]},
        headers={"X-CSRF-Token": csrf},
    ).status_code == 200
    reserved = client.post(
        "/api/v1/me/candidate-profile/handle", json={"handle": "admin"}, headers={"X-CSRF-Token": csrf}
    )
    malformed = client.post(
        "/api/v1/me/candidate-profile/handle", json={"handle": "bad--handle!"}, headers={"X-CSRF-Token": csrf}
    )
    assert reserved.status_code == malformed.status_code == 422
    handle_response = client.post(
        "/api/v1/me/candidate-profile/handle", json={"handle": handle}, headers={"X-CSRF-Token": csrf}
    )
    assert handle_response.status_code == 200
    assert handle_response.json() == {"handle": handle, "published": False}
    assert client.post(
        "/api/v1/me/candidate-profile/handle", json={"handle": "portfolio-qa-alt"}, headers={"X-CSRF-Token": csrf}
    ).status_code == 200
    assert client.post(
        "/api/v1/me/candidate-profile/handle", json={"handle": handle}, headers={"X-CSRF-Token": csrf}
    ).status_code == 200

    project = client.post(
        "/api/v1/me/projects", json={
            "request_key": str(uuid4()), "name": "Public project", "problem_statement": "Public problem",
            "contribution": "Public contribution", "technologies": ["Python"],
        }, headers={"X-CSRF-Token": csrf},
    )
    private_project = client.post(
        "/api/v1/me/projects", json={"request_key": str(uuid4()), "name": "Private project"},
        headers={"X-CSRF-Token": csrf},
    )
    assert project.status_code == private_project.status_code == 201
    project_id = project.json()["id"]
    source = client.post(
        f"/api/v1/me/projects/{project_id}/source-links", json={"url": "https://github.com/example/public-project"},
        headers={"X-CSRF-Token": csrf},
    )
    assert source.status_code == 201
    bad_consent = client.post(
        f"/api/v1/me/projects/{project_id}/publish",
        json={"expected_version": 0, "consent_version": "wrong"}, headers={"X-CSRF-Token": csrf},
    )
    assert bad_consent.status_code == 422
    published_project = client.post(
        f"/api/v1/me/projects/{project_id}/publish",
        json={"expected_version": 0, "consent_version": "public-project-v1"}, headers={"X-CSRF-Token": csrf},
    )
    assert published_project.status_code == 200 and published_project.json()["visibility"] == "public"
    repeated_project_publish = client.post(
        f"/api/v1/me/projects/{project_id}/publish",
        json={"expected_version": 0, "consent_version": "public-project-v1"}, headers={"X-CSRF-Token": csrf},
    )
    assert repeated_project_publish.status_code == 200
    assert client.get(f"/api/v1/public/portfolios/{handle}").status_code == 404
    edit_public_project = client.patch(
        f"/api/v1/me/projects/{project_id}",
        json={"expected_version": 0, "contribution": "Changed after publication"}, headers={"X-CSRF-Token": csrf},
    )
    add_public_link = client.post(
        f"/api/v1/me/projects/{project_id}/source-links", json={"url": "https://github.com/example/another"},
        headers={"X-CSRF-Token": csrf},
    )
    remove_public_link = client.delete(
        f"/api/v1/me/projects/{project_id}/source-links/{source.json()['id']}", headers={"X-CSRF-Token": csrf}
    )
    assert edit_public_project.status_code == add_public_link.status_code == remove_public_link.status_code == 409
    assert client.post("/api/v1/me/portfolio/unpublish", headers={"X-CSRF-Token": csrf}).status_code == 204
    staged = client.get(f"/api/v1/me/projects/{project_id}")
    assert staged.status_code == 200 and staged.json()["visibility"] == "private"
    assert client.post(
        f"/api/v1/me/projects/{project_id}/publish",
        json={"expected_version": 0, "consent_version": "public-project-v1"}, headers={"X-CSRF-Token": csrf},
    ).status_code == 200

    no_csrf_publish = client.post("/api/v1/me/portfolio/publish", json={"consent_version": "public-portfolio-v1"})
    invalid_consent = client.post(
        "/api/v1/me/portfolio/publish", json={"consent_version": "future-unspecified"}, headers={"X-CSRF-Token": csrf}
    )
    assert no_csrf_publish.status_code == 403 and invalid_consent.status_code == 422
    published_profile = client.post(
        "/api/v1/me/portfolio/publish", json={"consent_version": "public-portfolio-v1"}, headers={"X-CSRF-Token": csrf}
    )
    assert published_profile.status_code == 200
    assert published_profile.json()["public_handle_locked"] is True
    handle_change = client.post(
        "/api/v1/me/candidate-profile/handle", json={"handle": "portfolio-qa-renamed"}, headers={"X-CSRF-Token": csrf}
    )
    assert handle_change.status_code == 409
    assert handle_change.json()["code"] == "PROFILE_HANDLE_LOCKED"
    assert client.post(
        "/api/v1/me/portfolio/publish", json={"consent_version": "public-portfolio-v1"}, headers={"X-CSRF-Token": csrf}
    ).status_code == 200
    public = client.get(f"/api/v1/public/portfolios/{handle}")
    assert public.status_code == 200
    assert public.headers["cache-control"] == "no-store"
    assert public.json() == {
        "display_name": "Alex Johnson", "target_roles": ["Platform Engineer"], "skills": ["Python", "PostgreSQL"],
        "projects": [{
            "name": "Public project", "problem_statement": "Public problem", "contribution": "Public contribution",
            "technologies": ["Python"], "source_links": ["https://github.com/example/public-project"],
        }],
        "design": None,
    }
    assert "portfolio-publisher@example.com" not in public.text
    assert "user_id" not in public.text and private_project.json()["id"] not in public.text
    assert client.post(
        f"/api/v1/me/projects/{project_id}/unpublish", headers={"X-CSRF-Token": csrf}
    ).status_code == 204
    assert client.get(f"/api/v1/public/portfolios/{handle}").json()["projects"] == []
    assert client.get(f"/api/v1/me/projects/{project_id}").json()["visibility"] == "private"
    assert client.post(
        f"/api/v1/me/projects/{project_id}/publish",
        json={"expected_version": 0, "consent_version": "public-project-v1"}, headers={"X-CSRF-Token": csrf},
    ).status_code == 200

    with TestClient(client.app, base_url="https://proofpath.test") as other_client:
        register_and_login(other_client, mailer, "portfolio-viewer@example.com", purpose="employer")
        other_csrf = other_client.cookies[CSRF_COOKIE]
        other_client.patch(
            "/api/v1/me/candidate/onboarding", json={"expected_version": 0, "target_roles": ["Tester"]},
            headers={"X-CSRF-Token": other_csrf},
        )
        assert other_client.post(
            "/api/v1/me/candidate-profile/handle", json={"handle": handle}, headers={"X-CSRF-Token": other_csrf}
        ).status_code == 409
        assert other_client.get(f"/api/v1/me/projects/{project_id}").status_code == 404
        assert other_client.post(
            f"/api/v1/me/projects/{project_id}/publish",
            json={"expected_version": 0, "consent_version": "public-project-v1"}, headers={"X-CSRF-Token": other_csrf},
        ).status_code == 404
        assert other_client.post(
            f"/api/v1/me/projects/{project_id}/unpublish", headers={"X-CSRF-Token": other_csrf}
        ).status_code == 404
        assert other_client.get(f"/api/v1/public/portfolios/{handle}").status_code == 200

    assert client.post("/api/v1/me/portfolio/unpublish", headers={"X-CSRF-Token": csrf}).status_code == 204
    assert client.post(
        "/api/v1/me/candidate-profile/handle", json={"handle": "portfolio-qa-renamed"}, headers={"X-CSRF-Token": csrf}
    ).status_code == 409
    assert client.post("/api/v1/me/portfolio/unpublish", headers={"X-CSRF-Token": csrf}).status_code == 204
    assert client.post(
        f"/api/v1/me/projects/{project_id}/unpublish", headers={"X-CSRF-Token": csrf}
    ).status_code == 204
    assert client.get(f"/api/v1/public/portfolios/{handle}").status_code == 404
    private = client.get("/api/v1/me/portfolio").json()
    assert private["published_at"] is None
    assert private["public_handle_locked"] is True
    assert all(item["visibility"] == "private" for item in private["projects"])
    with connect_database(TEST_DATABASE_URL) as connection:
        consent = connection.execute(
            "SELECT metadata FROM audit_events WHERE action = 'candidate.portfolio_published'"
        ).fetchall()
        project_publish_count = connection.execute(
            "SELECT count(*) AS count FROM audit_events WHERE action = 'candidate.project_published' AND resource_id = %s",
            (project_id,),
        ).fetchone()["count"]
    assert len(consent) == 1 and consent[0]["metadata"] == {"consent_version": "public-portfolio-v1"}
    assert project_publish_count == 3


def test_portfolio_builder_entitlements_versions_privacy_and_downgrade(auth_client):
    client, mailer = auth_client
    design = {"template_key": "engineer", "theme_key": "teal", "sections": ["projects", "skills"]}
    save = {"expected_draft_version": 0, "design": design}
    assert client.get("/api/v1/me/entitlements").status_code == 401
    assert client.get("/api/v1/me/portfolio/versions").status_code == 401
    with TestClient(client.app, base_url="https://proofpath.test") as anonymous_client:
        assert anonymous_client.post("/api/v1/me/portfolio/versions", json=save).status_code == 401

    register_and_login(client, mailer, "portfolio-builder@example.com")
    csrf = client.cookies[CSRF_COOKIE]
    assert client.get("/api/v1/me/entitlements").json() == {
        "candidate_plan": "free", "portfolio_customization": False,
    }
    assert client.get("/api/v1/me/portfolio/versions").json() == {
        "draft_version_id": None, "published_version_id": None, "versions": [],
    }
    assert client.post("/api/v1/me/portfolio/versions", json=save).status_code == 403
    assert client.post(
        "/api/v1/me/portfolio/versions", json=save, headers={"X-CSRF-Token": csrf},
    ).status_code == 403
    assert client.post(
        "/api/v1/me/portfolio/versions", json=save | {"is_pro": True},
        headers={"X-CSRF-Token": csrf},
    ).status_code == 422

    onboarding = client.patch(
        "/api/v1/me/candidate/onboarding",
        json={"expected_version": 0, "full_name": "Portfolio Builder", "target_roles": ["Backend Engineer"], "skills": ["Python"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert onboarding.status_code == 200
    assert client.post(
        "/api/v1/me/candidate/onboarding/complete", json={"expected_version": onboarding.json()["version"]},
        headers={"X-CSRF-Token": csrf},
    ).status_code == 200
    handle = client.post(
        "/api/v1/me/candidate-profile/handle", json={"handle": "builder-qa"}, headers={"X-CSRF-Token": csrf},
    )
    assert handle.status_code == 200
    private_project = client.post(
        "/api/v1/me/projects", json={"request_key": str(uuid4()), "name": "Private draft"},
        headers={"X-CSRF-Token": csrf},
    )
    assert private_project.status_code == 201
    with connect_database(TEST_DATABASE_URL) as connection:
        user_id = connection.execute("SELECT id FROM users WHERE email = %s", ("portfolio-builder@example.com",)).fetchone()["id"]
        connection.execute(
            "INSERT INTO candidate_entitlement_grants (id, user_id, feature_key, grant_source) VALUES (%s, %s, 'portfolio_customization', 'test')",
            (uuid4(), user_id),
        )
    assert client.get("/api/v1/me/entitlements").json() == {
        "candidate_plan": "pro", "portfolio_customization": True,
    }
    unpublished_design = client.post(
        "/api/v1/me/portfolio/versions", json=save, headers={"X-CSRF-Token": csrf},
    )
    assert unpublished_design.status_code == 201
    version_one = unpublished_design.json()
    assert version_one["version_number"] == 1 and version_one["is_draft"]
    assert client.post(
        "/api/v1/me/portfolio/publish",
        json={"consent_version": "public-portfolio-v1", "draft_version_id": version_one["id"]},
        headers={"X-CSRF-Token": csrf},
    ).status_code == 409
    assert client.get("/api/v1/public/portfolios/builder-qa").status_code == 404

    bad_template = client.post(
        "/api/v1/me/portfolio/versions",
        json={"expected_draft_version": 1, "design": design | {"template_key": "unlimited"}},
        headers={"X-CSRF-Token": csrf},
    )
    duplicate_section = client.post(
        "/api/v1/me/portfolio/versions",
        json={"expected_draft_version": 1, "design": design | {"sections": ["projects", "projects"]}},
        headers={"X-CSRF-Token": csrf},
    )
    stale_draft = client.post(
        "/api/v1/me/portfolio/versions", json=save, headers={"X-CSRF-Token": csrf},
    )
    assert bad_template.status_code == duplicate_section.status_code == 422
    assert stale_draft.status_code == 409

    free_publish = client.post(
        "/api/v1/me/portfolio/publish", json={"consent_version": "public-portfolio-v1"},
        headers={"X-CSRF-Token": csrf},
    )
    assert free_publish.status_code == 200
    assert client.get("/api/v1/public/portfolios/builder-qa").json()["design"] is None
    assert client.post(
        "/api/v1/me/portfolio/publish",
        json={"consent_version": "public-portfolio-v1", "draft_version_id": version_one["id"]},
        headers={"X-CSRF-Token": csrf},
    ).status_code == 200
    public = client.get("/api/v1/public/portfolios/builder-qa")
    assert public.status_code == 200
    assert public.json()["design"] == design
    assert "Private draft" not in public.text and "user_id" not in public.text

    version_two_response = client.post(
        "/api/v1/me/portfolio/versions",
        json={"expected_draft_version": 1, "design": design | {"template_key": "case-study", "theme_key": "coral"}},
        headers={"X-CSRF-Token": csrf},
    )
    assert version_two_response.status_code == 201
    version_two = version_two_response.json()
    assert version_two["version_number"] == 2
    assert client.post(
        "/api/v1/me/portfolio/publish",
        json={"consent_version": "public-portfolio-v1", "draft_version_id": version_two["id"]},
        headers={"X-CSRF-Token": csrf},
    ).status_code == 200
    assert client.get("/api/v1/public/portfolios/builder-qa").json()["design"]["template_key"] == "case-study"
    failed_publish = client.post(
        "/api/v1/me/portfolio/publish",
        json={"consent_version": "public-portfolio-v1", "draft_version_id": str(uuid4())},
        headers={"X-CSRF-Token": csrf},
    )
    assert failed_publish.status_code == 404
    assert client.get("/api/v1/public/portfolios/builder-qa").json()["design"]["template_key"] == "case-study"
    rollback = client.post(
        "/api/v1/me/portfolio/publish",
        json={"consent_version": "public-portfolio-v1", "draft_version_id": version_one["id"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert rollback.status_code == 200
    assert client.get("/api/v1/public/portfolios/builder-qa").json()["design"] == design
    history = client.get("/api/v1/me/portfolio/versions").json()
    assert len(history["versions"]) == 2
    assert next(version for version in history["versions"] if version["id"] == version_one["id"])["is_published"]

    with TestClient(client.app, base_url="https://proofpath.test") as other_client:
        register_and_login(other_client, mailer, "portfolio-builder-other@example.com", purpose="employer")
        other_csrf = other_client.cookies[CSRF_COOKIE]
        assert other_client.get("/api/v1/me/entitlements").json()["candidate_plan"] == "free"
        assert other_client.get("/api/v1/me/portfolio/versions").json()["versions"] == []
        complete_and_publish_candidate_portfolio(
            other_client, full_name="Other User", handle="other-builder-qa", project_name="Other Work",
        )
        with connect_database(TEST_DATABASE_URL) as connection:
            other_id = connection.execute("SELECT id FROM users WHERE email = %s", ("portfolio-builder-other@example.com",)).fetchone()["id"]
            connection.execute(
                "INSERT INTO candidate_entitlement_grants (id, user_id, feature_key, grant_source) VALUES (%s, %s, 'portfolio_customization', 'test')",
                (uuid4(), other_id),
            )
        foreign_version = other_client.post(
            "/api/v1/me/portfolio/publish",
            json={"consent_version": "public-portfolio-v1", "draft_version_id": version_one["id"]},
            headers={"X-CSRF-Token": other_csrf},
        )
        assert foreign_version.status_code == 404
        assert other_client.get("/api/v1/public/portfolios/other-builder-qa").json()["design"] is None

    with connect_database(TEST_DATABASE_URL) as connection:
        connection.execute(
            "UPDATE candidate_entitlement_grants SET revoked_at = now() WHERE user_id = %s", (user_id,),
        )
    assert client.get("/api/v1/me/entitlements").json()["candidate_plan"] == "free"
    assert client.post(
        "/api/v1/me/portfolio/versions", json={"expected_draft_version": 1, "design": design},
        headers={"X-CSRF-Token": csrf},
    ).status_code == 403
    downgraded = client.get("/api/v1/public/portfolios/builder-qa")
    assert downgraded.status_code == 200 and downgraded.json()["design"] is None
    assert len(client.get("/api/v1/me/portfolio/versions").json()["versions"]) == 2


def test_candidate_discovery_requires_consent_and_hiring_role(auth_client):
    client, mailer = auth_client
    enable = {"discoverable": True, "consent_version": "candidate-discovery-v1"}
    disable = {"discoverable": False, "consent_version": "candidate-discovery-v1"}
    assert client.get("/api/v1/me/candidate-profile/discovery").status_code == 401
    assert client.patch("/api/v1/me/candidate-profile/discovery", json=enable).status_code == 401
    assert client.get(f"/api/v1/organizations/{uuid4()}/candidates").status_code == 401

    register_and_login(client, mailer, "discovery-candidate@example.com")
    csrf = client.cookies[CSRF_COOKIE]
    assert client.get("/api/v1/me/entitlements").json()["candidate_plan"] == "free"
    assert client.get("/api/v1/me/candidate-profile/discovery").json() == {
        "discoverable": False, "profile_published": False, "consent_version": None,
    }
    assert client.patch(
        "/api/v1/me/candidate-profile/discovery", json=enable,
    ).status_code == 403
    assert client.patch(
        "/api/v1/me/candidate-profile/discovery",
        json=enable | {"consent_version": "future-consent"}, headers={"X-CSRF-Token": csrf},
    ).status_code == 422
    assert client.patch(
        "/api/v1/me/candidate-profile/discovery", json=enable, headers={"X-CSRF-Token": csrf},
    ).status_code == 409

    candidate_id = client.get("/api/v1/auth/session").json()["user"]["id"]
    complete_and_publish_candidate_portfolio(
        client, full_name="Opted In Student", handle="discovery-student", project_name="Public Portfolio Project",
    )
    enabled = client.patch(
        "/api/v1/me/candidate-profile/discovery", json=enable, headers={"X-CSRF-Token": csrf},
    )
    assert enabled.status_code == 200
    assert enabled.json() == {
        "discoverable": True, "profile_published": True, "consent_version": "candidate-discovery-v1",
    }
    assert client.patch(
        "/api/v1/me/candidate-profile/discovery", json=enable, headers={"X-CSRF-Token": csrf},
    ).status_code == 200
    with connect_database(TEST_DATABASE_URL) as connection:
        assert connection.execute(
            "SELECT count(*) AS count FROM audit_events WHERE resource_id = %s AND action = 'candidate.discovery_enabled'",
            (candidate_id,),
        ).fetchone()["count"] == 1

    with TestClient(client.app, base_url="https://proofpath.test") as second_candidate:
        register_and_login(second_candidate, mailer, "discovery-employer-purpose@example.com", purpose="employer")
        second_csrf = second_candidate.cookies[CSRF_COOKIE]
        complete_and_publish_candidate_portfolio(
            second_candidate, full_name="Employer With Candidate Workspace", handle="second-discovery-student",
            project_name="Second Public Project",
        )
        assert second_candidate.get("/api/v1/me/candidate-profile/discovery").json()["discoverable"] is False
        with TestClient(client.app, base_url="https://proofpath.test") as owner:
            register_and_login(owner, mailer, "discovery-org-owner@example.com", purpose="employer")
            owner_csrf = owner.cookies[CSRF_COOKIE]
            org = owner.post(
                "/api/v1/organizations", json={"request_key": str(uuid4()), "name": "Discovery Hiring Team"},
                headers={"X-CSRF-Token": owner_csrf},
            )
            assert org.status_code == 201
            organization_id = org.json()["id"]
            onboarding = owner.patch(
                f"/api/v1/organizations/{organization_id}/onboarding",
                json={"expected_version": 0, "name": "Discovery Hiring Team"},
                headers={"X-CSRF-Token": owner_csrf},
            )
            assert onboarding.status_code == 200
            completed = owner.post(
                f"/api/v1/organizations/{organization_id}/onboarding/complete",
                json={"expected_version": onboarding.json()["version"]}, headers={"X-CSRF-Token": owner_csrf},
            )
            assert completed.status_code == 200

            listed = owner.get(f"/api/v1/organizations/{organization_id}/candidates")
            assert listed.status_code == 200 and len(listed.json()["candidates"]) == 1
            result = listed.json()["candidates"][0]
            assert result == {
                "display_name": "Opted In Student", "target_roles": ["Backend Engineer"], "skills": ["Python"],
                "profile_path": "/u/discovery-student",
            }
            assert "email" not in listed.text and "user_id" not in listed.text
            assert "Private Draft Project" not in listed.text and "Public Portfolio Project" not in listed.text
            assert listed.headers["cache-control"] == "no-store"
            assert owner.get(f"/api/v1/organizations/{organization_id}/candidates?q=x").status_code == 422
            assert owner.get(f"/api/v1/organizations/{organization_id}/candidates?q=No%20Match").json()["candidates"] == []

            assert second_candidate.patch(
                "/api/v1/me/candidate-profile/discovery", json=enable, headers={"X-CSRF-Token": second_csrf},
            ).status_code == 200
            first_page = owner.get(f"/api/v1/organizations/{organization_id}/candidates?limit=1&offset=0")
            second_page = owner.get(f"/api/v1/organizations/{organization_id}/candidates?limit=1&offset=1")
            assert first_page.json()["has_more"] is True and second_page.json()["has_more"] is False
            assert {item["display_name"] for item in first_page.json()["candidates"] + second_page.json()["candidates"]} == {
                "Opted In Student", "Employer With Candidate Workspace",
            }

            with connect_database(TEST_DATABASE_URL) as connection:
                connection.execute(
                    "INSERT INTO organization_memberships (organization_id, user_id, role) VALUES (%s, %s, 'member')",
                    (organization_id, candidate_id),
                )
            member_search = owner.get(f"/api/v1/organizations/{organization_id}/candidates")
            assert [item["display_name"] for item in member_search.json()["candidates"]] == [
                "Employer With Candidate Workspace",
            ]
            assert client.get(f"/api/v1/organizations/{organization_id}/candidates").status_code == 403
            assert second_candidate.get(f"/api/v1/organizations/{organization_id}/candidates").status_code == 404

            assert second_candidate.post(
                "/api/v1/me/portfolio/unpublish", headers={"X-CSRF-Token": second_csrf},
            ).status_code == 204
            preference = second_candidate.get("/api/v1/me/candidate-profile/discovery")
            assert preference.json() == {
                "discoverable": False, "profile_published": False, "consent_version": None,
            }
            assert owner.get(f"/api/v1/organizations/{organization_id}/candidates").json()["candidates"] == []
            assert second_candidate.post(
                "/api/v1/me/portfolio/publish", json={"consent_version": "public-portfolio-v1"},
                headers={"X-CSRF-Token": second_csrf},
            ).status_code == 200
            assert owner.get(f"/api/v1/organizations/{organization_id}/candidates").json()["candidates"] == []

    disabled = client.patch(
        "/api/v1/me/candidate-profile/discovery", json=disable, headers={"X-CSRF-Token": csrf},
    )
    assert disabled.status_code == 200 and disabled.json()["discoverable"] is False


def test_rate_limiter_enforces_bounded_window_and_key_count():
    limiter = RateLimiter()
    assert [limiter.allow("client", 2, 60) for _ in range(3)] == [True, True, False]
    for index in range(10_000):
        limiter.allow(f"key-{index}", 1, 60)
    assert limiter.allow("another-key", 1, 60) is False


def test_postgres_rate_limiter_is_shared_and_atomic_across_instances(auth_client):
    with connect_database(TEST_DATABASE_URL) as connection:
        connection.execute("TRUNCATE rate_limit_buckets")
        connection.execute(
            """INSERT INTO rate_limit_buckets (key_hash, window_seconds, event_times, expires_at)
               VALUES (%s, 60, ARRAY[]::timestamptz[], now() - interval '1 second')""",
            (bytes(32),),
        )
    first = PostgresRateLimiter(TEST_DATABASE_URL, "test-rate-limit-secret")
    second = PostgresRateLimiter(TEST_DATABASE_URL, "test-rate-limit-secret")
    key = f"shared-window:{uuid4()}"
    assert first.allow(key, 2, 60)
    with connect_database(TEST_DATABASE_URL) as connection:
        assert connection.execute(
            "SELECT 1 FROM rate_limit_buckets WHERE key_hash = %s", (bytes(32),)
        ).fetchone() is None
    assert second.allow(key, 2, 60)
    assert not first.allow(key, 2, 60)

    concurrent_key = f"concurrent-window:{uuid4()}"
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda index: (first if index % 2 else second).allow(concurrent_key, 7, 60), range(24)))
    assert sum(results) == 7


def assessment_definition(**overrides):
    definition = {
        "title": "Python API Reliability Exercise",
        "role_label": "Backend Engineer",
        "assessment_type": "live_project_change",
        "duration_minutes": 60,
        "competencies": [
            {"name": "Problem solving", "target_level": "mid"},
            {"name": "Code quality", "target_level": "mid"},
        ],
        "task_instructions": "Review the supplied API behavior, propose a safe change, and explain how you would verify it.",
        "rubric_criteria": [
            {"competency_name": "Problem solving", "name": "Correctness", "description": "The proposed change meets its stated requirements.", "weight_percent": 60},
            {"competency_name": "Code quality", "name": "Communication", "description": "The candidate explains tradeoffs and verification clearly.", "weight_percent": 40},
        ],
        "allowed_tools": ["editor"],
    }
    return definition | overrides


def create_published_assessment(client, organization_id, *, title, definition=None):
    csrf = client.cookies[CSRF_COOKIE]
    base = f"/api/v1/organizations/{organization_id}/assessments"
    created = client.post(
        base,
        json={"request_key": str(uuid4()), "definition": definition or assessment_definition(title=title)},
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 201, created.text
    published = client.post(
        f"{base}/{created.json()['id']}/publish",
        json={"expected_version": 1, "review_confirmation": "assessment-review-v1"},
        headers={"X-CSRF-Token": csrf},
    )
    assert published.status_code == 200 and published.json()["status"] == "published", published.text
    return published.json()


def test_candidate_private_python_assessment_workspace_and_evidence(auth_client):
    client, mailer = auth_client
    guest_attempt = uuid4()
    assert client.get(f"/api/v1/attempts/{guest_attempt}/workspace").status_code == 401
    assert client.put(f"/api/v1/attempts/{guest_attempt}/workspace", json={"expected_version": 0, "source_code": "x"}).status_code == 401
    assert client.post(f"/api/v1/attempts/{guest_attempt}/test-runs", json={"expected_workspace_version": 1, "request_key": str(uuid4())}).status_code == 401
    assert client.get(f"/api/v1/attempts/{guest_attempt}/test-runs/{uuid4()}").status_code == 401
    assert client.get(f"/api/v1/me/applications/{uuid4()}/evidence-sharing").status_code == 401
    assert client.put(f"/api/v1/me/applications/{uuid4()}/evidence-sharing", json={}).status_code == 401
    assert client.get(f"/api/v1/organizations/{uuid4()}/applications/{uuid4()}/evidence-report").status_code == 401
    register_and_login(client, mailer, "runner-owner@example.com", purpose="employer")
    organization_id, job_id = create_published_job(
        client, organization_name="Runner Lab",
        competency_requirements=[{"competency_code": "python.language_fundamentals", "target_level": "mid",
                                  "accepted_verification_methods": ["deterministic_test"], "max_age_days": 365}],
    )
    definition = assessment_definition(
        competencies=[{"name": "Python", "target_level": "mid", "competency_code": "python.language_fundamentals"}],
        rubric_criteria=[{"competency_name": "Python", "name": "Correctness", "description": "Implements the required deterministic behavior.", "weight_percent": 100}],
        allowed_tools=["editor", "test_runner"],
        python_test_suite={
            "entrypoint": "normalize",
            "cases": [
                {"competency_code": "python.language_fundamentals", "arguments": {"value": " Python "}, "expected": "python"},
                {"competency_code": "python.language_fundamentals", "arguments": {"value": "FASTAPI"}, "expected": "fastapi"},
            ],
        },
    )
    assessment = create_published_assessment(client, organization_id, title="Normalize text", definition=definition)

    class FakeRunner:
        calls = []

        def available(self):
            return True

        def run_case(self, source, entrypoint, arguments):
            self.calls.append((entrypoint, arguments.copy()))
            output = arguments["value"].strip().lower() if "return value.strip().lower()" in source else None
            from backend.app.assessment_runner import Execution
            return Execution("completed", output)

    runner = FakeRunner()
    client.app.state.assessment_runner = runner
    owner_csrf = client.cookies[CSRF_COOKIE]
    with TestClient(client.app, base_url="https://proofpath.test") as candidate:
        register_and_login(candidate, mailer, "runner-candidate@example.com")
        complete_and_publish_candidate_portfolio(candidate, full_name="Runner Candidate", handle="runner-candidate")
        candidate_csrf = candidate.cookies[CSRF_COOKIE]
        application = candidate.post(
            f"/api/v1/jobs/{job_id}/applications",
            json={"request_key": str(uuid4()), "portfolio_consent_version": "job-application-public-portfolio-v1"},
            headers={"X-CSRF-Token": candidate_csrf},
        )
        assert application.status_code == 201, application.text
        app_id = application.json()["id"]
        employer_report_path = f"/api/v1/organizations/{organization_id}/applications/{app_id}/evidence-report"
        initial_report = client.get(employer_report_path)
        assert initial_report.status_code == 200, initial_report.text
        assert initial_report.json()["requirements"][0]["status"] == "unverified"
        recommendation = initial_report.json()["recommended_assessment"]
        assert recommendation["assessment_id"] == assessment["id"]
        assert recommendation["assessment_version"] == 1
        assert recommendation["competency_codes"] == ["python.language_fundamentals"]
        assert recommendation["focused_test_case_count"] == 2
        assert set(recommendation) == {
            "assessment_id", "assessment_title", "assessment_version", "competency_codes",
            "focused_test_case_count", "reason",
        }
        stage = client.patch(
            f"/api/v1/organizations/{organization_id}/applications/{app_id}/stage",
            json={"expected_version": 1, "stage": "in_review"}, headers={"X-CSRF-Token": owner_csrf},
        )
        assert stage.status_code == 200, stage.text
        invitation = client.post(
            f"/api/v1/organizations/{organization_id}/assessment-invitations",
            json={"request_key": str(uuid4()), "application_id": app_id, "expected_application_version": 2,
                  "assessment_id": assessment["id"], "assessment_version": 1},
            headers={"X-CSRF-Token": owner_csrf},
        )
        assert invitation.status_code == 201, invitation.text
        assert client.get(employer_report_path).json()["recommended_assessment"] is None
        invitation_id = invitation.json()["id"]
        briefing_path = f"/api/v1/me/assessment-invitations/{invitation_id}"
        briefing = candidate.get(briefing_path)
        assert briefing.status_code == 200
        assert "python_test_suite" not in briefing.json()["assessment"]
        assert briefing.json()["assessment"]["python_entrypoint"] == "normalize"
        accepted = candidate.post(
            f"{briefing_path}/respond",
            json={"expected_version": 1, "action": "accept", "consent_version": "assessment-invitation-v1"},
            headers={"X-CSRF-Token": candidate_csrf},
        )
        assert accepted.status_code == 200, accepted.text
        attempt = candidate.post(
            f"/api/v1/assessment-invitations/{invitation_id}/attempts",
            json={"request_key": str(uuid4()), "expected_invitation_version": accepted.json()["version"]},
            headers={"X-CSRF-Token": candidate_csrf},
        )
        assert attempt.status_code == 201, attempt.text
        attempt_id = attempt.json()["id"]
        workspace_path = f"/api/v1/attempts/{attempt_id}/workspace"
        assert candidate.get(workspace_path).status_code == 409
        consent = candidate.put(
            f"/api/v1/attempts/{attempt_id}/consents",
            json={"expected_version": 1, "action": "accept", "consent_version": "assessment-attempt-v1"},
            headers={"X-CSRF-Token": candidate_csrf},
        )
        assert consent.status_code == 200, consent.text
        workspace = candidate.get(workspace_path)
        assert workspace.status_code == 200 and workspace.headers["cache-control"] == "no-store"
        save_path = workspace_path
        source = "def normalize(value):\n    return value.strip().lower()\n"
        assert candidate.put(save_path, json={"expected_version": 0, "source_code": source}).status_code == 403
        saved = candidate.put(
            save_path, json={"expected_version": 0, "source_code": source}, headers={"X-CSRF-Token": candidate_csrf}
        )
        assert saved.status_code == 200 and saved.json()["version"] == 1 and saved.json()["status"] == "active"
        stale = candidate.put(
            save_path, json={"expected_version": 0, "source_code": source + "# stale\n"}, headers={"X-CSRF-Token": candidate_csrf}
        )
        assert stale.status_code == 409
        key = str(uuid4())
        run_path = f"/api/v1/attempts/{attempt_id}/test-runs"
        arbitrary_command = candidate.post(
            run_path, json={"expected_workspace_version": 1, "request_key": str(uuid4()), "shell_command": "echo unsafe"},
            headers={"X-CSRF-Token": candidate_csrf},
        )
        assert arbitrary_command.status_code == 422
        run = candidate.post(
            run_path, json={"expected_workspace_version": 1, "request_key": key},
            headers={"X-CSRF-Token": candidate_csrf},
        )
        assert run.status_code == 201, run.text
        assert run.headers["cache-control"] == "no-store"
        assert run.json()["status"] == "passed"
        assert run.json()["competency_results"] == [{
            "competency_code": "python.language_fundamentals", "status": "demonstrated", "passed_cases": 2, "total_cases": 2,
        }]
        assert "expected" not in run.text and "source_snapshot" not in run.text and source not in run.text
        replay = candidate.post(
            run_path, json={"expected_workspace_version": 1, "request_key": key},
            headers={"X-CSRF-Token": candidate_csrf},
        )
        assert replay.status_code == 200 and replay.json() == run.json() and len(runner.calls) == 2
        bad_solution = candidate.put(
            save_path, json={"expected_version": 1, "source_code": "def normalize(value):\n    return value\n"},
            headers={"X-CSRF-Token": candidate_csrf},
        )
        assert bad_solution.status_code == 200 and bad_solution.json()["version"] == 2
        failed_run = candidate.post(
            run_path, json={"expected_workspace_version": 2, "request_key": str(uuid4())},
            headers={"X-CSRF-Token": candidate_csrf},
        )
        assert failed_run.status_code == 201 and failed_run.json()["status"] == "failed"
        assert failed_run.json()["competency_results"][0]["status"] == "insufficient"
        assert len(runner.calls) == 4
        assert client.get(f"{run_path}/{run.json()['id']}").status_code == 404
        with TestClient(client.app, base_url="https://proofpath.test") as other_candidate:
            register_and_login(other_candidate, mailer, "runner-other@example.com")
            assert other_candidate.get(workspace_path).status_code == 404
            assert other_candidate.get(f"{run_path}/{run.json()['id']}").status_code == 404
            assert other_candidate.get(f"/api/v1/me/applications/{app_id}/evidence-sharing").status_code == 404
        with connect_database(TEST_DATABASE_URL) as connection:
            evidence_rows = connection.execute(
                """SELECT evidence.verification_method, evidence.review_status, evidence.sharing_scope,
                          evidence.source_test_run_id, run.source_sha256, evidence.id,
                          evidence.proficiency_level, run.status
                   FROM candidate_competency_evidence AS evidence
                   JOIN assessment_test_runs AS run ON run.id = evidence.source_test_run_id
                   WHERE evidence.assessment_attempt_id = %s ORDER BY run.created_at""",
                (attempt_id,),
            ).fetchall()
        assert len(evidence_rows) == 2
        passing_evidence = next(item for item in evidence_rows if str(item["source_test_run_id"]) == run.json()["id"])
        failing_evidence = next(item for item in evidence_rows if str(item["source_test_run_id"]) == failed_run.json()["id"])
        assert all(item["verification_method"] == "deterministic_test" for item in evidence_rows)
        assert all(item["review_status"] == "unreviewed" and item["sharing_scope"] == "candidate_only" for item in evidence_rows)
        assert passing_evidence["proficiency_level"] == "mid" and failing_evidence["proficiency_level"] == "mid"
        assert len(bytes(passing_evidence["source_sha256"])) == 32

        claim = candidate.post(
            "/api/v1/me/projects",
            json={"request_key": str(uuid4()), "name": "Python practice", "competency_claims":[
                {"competency_code": "python.language_fundamentals", "statement": "I used Python functions and collections."}
            ]},
            headers={"X-CSRF-Token": candidate_csrf},
        )
        assert claim.status_code == 201, claim.text
        claim_evidence_id = claim.json()["competency_claims"][0]["id"]

        share_path = f"/api/v1/me/applications/{app_id}/evidence-sharing"
        private = candidate.get(share_path)
        assert private.status_code == 200 and private.headers["cache-control"] == "no-store"
        assert private.json()["share_status"] == "not_shared"
        assert private.json()["private_preview"][0]["status"] == "demonstrated"
        before_share = client.get(employer_report_path)
        assert before_share.status_code == 200 and before_share.json()["share_status"] == "not_shared"
        assert before_share.json()["requirements"][0]["status"] == "unverified"
        consent_body = {"expected_version": 0, "action": "share", "consent_version": "candidate-application-evidence-v1",
                        "evidence_ids": [str(failing_evidence["id"])]}
        invalid_consent = candidate.put(
            share_path, json={"expected_version": 0, "action": "share", "evidence_ids": [str(passing_evidence["id"])]},
            headers={"X-CSRF-Token": candidate_csrf},
        )
        assert invalid_consent.status_code == 422
        assert candidate.put(share_path, json=consent_body).status_code == 403
        insufficient = candidate.put(
            share_path, json=consent_body, headers={"X-CSRF-Token": candidate_csrf},
        )
        assert insufficient.status_code == 200 and insufficient.json()["share_status"] == "shared"
        assert client.get(employer_report_path).json()["requirements"][0]["status"] == "insufficient"
        claim_share = candidate.put(
            share_path, json=consent_body | {"expected_version": 1, "evidence_ids": [str(claim_evidence_id)]},
            headers={"X-CSRF-Token": candidate_csrf},
        )
        assert claim_share.status_code == 200 and claim_share.json()["share_version"] == 2
        claim_report = client.get(employer_report_path).json()["requirements"][0]
        assert claim_report["status"] == "unverified"
        assert claim_report["evidence"][0]["verification_method"] == "self_reported"
        assert "not independently verified" in claim_report["evidence"][0]["evidence_limitations"]
        stale = candidate.put(
            share_path, json=consent_body | {"expected_version": 1, "evidence_ids": [str(passing_evidence["id"])]},
            headers={"X-CSRF-Token": candidate_csrf},
        )
        assert stale.status_code == 409
        share_passing = candidate.put(
            share_path, json=consent_body | {"expected_version": 2, "evidence_ids": [str(passing_evidence["id"])]},
            headers={"X-CSRF-Token": candidate_csrf},
        )
        assert share_passing.status_code == 200 and share_passing.json()["share_version"] == 3
        assert client.get(employer_report_path).json()["requirements"][0]["status"] == "demonstrated"
        revoke_body = {"expected_version": 3, "action": "revoke"}
        revoked = candidate.put(share_path, json=revoke_body, headers={"X-CSRF-Token": candidate_csrf})
        assert revoked.status_code == 200 and revoked.json()["share_status"] == "revoked"
        assert candidate.put(share_path, json=revoke_body, headers={"X-CSRF-Token": candidate_csrf}).json() == revoked.json()
        after_revoke = client.get(employer_report_path)
        assert after_revoke.status_code == 200 and after_revoke.json()["share_status"] == "revoked"
        assert after_revoke.json()["requirements"][0]["status"] == "unverified"
        assert after_revoke.json()["requirements"][0]["evidence"] == []


def test_assessment_authoring_is_tenant_scoped_versioned_and_immutable(auth_client):
    client, mailer = auth_client
    guest_body = {"request_key": str(uuid4()), "definition": assessment_definition()}
    guest_org = uuid4()
    assert client.get(f"/api/v1/organizations/{guest_org}/assessments").status_code == 401
    assert client.post(f"/api/v1/organizations/{guest_org}/assessments", json=guest_body).status_code == 401
    assert client.patch(
        f"/api/v1/organizations/{guest_org}/assessments/{uuid4()}",
        json={"expected_version": 1, "definition": assessment_definition()},
    ).status_code == 401
    assert client.post(
        f"/api/v1/organizations/{guest_org}/assessments/{uuid4()}/publish",
        json={"expected_version": 1, "review_confirmation": "assessment-review-v1"},
    ).status_code == 401

    register_and_login(client, mailer, "assessment-owner@example.com", purpose="employer")
    organization_id, _ = create_published_job(client, organization_name="Assessment Lab")
    csrf = client.cookies[CSRF_COOKIE]
    base = f"/api/v1/organizations/{organization_id}/assessments"
    request_key = str(uuid4())
    create_body = {"request_key": request_key, "definition": assessment_definition()}

    assert client.post(base, json=create_body).status_code == 403
    invalid = assessment_definition(rubric_criteria=[
        {"competency_name": "Problem solving", "name": "Correctness", "description": "The proposed change meets its stated requirements.", "weight_percent": 50},
        {"competency_name": "Code quality", "name": "Communication", "description": "The candidate explains tradeoffs and verification clearly.", "weight_percent": 40},
    ])
    assert client.post(base, json={"request_key": str(uuid4()), "definition": invalid}, headers={"X-CSRF-Token": csrf}).status_code == 422
    invalid_mapping = assessment_definition(rubric_criteria=[
        {"competency_name": "Unknown competency", "name": "Correctness", "description": "The proposed change meets its stated requirements.", "weight_percent": 60},
        {"competency_name": "Code quality", "name": "Communication", "description": "The candidate explains tradeoffs and verification clearly.", "weight_percent": 40},
    ])
    assert client.post(base, json={"request_key": str(uuid4()), "definition": invalid_mapping}, headers={"X-CSRF-Token": csrf}).status_code == 422
    short_after_normalization = assessment_definition(task_instructions=f"x{' ' * 50}")
    assert client.post(
        base, json={"request_key": str(uuid4()), "definition": short_after_normalization},
        headers={"X-CSRF-Token": csrf},
    ).status_code == 422
    created = client.post(base, json=create_body, headers={"X-CSRF-Token": csrf})
    replay = client.post(base, json=create_body, headers={"X-CSRF-Token": csrf})
    changed_replay = client.post(
        base, json={"request_key": request_key, "definition": assessment_definition(title="Changed assessment title")},
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 201 and replay.status_code == 200
    assert created.json() == replay.json()
    assert changed_replay.status_code == 409
    assert created.headers["location"] == f"{base}/{created.json()['id']}"
    assessment_id = created.json()["id"]
    detail_path = f"{base}/{assessment_id}"
    detail = client.get(detail_path)
    assert detail.status_code == 200 and detail.headers["cache-control"] == "no-store"
    assert detail.json()["status"] == "draft" and detail.json()["version"] == 1
    assert client.get(f"{base}?limit=0").status_code == 422
    assert client.get(base).json()["assessments"][0]["id"] == assessment_id

    updated_definition = assessment_definition(title="Updated Python API Exercise")
    updated = client.patch(
        detail_path, json={"expected_version": 1, "definition": updated_definition}, headers={"X-CSRF-Token": csrf}
    )
    conflict = client.patch(
        detail_path, json={"expected_version": 1, "definition": assessment_definition(title="Stale edit")},
        headers={"X-CSRF-Token": csrf},
    )
    assert updated.status_code == 200 and updated.json()["version"] == 2
    assert conflict.status_code == 409

    missing_review = client.post(
        f"{detail_path}/publish", json={"expected_version": 2}, headers={"X-CSRF-Token": csrf}
    )
    published = client.post(
        f"{detail_path}/publish",
        json={"expected_version": 2, "review_confirmation": "assessment-review-v1"},
        headers={"X-CSRF-Token": csrf},
    )
    replay_publish = client.post(
        f"{detail_path}/publish",
        json={"expected_version": 2, "review_confirmation": "assessment-review-v1"},
        headers={"X-CSRF-Token": csrf},
    )
    assert missing_review.status_code == 422
    assert published.status_code == replay_publish.status_code == 200
    assert published.json() == replay_publish.json()
    assert published.json()["status"] == "published" and published.json()["version"] == 3
    assert published.json()["published_version"] == 1
    assert published.json()["definition"]["title"] == "Updated Python API Exercise"
    assert client.patch(
        detail_path, json={"expected_version": 3, "definition": assessment_definition()}, headers={"X-CSRF-Token": csrf}
    ).status_code == 409
    with connect_database(TEST_DATABASE_URL) as connection:
        versions = connection.execute(
            "SELECT version_number, definition FROM organization_assessment_versions WHERE assessment_id = %s",
            (assessment_id,),
        ).fetchall()
        actions = connection.execute(
            "SELECT action, metadata FROM audit_events WHERE resource_id = %s ORDER BY action", (assessment_id,)
        ).fetchall()
    assert len(versions) == 1 and versions[0]["version_number"] == 1
    assert versions[0]["definition"] == published.json()["definition"]
    assert [row["action"] for row in actions] == [
        "organization.assessment_created", "organization.assessment_published", "organization.assessment_updated"
    ]
    assert actions[1]["metadata"] == {"review_confirmation": "assessment-review-v1", "version_number": 1}

    with TestClient(client.app, base_url="https://proofpath.test") as member:
        register_and_login(member, mailer, "assessment-member@example.com", purpose="employer")
        with connect_database(TEST_DATABASE_URL) as connection:
            member_id = connection.execute(
                "SELECT id FROM users WHERE email = 'assessment-member@example.com'"
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO organization_memberships (organization_id, user_id, role) VALUES (%s, %s, 'member')",
                (organization_id, member_id),
            )
        assert member.get(base).status_code == 403
        assert member.get(detail_path).status_code == 403
        member_csrf = member.cookies[CSRF_COOKIE]
        assert member.post(base, json={"request_key": str(uuid4()), "definition": assessment_definition()}, headers={"X-CSRF-Token": member_csrf}).status_code == 403
        assert member.patch(detail_path, json={"expected_version": 1, "definition": assessment_definition()}, headers={"X-CSRF-Token": member_csrf}).status_code == 403
        assert member.post(
            f"{detail_path}/publish", json={"expected_version": 1, "review_confirmation": "assessment-review-v1"},
            headers={"X-CSRF-Token": member_csrf},
        ).status_code == 403

    with TestClient(client.app, base_url="https://proofpath.test") as recruiter:
        register_and_login(recruiter, mailer, "assessment-recruiter@example.com", purpose="employer")
        with connect_database(TEST_DATABASE_URL) as connection:
            recruiter_id = connection.execute(
                "SELECT id FROM users WHERE email = 'assessment-recruiter@example.com'"
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO organization_memberships (organization_id, user_id, role) VALUES (%s, %s, 'recruiter')",
                (organization_id, recruiter_id),
            )
        recruiter_csrf = recruiter.cookies[CSRF_COOKIE]
        recruiter_draft = recruiter.post(
            base, json={"request_key": str(uuid4()), "definition": assessment_definition()},
            headers={"X-CSRF-Token": recruiter_csrf},
        )
        assert recruiter_draft.status_code == 201
        assert recruiter.post(
            f"{base}/{recruiter_draft.json()['id']}/publish",
            json={"expected_version": 1, "review_confirmation": "assessment-review-v1"},
            headers={"X-CSRF-Token": recruiter_csrf},
        ).status_code == 403

    with TestClient(client.app, base_url="https://proofpath.test") as outsider:
        register_and_login(outsider, mailer, "assessment-outsider@example.com", purpose="employer")
        other_org, _ = create_published_job(outsider, organization_name="Other Assessment Lab")
        other_base = f"/api/v1/organizations/{other_org}/assessments"
        outsider_csrf = outsider.cookies[CSRF_COOKIE]
        assert outsider.get(base).status_code == 404
        assert outsider.get(f"{base}?limit=10").status_code == 404
        assert outsider.get(detail_path).status_code == 404
        assert outsider.post(base, json={"request_key": str(uuid4()), "definition": assessment_definition()}, headers={"X-CSRF-Token": outsider_csrf}).status_code == 404
        assert outsider.patch(detail_path, json={"expected_version": 1, "definition": assessment_definition()}, headers={"X-CSRF-Token": outsider_csrf}).status_code == 404
        assert outsider.post(
            f"{detail_path}/publish", json={"expected_version": 1, "review_confirmation": "assessment-review-v1"},
            headers={"X-CSRF-Token": outsider_csrf},
        ).status_code == 404
        assert outsider.get(other_base).json()["assessments"] == []

    openapi = client.get("/openapi.json").json()
    assert openapi["paths"][base.replace(organization_id, "{org_id}")]["post"]["security"] == [
        {"ProofpathSession": [], "ProofpathCSRF": []}
    ]
    assert openapi["paths"]["/api/v1/organizations/{org_id}/assessments/{assessment_id}/publish"]["post"]["responses"]["200"]
    assert "AssessmentDefinition" in openapi["components"]["schemas"]


def test_assessment_competencies_validate_catalog_links_and_freeze_them(auth_client):
    client, mailer = auth_client
    register_and_login(client, mailer, "assessment-catalog-owner@example.com", purpose="employer")
    organization_id, _ = create_published_job(client, organization_name="Assessment Catalog Lab")
    csrf = client.cookies[CSRF_COOKIE]
    base = f"/api/v1/organizations/{organization_id}/assessments"
    linked_competencies = [
        {"name": "Problem solving", "target_level": "mid", "competency_code": "fastapi.api_design"},
        {"name": "Code quality", "target_level": "mid"},
    ]
    invalid = client.post(
        base, json={"request_key": str(uuid4()), "definition": assessment_definition(competencies=[
            linked_competencies[0] | {"competency_code": "java.bytecode"}, linked_competencies[1],
        ])}, headers={"X-CSRF-Token": csrf},
    )
    repeated_code = client.post(
        base, json={"request_key": str(uuid4()), "definition": assessment_definition(competencies=[
            linked_competencies[0], linked_competencies[1] | {"competency_code": "fastapi.api_design"},
        ])}, headers={"X-CSRF-Token": csrf},
    )
    assert invalid.status_code == repeated_code.status_code == 422

    created = client.post(
        base, json={"request_key": str(uuid4()), "definition": assessment_definition(competencies=linked_competencies)},
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 201, created.text
    assert created.json()["definition"]["competencies"][0]["competency_code"] == "fastapi.api_design"
    assessment_id = created.json()["id"]
    unknown_update = client.patch(
        f"{base}/{assessment_id}",
        json={"expected_version": 1, "definition": assessment_definition(competencies=[
            linked_competencies[0] | {"competency_code": "java.bytecode"}, linked_competencies[1],
        ])}, headers={"X-CSRF-Token": csrf},
    )
    assert unknown_update.status_code == 422
    published = client.post(
        f"{base}/{assessment_id}/publish",
        json={"expected_version": 1, "review_confirmation": "assessment-review-v1"},
        headers={"X-CSRF-Token": csrf},
    )
    assert published.status_code == 200
    with connect_database(TEST_DATABASE_URL) as connection:
        version = connection.execute(
            "SELECT definition FROM organization_assessment_versions WHERE assessment_id = %s", (assessment_id,)
        ).fetchone()
    assert version["definition"]["competencies"][0]["competency_code"] == "fastapi.api_design"


def test_assessment_invitations_bind_published_versions_and_record_candidate_consent(auth_client):
    client, mailer = auth_client
    guest_org, guest_invitation = uuid4(), uuid4()
    assert client.get("/api/v1/me/dashboard").status_code == 401
    guest_candidate_body = {"expected_version": 1, "action": "accept", "consent_version": "assessment-invitation-v1"}
    assert client.put(
        f"/api/v1/attempts/{guest_invitation}/consents",
        json={"expected_version": 1, "action": "accept", "consent_version": "assessment-attempt-v1"},
    ).status_code == 401
    assert client.get("/api/v1/me/assessment-invitations").status_code == 401
    assert client.get(f"/api/v1/me/assessment-invitations/{guest_invitation}").status_code == 401
    assert client.get("/api/v1/me/attempts").status_code == 401
    assert client.get(f"/api/v1/attempts/{guest_invitation}").status_code == 401
    assert client.get(f"/api/v1/attempts/{guest_invitation}/readiness").status_code == 401
    assert client.get(f"/api/v1/organizations/{guest_org}/assessment-invitations").status_code == 401
    assert client.post(
        f"/api/v1/assessment-invitations/{guest_invitation}/attempts",
        json={"request_key": str(uuid4()), "expected_invitation_version": 2},
    ).status_code == 401
    assert client.post(
        f"/api/v1/organizations/{guest_org}/assessment-invitations",
        json={"request_key": str(uuid4()), "application_id": str(uuid4()), "expected_application_version": 2,
              "assessment_id": str(uuid4()), "assessment_version": 1},
    ).status_code == 401
    assert client.post(
        f"/api/v1/organizations/{guest_org}/assessment-invitations/{guest_invitation}/cancel",
        json={"expected_version": 1, "reason": "The hiring team changed the exercise."},
    ).status_code == 401
    assert client.post(f"/api/v1/me/assessment-invitations/{guest_invitation}/respond", json=guest_candidate_body).status_code == 401

    register_and_login(client, mailer, "assessment-inviter@example.com", purpose="employer")
    assert client.get("/api/v1/me/dashboard").status_code == 403
    organization_id, job_id = create_published_job(client, organization_name="Invitation Lab")
    consented_assessment = create_published_assessment(client, organization_id, title="Candidate Invitation Exercise")
    cancelled_assessment = create_published_assessment(client, organization_id, title="Cancelled Invitation Exercise")
    expired_assessment = create_published_assessment(client, organization_id, title="Expired Invitation Exercise")
    declined_assessment = create_published_assessment(client, organization_id, title="Declined Invitation Exercise")
    withdrawn_assessment = create_published_assessment(client, organization_id, title="Withdrawn Application Exercise")

    with TestClient(client.app, base_url="https://proofpath.test") as candidate:
        register_and_login(candidate, mailer, "invited-candidate@example.com")
        complete_and_publish_candidate_portfolio(candidate, full_name="Invited Candidate", handle="invited-candidate")
        candidate_csrf = candidate.cookies[CSRF_COOKIE]
        dashboard = candidate.get("/api/v1/me/dashboard")
        assert dashboard.status_code == 200 and dashboard.headers["cache-control"] == "no-store"
        assert dashboard.json() == {
            "candidate_name": "Invited Candidate", "candidate_setup_complete": True,
            "portfolio_public": True, "public_handle": "invited-candidate", "public_project_count": 1,
            "application_count": 0, "saved_job_count": 0,
            "pending_assessment_invitation_count": 0, "active_assessment_attempt_count": 0,
        }
        assert candidate.put(f"/api/v1/me/saved-jobs/{job_id}", headers={"X-CSRF-Token": candidate_csrf}).status_code == 204
        application = candidate.post(
            f"/api/v1/jobs/{job_id}/applications",
            json={
                "request_key": str(uuid4()),
                "portfolio_consent_version": "job-application-public-portfolio-v1",
            },
            headers={"X-CSRF-Token": candidate_csrf},
        )
        assert application.status_code == 201, application.text
        application_id = application.json()["id"]
        owner_csrf = client.cookies[CSRF_COOKIE]
        stage = client.patch(
            f"/api/v1/organizations/{organization_id}/applications/{application_id}/stage",
            json={"expected_version": 1, "stage": "in_review"},
            headers={"X-CSRF-Token": owner_csrf},
        )
        assert stage.status_code == 200 and stage.json()["version"] == 2

        base = f"/api/v1/organizations/{organization_id}/assessment-invitations"
        invite_body = {
            "request_key": str(uuid4()),
            "application_id": application_id,
            "expected_application_version": 2,
            "assessment_id": consented_assessment["id"],
            "assessment_version": 1,
        }
        no_csrf = client.post(base, json=invite_body)
        invalid_assessment_version = client.post(
            base, json=invite_body | {"assessment_version": 0}, headers={"X-CSRF-Token": owner_csrf}
        )
        assert no_csrf.status_code == 403 and invalid_assessment_version.status_code == 422
        with TestClient(client.app, base_url="https://proofpath.test") as assigned_recruiter:
            reviewer_login = register_and_login(assigned_recruiter, mailer, "assessment-assigned-reviewer@example.com", purpose="employer")
            reviewer_id = reviewer_login["user"]["id"]
            reviewer_csrf = assigned_recruiter.cookies[CSRF_COOKIE]
            with connect_database(TEST_DATABASE_URL) as connection:
                connection.execute(
                    "INSERT INTO organization_memberships (organization_id, user_id, role) VALUES (%s, %s, 'recruiter')",
                    (organization_id, reviewer_id),
                )
            assert assigned_recruiter.get(base).json()["invitations"] == []
            assert assigned_recruiter.post(base, json=invite_body, headers={"X-CSRF-Token": reviewer_csrf}).status_code == 404
            assignment = client.post(
                f"/api/v1/organizations/{organization_id}/applications/{application_id}/assign-reviewer",
                json={"request_key": str(uuid4()), "expected_version": 0, "reviewer_user_id": reviewer_id},
                headers={"X-CSRF-Token": owner_csrf},
            )
            assert assignment.status_code == 201
            created = assigned_recruiter.post(base, json=invite_body, headers={"X-CSRF-Token": reviewer_csrf})
            assert created.status_code == 201, created.text
            assert len(assigned_recruiter.get(base).json()["invitations"]) == 1
        replay = client.post(base, json=invite_body, headers={"X-CSRF-Token": owner_csrf})
        reused_key = client.post(
            base, json=invite_body | {"assessment_id": cancelled_assessment["id"]},
            headers={"X-CSRF-Token": owner_csrf},
        )
        duplicate_assignment = client.post(
            base, json=invite_body | {"request_key": str(uuid4())}, headers={"X-CSRF-Token": owner_csrf}
        )
        assert created.status_code == 201 and replay.status_code == 200
        assert created.json() == replay.json()
        assert reused_key.status_code == duplicate_assignment.status_code == 409
        invitation_id = created.json()["id"]
        dashboard = candidate.get("/api/v1/me/dashboard").json()
        assert dashboard["application_count"] == 1
        assert dashboard["saved_job_count"] == 1
        assert dashboard["pending_assessment_invitation_count"] == 1
        application_after_invite = candidate.get(f"/api/v1/me/applications/{application_id}")
        assert application_after_invite.json()["status"] == "assessment"
        assert application_after_invite.json()["version"] == 3

        organization_list = client.get(base)
        assert organization_list.status_code == 200 and organization_list.headers["cache-control"] == "no-store"
        assert organization_list.json()["invitations"][0]["applicant_name"] == "Invited Candidate"
        candidate_list = candidate.get("/api/v1/me/assessment-invitations?limit=50")
        assert candidate_list.status_code == 200 and candidate_list.headers["cache-control"] == "no-store"
        assert candidate_list.json()["invitations"][0]["id"] == invitation_id
        briefing_path = f"/api/v1/me/assessment-invitations/{invitation_id}"
        briefing = candidate.get(briefing_path)
        assert briefing.status_code == 200 and briefing.headers["cache-control"] == "no-store"
        assert briefing.json()["assessment"]["task_instructions"] == assessment_definition()["task_instructions"]
        missing_consent = candidate.post(
            f"{briefing_path}/respond", json={"expected_version": 1, "action": "accept"},
            headers={"X-CSRF-Token": candidate_csrf},
        )
        no_csrf_response = candidate.post(
            f"{briefing_path}/respond",
            json={"expected_version": 1, "action": "accept", "consent_version": "assessment-invitation-v1"},
        )
        accepted = candidate.post(
            f"{briefing_path}/respond", json={"expected_version": 1, "action": "accept", "consent_version": "assessment-invitation-v1"},
            headers={"X-CSRF-Token": candidate_csrf},
        )
        repeat_accept = candidate.post(
            f"{briefing_path}/respond", json={"expected_version": 1, "action": "accept", "consent_version": "assessment-invitation-v1"},
            headers={"X-CSRF-Token": candidate_csrf},
        )
        assert missing_consent.status_code == 422 and no_csrf_response.status_code == 403
        assert accepted.status_code == repeat_accept.status_code == 200
        assert accepted.json() == repeat_accept.json()
        assert accepted.json()["status"] == "accepted"
        assert accepted.json()["consent_version"] == "assessment-invitation-v1"
        assert accepted.json()["consented_at"] is not None
        assert client.get(base).json()["invitations"][0]["status"] == "accepted"

        attempt_path = f"/api/v1/assessment-invitations/{invitation_id}/attempts"
        attempt_body = {"request_key": str(uuid4()), "expected_invitation_version": accepted.json()["version"]}
        invalid_attempt = candidate.post(
            attempt_path, json=attempt_body | {"expected_invitation_version": 0},
            headers={"X-CSRF-Token": candidate_csrf},
        )
        stale_attempt = candidate.post(
            attempt_path, json=attempt_body | {"request_key": str(uuid4()), "expected_invitation_version": 1},
            headers={"X-CSRF-Token": candidate_csrf},
        )
        simultaneous_posts = Barrier(2)

        def create_attempt_once(_):
            simultaneous_posts.wait(timeout=5)
            return candidate.post(attempt_path, json=attempt_body, headers={"X-CSRF-Token": candidate_csrf})

        with ThreadPoolExecutor(max_workers=2) as executor:
            concurrent_attempts = list(executor.map(create_attempt_once, range(2)))
        created_attempt = next(result for result in concurrent_attempts if result.status_code == 201)
        repeated_attempt = next(result for result in concurrent_attempts if result.status_code == 200)
        duplicate_active_attempt = candidate.post(
            attempt_path, json=attempt_body | {"request_key": str(uuid4())}, headers={"X-CSRF-Token": candidate_csrf}
        )
        assert invalid_attempt.status_code == 422 and stale_attempt.status_code == 409
        assert created_attempt.status_code == 201 and created_attempt.json()["status"] == "created"
        assert created_attempt.json()["requires_consent"] is True
        assert created_attempt.json()["next_action"] == "prepare"
        assert repeated_attempt.status_code == 200 and repeated_attempt.json() == created_attempt.json()
        assert duplicate_active_attempt.status_code == 409, duplicate_active_attempt.text
        attempt_id = created_attempt.json()["id"]
        assert candidate.get("/api/v1/me/dashboard").json()["active_assessment_attempt_count"] == 1
        attempt_detail_path = f"/api/v1/attempts/{attempt_id}"
        readiness_path = f"{attempt_detail_path}/readiness"
        attempt_detail = candidate.get(attempt_detail_path)
        readiness = candidate.get(readiness_path)
        attempt_list = candidate.get("/api/v1/me/attempts?limit=50")
        assert attempt_detail.status_code == 200 and attempt_detail.headers["cache-control"] == "no-store"
        assert readiness.status_code == 200 and readiness.headers["cache-control"] == "no-store"
        assert readiness.json()["can_start"] is False
        assert readiness.json()["requires_consent"] is True
        assert readiness.json()["execution_available"] is False and readiness.json()["recording_enabled"] is False
        assert readiness.json()["blockers"] == ["ASSESSMENT_CONSENT_REQUIRED", "ASSESSMENT_TEST_SUITE_UNAVAILABLE", "ISOLATED_RUNTIME_UNAVAILABLE"]
        assert attempt_list.status_code == 200 and attempt_list.headers["cache-control"] == "no-store"
        assert [item["id"] for item in attempt_list.json()["attempts"]] == [attempt_id]
        assert client.get(attempt_detail_path).status_code == 404
        assert client.get(readiness_path).status_code == 404

        consent_path = f"{attempt_detail_path}/consents"
        consent_body = {
            "expected_version": 1,
            "action": "accept",
            "consent_version": "assessment-attempt-v1",
            "recording_preferences": {"audio": False, "video": False, "screen": False, "transcript": False},
        }
        assert candidate.put(consent_path, json=consent_body).status_code == 403
        assert candidate.put(
            consent_path, json=consent_body | {"recording_preferences": consent_body["recording_preferences"] | {"audio": True}},
            headers={"X-CSRF-Token": candidate_csrf},
        ).status_code == 422
        assert client.put(
            consent_path, json=consent_body, headers={"X-CSRF-Token": owner_csrf}
        ).status_code == 404
        accepted_consent = candidate.put(
            consent_path, json=consent_body, headers={"X-CSRF-Token": candidate_csrf}
        )
        assert accepted_consent.status_code == 200, accepted_consent.text
        assert accepted_consent.headers["cache-control"] == "no-store"
        assert accepted_consent.json()["attempt_status"] == "consented"
        assert accepted_consent.json()["attempt_version"] == 2
        assert accepted_consent.json()["consent_status"] == "accepted"
        assert accepted_consent.json()["recording_preferences"] == consent_body["recording_preferences"]
        assert candidate.put(
            consent_path, json=consent_body, headers={"X-CSRF-Token": candidate_csrf}
        ).json() == accepted_consent.json()
        assert candidate.get(attempt_detail_path).json()["requires_consent"] is False
        ready_after_consent = candidate.get(readiness_path).json()
        assert ready_after_consent["requires_consent"] is False
        assert ready_after_consent["blockers"] == ["ASSESSMENT_TEST_SUITE_UNAVAILABLE", "ISOLATED_RUNTIME_UNAVAILABLE"]

        revoke_body = {"expected_version": 2, "action": "revoke"}
        revoked_consent = candidate.put(
            consent_path, json=revoke_body, headers={"X-CSRF-Token": candidate_csrf}
        )
        assert revoked_consent.status_code == 200, revoked_consent.text
        assert revoked_consent.json()["attempt_status"] == "created"
        assert revoked_consent.json()["attempt_version"] == 3
        assert revoked_consent.json()["consent_status"] == "revoked"
        assert candidate.get(readiness_path).json()["blockers"] == ["ASSESSMENT_CONSENT_REQUIRED", "ASSESSMENT_TEST_SUITE_UNAVAILABLE", "ISOLATED_RUNTIME_UNAVAILABLE"]
        assert candidate.put(
            consent_path, json=revoke_body, headers={"X-CSRF-Token": candidate_csrf}
        ).json() == revoked_consent.json()
        consented_again = candidate.put(
            consent_path,
            json=consent_body | {"expected_version": 3},
            headers={"X-CSRF-Token": candidate_csrf},
        )
        assert consented_again.status_code == 200 and consented_again.json()["attempt_version"] == 4
        with connect_database(TEST_DATABASE_URL) as connection:
            consent_events = connection.execute(
                """SELECT attempt_version, action, recording_preferences FROM assessment_attempt_consent_events
                   WHERE attempt_id = %s ORDER BY attempt_version""",
                (attempt_id,),
            ).fetchall()
            consent_audits = connection.execute(
                "SELECT action FROM audit_events WHERE resource_id = %s AND action LIKE 'candidate.assessment_attempt_consent_%%' ORDER BY occurred_at",
                (attempt_id,),
            ).fetchall()
        assert [(event["attempt_version"], event["action"]) for event in consent_events] == [
            (2, "accepted"), (3, "revoked"), (4, "accepted")
        ]
        assert all(event["recording_preferences"] == consent_body["recording_preferences"] for event in consent_events)
        assert [event["action"] for event in consent_audits] == [
            "candidate.assessment_attempt_consent_accepted",
            "candidate.assessment_attempt_consent_revoked",
            "candidate.assessment_attempt_consent_accepted",
        ]

        cancel_body = {
            "request_key": str(uuid4()),
            "application_id": application_id,
            "expected_application_version": 3,
            "assessment_id": cancelled_assessment["id"],
            "assessment_version": 1,
        }
        cancel_invitation = client.post(base, json=cancel_body, headers={"X-CSRF-Token": owner_csrf})
        assert cancel_invitation.status_code == 201
        cancellation_path = f"{base}/{cancel_invitation.json()['id']}/cancel"
        invalid_cancel = client.post(
            cancellation_path, json={"expected_version": 1, "reason": "short"}, headers={"X-CSRF-Token": owner_csrf}
        )
        with TestClient(client.app, base_url="https://proofpath.test") as assigned_reviewer:
            reviewer_login = assigned_reviewer.post(
                "/api/v1/auth/login", json={"email": "assessment-assigned-reviewer@example.com", "password": "a long secure password"}
            )
            assert reviewer_login.status_code == 200
            reviewer_csrf = assigned_reviewer.cookies[CSRF_COOKIE]
            cancelled = assigned_reviewer.post(
                cancellation_path, json={"expected_version": 1, "reason": "The hiring team changed the exercise."},
                headers={"X-CSRF-Token": reviewer_csrf},
            )
        repeat_cancel = client.post(
            cancellation_path, json={"expected_version": 1, "reason": "The hiring team changed the exercise."},
            headers={"X-CSRF-Token": owner_csrf},
        )
        assert invalid_cancel.status_code == 422 and cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert repeat_cancel.json() == cancelled.json()

        declined_invitation = client.post(
            base,
            json={
                "request_key": str(uuid4()), "application_id": application_id,
                "expected_application_version": 3, "assessment_id": declined_assessment["id"],
                "assessment_version": 1,
            },
            headers={"X-CSRF-Token": owner_csrf},
        )
        assert declined_invitation.status_code == 201
        declined_response = candidate.post(
            f"/api/v1/me/assessment-invitations/{declined_invitation.json()['id']}/respond",
            json={"expected_version": 1, "action": "decline"}, headers={"X-CSRF-Token": candidate_csrf},
        )
        assert declined_response.status_code == 200
        assert declined_response.json()["status"] == "declined"
        assert declined_response.json()["consent_version"] is None
        assert candidate.post(
            f"/api/v1/assessment-invitations/{declined_invitation.json()['id']}/attempts",
            json={"request_key": str(uuid4()), "expected_invitation_version": declined_response.json()["version"]},
            headers={"X-CSRF-Token": candidate_csrf},
        ).status_code == 409

        expiring = client.post(
            base,
            json={
                "request_key": str(uuid4()), "application_id": application_id,
                "expected_application_version": 3, "assessment_id": expired_assessment["id"],
                "assessment_version": 1,
            },
            headers={"X-CSRF-Token": owner_csrf},
        )
        assert expiring.status_code == 201
        expiring_id = expiring.json()["id"]
        with connect_database(TEST_DATABASE_URL) as connection:
            connection.execute(
                "UPDATE assessment_invitations SET expires_at = now() - interval '1 minute' WHERE id = %s",
                (expiring_id,),
            )
        expired_cancel = client.post(
            f"{base}/{expiring_id}/cancel",
            json={"expected_version": 1, "reason": "The hiring team changed the exercise."},
            headers={"X-CSRF-Token": owner_csrf},
        )
        assert expired_cancel.status_code == 409
        assert expired_cancel.json()["code"] == "ASSESSMENT_INVITATION_EXPIRED"
        expired_list = candidate.get("/api/v1/me/assessment-invitations?limit=50")
        expired_item = next(item for item in expired_list.json()["invitations"] if item["id"] == expiring_id)
        assert expired_item["status"] == "expired" and expired_item["version"] == 2
        expired_response = candidate.post(
            f"/api/v1/me/assessment-invitations/{expiring_id}/respond",
            json={"expected_version": 1, "action": "decline"}, headers={"X-CSRF-Token": candidate_csrf},
        )
        assert expired_response.status_code == 409
        assert expired_response.json()["code"] == "ASSESSMENT_INVITATION_EXPIRED"

        with TestClient(client.app, base_url="https://proofpath.test") as other_candidate:
            register_and_login(other_candidate, mailer, "other-invited-candidate@example.com")
            other_candidate_csrf = other_candidate.cookies[CSRF_COOKIE]
            assert other_candidate.get(f"/api/v1/me/assessment-invitations/{invitation_id}").status_code == 404
            assert other_candidate.get("/api/v1/me/assessment-invitations").json()["invitations"] == []
            assert other_candidate.get(attempt_detail_path).status_code == 404
            assert other_candidate.get(readiness_path).status_code == 404
            assert other_candidate.get("/api/v1/me/attempts").json()["attempts"] == []
            assert other_candidate.post(
                attempt_path,
                json={"request_key": str(uuid4()), "expected_invitation_version": 2},
                headers={"X-CSRF-Token": other_candidate_csrf},
            ).status_code == 404
            assert other_candidate.post(
                f"/api/v1/me/assessment-invitations/{invitation_id}/respond",
                json={"expected_version": 1, "action": "decline"},
                headers={"X-CSRF-Token": other_candidate_csrf},
            ).status_code == 404
            assert other_candidate.put(
                consent_path, json=consent_body, headers={"X-CSRF-Token": other_candidate_csrf}
            ).status_code == 404

        withdrawn_invitation = client.post(
            base,
            json={
                "request_key": str(uuid4()), "application_id": application_id,
                "expected_application_version": 3, "assessment_id": withdrawn_assessment["id"],
                "assessment_version": 1,
            },
            headers={"X-CSRF-Token": owner_csrf},
        )
        assert withdrawn_invitation.status_code == 201
        withdrawn = candidate.post(
            f"/api/v1/me/applications/{application_id}/withdraw",
            json={"expected_version": 3}, headers={"X-CSRF-Token": candidate_csrf},
        )
        assert withdrawn.status_code == 200 and withdrawn.json()["status"] == "withdrawn"
        candidate_invitations = candidate.get("/api/v1/me/assessment-invitations?limit=50").json()["invitations"]
        withdrawal_cancelled = next(item for item in candidate_invitations if item["id"] == withdrawn_invitation.json()["id"])
        assert withdrawal_cancelled["status"] == "cancelled"
        withdrawn_attempt = candidate.get("/api/v1/me/attempts?limit=50").json()["attempts"]
        cancelled_attempt = next(item for item in withdrawn_attempt if item["id"] == attempt_id)
        assert cancelled_attempt["status"] == "cancelled" and cancelled_attempt["version"] == 5
        assert cancelled_attempt["requires_consent"] is False and cancelled_attempt["next_action"] == "none"
        cancelled_readiness = candidate.get(readiness_path).json()
        assert cancelled_readiness["blockers"] == ["ATTEMPT_CANCELLED", "ASSESSMENT_TEST_SUITE_UNAVAILABLE", "ISOLATED_RUNTIME_UNAVAILABLE"]
        final_dashboard = candidate.get("/api/v1/me/dashboard").json()
        assert final_dashboard["application_count"] == 0
        assert final_dashboard["pending_assessment_invitation_count"] == 0
        assert final_dashboard["active_assessment_attempt_count"] == 0
        assert final_dashboard["public_project_count"] == 1 and final_dashboard["saved_job_count"] == 1
        assert all(item["id"] != withdrawn_invitation.json()["id"] for item in client.get(base).json()["invitations"])

    with TestClient(client.app, base_url="https://proofpath.test") as member:
        register_and_login(member, mailer, "assessment-invitation-member@example.com", purpose="employer")
        with connect_database(TEST_DATABASE_URL) as connection:
            member_id = connection.execute(
                "SELECT id FROM users WHERE email = 'assessment-invitation-member@example.com'"
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO organization_memberships (organization_id, user_id, role) VALUES (%s, %s, 'member')",
                (organization_id, member_id),
            )
        member_csrf = member.cookies[CSRF_COOKIE]
        assert member.get(base).status_code == 403
        assert member.post(
            f"{base}/{invitation_id}/cancel",
            json={"expected_version": 1, "reason": "The hiring team changed the exercise."},
            headers={"X-CSRF-Token": member_csrf},
        ).status_code == 403
        assert member.post(
            base,
            json={"request_key": str(uuid4()), "application_id": application_id, "expected_application_version": 3,
                  "assessment_id": consented_assessment["id"], "assessment_version": 1},
            headers={"X-CSRF-Token": member_csrf},
        ).status_code == 403

    with TestClient(client.app, base_url="https://proofpath.test") as outsider:
        register_and_login(outsider, mailer, "assessment-invitation-outsider@example.com", purpose="employer")
        other_org, _ = create_published_job(outsider, organization_name="Other Invitation Lab")
        outsider_csrf = outsider.cookies[CSRF_COOKIE]
        assert outsider.get(base).status_code == 404
        assert outsider.get(f"/api/v1/organizations/{other_org}/assessment-invitations").json()["invitations"] == []
        assert outsider.post(
            f"/api/v1/organizations/{other_org}/assessment-invitations/{invitation_id}/cancel",
            json={"expected_version": 1, "reason": "The hiring team changed the exercise."},
            headers={"X-CSRF-Token": outsider_csrf},
        ).status_code == 404
        assert outsider.post(
            base,
            json={"request_key": str(uuid4()), "application_id": application_id, "expected_application_version": 3,
                  "assessment_id": consented_assessment["id"], "assessment_version": 1},
            headers={"X-CSRF-Token": outsider_csrf},
        ).status_code == 404

    openapi = client.get("/openapi.json").json()
    assert openapi["paths"]["/api/v1/me/dashboard"]["get"]["security"] == [{"ProofpathSession": []}]
    assert "CandidateDashboardResponse" in openapi["components"]["schemas"]
    assert openapi["paths"]["/api/v1/organizations/{org_id}/assessment-invitations"]["post"]["security"] == [
        {"ProofpathSession": [], "ProofpathCSRF": []}
    ]
    assert openapi["paths"]["/api/v1/me/assessment-invitations/{invite_id}/respond"]["post"]["security"] == [
        {"ProofpathSession": [], "ProofpathCSRF": []}
    ]
    assert openapi["paths"]["/api/v1/assessment-invitations/{invite_id}/attempts"]["post"]["security"] == [
        {"ProofpathSession": [], "ProofpathCSRF": []}
    ]
    assert openapi["paths"]["/api/v1/attempts/{attempt_id}/consents"]["put"]["security"] == [
        {"ProofpathSession": [], "ProofpathCSRF": []}
    ]
    assert "AssessmentAttemptCreateRequest" in openapi["components"]["schemas"]
    assert "AssessmentAttemptConsentRequest" in openapi["components"]["schemas"]
    assert "AssessmentAttemptConsentResponse" in openapi["components"]["schemas"]
    assert "AssessmentAttemptReadinessResponse" in openapi["components"]["schemas"]
    assert "AssessmentInvitationRespondRequest" in openapi["components"]["schemas"]
