# Proofpath

Proofpath is a skills-first hiring platform backed by FastAPI and PostgreSQL. Candidates can create a free public profile at `/u/{handle}`, optionally make it discoverable to eligible hiring teams, download a reauthenticated copy of their account data, share selected projects, customize a premium portfolio at `/app/portfolio/builder`, find jobs, and complete focused practical assessments. Hiring teams review consented evidence and make human decisions. Billing is not configured; Pro access currently depends on trusted server-side entitlement grants. See `docs/IMPLEMENTATION_PROGRESS.md` for the verified audit and roadmap.

## Local setup

1. Install Python 3.10+ and PostgreSQL. Create a dedicated application role and separate development and test databases; do not connect the app as a PostgreSQL superuser.
2. Copy `.env.example` to `.env` and set `DATABASE_URL` for the development database.
3. Install dependencies: `python -m pip install -r requirements-dev.txt`.
4. Start the API in one terminal: `python -m uvicorn backend.app.main:app --reload`.
5. Install Node.js 20.9+ and, in a second terminal, start the Next.js interface:

   ```powershell
   cd frontend
   npm install
   npm run dev
   ```

Open `http://localhost:3001`. The Next.js frontend includes a responsive homepage, registration and sign-in, candidate onboarding, dashboard, projects, skills and evidence, opportunity search, job applications, profile, and settings. Open `/explore` to try the complete example workspace without the API: project and profile edits, saved roles, and example applications persist in this browser. Example roles and activity are explicitly labeled; nothing is sent to employers.

Real account routes use the existing FastAPI API at `http://127.0.0.1:8000`; set `PROOFPATH_API_ORIGIN` if the API runs elsewhere. The specialized employer, assessment, account security, and publishing controls retain their existing API-backed screens. `/manage/projects` opens source links, evidence claims, publishing, and discovery controls; `/manage/applications` opens assessment invitations, evidence sharing, and withdrawals. The API can also serve the older workflow pages directly at port 8000. The local email sink writes verification links to `backend/data/dev-mailbox.jsonl`, which is ignored by Git. This checkout uses a local-preview-only terms label and example URL; replace them with approved terms before any public deployment.

The interface uses React, TypeScript, and local CSS with no additional UI dependencies. Shared icons, artwork, dialogs, and workspace views live in `frontend/app/components`; API handling and typed example data live in `frontend/app/lib`. Native dialogs provide keyboard focus containment, the mobile menu supports Escape and focus restoration, and reduced-motion preferences are respected. Project artwork is rendered locally with CSS, so the interface does not rely on remote image or font services.

Run `npm test` in `frontend` for the filtering, evidence association, browser-state recovery, and API/CSRF tests. Run `npm run build` for the production build and TypeScript validation. Real registration, application delivery, and private account workflows require a running configured API and database.

The API uses `/api/v1`; FastAPI's OpenAPI document is at `/openapi.json` and the interactive reference is at `/docs` (both also pass through Next.js). Free profile publishing does not require a paid plan. Portfolio customization is checked by the API against server-owned entitlements; no checkout or live payment flow is enabled.

## Verification

Set `PROOFPATH_TEST_DATABASE_URL` to the isolated test database, then run `.venv/Scripts/python.exe -m pytest` on Windows or `python -m pytest` elsewhere. The suite covers PostgreSQL persistence, validation, token expiry/replay, session security, CSRF, rate limiting, tenant and role authorization, private project CRUD, free and Pro portfolio publication/version history, opt-in employer discovery, application consent, reviewer assignment, immutable assessment publication, invitation and attempt state, isolated test execution, evidence provenance and privacy, gap matching, assessment recommendations, and evidence-sharing revocation. Chromium tests cover signup, the portfolio builder and downgrade fallback, plus the candidate-to-employer discovery and assessment/evidence journeys. Install the Playwright Chromium browser once with `python -m playwright install chromium`.

See [implementation progress](docs/IMPLEMENTATION_PROGRESS.md), [API status matrix](docs/API_STATUS.tsv), and [ADR 0001](docs/adr/0001-fastapi-postgresql-modular-monolith.md) for scope, evidence, constraints, and the next vertical slices.
