"""Seed data (idempotent): 3 projects, 1,000,000 tasks across 200,000 assignees and 200,000
comments, generated server-side so seeding takes seconds."""

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.db import Project, engine

TASKS = 1_000_000
ASSIGNEES = 200_000
COMMENTS = 200_000


def main() -> None:
    with Session(engine) as db:
        if db.scalar(select(func.count()).select_from(Project)):
            return
        db.add_all([Project(name=n) for n in ("Website", "Mobile", "Billing")])
        db.flush()
        db.execute(text(
            "INSERT INTO tasks (project_id, title, assignee_email, done) "
            "SELECT p.ids[1 + g % 3], 'Task ' || g, 'user' || (g % :assignees) || '@example.com', false "
            "FROM generate_series(0, :n - 1) g, (SELECT array_agg(id ORDER BY id) AS ids FROM projects) p"
        ), {"n": TASKS, "assignees": ASSIGNEES})
        db.execute(text(
            "INSERT INTO comments (task_id, body) SELECT t.first + (g % 50000), 'looks good' "
            "FROM generate_series(0, :n - 1) g, (SELECT min(id) AS first FROM tasks) t"
        ), {"n": COMMENTS})
        db.commit()
        db.execute(text("ANALYZE"))


if __name__ == "__main__":
    main()
