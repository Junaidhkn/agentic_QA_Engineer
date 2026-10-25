# Contributing

- **Design first.** Significant changes need an ADR (`docs/architecture/decisions/`). Never edit an
  accepted ADR's substance — supersede it. Keep `design.md` and `plan.md` current.
- **Traceability.** New modules/files should implement a requirement (FR/NFR) or ADR; reference it in
  the module docstring.
- **Module boundaries.** Modules talk through `service.py`; only a module's repository touches its
  tables. `lint-imports` enforces the critical boundaries (agents cannot import privileged modules).
- **Tenancy.** Every tenant table has `org_id` and an RLS policy; cross-tenant code uses
  `platform.db.system_tx()` and only from allowed modules.
- **Safety-critical code** (`admission`, `runs`, `runners`, `verification`, `integrations/github`,
  `targets`) needs a second reviewer.
- Run `make lint test` before pushing. Regenerate the API client with `make openapi` when routes change.
