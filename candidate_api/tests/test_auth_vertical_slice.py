from concurrent.futures import ThreadPoolExecutor
import base64
from threading import Barrier
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
from psycopg.conninfo import conninfo_to_dict
from fastapi.testclient import TestClient

from candidate_api.app.db import connect_database
from candidate_api.app.main import CSRF_COOKIE, SESSION_COOKIE, create_app
from candidate_api.app.rate_limit import PostgresRateLimiter
from candidate_api.app.security import RateLimiter, hash_token, verify_password


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


def register_and_login(client, mailer, email):
    message_index = len(mailer.messages)
    response = client.post(
        "/api/v1/auth/register",
        json=registration_body(email=email),
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
    invalid_employer = client.post(
        "/api/v1/auth/register", json=registration_body(initial_purpose="employer")
    )
    stale_terms = client.post(
        "/api/v1/auth/register", json=registration_body(terms_version="old-terms")
    )

    assert malformed.status_code == extra_field.status_code == invalid_purpose.status_code == invalid_employer.status_code == 422
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
    assert document["applications"] == []
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


def test_candidate_resume_is_private_reviewable_exportable_and_deletable(auth_client):
    client, mailer = auth_client
    text = "Alex Johnson\nalex@example.com\nPython FastAPI PostgreSQL backend engineer with experience delivering reliable services."
    assert client.get("/api/v1/me/resume").status_code == 401
    assert client.post("/api/v1/me/resume/text", json={"text": text}).status_code == 401
    register_and_login(client, mailer, "resume-owner@example.com")
    csrf = client.cookies[CSRF_COOKIE]
    assert client.get("/api/v1/me/resume").json() == {"resume": None}
    assert client.post("/api/v1/me/resume/text", json={"text": text}).status_code == 403
    uploaded = client.post("/api/v1/me/resume/text", json={"text": text}, headers={"X-CSRF-Token": csrf})
    assert uploaded.status_code == 200, uploaded.text
    resume = uploaded.json()
    assert resume["media_type"] == "text/plain"
    assert resume["extraction_method"] == "heuristic"
    assert resume["extracted_data"]["full_name"] == "Alex Johnson"
    assert resume["extracted_data"]["skills"] == ["PostgreSQL", "FastAPI", "Python"]
    assert client.post("/api/v1/me/resume/text", json={"text": text}, headers={"X-CSRF-Token": csrf}).json()["id"] == resume["id"]

    edited = resume["extracted_data"] | {"skills": ["Python", "FastAPI"], "target_roles": ["Backend Engineer"]}
    reviewed = client.post(
        "/api/v1/me/resume/review",
        json={"expected_sha256": resume["sha256"], "fields": edited},
        headers={"X-CSRF-Token": csrf},
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["reviewed_at"]
    assert reviewed.json()["extracted_data"]["skills"] == ["Python", "FastAPI"]
    assert client.post(
        "/api/v1/me/resume/review",
        json={"expected_sha256": "0" * 64, "fields": edited}, headers={"X-CSRF-Token": csrf},
    ).status_code == 409

    login = client.get("/api/v1/me/candidate/onboarding").json()
    assert login["full_name"] == "Alex Johnson" and login["skills"] == []
    exported = client.post(
        "/api/v1/me/data-exports", json={"current_password": "a long secure password"}, headers={"X-CSRF-Token": csrf},
    )
    assert exported.status_code == 200, exported.text
    exported_resume = exported.json()["resume"]
    assert exported_resume["filename"] == "Pasted resume text.txt"
    assert base64.b64decode(exported_resume["content_base64"]).decode() == text

    with TestClient(client.app, base_url="https://proofpath.test") as other:
        register_and_login(other, mailer, "resume-other@example.com")
        assert other.get("/api/v1/me/resume").json() == {"resume": None}
        assert other.get("/api/v1/me/resume/file").status_code == 404
    assert client.delete("/api/v1/me/resume").status_code == 403
    assert client.delete("/api/v1/me/resume", headers={"X-CSRF-Token": csrf}).status_code == 204
    assert client.get("/api/v1/me/resume").json() == {"resume": None}


def test_frontend_routes_and_public_links_are_connected(auth_client):
    client, _ = auth_client
    for path in ("/signup", "/login", "/verify-email", "/candidate/onboarding", "/security/sessions", "/account/privacy", "/reset-password", "/app/portfolio/builder", "/portfolio-renderer.js", "/u/sample-handle", "/portfolio/sample-handle"):
        assert client.get(path).status_code == 200, path

    for path in ("/", "/jobs", "/applications", "/organization/jobs", "/organization/onboarding", "/assessment-workspace", "/candidate/dashboard"):
        assert client.get(path).status_code == 404, path

    metadata = client.get("/api/v1/meta/public").json()
    assert metadata["initial_purposes"] == ["candidate"]
    assert "employer" not in client.get("/signup").text.lower()
    openapi = client.get("/openapi.json").json()
    assert "/api/v1/me/projects" in openapi["paths"]
    assert "/api/v1/me/portfolio" in openapi["paths"]
    assert "/api/v1/public/portfolios/{handle}" in openapi["paths"]
    assert not any(any(term in path for term in ("/jobs", "/applications", "/organizations", "assessment-invitations")) for path in openapi["paths"])

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
    updated_complete_profile = client.patch(
        "/api/v1/me/candidate/onboarding",
        json={"expected_version": 3, "target_roles": ["Staff Engineer"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert completed.status_code == repeated.status_code == 200
    assert completed.json()["status"] == "complete" and completed.json()["version"] == 3
    assert repeated.json() == completed.json()
    assert updated_complete_profile.status_code == 200
    assert updated_complete_profile.json()["status"] == "complete"
    assert updated_complete_profile.json()["target_roles"] == ["Staff Engineer"]
    with connect_database(TEST_DATABASE_URL) as connection:
        assert connection.execute(
            "SELECT count(*) AS count FROM audit_events WHERE action = 'candidate.onboarding_completed'"
        ).fetchone()["count"] == 1
        assert connection.execute("SELECT full_name FROM users").fetchone()["full_name"] == "Alex Rivera"


def test_candidate_profiles_are_owner_scoped(auth_client):
    client, mailer = auth_client
    first = register_and_login(client, mailer, "first@example.com")
    first_csrf = client.cookies[CSRF_COOKIE]
    first_saved = client.patch(
        "/api/v1/me/candidate/onboarding",
        json={"expected_version": 0, "target_roles": ["First User Role"]},
        headers={"X-CSRF-Token": first_csrf},
    )
    assert first_saved.status_code == 200

    with TestClient(client.app, base_url="https://proofpath.test") as second_client:
        second = register_and_login(second_client, mailer, "second@example.com")
        assert second["next_action"] == "candidate_onboarding"
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
        register_and_login(other_client, mailer, "portfolio-viewer@example.com")
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
        register_and_login(other_client, mailer, "portfolio-builder-other@example.com")
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
