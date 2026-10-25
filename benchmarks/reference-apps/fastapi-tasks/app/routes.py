from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import Comment, Project, Task, session
from app.defects import on

router = APIRouter(prefix="/api")


class TaskIn(BaseModel):
    project_id: int
    title: str = Field(..., min_length=1)
    assignee_email: str


class CommentIn(BaseModel):
    body: str = Field(..., min_length=1)


def _task(t: Task) -> dict[str, object]:
    return {"id": t.id, "project_id": t.project_id, "title": t.title, "assignee_email": t.assignee_email,
            "done": t.done}


@router.get("/projects")
def list_projects(db: Session = Depends(session)) -> list[dict[str, object]]:
    return [{"id": p.id, "name": p.name} for p in db.scalars(select(Project).order_by(Project.id))]


@router.get("/projects/{project_id}/tasks")
def project_tasks(project_id: int, db: Session = Depends(session)) -> list[dict[str, object]]:
    tasks = list(db.scalars(select(Task).where(Task.project_id == project_id).order_by(Task.id).limit(200)))
    if on("n_plus_one"):
        return [{**_task(t), "comments": db.scalar(select(func.count()).where(Comment.task_id == t.id))} for t in tasks]
    counts = dict(db.execute(select(Comment.task_id, func.count()).where(Comment.task_id.in_([t.id for t in tasks]))
                             .group_by(Comment.task_id)).all())
    return [{**_task(t), "comments": counts.get(t.id, 0)} for t in tasks]


@router.get("/tasks")
def find_tasks(assignee: str, db: Session = Depends(session)) -> list[dict[str, object]]:
    return [_task(t) for t in db.scalars(select(Task).where(Task.assignee_email == assignee).limit(50))]


@router.post("/tasks", status_code=201)
def create_task(body: TaskIn, db: Session = Depends(session)) -> dict[str, object]:
    if db.get(Project, body.project_id) is None:
        raise HTTPException(404, "project not found")
    task = Task(project_id=body.project_id, title=body.title, assignee_email=body.assignee_email)
    db.add(task)
    db.flush()
    if on("pool_exhaustion"):
        time.sleep(0.3)  # Defect: slow notification call while the transaction holds a connection
    db.commit()
    return _task(task)


@router.get("/tasks/{task_id}")
def get_task(task_id: int, db: Session = Depends(session)) -> dict[str, object]:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    return _task(task)


@router.post("/tasks/{task_id}/comments", status_code=201)
def add_comment(task_id: int, body: CommentIn, db: Session = Depends(session)) -> dict[str, object]:
    if db.get(Task, task_id) is None:
        raise HTTPException(404, "task not found")
    comment = Comment(task_id=task_id, body=body.body)
    db.add(comment)
    db.commit()
    return {"id": comment.id, "task_id": task_id, "body": comment.body}


@router.patch("/tasks/{task_id}/done")
def complete_task(task_id: int, db: Session = Depends(session)) -> dict[str, object]:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    task.done = True
    db.commit()
    return _task(task)
