# Database migrations

Alembic migration scaffolding is reserved for Phase 2 domain tables. Phase 1 intentionally creates no trading schema.

When migrations are added, they must be reviewed for:

- UTC timestamps
- append-only audit semantics
- idempotent startup behavior
- safe rollback expectations
- indexes for proposal, order, fill, and audit lookups

