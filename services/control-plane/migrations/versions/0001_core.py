"""Core schema, row-level security and role grants.

Revision ID: 0001_core
Revises:
"""

from pathlib import Path

from alembic import op


def _sql(statement: str) -> None:
    """Run raw SQL without bind-parameter parsing (DDL contains ':' and '%')."""
    op.get_bind().exec_driver_sql(statement)

revision = "0001_core"
down_revision = None
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parent.parent / "sql" / "0001_core.sql"

# Tables with a NOT NULL org_id column and the standard tenant policy.
TENANT_TABLES = [
    "invitations", "api_keys", "projects", "github_installations", "repositories", "targets",
    "project_secrets", "ingest_tokens", "openapi_specs", "discovery_runs", "workflows", "test_suites",
    "test_cases", "load_profiles", "runs", "run_events", "run_metric_summaries", "runner_leases",
    "execution_jobs", "artifacts", "findings", "evidence", "diagnoses", "hypotheses", "hypothesis_evidence",
    "repair_attempts", "verification_gates", "performance_comparisons", "pull_requests", "reports",
    "approvals", "idempotency_keys", "usage_ledger", "llm_usage", "agent_threads",
]
# Readable by the app role, writable only by the system role.
APP_READ_ONLY = ["plans"]
SYSTEM_ONLY = ["audit_anchors", "abuse_reviews"]


def upgrade() -> None:
    _sql(SQL.read_text())
    _sql(
        """
        CREATE FUNCTION app_org_ids() RETURNS uuid[] LANGUAGE sql STABLE AS $$
          SELECT string_to_array(nullif(current_setting('app.org_ids', true), ''), ',')::uuid[]
        $$;
        CREATE FUNCTION app_user_id() RETURNS uuid LANGUAGE sql STABLE AS $$
          SELECT nullif(current_setting('app.user_id', true), '')::uuid
        $$;
        GRANT EXECUTE ON FUNCTION app_org_ids(), app_user_id() TO re_app, re_system;
        """
    )
    for table in TENANT_TABLES:
        _sql(
            f"""
            ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
            CREATE POLICY tenant_isolation ON {table}
              USING (org_id = ANY (app_org_ids()))
              WITH CHECK (org_id = ANY (app_org_ids()));
            GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO re_app;
            """
        )
    _sql(
        """
        ALTER TABLE organizations ENABLE ROW LEVEL SECURITY;
        ALTER TABLE organizations FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON organizations
          USING (id = ANY (app_org_ids())) WITH CHECK (id = ANY (app_org_ids()));
        GRANT SELECT, INSERT, UPDATE ON organizations TO re_app;

        ALTER TABLE memberships ENABLE ROW LEVEL SECURITY;
        ALTER TABLE memberships FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON memberships
          USING (org_id = ANY (app_org_ids()) OR user_id = app_user_id())
          WITH CHECK (org_id = ANY (app_org_ids()));
        GRANT SELECT, INSERT, UPDATE, DELETE ON memberships TO re_app;

        ALTER TABLE feature_flags ENABLE ROW LEVEL SECURITY;
        ALTER TABLE feature_flags FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_read ON feature_flags FOR SELECT
          USING (org_id IS NULL OR org_id = ANY (app_org_ids()));
        GRANT SELECT ON feature_flags TO re_app;

        ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;
        ALTER TABLE audit_events FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_read ON audit_events FOR SELECT USING (org_id = ANY (app_org_ids()));
        CREATE POLICY tenant_insert ON audit_events FOR INSERT
          WITH CHECK (org_id IS NULL OR org_id = ANY (app_org_ids()));
        GRANT SELECT, INSERT ON audit_events TO re_app;

        GRANT SELECT, INSERT, UPDATE ON users TO re_app;
        GRANT INSERT ON outbox TO re_app;
        """
    )
    for table in APP_READ_ONLY:
        _sql(f"GRANT SELECT ON {table} TO re_app")
    _sql(
        """
        GRANT USAGE ON SCHEMA public TO re_app, re_system;
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO re_app, re_system;
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO re_system;
        REVOKE UPDATE, DELETE, TRUNCATE ON audit_events FROM re_app, re_system;
        """
    )


def downgrade() -> None:
    _sql(
        """
        DO $$
        DECLARE r record;
        BEGIN
          FOR r IN SELECT tablename FROM pg_tables
                   WHERE schemaname = 'public' AND tablename <> 'alembic_version'
                     AND tablename NOT IN (SELECT inhrelid::regclass::text FROM pg_inherits) LOOP
            EXECUTE 'DROP TABLE IF EXISTS ' || quote_ident(r.tablename) || ' CASCADE';
          END LOOP;
        END $$;
        DROP FUNCTION IF EXISTS app_org_ids();
        DROP FUNCTION IF EXISTS app_user_id();
        """
    )
