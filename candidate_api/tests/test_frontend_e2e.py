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

from candidate_api.app.db import connect_database

PROJECT_ROOT = Path(__file__).resolve().parents[2]
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
        [sys.executable, "-m", "uvicorn", "candidate_api.app.main:app", "--host", "127.0.0.1", "--port", str(port)],
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
            page.get_by_role("link", name="Projects", exact=True).click()
            page.locator("#projects-list").wait_for()
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
