# Portfolio plans and subscriptions

## Product policy

| Capability | Candidate Free | Candidate Pro |
| --- | --- | --- |
| Profile, skills, work history, and projects | Yes | Yes |
| Canonical public profile and basic sharing | Yes | Yes |
| Job discovery, applications, and assessments | Yes | Yes |
| Basic professional presentation | Yes | Yes |
| Curated templates and layout/theme controls | No | Yes |
| Reorderable sections, responsive preview, and published versions | No | Yes |
| Hiring priority, ranking boost, or preferential assessment access | Never | Never |

Prices, currency, trials, renewals, taxes, grace periods, and refunds are undecided and must not be invented in the UI or API.

## Verified current state

The Free profile and project publication workflow is backed by PostgreSQL and supports explicit publication/revocation. New share links use `/u/{handle}`; handles become immutable after first publication. The builder at `/app/portfolio/builder` supports four templates, three themes, section visibility/order, responsive previews, immutable versions, publication, and rollback.

Migration `023_portfolio_builder_entitlements.sql` adds `candidate_entitlement_grants` and `candidate_portfolio_versions`. The API treats an unexpired, unrevoked `portfolio_customization` grant as the sole source of the Pro builder privilege. A candidate identity, employer purpose, organization membership, and client fields such as `is_pro` do not grant it. At present grants can only be created by trusted server/database operations; there is no billing or user-facing upgrade flow.

## API and downgrade behavior

- `GET /api/v1/me/entitlements` returns the signed-in user's derived plan and customization entitlement.
- `GET /api/v1/me/portfolio/versions` returns only that user's immutable versions and remains available after downgrade.
- `POST /api/v1/me/portfolio/versions` requires verified email, CSRF, active Pro entitlement, strict design validation, and an optimistic draft version.
- `POST /api/v1/me/portfolio/publish` accepts an optional saved version ID. Publishing a premium design requires the Free profile to have already been published. A failed publication leaves the current live design unchanged; any saved version can be selected to roll back.
- Public portfolio responses expose the selected template, theme, and sections only while the owner has an active entitlement. After downgrade, the Free profile remains public with the basic layout; projects and design history are not deleted.

Free publication, job applications, assessment participation, and hiring decisions do not require Pro.

When a subscription ends, preserve all projects, drafts, and premium design settings. Render the basic Free profile publicly while the account is not entitled to premium presentation. Never delete portfolio content on downgrade.

## Payment release gate

The provider must remain replaceable. First implement sandbox/test mode with server-owned plan identifiers/prices, hosted checkout, raw-body signature verification, idempotent webhook processing, renewal/cancellation/expiry transitions, and account-scoped billing history. Use provider-confirmed subscription state as the entitlement source; redirects and browser claims are not proof of payment. Do not store card details. Live charging requires explicit business-owner approval after test-mode verification.

No provider, payment policy, or pricing is configured in this checkout. Billing remains deferred; no real or test charges are enabled.
