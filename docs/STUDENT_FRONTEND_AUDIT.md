# Student frontend audit

Audit of the supplied student-experience brief against the current Next.js routes and components. This is an implementation inventory, not an end-to-end verification report.

## Working today

- `/`, `/signup`, `/login`, and `/candidate/onboarding` provide the public, authentication, and onboarding entry points. Onboarding collects a limited private profile draft; it does not cover the brief's education, links, image, or first-project options.
- `/candidate/dashboard`, `/projects`, `/skills`, `/jobs`, `/jobs/:id`, `/applications`, `/profile`, and `/settings` use the native React workspace and shared profile, project, job, and application data. The demo routes use browser-local data and label example activity.
- Projects can be searched, filtered by visibility, and created or edited. Skills display linked project evidence. Jobs can be searched/filtered/saved; the detail flow collects consent before sharing a published portfolio with an application.
- Portfolio customization, public profile rendering, detailed project publishing/source-link controls, assessment submissions, and interview responses already have API-backed legacy screens/routes. Keep and reuse them rather than building replacements.
- Dialogs, mobile navigation, reduced-motion handling, and API error paths already have shared implementations.

## Gaps to resolve, in dependency order

1. **Surface the existing student routes.** The workspace now links directly to My Portfolio. Student assessment and interview tasks remain in the existing application/assessment screens, but are not yet surfaced in primary navigation. Keep employer routes out of this phase.
2. **Use real notification data for real accounts.** The workspace activity dialog now loads the authenticated notification list, supports read and read-all actions, and shows explicit local-example activity in demo mode. The API owns real notifications.
3. **Complete the student portfolio journey.** The API supports portfolio configuration, publication, versions, and entitlements, but the main native workspace has no portfolio overview/preview entry. Connect the existing builder, preview, and published-profile pages in one student path.
4. **Broaden the profile and onboarding forms to supported candidate data.** The native `Profile` type only carries name, target roles, skills, status, and version; students cannot manage education, experience, professional links, certifications, achievements, or resume here. Extend only fields supported by existing API contracts.
5. **Close project-management gaps in the native collection.** Current cards/editor focus on description, contribution, skills, and visibility; sorting, status, links, preview, archive, and duplicate actions are not surfaced there. Reuse existing source-link and publishing screens and don't invent unsupported backend behavior.
6. **Connect dashboard content to real actionable state.** Add saved roles, portfolio status, and available assessment/interview actions where the API supplies them; keep empty states useful and never imply employer activity that is not in data.

## Delivery order

Navigation and accurate notifications → portfolio overview/preview integration → profile/onboarding fields supported by API → project actions and journey checks. For each slice, preserve local-example labels, private defaults, consent boundaries, accessibility, and recovery states. The configured API and database must be running for real-account end-to-end checks.
