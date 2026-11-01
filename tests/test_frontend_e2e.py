import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen
from uuid import uuid4

import pytest
from psycopg.conninfo import conninfo_to_dict
from playwright.sync_api import sync_playwright

from backend.app.db import connect_database

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = os.getenv(
    "PROOFPATH_TEST_DATABASE_URL",
    "postgresql://proofpath_codex_app@127.0.0.1:5432/proofpath_codex_test",
)


def test_signup_verification_login_and_logout_in_browser(tmp_path):
    database_name = conninfo_to_dict(TEST_DATABASE_URL).get("dbname", "")
    if not database_name.endswith("_test"):
        pytest.fail("PROOFPATH_TEST_DATABASE_URL must target a database whose name ends in _test.")
    with connect_database(TEST_DATABASE_URL) as connection:
        connection.execute("TRUNCATE audit_events, auth_sessions, email_verification_tokens, rate_limit_buckets, users CASCADE")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base_url = f"http://localhost:{port}"
    mailbox = tmp_path / "dev-mailbox.jsonl"
    environment = os.environ.copy() | {
        "APP_ENV": "development",
        "DATABASE_URL": TEST_DATABASE_URL,
        "PROOFPATH_PUBLIC_URL": base_url,
        "PROOFPATH_TERMS_VERSION": "e2e-test-terms",
        "PROOFPATH_TERMS_URL": "https://example.invalid/e2e-test-terms",
        "PROOFPATH_DEV_MAILBOX": str(mailbox),
    }
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(200):
            if server.poll() is not None:
                raise AssertionError("The local API server exited before becoming ready.")
            try:
                with urlopen(f"{base_url}/api/v1/meta/public", timeout=1) as response:
                    if response.status == 200:
                        break
            except (URLError, TimeoutError):
                time.sleep(0.1)
        else:
            raise AssertionError("The local API server did not become ready.")

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            address = f"proofpath-{time.time_ns()}@example.com"

            page.goto(f"{base_url}/signup?purpose=candidate")
            page.locator("#submit").wait_for()
            page.wait_for_function("document.querySelector('#submit').disabled === false")
            assert page.locator("#purpose").input_value() == "candidate"
            page.get_by_role("button", name="Show password").click()
            assert page.locator("#password").get_attribute("type") == "text"
            page.get_by_role("button", name="Hide password").click()
            assert page.locator("#password").get_attribute("type") == "password"
            page.locator("#full-name").fill("Browser Test User")
            page.locator("#email").fill(address)
            page.locator("#password").fill("browser test secure password")
            page.locator("#terms").check()
            page.locator("#submit").click()
            page.get_by_role("status").filter(has_text="Request received").wait_for()

            for _ in range(100):
                if mailbox.exists() and mailbox.read_text(encoding="utf-8").strip():
                    mail = json.loads(mailbox.read_text(encoding="utf-8").splitlines()[-1])
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("The verification message was not written to the development mailbox.")

            page.goto(mail["verification_link"])
            page.get_by_role("status").filter(has_text="Your email is verified").wait_for()
            page.get_by_role("link", name="Continue to sign in").click()
            page.locator("#email").fill(address)
            page.locator("#password").fill("browser test secure password")
            page.locator("#submit").click()
            page.wait_for_url("**/candidate/onboarding")
            page.locator("#full-name").wait_for()
            assert page.locator("#full-name").input_value() == "Browser Test User"
            page.locator("#full-name").fill("Browser Test Updated")
            page.locator("#target-roles").fill("Product Engineer\nPlatform Engineer")
            page.locator("#skills").fill("Python\nPostgreSQL")
            page.locator("#save").click()
            page.get_by_role("status").filter(has_text="Your draft is saved").wait_for()
            page.locator("#complete").click()
            page.get_by_role("status").filter(has_text="Your candidate setup is complete").wait_for()
            page.get_by_role("link", name="Security & sessions").click()
            page.get_by_role("heading", name="Security & sessions").wait_for()
            page.locator("#session-list .session").wait_for()
            assert page.locator("#session-list .session").count() == 1
            assert page.locator("#session-list").get_by_role("heading", name="This session").is_visible()
            page.get_by_role("link", name="Candidate dashboard").click()
            page.get_by_role("heading", name="Welcome, Browser Test Updated").wait_for()
            assert page.locator("#dashboard").is_visible()
            assert page.locator("#public-project-count").inner_text() == "0"
            assert page.locator("#application-count").inner_text() == "0"
            assert page.locator("#saved-job-count").inner_text() == "0"
            page.get_by_role("link", name="Projects", exact=True).click()
            page.locator("#project-name").wait_for()
            page.locator("#project-name").fill("Browser Project")
            page.locator("#problem-statement").fill("A concrete problem statement.")
            page.locator("#contribution").fill("A self-reported contribution.")
            page.locator("#technologies").fill("Python, PostgreSQL")
            claim_statement = "I used Python collections to manage project data consistently."
            page.get_by_role("checkbox", name="Python language fundamentals").check()
            page.get_by_role("textbox", name="How does this project relate to Python language fundamentals?").fill(claim_statement)
            page.locator("#save-project").click()
            page.wait_for_function(
                "document.querySelector('#message').textContent.includes('Your private project draft is saved') || document.querySelector('#message').dataset.kind === 'error'",
                timeout=10000,
            )
            assert page.locator("#message").inner_text() == "Your private project draft is saved.", page.locator("#message").inner_text()
            assert page.locator("#projects-list").get_by_text("Browser Project").is_visible()
            assert page.get_by_text("Self-reported competency claims · unverified · private").is_visible()
            page.locator(".source-form input").fill("https://github.com/proofpath/browser-project/")
            page.get_by_role("button", name="Add link").click()
            page.get_by_role("status").filter(has_text="Repository contents were not fetched").wait_for()
            assert page.locator(".source-row a").get_attribute("href") == "https://github.com/proofpath/browser-project"
            page.get_by_role("button", name="Remove link").click()
            page.get_by_role("status").filter(has_text="GitHub link removed").wait_for()
            assert page.locator(".source-row").count() == 0
            page.locator("#project-name").fill("Second Browser Project")
            page.locator("#save-project").click()
            page.get_by_role("status").filter(has_text="Your private project draft is saved").wait_for()
            page.get_by_role("button", name="Move up").click()
            page.get_by_role("status").filter(has_text="Private portfolio order updated").wait_for()
            assert page.locator("#projects-list h3").all_text_contents() == ["Second Browser Project", "Browser Project"]
            page.get_by_role("button", name="Edit").nth(1).click()
            page.locator("#contribution").fill("Updated contribution.")
            page.locator("#save-project").click()
            page.get_by_role("status").filter(has_text="Your project changes are saved").wait_for()
            assert page.locator("#projects-list").get_by_text("Updated contribution.").is_visible()
            page.goto(f"{base_url}/security/sessions")
            page.get_by_role("link", name="Privacy & data").click()
            page.get_by_role("heading", name="Privacy & data").wait_for()
            page.locator("#current-password").fill("browser test secure password")
            with page.expect_download() as download_info:
                page.get_by_role("button", name="Download my data").click()
            export_download = download_info.value
            export_document = json.loads(Path(export_download.path()).read_text(encoding="utf-8"))
            assert export_download.suggested_filename.startswith("proofpath-account-data-")
            assert export_document["account"]["full_name"] == "Browser Test Updated"
            assert {item["name"] for item in export_document["projects"]} == {"Browser Project", "Second Browser Project"}
            assert "password_hash" not in json.dumps(export_document)
            page.goto(f"{base_url}/projects")
            page.locator("#public-handle").fill("browser-test-profile")
            page.locator("#save-handle").click()
            page.wait_for_function(
                "document.querySelector('#message').textContent.includes('Public profile address saved') || document.querySelector('#message').dataset.kind === 'error'",
                timeout=10000,
            )
            assert page.locator("#message").inner_text() == "Public profile address saved.", page.locator("#message").inner_text()
            page.locator("#portfolio-consent").check()
            page.locator("#publish-portfolio").click()
            page.get_by_role("status").filter(has_text="Your portfolio is public").wait_for()
            assert page.locator("#public-link").inner_text() == f"{base_url}/u/browser-test-profile"
            assert page.locator("#public-handle").is_disabled()
            public_project = page.locator("#projects-list > .project").nth(1)
            page.once("dialog", lambda dialog: dialog.accept())
            public_project.get_by_role("button", name="Publish project").click()
            page.get_by_role("status").filter(has_text="Project published to your portfolio").wait_for()
            page.goto(f"{base_url}/u/browser-test-profile")
            page.get_by_role("heading", name="Browser Test Updated", exact=True).wait_for()
            page.get_by_role("heading", name="Browser Project", exact=True).wait_for()
            assert page.get_by_role("heading", name="Second Browser Project", exact=True).count() == 0
            assert claim_statement not in page.locator("body").inner_text()
            assert "proofpath-" not in page.locator("body").inner_text()
            with connect_database(TEST_DATABASE_URL) as connection:
                user_id = connection.execute("SELECT id FROM users WHERE email = %s", (address,)).fetchone()["id"]
                connection.execute(
                    "INSERT INTO candidate_entitlement_grants (id, user_id, feature_key, grant_source) VALUES (%s, %s, 'portfolio_customization', 'test')",
                    (uuid4(), user_id),
                )
            page.goto(f"{base_url}/app/portfolio/builder")
            page.get_by_role("heading", name="Portfolio builder").wait_for()
            page.locator("#template-key").select_option("engineer")
            page.locator('input[name="theme-key"][value="teal"]').check()
            page.locator("#section-list").get_by_role("checkbox", name="Target roles").uncheck()
            assert "portfolio-template--engineer" in page.locator("#preview-content").get_attribute("class")
            page.get_by_role("button", name="Mobile").click()
            assert page.locator("#preview-frame").get_attribute("data-viewport") == "mobile"
            page.locator("#save-draft").click()
            page.get_by_role("status").filter(has_text="Draft saved").wait_for()
            assert page.locator("#version-list").get_by_text("Version 1 · Draft").is_visible()
            page.once("dialog", lambda dialog: dialog.accept())
            page.locator("#publish-design").click()
            page.get_by_role("status").filter(has_text="The portfolio design is published").wait_for()
            page.goto(f"{base_url}/u/browser-test-profile")
            page.locator("#portfolio-view.portfolio-template--engineer.portfolio-theme--teal").wait_for()
            assert page.locator('#portfolio-view [data-section="roles"]').count() == 0
            with connect_database(TEST_DATABASE_URL) as connection:
                connection.execute(
                    "UPDATE candidate_entitlement_grants SET revoked_at = now() WHERE user_id = %s", (user_id,),
                )
            page.reload()
            page.locator("#portfolio-view.portfolio-template--basic").wait_for()
            page.goto(f"{base_url}/app/portfolio/builder")
            page.locator("#save-draft").wait_for()
            assert page.locator("#save-draft").is_disabled()
            assert page.locator("#version-list").get_by_text("Version 1 · Published").is_visible()
            page.goto(f"{base_url}/projects")
            page.locator("#unpublish-portfolio").click()
            page.get_by_role("status").filter(has_text="Your portfolio and its projects are private").wait_for()
            assert page.locator("#public-handle").is_disabled()
            assert page.evaluate("fetch('/api/v1/public/portfolios/browser-test-profile').then(response => response.status)") == 404
            page.goto(f"{base_url}/portfolio/browser-test-profile")
            page.get_by_role("status").filter(has_text="This portfolio is unavailable").wait_for()
            page.goto(f"{base_url}/projects")
            page.once("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name="Delete").nth(1).click()
            page.get_by_role("status").filter(has_text="Project draft deleted").wait_for()
            page.wait_for_function("document.querySelectorAll('#projects-list > .project').length === 1")
            page.once("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name="Delete").click()
            page.wait_for_function("document.querySelectorAll('#projects-list > .project').length === 0")
            assert page.evaluate("fetch('/api/v1/me/projects').then(async response => (await response.json()).projects.length)") == 0

            cookies = {cookie["name"]: cookie for cookie in context.cookies()}
            session_cookie = cookies["__Host-proofpath_session"]
            assert session_cookie["secure"] and session_cookie["httpOnly"]
            assert cookies["__Host-proofpath_csrf"]["secure"]
            page.locator("#logout").click()
            page.wait_for_url("**/login")
            assert page.evaluate("fetch('/api/v1/auth/session').then(response => response.status)") == 401

            page.goto(f"{base_url}/login")
            page.get_by_role("link", name="Forgot password?").click()
            page.locator("#email").fill(address)
            page.locator("#request-button").click()
            page.get_by_role("status").filter(has_text="If the account exists").wait_for()
            reset_mail = json.loads(mailbox.read_text(encoding="utf-8").splitlines()[-1])
            page.goto(reset_mail["password_reset_link"])
            assert page.locator("#reset-form").is_visible()
            page.locator("#new-password").fill("browser test changed password")
            page.locator("#reset-button").click()
            page.get_by_role("status").filter(has_text="Your password has been reset").wait_for()
            page.get_by_role("link", name="Continue to sign in").click()
            page.locator("#email").fill(address)
            page.locator("#password").fill("browser test secure password")
            page.locator("#submit").click()
            page.get_by_role("status").filter(has_text="Invalid email or password").wait_for()
            page.locator("#password").fill("browser test changed password")
            page.locator("#submit").click()
            page.wait_for_url("**/candidate/onboarding")
            page.goto(f"{base_url}/organization/onboarding")
            page.get_by_role("status").filter(has_text="Create your first organization workspace").wait_for()
            page.locator("#new-name").fill("Proofpath Browser Organization")
            page.locator("#create-button").click()
            page.wait_for_url("**/organization/onboarding?organization_id=*")
            page.locator("#organization-name").wait_for()
            page.locator("#organization-name").fill("Proofpath Browser Labs")
            page.locator("#save").click()
            page.get_by_role("status").filter(has_text="Your organization draft is saved").wait_for()
            page.locator("#complete").click()
            page.get_by_role("status").filter(has_text="Organization setup is complete").wait_for()
            assert page.get_by_role("link", name="Add or open your candidate workspace").is_visible()
            organization_onboarding_url = page.url
            organization_id = organization_onboarding_url.split("organization_id=", 1)[1]
            page.goto(organization_onboarding_url)
            page.get_by_role("link", name="Manage jobs for this organization").click()
            page.wait_for_url("**/organization/jobs?organization_id=*")
            page.locator("#title").fill("Browser Test Engineer")
            page.locator("#description").fill("Build reliable systems that help teams hire with confidence.")
            api_design = page.locator(".requirement").filter(has=page.locator('[data-code="fastapi.api_design"]'))
            api_design.locator('[data-code="fastapi.api_design"]').check()
            api_design.locator("select").first.select_option("mid")
            api_design.locator('[data-method="deterministic_test"]').check()
            page.locator("#save").click()
            page.wait_for_function(
                "document.querySelector('#message').textContent.includes('Job draft saved') || document.querySelector('#message').dataset.kind === 'error'",
                timeout=10000,
            )
            assert page.locator("#message").inner_text() == "Job draft saved.", page.locator("#message").inner_text()
            page.get_by_role("button", name="Publish job").click()
            page.get_by_role("status").filter(has_text="Job published").wait_for()
            organization_jobs_url = page.url
            organization_id = organization_jobs_url.split("organization_id=", 1)[1]
            page.get_by_role("link", name="Assessments").click()
            page.get_by_role("heading", name="Assessment library").wait_for()
            page.locator("#title").fill("Browser Python Exercise")
            page.locator("#role").fill("Backend Engineer")
            page.locator("#competencies").fill("FastAPI API design | mid\nRequest validation | mid")
            page.get_by_label("Stable catalog competency for FastAPI API design").select_option(label="FastAPI API design")
            page.get_by_label("Stable catalog competency for Request validation").select_option(label="Request validation")
            page.locator("#instructions").fill("Implement normalize(value) to trim whitespace and lowercase the text. Explain one edge case you would test.")
            page.locator("#rubric").fill("FastAPI API design | Correctness | Meets the task requirements | 60\nRequest validation | Communication | Explains tradeoffs and verification | 40")
            page.locator('input[name="tool"][value="test_runner"]').check()
            page.locator("#python-tests").fill(json.dumps({
                "entrypoint": "normalize",
                "cases": [
                    {"competency_code": "fastapi.api_design", "arguments": {"value": " API "}, "expected": "api"},
                    {"competency_code": "fastapi.input_validation", "arguments": {"value": "Safe "}, "expected": "safe"},
                ],
            }))
            page.get_by_role("button", name="Save draft").click()
            page.get_by_role("status").filter(has_text="Assessment draft saved").wait_for()
            page.get_by_role("heading", name="Browser Python Exercise", exact=True).wait_for()
            review_confirmation = page.locator(".review-confirmation")
            review_confirmation.focus()
            page.keyboard.press("Space")
            assert review_confirmation.is_checked()
            page.get_by_role("button", name="Publish reviewed version").click()
            page.get_by_role("status").filter(has_text="Assessment version published and frozen").wait_for()
            page.get_by_text("Published version 1 is frozen.").wait_for()
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.goto(f"{base_url}/jobs")
            page.get_by_role("heading", name="Browser Test Engineer", exact=True).wait_for()
            page.get_by_text("Evidence requirements: FastAPI API design · mid", exact=True).wait_for()
            page.get_by_role("button", name="Save job").click()
            page.get_by_role("status").filter(has_text="1 saved").wait_for()
            page.get_by_role("link", name="Browser Test Engineer", exact=True).first.click()
            page.get_by_role("heading", name="Browser Test Engineer", exact=True).wait_for()
            page.get_by_text("FastAPI API design · mid · deterministic test · evidence no older than 365 days", exact=True).wait_for()
            job_url = page.url
            job_id = job_url.rstrip("/").split("/")[-1]

            applicant_context = browser.new_context(viewport={"width": 1280, "height": 900})
            applicant_page = applicant_context.new_page()
            applicant_email = "browser-applicant@example.com"
            applicant_page.goto(f"{base_url}/signup?purpose=candidate")
            applicant_page.locator("#submit").wait_for()
            applicant_page.wait_for_function("document.querySelector('#submit').disabled === false")
            applicant_page.locator("#full-name").fill("Browser Applicant")
            applicant_page.locator("#email").fill(applicant_email)
            applicant_page.locator("#password").fill("browser applicant secure password")
            applicant_page.locator("#terms").check()
            applicant_page.locator("#submit").click()
            applicant_page.get_by_role("status").filter(has_text="Request received").wait_for()
            applicant_verify = json.loads(mailbox.read_text(encoding="utf-8").splitlines()[-1])
            applicant_page.goto(applicant_verify["verification_link"])
            applicant_page.get_by_role("status").filter(has_text="Your email is verified").wait_for()
            applicant_page.get_by_role("link", name="Continue to sign in").click()
            applicant_page.locator("#email").fill(applicant_email)
            applicant_page.locator("#password").fill("browser applicant secure password")
            applicant_page.locator("#submit").click()
            applicant_page.wait_for_url("**/candidate/onboarding")
            applicant_page.locator("#full-name").fill("Browser Applicant")
            applicant_page.locator("#target-roles").fill("Backend Engineer")
            applicant_page.locator("#save").click()
            applicant_page.get_by_role("status").filter(has_text="Your draft is saved").wait_for()
            applicant_page.locator("#complete").click()
            applicant_page.get_by_role("status").filter(has_text="Your candidate setup is complete").wait_for()
            applicant_page.goto(f"{base_url}/projects")
            applicant_page.locator("#public-handle").fill("browser-applicant")
            applicant_page.locator("#save-handle").click()
            applicant_page.get_by_role("status").filter(has_text="Public profile address saved").wait_for(timeout=10000)
            applicant_page.locator("#portfolio-consent").check()
            applicant_page.locator("#publish-portfolio").click()
            applicant_page.get_by_role("status").filter(has_text="Your portfolio is public").wait_for()
            assert applicant_page.locator("#discovery-enabled").is_checked() is False
            applicant_page.locator("#discovery-enabled").check()
            applicant_page.locator("#save-discovery").click()
            applicant_page.get_by_role("status").filter(has_text="searchable by eligible hiring teams").wait_for()
            page.goto(f"{base_url}/organization/candidates?organization_id={organization_id}")
            page.locator("#query").fill("Browser Applicant")
            page.locator("#search-button").click()
            page.locator(".candidate").get_by_role("heading", name="Browser Applicant", exact=True).wait_for()
            candidate_profile = page.locator(".candidate").get_by_role("link", name="View public profile")
            assert candidate_profile.get_attribute("href") == "/u/browser-applicant"
            assert applicant_email not in page.locator("body").inner_text()
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.locator("#query").press("Enter")
            page.locator(".candidate").get_by_role("heading", name="Browser Applicant", exact=True).wait_for()
            page.set_viewport_size({"width": 1280, "height": 900})
            applicant_page.goto(job_url)
            applicant_page.get_by_role("heading", name="Apply for this role").wait_for()
            consent = applicant_page.locator("#portfolio-consent")
            consent.wait_for(state="visible")
            consent.focus()
            applicant_page.keyboard.press("Space")
            assert consent.is_checked()
            applicant_page.keyboard.press("Space")
            assert not consent.is_checked()
            applicant_page.keyboard.press("Space")
            assert consent.is_checked()
            applicant_page.locator("#cover-note").fill("I have delivered dependable software for real teams.")
            applicant_page.locator("#apply").click()
            applicant_page.get_by_role("status").filter(has_text="Your application was submitted").wait_for()
            applicant_page.get_by_role("link", name="Track my applications").click()
            applicant_page.get_by_role("heading", name="My applications").wait_for()
            applicant_page.get_by_text("submitted", exact=True).wait_for()
            applicant_page.get_by_role("button", name="View submitted snapshot").click()
            applicant_page.get_by_role("heading", name="Browser Applicant", exact=True).wait_for()
            applicant_page.set_viewport_size({"width": 390, "height": 844})
            assert applicant_page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

            page.goto(f"{base_url}/organization/applications?organization_id={organization_id}&job_id={job_id}")
            page.get_by_role("heading", name="Applications · Browser Test Engineer").wait_for()
            page.get_by_role("button", name="Review application").click()
            page.get_by_role("heading", name="Browser Applicant", exact=True).wait_for()
            page.get_by_text("FastAPI API design · mid · deterministic_test · 365 day freshness", exact=True).wait_for()
            page.locator("#reviewer-choice").select_option(label="Browser Test Updated · owner")
            page.get_by_role("button", name="Assign reviewer").click()
            page.get_by_role("status").filter(has_text="Application reviewer assigned").wait_for()
            page.get_by_role("button", name="Review application").click()
            page.wait_for_function("document.querySelector('#reviewer-summary').textContent.includes('Assigned to')", timeout=5000)
            assert "Assigned to" in page.locator("#reviewer-summary").inner_text()
            page.get_by_role("button", name="View evidence report").click()
            page.wait_for_function(
                "document.querySelector('#evidence-report').hidden === false || document.querySelector('#message').dataset.kind === 'error'",
                timeout=10000,
            )
            assert page.locator("#evidence-report").is_visible(), f"Evidence report failed: {page.locator('#message').inner_text()}"
            page.get_by_text("Candidate sharing status: not_shared.", exact=True).wait_for()
            page.get_by_text("FastAPI API design · unverified", exact=True).wait_for()
            page.get_by_text("Focused assessment recommendation: Browser Python Exercise · version 1", exact=False).wait_for()
            page.get_by_role("button", name="Move to in review").click()
            page.get_by_role("status").filter(has_text="Application stage updated").wait_for()
            page.get_by_role("button", name="View evidence report").click()
            page.get_by_role("button", name="Use recommended assessment").click()
            assert page.locator("#assessment-choice").evaluate("element => element.selectedOptions[0].textContent") == "Browser Python Exercise · version 1"
            page.get_by_role("button", name="Invite to assessment").click()
            page.get_by_role("status").filter(has_text="Assessment invitation sent").wait_for()
            page.locator("#application-assessment-invitations").get_by_text("issued").wait_for()

            applicant_page.reload()
            applicant_page.get_by_role("heading", name="Assessment invitations").wait_for()
            applicant_page.get_by_role("heading", name="Browser Python Exercise", exact=True).wait_for()
            applicant_page.get_by_role("button", name="View assessment briefing").click()
            applicant_page.get_by_text("FastAPI API design · mid · catalog linked", exact=True).wait_for()
            applicant_page.get_by_text("Implement normalize(value)", exact=False).wait_for()
            invitation_consent = applicant_page.locator("input[id^='assessment-consent-']")
            invitation_consent.focus()
            applicant_page.keyboard.press("Space")
            assert invitation_consent.is_checked()
            applicant_page.get_by_role("button", name="Accept invitation").click()
            applicant_page.get_by_role("status").filter(has_text="Assessment invitation accepted").wait_for()
            applicant_page.get_by_role("button", name="View assessment briefing").click()
            applicant_page.get_by_text("Creating an attempt does not start a session", exact=False).wait_for()
            applicant_page.get_by_role("button", name="Create assessment attempt").click()
            applicant_page.get_by_role("status").filter(has_text="Assessment attempt created. No session has started.").wait_for()
            applicant_page.get_by_role("button", name="View assessment briefing").click()
            applicant_page.locator("#assessment-invitations").get_by_text("Assessment attempt created", exact=False).wait_for()
            attempt_consent = applicant_page.locator("input[id^='attempt-consent-']")
            assert not attempt_consent.is_checked()
            consent_button = applicant_page.get_by_role("button", name="Give assessment consent")
            assert consent_button.is_disabled()
            attempt_consent.check()
            consent_button.click()
            applicant_page.get_by_role("status").filter(has_text="Assessment consent recorded. No session has started.").wait_for()
            applicant_page.get_by_role("button", name="View assessment briefing").click()
            applicant_page.get_by_text("Opening the workspace starts your assessment", exact=False).wait_for()
            applicant_page.get_by_role("button", name="Revoke assessment consent").click()
            applicant_page.get_by_role("status").filter(has_text="Assessment consent revoked.").wait_for()
            applicant_page.get_by_role("button", name="View assessment briefing").click()
            applicant_page.locator("input[id^='attempt-consent-']").check()
            applicant_page.get_by_role("button", name="Give assessment consent").click()
            applicant_page.get_by_role("status").filter(has_text="Assessment consent recorded. No session has started.").wait_for()
            applicant_page.get_by_role("button", name="View assessment briefing").click()
            applicant_page.get_by_role("link", name="Open Python assessment workspace").click()
            applicant_page.get_by_role("heading", name="Browser Python Exercise", exact=True).wait_for()
            applicant_page.locator("#source").fill("def normalize(value):\n    return value.strip().lower()\n")
            applicant_page.get_by_role("button", name="Save and run private tests").click()
            applicant_page.get_by_text("All private deterministic cases passed.", exact=False).wait_for(timeout=20000)
            assert applicant_page.locator(".result").count() == 2
            assert "python_test_suite" not in applicant_page.locator("body").inner_text()
            applicant_page.get_by_role("link", name="My applications").click()
            applicant_page.get_by_role("heading", name="My applications").wait_for()
            applicant_page.get_by_role("button", name="Review evidence and sharing").click()
            applicant_page.locator(".evidence-choice").filter(has_text="FastAPI API design").get_by_role("checkbox").check()
            applicant_page.get_by_label("Consent to share selected evidence").check()
            applicant_page.get_by_role("button", name="Share selected evidence").click()
            applicant_page.get_by_role("status").filter(has_text="Evidence sharing updated for this application").wait_for()
            applicant_page.set_viewport_size({"width": 390, "height": 844})
            assert applicant_page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

            page.reload()
            page.get_by_role("heading", name="Applications · Browser Test Engineer").wait_for()
            assert page.evaluate("fetch('/api/v1/auth/session').then(response => response.status)") == 200
            page.get_by_role("button", name="Review application").click()
            page.locator("#application-assessment-invitations").get_by_text("accepted").wait_for()
            page.get_by_role("button", name="View evidence report").click()
            page.get_by_text("FastAPI API design · demonstrated", exact=True).wait_for()
            page.get_by_text("1/1 deterministic cases", exact=False).wait_for()
            page.get_by_text("No follow-up assessment is recommended for the demonstrated requirements.", exact=True).wait_for()
            page.get_by_role("button", name="Move to interview").click()
            page.get_by_role("status").filter(has_text="Application stage updated").wait_for()
            first_interview_time = page.evaluate("""() => {
              const date = new Date(Date.now() + 2 * 24 * 60 * 60 * 1000);
              const pad = value => String(value).padStart(2, "0");
              return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
            }""")
            page.locator("#interview-time").fill(first_interview_time)
            page.locator("#interview-link").fill("https://meet.example.com/browser-interview")
            page.locator("#interview-note").fill("We will discuss your portfolio and project choices.")
            page.get_by_role("button", name="Schedule interview").click()
            page.get_by_role("status").filter(has_text="Interview scheduled").wait_for()
            applicant_page.reload()
            applicant_page.get_by_role("heading", name="Interview schedules").wait_for()
            applicant_page.locator("#notifications").get_by_text("Interview scheduled", exact=True).wait_for()
            applicant_page.locator("#notifications").get_by_role("button", name="Mark as read").click()
            applicant_page.get_by_text("You’re up to date.", exact=True).wait_for()
            applicant_page.get_by_text("Your response: pending", exact=True).wait_for()
            applicant_page.get_by_role("link", name="Open meeting link").wait_for()
            applicant_page.locator("#interviews input").fill("I have class at that time.")
            applicant_page.get_by_role("button", name="Request another time").click()
            applicant_page.get_by_role("status").filter(has_text="A new time was requested").wait_for()
            page.reload()
            page.get_by_role("heading", name="Applications · Browser Test Engineer").wait_for()
            page.locator("#team-notifications").get_by_text("Interview response received", exact=True).wait_for()
            page.get_by_role("button", name="Review application").click()
            page.get_by_text("Candidate response: reschedule requested · I have class at that time.", exact=True).wait_for()
            second_interview_time = page.evaluate("""() => {
              const date = new Date(Date.now() + 3 * 24 * 60 * 60 * 1000);
              const pad = value => String(value).padStart(2, "0");
              return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
            }""")
            page.locator("#interview-list input[type='datetime-local']").fill(second_interview_time)
            page.get_by_role("button", name="Change time").click()
            page.get_by_role("status").filter(has_text="Interview time updated").wait_for()
            applicant_page.reload()
            applicant_page.locator("#notifications").get_by_text("Interview time updated", exact=True).wait_for()
            applicant_page.get_by_text("Your response: pending", exact=True).wait_for()
            applicant_page.get_by_role("button", name="Accept interview").click()
            applicant_page.get_by_role("status").filter(has_text="Interview accepted").wait_for()
            page.reload()
            page.get_by_role("heading", name="Applications · Browser Test Engineer").wait_for()
            page.get_by_role("button", name="Review application").click()
            page.get_by_text("Candidate response: accepted", exact=True).wait_for()
            applicant_page.get_by_role("button", name="Review evidence and sharing").click()
            applicant_page.get_by_role("button", name="Revoke evidence sharing").click()
            applicant_page.get_by_role("status").filter(has_text="Evidence sharing revoked for this application").wait_for()
            page.get_by_role("button", name="View evidence report").click()
            page.get_by_text("Candidate sharing status: revoked.", exact=True).wait_for()
            page.get_by_text("FastAPI API design · unverified", exact=True).wait_for()
            page.locator("#decision-reason").fill("The team closed this role before an assessment session could start.")
            page.get_by_role("button", name="Record rejection").click()
            page.wait_for_function(
                "document.querySelector('#message').textContent.includes('Human decision recorded') || document.querySelector('#message').dataset.kind === 'error'",
                timeout=10000,
            )
            assert page.locator("#message").inner_text() == "Human decision recorded: rejected."
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            applicant_page.reload()
            applicant_page.get_by_text("rejected", exact=True).wait_for()
            applicant_page.locator("#notifications").get_by_text("Interview cancelled", exact=True).wait_for()
            applicant_page.get_by_text("Cancelled: The application reached a final hiring decision.", exact=True).wait_for()
            applicant_page.get_by_role("button", name="View assessment briefing").click()
            applicant_page.locator("#assessment-invitations").get_by_text("Assessment attempt cancelled. No further code runs are accepted.").wait_for()

            page.goto(organization_jobs_url)
            page.get_by_role("button", name="Close job").click()
            page.get_by_role("status").filter(has_text="Job closed").wait_for()
            page.goto(f"{base_url}/jobs")
            page.get_by_text("Closed", exact=True).wait_for()
            page.locator(".saved-job").get_by_role("button", name="Remove").click()
            page.get_by_role("status").filter(has_text="You have no saved jobs").wait_for()
            page.goto(organization_onboarding_url)
            page.locator("#invite-email").fill("browser-invitee@example.com")
            page.locator("#invite-role").select_option("recruiter")
            page.locator("#send-invitation").click()
            page.get_by_role("status").filter(has_text="Invitation sent").wait_for()
            invite_message = json.loads(mailbox.read_text(encoding="utf-8").splitlines()[-1])
            invite_context = browser.new_context()
            invite_page = invite_context.new_page()
            invite_page.goto(f"{base_url}/signup?purpose=employer")
            invite_page.locator("#submit").wait_for()
            invite_page.wait_for_function("document.querySelector('#submit').disabled === false")
            invite_page.locator("#full-name").fill("Browser Invitee")
            invite_page.locator("#email").fill("browser-invitee@example.com")
            invite_page.locator("#password").fill("browser invitee secure password")
            invite_page.locator("#terms").check()
            invite_page.locator("#submit").click()
            invite_page.get_by_role("status").filter(has_text="Request received").wait_for()
            verify_message = json.loads(mailbox.read_text(encoding="utf-8").splitlines()[-1])
            invite_page.goto(verify_message["verification_link"])
            invite_page.get_by_role("status").filter(has_text="Your email is verified").wait_for()
            invite_page.get_by_role("link", name="Continue to sign in").click()
            invite_page.locator("#email").fill("browser-invitee@example.com")
            invite_page.locator("#password").fill("browser invitee secure password")
            invite_page.locator("#submit").click()
            invite_page.wait_for_url("**/organization/onboarding")
            invite_page.goto(invite_message["organization_invitation_link"])
            invite_page.wait_for_function("document.querySelector('#accept').disabled === false")
            invite_page.locator("#accept").click()
            invite_page.get_by_role("status").filter(has_text="You joined Proofpath Browser Labs as recruiter").wait_for()
            assert "#token=" not in invite_page.url
            page.reload()
            page.locator("#members-list").get_by_text("Browser Invitee · recruiter").wait_for()
            page.get_by_label("Role for Browser Invitee").select_option("member")
            invitee_row = page.locator("#members-list > div").filter(has_text="Browser Invitee")
            invitee_row.get_by_role("button", name="Save role").click()
            page.get_by_role("status").filter(has_text="Member role updated").wait_for()
            page.locator("#members-list").get_by_text("Browser Invitee · member").wait_for()
            page.once("dialog", lambda dialog: dialog.accept())
            invitee_row = page.locator("#members-list > div").filter(has_text="Browser Invitee")
            invitee_row.get_by_role("button", name="Remove").click()
            page.get_by_role("status").filter(has_text="Member removed").wait_for()
            assert page.locator("#members-list").get_by_text("Browser Invitee").count() == 0
            assert invite_page.evaluate("fetch('/api/v1/me/workspaces').then(async response => (await response.json()).workspaces.length)") == 0
            invite_context.close()
            applicant_context.close()
            page.locator("#logout").click()
            page.get_by_role("status").filter(has_text="You are signed out").wait_for()
            browser.close()
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=10)
        with connect_database(TEST_DATABASE_URL) as connection:
            connection.execute("TRUNCATE audit_events, auth_sessions, email_verification_tokens, rate_limit_buckets, users CASCADE")
