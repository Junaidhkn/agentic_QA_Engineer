from sqlalchemy import text

from app.db import engine


def test_tasks_can_be_looked_up_by_assignee_with_an_index() -> None:
    """GET /api/tasks?assignee= filters on tasks.assignee_email; without an index every lookup
    scans the whole table."""
    with engine.connect() as conn:
        defs = [r[0] for r in conn.execute(text("SELECT indexdef FROM pg_indexes WHERE tablename = 'tasks'"))]
    assert any("(assignee_email)" in d for d in defs), defs
