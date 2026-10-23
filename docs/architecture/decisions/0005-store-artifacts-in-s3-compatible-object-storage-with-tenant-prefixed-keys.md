---
status: accepted
date: 2026-10-23
decision-makers: [user, claude]
tags: [storage, security]
supersedes: 
superseded-by:
---

# ADR-0005: Store artifacts in S3-compatible object storage with tenant-prefixed keys

## Context and problem statement

Runs produce large artifacts: k6 raw output, Playwright traces/videos/HAR, screenshots,
logs, code snapshots, test bundles, patches, scan reports, HTML reports. Sizes range from KB
to hundreds of MB. Some may contain secrets or source code (threat model A16). The brief
names S3-compatible storage. The execution plane must upload without holding storage
credentials (ADR-0008).

## Decision drivers

- Large binary objects; durability; lifecycle expiry (NFR-8).
- Tenant isolation (ADR-0003); least privilege for uploaders.
- Portability (S3 API; MinIO locally).

## Considered options

1. **One private bucket per environment, SSE-KMS, keys `org/<org>/project/<p>/<kind>/<id>/…`, access only via short-lived presigned URLs, lifecycle rules per prefix/kind**.
2. Bucket per tenant.
3. Store artifacts in Postgres (bytea / large objects).

## Decision outcome

Chosen option: "**Single private bucket with tenant-prefixed keys and presigned access**",
because presigned PUT/GET URLs minted by the control plane after authorization give
per-object least privilege to runners and browsers, while lifecycle rules enforce retention
classes. Keys are never user-supplied. Artifacts flagged `may_contain_secrets` require
engineer role to download; `source_code` snapshots expire ≤ 24 h. Downloads are served from
a separate artifacts origin with `Content-Disposition: attachment` (images inline).

### Confidence

Confirmed (S3-compatible storage from the brief); key/access design Recommended.

### Consequences

- Good: no storage credentials outside the control plane; simple retention.
- Bad / trade-off: bucket-per-tenant isolation not available; relies on correct key construction (centralized in `ObjectStore` adapter).
- Follow-up: redaction pass before upload for HAR/trace artifacts.

### Confirmation

Adapter unit tests for key construction; integration tests with MinIO; contract test that
presigned URLs are only issued after authz.

## Pros and cons of the options

### Single bucket + prefixes
- Good: simple, scalable, cheap.
- Bad: isolation by convention + IAM, not physical.

### Bucket per tenant
- Good: physical isolation, per-tenant policies.
- Bad: bucket quotas/limits, operational overhead for hundreds of tenants.

### Postgres storage
- Good: transactional with metadata.
- Bad: bloats DB, backups, cost.

## More information

data-model.md §6–7; threat-model.md A16.
