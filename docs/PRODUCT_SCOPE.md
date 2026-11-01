# Proofpath product scope

**Product promise:** Showcase your skills. Demonstrate your work. Get discovered and get hired.

Proofpath helps students and early-career professionals create useful profiles, present projects and achievements, share work with consent, find opportunities, and complete job-relevant assessments. Hiring teams review the information candidates choose to share and make human decisions.

## Product boundaries

- Every candidate can publish a free, shareable profile at `/u/{handle}`. The profile uses a stable handle after its first publication.
- Candidate Free and Candidate Pro are commercial entitlements. Candidate/employer/admin permissions and organization membership are separate authorization rules.
- Pro pays for meaningful presentation control: curated templates, themes, section layout, responsive preview, and versioned publishing. It never buys search rank, assessment priority, a verified-skill label, or preferential hiring.
- Employer assessments are bounded work samples with reviewable evidence. Humans make hiring decisions.
- Candidate projects, evidence, and portfolio fields remain private until the candidate explicitly publishes or shares them.

## Non-goals

Proofpath is not a repository vulnerability scanner, code analysis platform, GitHub clone, universal certification service, autonomous hiring system, or AI interview demo. Custom domains, advanced analytics, and live billing are later work. AI is optional and cannot silently invent candidate claims or decide employment outcomes.

## Current implementation

Identity, free projects/profiles, organization workspaces, jobs, applications, consented evidence, and local practical assessment execution are implemented in the existing FastAPI/PostgreSQL application. The Pro builder and billing are not implemented. See [implementation progress](IMPLEMENTATION_PROGRESS.md) for the evidence-backed audit and test history.
