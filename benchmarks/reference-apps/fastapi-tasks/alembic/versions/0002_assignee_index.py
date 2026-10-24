"""index tasks by assignee (skipped when the missing_index defect is on)"""

import os

from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    if "missing_index" in os.environ.get("DEFECTS", ""):
        return
    op.create_index("tasks_assignee_idx", "tasks", ["assignee_email"])


def downgrade() -> None:
    op.drop_index("tasks_assignee_idx", if_exists=True)
