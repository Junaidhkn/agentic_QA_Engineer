"""Seed data: 3 projects, 3,000 tasks across 50 assignees, a few comments (idempotent)."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import Comment, Project, Task, engine


def main() -> None:
    with Session(engine) as db:
        if db.scalar(select(func.count()).select_from(Project)):
            return
        projects = [Project(name=n) for n in ("Website", "Mobile", "Billing")]
        db.add_all(projects)
        db.flush()
        tasks = [
            Task(project_id=projects[i % 3].id, title=f"Task {i}", assignee_email=f"user{i % 50}@example.com")
            for i in range(3000)
        ]
        db.add_all(tasks)
        db.flush()
        db.add_all(Comment(task_id=t.id, body="looks good") for t in tasks[:600])
        db.commit()


if __name__ == "__main__":
    main()
