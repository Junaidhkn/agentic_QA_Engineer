# Sub-processors

> **Draft for legal review — not yet in force.** Version 2026-10-25.

| Sub-processor | Purpose | Data | Location |
|---|---|---|---|
| Amazon Web Services | Hosting, database, storage, execution plane, email (SES), key management | All Customer Data | United States (us-east-1; backups us-west-2) |
| Anthropic | AI models for discovery, test generation, diagnosis and repair | Code excerpts, test results and evidence sent in prompts | United States |
| Clerk | Sign-in and multi-factor authentication | Member name, email, sign-in events | United States |
| Functional Software (Sentry) | Error tracking for our own services | Error reports (scrubbed of request bodies and secrets) | United States |
| GitHub | Repository access and pull requests — only when you install our GitHub App | Repository contents you grant access to | United States |

We notify organization owners 30 days before adding or replacing a sub-processor.
