# Proofpath

Proofpath helps students and early-career professionals present their skills through a profile, project evidence, and a shareable portfolio.

## Product status

The current frontend and FastAPI service support account registration and sessions, candidate profiles, PDF/DOCX/pasted-text resume intake with editable heuristic suggestions, project CRUD, competency claims marked as self-reported, portfolio publication, public profiles, portfolio customization/version history, privacy controls, and account-data export. Resume uploads are private and replaceable; demo resume edits stay in the browser and are labeled as examples. Resume parsing uses deterministic extraction rather than an AI provider, so suggestions need user review before they are added to a profile.

Proofpath is not a hiring marketplace. Job boards, applications, employer workspaces, interview workflows, and Project Defense are removed from the live application. Existing PostgreSQL migration history and legacy account-export data are preserved; no migration or stored data was deleted.

Resume intake and review are live. Assessment generation, sessions, AI evaluation, and assessment-progress history are not yet user workflows. The isolated runner and deterministic evaluator are retained as backend building blocks and are not yet connected to the product UI.

## Architecture

- `candidate_api` — FastAPI identity, candidate profile, resume intake, projects, portfolios, account security, and PostgreSQL migrations.
- `assessment_runner` — isolated execution of candidate-provided code.
- `assessment_evaluator` — deterministic evaluation of assessment test cases.
- `frontend` — the single Next.js/React application, including the example workspace.

`candidate_api` runs as the HTTP API. `assessment_runner` and `assessment_evaluator` are focused Python packages, not separate deployed servers. The product runs as one API process, one frontend process, and PostgreSQL.

## Local development

1. Install Python 3.10+ and PostgreSQL. Create separate development and test databases.
2. Copy `.env.example` to `.env` and set `DATABASE_URL` for the development database.
3. Install backend dependencies from the repository root: `python -m pip install -r requirements-dev.txt`.
4. Run the API: `python -m uvicorn candidate_api.app.main:app --reload`.
5. In another terminal, run the frontend:

   ```powershell
   cd frontend
   npm install
   npm run dev
   ```

Open `http://localhost:3001`. API docs are at `http://127.0.0.1:8000/docs`. Configure `PROOFPATH_API_ORIGIN` if the API uses a different origin. Local verification and password-reset messages are written to ignored `candidate_api/data/dev-mailbox.jsonl`.

## Verification

- Frontend unit tests: `cd frontend && npm test`
- Frontend production build and TypeScript check: `cd frontend && npm run build`
- Backend tests: set `PROOFPATH_TEST_DATABASE_URL` to a separate database whose name ends in `_test`, then run `python -m pytest candidate_api/tests assessment_runner/tests assessment_evaluator/tests`.

Use only a dedicated test database: backend tests truncate test tables. Local terms configuration in `.env.example` is preview-only and must be replaced before any public deployment.
