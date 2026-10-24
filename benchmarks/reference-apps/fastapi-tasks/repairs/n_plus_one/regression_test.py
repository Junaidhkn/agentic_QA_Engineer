from fastapi.testclient import TestClient
from sqlalchemy import event

from app import defects
from app.db import engine
from app.main import app


def test_project_tasks_use_a_constant_number_of_queries(monkeypatch) -> None:
    monkeypatch.setattr(defects, "ACTIVE", {"n_plus_one"})
    statements: list[str] = []

    def count(conn, cursor, statement, *args) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    try:
        with TestClient(app) as client:
            resp = client.get("/api/projects/1/tasks")
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert resp.status_code == 200 and len(resp.json()) > 10
    assert len(statements) <= 5, f"{len(statements)} queries for {len(resp.json())} tasks"
