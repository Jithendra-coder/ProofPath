# Routes

All requests are matched by `frontend/app/[[...segments]]/page.tsx`.

| URL | Source HTML / screen | Notes |
|---|---|---|
| `/` | `frontend/landing.html` | Marketing and workflow overview |
| `/signup` | `frontend/signup.html` | Candidate or employer registration |
| `/login` | `frontend/login.html` | Sign-in and logout |
| `/security/sessions` | `frontend/security-sessions.html` | Session management |
| `/verify-email` | `frontend/verify-email.html` | Email verification |
| `/candidate/onboarding` | `frontend/candidate-onboarding.html` | Candidate profile setup |
| `/candidate/dashboard` | `frontend/candidate-dashboard.html` | Candidate summary |
| `/projects` | `frontend/projects.html` | Candidate projects and privacy controls |
| `/jobs` | `frontend/jobs.html` | Published jobs |
| `/jobs/:id` | `frontend/job-detail.html` | Job details and application |
| `/applications` | `frontend/applications.html` | Applications and assessment invitations |
| `/organization/onboarding` | `frontend/organization-onboarding.html` | Organization setup and job workspace |
| `/organization/jobs` | `frontend/organization-jobs.html` | Employer job management |
| `/organization/applications` | `frontend/organization-applications.html` | Employer application review |
| `/organization/assessments` | `frontend/organization-assessments.html` | Assessment authoring and publication |
| `/organization/invitation` | `frontend/organization-invitation.html` | Invitation acceptance |
| `/portfolio/:handle` | `frontend/public-portfolio.html` | Public portfolio page |
| `/reset-password` | `frontend/reset-password.html` | Password reset |

The React route wrapper fetches the matched FastAPI HTML route through `/__legacy/*`; `/api/*` is proxied to FastAPI by `frontend/next.config.ts`.
