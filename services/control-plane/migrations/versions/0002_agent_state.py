"""Schema for LangGraph checkpoints (ADR-0006).

Checkpoint tables are created by the checkpointer's own idempotent ``setup()`` inside this
schema; only the system role (used by agent workers) can reach it.

Revision ID: 0002_agent_state
Revises: 0001_core
"""

from alembic import op

revision = "0002_agent_state"
down_revision = "0001_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        CREATE SCHEMA IF NOT EXISTS agent_state;
        REVOKE ALL ON SCHEMA agent_state FROM PUBLIC;
        GRANT USAGE, CREATE ON SCHEMA agent_state TO re_system;
        """
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql("DROP SCHEMA IF EXISTS agent_state CASCADE")
