"""Verification pipeline state (ADR-0016) and repair context.

``verifications`` holds one deterministic gate pipeline per repair attempt: the frozen inputs
(base archive, patch, regression test, commands) and the archives built from them. Gate rows
and comparisons stay in ``verification_gates`` / ``performance_comparisons``.

Revision ID: 0004_verifications
Revises: 0003_sandbox_builds
"""

from alembic import op

revision = "0004_verifications"
down_revision = "0003_sandbox_builds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        ALTER TABLE repair_attempts ADD COLUMN context jsonb NOT NULL DEFAULT '{}'::jsonb;

        -- Repair checks are a runner job type (sandbox + allowlisted commands).
        ALTER TABLE runner_leases DROP CONSTRAINT runner_leases_job_type_check;
        ALTER TABLE runner_leases ADD CONSTRAINT runner_leases_job_type_check CHECK (job_type IN
          ('functional','load','probe','sandbox_build','sandbox_env','verification','coding_session',
           'security_scan','discovery_snapshot','test_validation','repair_check'));

        CREATE TABLE verifications (
          id                uuid PRIMARY KEY,
          org_id            uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
          project_id        uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          repair_attempt_id uuid NOT NULL UNIQUE REFERENCES repair_attempts(id) ON DELETE CASCADE,
          status            text NOT NULL CHECK (status IN ('running','verified','failed','errored')),
          inputs            jsonb NOT NULL,
          archives          jsonb NOT NULL DEFAULT '{}'::jsonb,
          summary           jsonb,
          created_at        timestamptz NOT NULL DEFAULT now(),
          finished_at       timestamptz
        );
        ALTER TABLE verifications ENABLE ROW LEVEL SECURITY;
        ALTER TABLE verifications FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON verifications
          USING (org_id = ANY (app_org_ids())) WITH CHECK (org_id = ANY (app_org_ids()));
        GRANT SELECT, INSERT, UPDATE, DELETE ON verifications TO re_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON verifications TO re_system;

        -- A/A noise-floor comparisons have no repair attempt; find them by project.
        ALTER TABLE performance_comparisons ADD COLUMN project_id uuid REFERENCES projects(id) ON DELETE CASCADE;
        CREATE INDEX performance_comparisons_noise ON performance_comparisons (project_id, created_at DESC)
          WHERE repair_attempt_id IS NULL;
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        DROP INDEX IF EXISTS performance_comparisons_noise;
        ALTER TABLE performance_comparisons DROP COLUMN IF EXISTS project_id;
        DROP TABLE IF EXISTS verifications;
        ALTER TABLE runner_leases DROP CONSTRAINT runner_leases_job_type_check;
        ALTER TABLE runner_leases ADD CONSTRAINT runner_leases_job_type_check CHECK (job_type IN
          ('functional','load','probe','sandbox_build','sandbox_env','coding_session','security_scan',
           'discovery_snapshot','test_validation'));
        ALTER TABLE repair_attempts DROP COLUMN IF EXISTS context;
        """
    )
