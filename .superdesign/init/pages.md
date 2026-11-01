# Page Dependency Trees

## `/` (home)
Entry: `frontend/app/[[...segments]]/page.tsx`

- `frontend/app/[[...segments]]/page.tsx`
  - `frontend/app/components/legacy-screen.tsx`
    - `frontend/app/components/ui.tsx`
  - `frontend/landing.html` (served by FastAPI and fetched via `/__legacy/`)
    - inline runtime Tailwind CDN config and page scripts
  - `frontend/app/globals.css`

## `/login`
Entry: `frontend/app/[[...segments]]/page.tsx`

- `frontend/app/[[...segments]]/page.tsx`
  - `frontend/app/components/legacy-screen.tsx`
    - `frontend/app/components/ui.tsx`
  - `frontend/login.html`
  - `frontend/auth.css`
  - `frontend/app/globals.css`

## `/candidate/dashboard`
Entry: `frontend/app/[[...segments]]/page.tsx`

- `frontend/app/[[...segments]]/page.tsx`
  - `frontend/app/components/legacy-screen.tsx`
    - `frontend/app/components/ui.tsx`
  - `frontend/candidate-dashboard.html` (page-specific CSS and dashboard API script)
  - `frontend/app/globals.css`

## `/projects`
Entry: `frontend/app/[[...segments]]/page.tsx`

- `frontend/app/[[...segments]]/page.tsx`
  - `frontend/app/components/legacy-screen.tsx`
    - `frontend/app/components/ui.tsx`
  - `frontend/projects.html` (page-specific CSS and project API scripts)
  - `frontend/app/globals.css`

## `/jobs`
Entry: `frontend/app/[[...segments]]/page.tsx`

- `frontend/app/[[...segments]]/page.tsx`
  - `frontend/app/components/legacy-screen.tsx`
    - `frontend/app/components/ui.tsx`
  - `frontend/jobs.html` (page-specific CSS and jobs API script)
  - `frontend/app/globals.css`

## `/applications`
Entry: `frontend/app/[[...segments]]/page.tsx`

- `frontend/app/[[...segments]]/page.tsx`
  - `frontend/app/components/legacy-screen.tsx`
    - `frontend/app/components/ui.tsx`
  - `frontend/applications.html` (page-specific CSS and application API script)
  - `frontend/app/globals.css`

## `/organization/jobs`
Entry: `frontend/app/[[...segments]]/page.tsx`

- `frontend/app/[[...segments]]/page.tsx`
  - `frontend/app/components/legacy-screen.tsx`
    - `frontend/app/components/ui.tsx`
  - `frontend/organization-jobs.html` (page-specific CSS and job API scripts)
  - `frontend/app/globals.css`

## `/organization/applications`
Entry: `frontend/app/[[...segments]]/page.tsx`

- `frontend/app/[[...segments]]/page.tsx`
  - `frontend/app/components/legacy-screen.tsx`
    - `frontend/app/components/ui.tsx`
  - `frontend/organization-applications.html` (page-specific CSS and application API scripts)
  - `frontend/app/globals.css`

## `/organization/assessments`
Entry: `frontend/app/[[...segments]]/page.tsx`

- `frontend/app/[[...segments]]/page.tsx`
  - `frontend/app/components/legacy-screen.tsx`
    - `frontend/app/components/ui.tsx`
  - `frontend/organization-assessments.html` (page-specific CSS and assessment API scripts)
  - `frontend/app/globals.css`
