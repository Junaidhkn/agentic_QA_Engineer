"""Browser exploration lease type (FR-8).

Revision ID: 0005_exploration
Revises: 0004_verifications
"""

from alembic import op

revision = "0005_exploration"
down_revision = "0004_verifications"
branch_labels = None
depends_on = None

TYPES = (
    "'functional','load','probe','sandbox_build','sandbox_env','verification','coding_session',"
    "'security_scan','discovery_snapshot','test_validation','repair_check'"
)


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        f"""
        ALTER TABLE runner_leases DROP CONSTRAINT runner_leases_job_type_check;
        ALTER TABLE runner_leases ADD CONSTRAINT runner_leases_job_type_check
          CHECK (job_type IN ({TYPES},'ui_explore'));
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        f"""
        ALTER TABLE runner_leases DROP CONSTRAINT runner_leases_job_type_check;
        ALTER TABLE runner_leases ADD CONSTRAINT runner_leases_job_type_check CHECK (job_type IN ({TYPES}));
        """
    )
