"""Standalone sandbox builds (ADR-0011): buildability checks, plans and build reports.

Revision ID: 0003_sandbox_builds
Revises: 0002_agent_state
"""

from alembic import op

revision = "0003_sandbox_builds"
down_revision = "0002_agent_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        CREATE TABLE sandbox_builds (
          id           uuid PRIMARY KEY,
          org_id       uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
          project_id   uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          status       text NOT NULL CHECK (status IN ('queued','planning','building','succeeded','failed','cancelled')),
          source       jsonb NOT NULL,
          commit_sha   text,
          plan         jsonb,
          report       jsonb,
          error        text,
          requested_by uuid REFERENCES users(id),
          created_at   timestamptz NOT NULL DEFAULT now(),
          finished_at  timestamptz
        );
        CREATE INDEX sandbox_builds_project ON sandbox_builds (project_id, created_at DESC);
        ALTER TABLE sandbox_builds ENABLE ROW LEVEL SECURITY;
        ALTER TABLE sandbox_builds FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON sandbox_builds
          USING (org_id = ANY (app_org_ids())) WITH CHECK (org_id = ANY (app_org_ids()));
        GRANT SELECT, INSERT, UPDATE, DELETE ON sandbox_builds TO re_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON sandbox_builds TO re_system;
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql("DROP TABLE IF EXISTS sandbox_builds")
