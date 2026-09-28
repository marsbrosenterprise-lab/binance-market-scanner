# Database migrations

Alembic migration scaffolding is reserved for Phase 2 domain tables. Phase 1 intentionally creates no trading schema.

When migrations are added, they must be reviewed for:

- Alembic revision identifiers must be no longer than 32 characters because
  `alembic_version.version_num` is `VARCHAR(32)`.

Migration `0008_safety_and_candle_environment.py` uses the internal revision
identifier `0008_safety_candle_env`. The filename is descriptive and does not
affect Alembic’s chain.

## Compatibility procedure for the rejected 0008 revision

The failed CI migration cannot record `0008_safety_and_candle_environment` in a
standard Alembic database because the `alembic_version` column rejects values
longer than 32 characters. PostgreSQL rolls back the migration transaction, so
the normal supported state remains at `0007_convert_execution_recovery`.

Before upgrading a database, inspect the current revision and take a backup:

```sql
SELECT version_num FROM alembic_version;
```

If it reports `0007_convert_execution_recovery`, run `alembic upgrade head`.
Do not stamp or delete migration records. If a non-standard database has a
widened `alembic_version` column and contains the rejected long value, stop and
have an operator verify that all 0008 schema objects exist and that the failed
transaction did not partially apply; only then may the operator perform a
reviewed metadata-only correction to `0008_safety_candle_env`. Never apply this
procedure to an ordinary database or use it to conceal a migration error.

- UTC timestamps
- append-only audit semantics
- idempotent startup behavior
- safe rollback expectations
- indexes for proposal, order, fill, and audit lookups

