from __future__ import annotations

import os
from collections.abc import Iterator

from sqlalchemy import Boolean, ForeignKey, Integer, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.defects import on

engine = create_engine(
    os.environ["DATABASE_URL"],
    pool_size=2 if on("pool_exhaustion") else 10,
    max_overflow=0 if on("pool_exhaustion") else 10,
    pool_timeout=2 if on("pool_exhaustion") else 30,
)


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text)


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    title: Mapped[str] = mapped_column(Text)
    assignee_email: Mapped[str] = mapped_column(Text)
    done: Mapped[bool] = mapped_column(Boolean, default=False)


class Comment(Base):
    __tablename__ = "comments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"))
    body: Mapped[str] = mapped_column(Text)


def session() -> Iterator[Session]:
    with Session(engine) as s:
        yield s
