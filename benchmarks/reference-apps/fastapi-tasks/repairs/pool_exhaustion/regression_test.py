from fastapi.testclient import TestClient

from app import defects, routes
from app.db import engine
from app.main import app


def test_no_connection_is_held_during_the_notification(monkeypatch) -> None:
    monkeypatch.setattr(defects, "ACTIVE", {"pool_exhaustion"})
    held: list[int] = []
    real_sleep = routes.time.sleep

    def spy(seconds: float) -> None:
        held.append(engine.pool.checkedout())
        real_sleep(0)

    monkeypatch.setattr(routes.time, "sleep", spy)
    with TestClient(app) as client:
        resp = client.post("/api/tasks", json={"project_id": 1, "title": "t", "assignee_email": "a@example.com"})
    assert resp.status_code == 201
    assert all(n == 0 for n in held), f"database connections checked out during the slow call: {held}"
