CREATE TABLE migration_integration_audit (
    migration_version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
