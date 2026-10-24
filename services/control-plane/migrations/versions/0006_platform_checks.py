"""Platform self-checks (operations §6, §7): synthetic canary, integrity invariants, restore drills.

One row per check with its latest outcome; the metrics endpoint turns them into
``platform_check_*`` gauges that the SLO rules alert on. Platform-level, no tenant data,
written by the system role only.

Revision ID: 0006_platform_checks
Revises: 0005_exploration
"""

from alembic import op

revision = "0006_platform_checks"
down_revision = "0005_exploration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        CREATE TABLE platform_checks (
          name            text PRIMARY KEY,
          status          text NOT NULL CHECK (status IN ('ok','failed','running')),
          detail          jsonb NOT NULL DEFAULT '{}'::jsonb,
          checked_at      timestamptz NOT NULL DEFAULT now(),
          last_success_at timestamptz,
          consecutive_failures int NOT NULL DEFAULT 0
        );
        REVOKE ALL ON platform_checks FROM re_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON platform_checks TO re_system;
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql("DROP TABLE platform_checks")
