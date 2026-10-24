# fastapi-tasks (reference app)

Team task tracker on PostgreSQL (SQLAlchemy 2, psycopg 3, Alembic migrations, seed script).
Needs `DATABASE_URL` (e.g. `postgresql+psycopg://user:pw@localhost/tasks`).

```bash
alembic upgrade head && python -m app.seed && uvicorn app.main:app --port 8000
```

The seed loads 1,000,000 tasks (200,000 distinct assignees) and 200,000 comments server-side
with `generate_series` (~10 s), large enough that a missing index on a selective filter shows
up as tens of milliseconds per request.

## Defect toggles (`DEFECTS=…`)

| Defect | Symptom | Mechanism | Fix touches |
|---|---|---|---|
| `missing_index` | `GET /api/tasks?assignee=` slow as data grows | migration `0002` skipped: no index on `tasks.assignee_email` → sequential scans | `alembic/versions/0002_assignee_index.py`, `app/db.py` |
| `n_plus_one` | `GET /api/projects/{id}/tasks` latency grows with task count | comments counted with one query per task | `app/routes.py` |
| `pool_exhaustion` | timeouts/5xx under concurrency on `POST /api/tasks` | connection held during a slow call with a pool of 2 | `app/routes.py`, `app/db.py` |
