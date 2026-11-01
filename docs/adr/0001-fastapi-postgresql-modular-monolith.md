# ADR 0001: FastAPI modular monolith with PostgreSQL

**Status:** Accepted for the first implementation stages  
**Date:** 2026-10-30

## Decision

Use one Python/FastAPI application with domain modules and a PostgreSQL source of truth. Use Psycopg 3 with explicit SQL migrations and transactions for the first slices; do not introduce an ORM, Redis, workers, or microservices until an implemented workflow needs them. Keep request/response DTOs strict and publish the running contract through FastAPI OpenAPI.

Use a single `users` identity with a candidate profile and zero or more organization memberships. `initial_purpose` records the first onboarding route; it is not a global authorization role. Organization roles and resource assignments remain tenant scoped.

Use opaque random server sessions. Store only session-token and CSRF-token hashes in PostgreSQL. Send the session in a `Secure`, `HttpOnly`, `SameSite=Lax`, `__Host-` cookie and require an explicit CSRF header for authenticated mutations. Hash passwords with a reviewed password-hashing scheme and verification/reset tokens before persistence. Do not put secrets in logs.

Keep candidate artifacts private in object storage when that capability is built. Execute candidate code only in a separately isolated sandbox with hard resource and network limits. AI receives explicit, permission-scoped inputs; deterministic test results, AI drafts, human notes, and final human decisions remain separate records.

## Consequences and constraints

- PostgreSQL migrations and transaction tests are required before each database-backed slice is accepted.
- A local development database is isolated from the existing user databases. The currently installed PostgreSQL cluster trusts loopback clients, so it is only a local test target and must not be exposed as a production configuration.
- No production SMTP, OAuth, object-storage, LiveKit, sandbox, or AI credentials are present. Integrations stay disabled or use clearly labeled development-only adapters until configured.
- Approved Terms of Service content is not in the repository. Registration may record only a configured, displayed version; do not claim legal acceptance until the corresponding text is available to the user.
- Prefer database-backed correctness first. Add a queue, cache, or external service only when a completed user journey requires it.
