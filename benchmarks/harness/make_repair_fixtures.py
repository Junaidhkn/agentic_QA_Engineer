"""Regenerates the repair fixtures of the reference corpus (``<app>/repairs/``).

For every seeded defect: a regression test (fails on the defect, passes once fixed), an oracle
fix and a decoy fix, both as unified diffs against the app as committed. Decoys are plausible
but wrong and each targets one verification gate; the repair benchmark checks that oracles are
verified and decoys never are. Run from the repository root:

    python benchmarks/harness/make_repair_fixtures.py
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path

CORPUS = Path(__file__).resolve().parents[1] / "reference-apps"


def diff(app: Path, edits: dict[str, str | None]) -> str:
    out: list[str] = []
    for path, new in sorted(edits.items()):
        old_file = app / path
        old = old_file.read_text() if old_file.exists() else None
        out += difflib.unified_diff(
            (old or "").splitlines(keepends=True),
            (new or "").splitlines(keepends=True),
            fromfile="/dev/null" if old is None else f"a/{path}",
            tofile="/dev/null" if new is None else f"b/{path}",
        )
    return "".join(out)


def replace(app: Path, path: str, old: str, new: str) -> str:
    text = (app / path).read_text()
    if old not in text:
        raise SystemExit(f"{app.name}/{path}: anchor not found: {old[:60]!r}")
    return text.replace(old, new, 1)


# ------------------------------------------------------------------- tasks ----
TASKS = CORPUS / "fastapi-tasks"
TASKS_TESTS = {
    "missing_index": '''from sqlalchemy import text

from app.db import engine


def test_tasks_can_be_looked_up_by_assignee_with_an_index() -> None:
    """GET /api/tasks?assignee= filters on tasks.assignee_email; without an index every lookup
    scans the whole table."""
    with engine.connect() as conn:
        defs = [r[0] for r in conn.execute(text("SELECT indexdef FROM pg_indexes WHERE tablename = 'tasks'"))]
    assert any("(assignee_email)" in d for d in defs), defs
''',
    "n_plus_one": '''from fastapi.testclient import TestClient
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
''',
    "pool_exhaustion": '''from fastapi.testclient import TestClient

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
''',
}


def tasks_fixtures() -> dict[str, dict[str, object]]:
    routes = "app/routes.py"
    n_plus_one_old = '''    if on("n_plus_one"):
        return [{**_task(t), "comments": db.scalar(select(func.count()).where(Comment.task_id == t.id))} for t in tasks]
'''
    pool_old = '''    db.add(task)
    db.flush()
    if on("pool_exhaustion"):
        time.sleep(0.3)  # Defect: slow notification call while the transaction holds a connection
    db.commit()
    return _task(task)
'''
    return {
        "missing_index": {
            "fix": {
                "alembic/versions/0003_assignee_index_always.py": '''"""index tasks by assignee (also where 0002 was skipped)"""

from alembic import op

revision = "0003"
down_revision = "0002"


def upgrade() -> None:
    op.execute("CREATE INDEX IF NOT EXISTS tasks_assignee_idx ON tasks (assignee_email)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS tasks_assignee_idx")
'''
            },
            "decoy": {
                "alembic/versions/0003_task_title_index.py": '''"""index tasks by title"""

from alembic import op

revision = "0003"
down_revision = "0002"


def upgrade() -> None:
    op.execute("CREATE INDEX IF NOT EXISTS tasks_title_idx ON tasks (title)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS tasks_title_idx")
'''
            },
            "decoy_caught_by": "fail_to_pass",
            "decoy_description": "indexes the wrong column",
            "approach": "add a migration that creates the assignee index unconditionally",
        },
        "n_plus_one": {
            "fix": {routes: replace(TASKS, routes, n_plus_one_old, "")},
            "decoy": {
                routes: replace(
                    TASKS,
                    routes,
                    n_plus_one_old,
                    '''    if on("n_plus_one"):
        ids = ",".join(str(t.id) for t in tasks) or "0"
        rows = db.execute(text(f"SELECT task_id, count(*) FROM comments WHERE task_id IN ({ids}) GROUP BY task_id"))
        counts = dict(rows.all())
        return [{**_task(t), "comments": counts.get(t.id, 0)} for t in tasks]
''',
                ).replace("from sqlalchemy import func, select", "from sqlalchemy import func, select, text")
            },
            "decoy_caught_by": "security",
            "decoy_description": "batches the query by formatting ids into raw SQL",
            "approach": "load the comment counts in one grouped query instead of one per task",
        },
        "pool_exhaustion": {
            "fix": {
                routes: replace(
                    TASKS,
                    routes,
                    pool_old,
                    '''    db.add(task)
    db.flush()
    result = _task(task)
    db.commit()  # release the connection before the slow notification call
    if on("pool_exhaustion"):
        time.sleep(0.3)  # slow notification call, made after the transaction has ended
    return result
''',
                )
            },
            "decoy": {
                "app/db.py": replace(TASKS, "app/db.py", 'pool_size=2 if on("pool_exhaustion") else 10', "pool_size=50"),
                ".env.production": "DATABASE_POOL_SIZE=50\n",
            },
            "decoy_caught_by": "diff_policy",
            "decoy_description": "raises the pool size and edits an environment file",
            "approach": "commit before the slow notification so the connection returns to the pool",
        },
    }


# -------------------------------------------------------------------- shop ----
SHOP = CORPUS / "fastapi-shop"
SHOP_TESTS = {
    "race_stock": '''import threading

from fastapi import HTTPException

from app import db, defects
from app.orders import OrderIn, create_order


def test_limited_stock_is_never_oversold(monkeypatch) -> None:
    monkeypatch.setattr(defects, "ACTIVE", {"race_stock"})
    db.init()
    db.execute("UPDATE products SET stock = 10 WHERE id = 3")
    sold: list[int] = []
    start = threading.Barrier(20)

    def buy() -> None:
        start.wait()
        try:
            create_order(OrderIn(product_id=3, quantity=1), user=1)
            sold.append(1)
        except HTTPException as exc:
            assert exc.status_code == 409

    threads = [threading.Thread(target=buy) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stock = db.query("SELECT stock FROM products WHERE id = 3")[0]["stock"]
    assert stock >= 0, f"stock went negative: {stock}"
    assert len(sold) + stock == 10, f"sold {len(sold)} with stock {stock} left of 10"
''',
    "pay_errors": '''from app import db, defects, orders


def test_transient_gateway_errors_are_retried(monkeypatch) -> None:
    monkeypatch.setattr(defects, "ACTIVE", {"pay_errors"})
    db.init()
    oid = db.execute("INSERT INTO orders (user_id, product_id, quantity, status) VALUES (1, 1, 1, 'pending')")
    draws = iter([0.0, 0.99, 0.99, 0.99])  # the first charge times out, the next succeeds
    calls: list[int] = []

    def fake_random() -> float:
        calls.append(1)
        return next(draws)

    monkeypatch.setattr(orders.random, "random", fake_random)
    result = orders.pay_order(oid, user=1)
    assert result["status"] == "paid"
    assert len(calls) >= 2, "the failed charge was not retried"
''',
    "slow_search": '''from app import db, defects, products


def test_search_does_not_block_the_request(monkeypatch) -> None:
    monkeypatch.setattr(defects, "ACTIVE", {"slow_search"})
    db.init()

    def no_sleep(seconds: float) -> None:
        raise AssertionError("time.sleep() called while serving a search request")

    monkeypatch.setattr(products.time, "sleep", no_sleep)
    assert [p["name"] for p in products.list_products(q="mug")] == ["Limited mug"]
''',
}


def shop_fixtures() -> dict[str, dict[str, object]]:
    orders, products = "app/orders.py", "app/products.py"
    race_old = '''    if on("race_stock"):
        # Defect: check-then-act without holding the lock across the read and the write.
        rows = db.query("SELECT stock FROM products WHERE id = ?", body.product_id)
        if not rows:
            raise HTTPException(404, "product not found")
        if rows[0]["stock"] < body.quantity:
            raise HTTPException(409, "out of stock")
        time.sleep(0.01)
        db.execute("UPDATE products SET stock = stock - ? WHERE id = ?", body.quantity, body.product_id)
    else:
        with db.cursor() as c:
            row = c.execute("SELECT stock FROM products WHERE id = ?", (body.product_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "product not found")
            if row["stock"] < body.quantity:
                raise HTTPException(409, "out of stock")
            c.execute("UPDATE products SET stock = stock - ? WHERE id = ?", (body.quantity, body.product_id))
'''
    race_fix = '''    with db.cursor() as c:  # hold the lock across the check and the update
        row = c.execute("SELECT stock FROM products WHERE id = ?", (body.product_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "product not found")
        if row["stock"] < body.quantity:
            raise HTTPException(409, "out of stock")
        c.execute("UPDATE products SET stock = stock - ? WHERE id = ?", (body.quantity, body.product_id))
'''
    pay_old = '''    if on("pay_errors") and random.random() < 0.05:
        raise RuntimeError("payment gateway timeout")  # Defect: unhandled adapter error → 500
    db.execute("UPDATE orders SET status = 'paid' WHERE id = ?", order_id)
'''
    search_old = '''        if on("slow_search"):
            rows = db.query("SELECT * FROM products")
            time.sleep(0.05)
            return [_row(r) for r in rows if q.lower() in r["name"].lower()]
'''
    return {
        "race_stock": {
            "fix": {orders: replace(SHOP, orders, race_old, race_fix)},
            "decoy": {
                orders: replace(
                    SHOP,
                    orders,
                    'db.execute("UPDATE products SET stock = stock - ? WHERE id = ?", body.quantity, body.product_id)',
                    'db.execute("UPDATE products SET stock = MAX(stock - ?, 0) WHERE id = ?", body.quantity, body.product_id)',
                )
            },
            "decoy_caught_by": "fail_to_pass",
            "decoy_description": "clamps stock at zero instead of preventing the oversell",
            "approach": "hold the database lock across the stock check and the decrement",
        },
        "pay_errors": {
            "fix": {
                orders: replace(
                    SHOP,
                    orders,
                    pay_old,
                    '''    for attempt in range(3):  # retry transient gateway timeouts with a short backoff
        try:
            _charge(order_id)
            break
        except RuntimeError:
            if attempt == 2:
                raise HTTPException(502, "payment gateway unavailable") from None
            time.sleep(0.01 * (attempt + 1))
    db.execute("UPDATE orders SET status = 'paid' WHERE id = ?", order_id)
''',
                ).replace(
                    '''@router.post("/{order_id}/pay")''',
                    '''def _charge(order_id: int) -> None:
    """Payment adapter call (simulated gateway; times out transiently under the defect toggle)."""
    if on("pay_errors") and random.random() < 0.05:
        raise RuntimeError("payment gateway timeout")


@router.post("/{order_id}/pay")''',
                )
            },
            "decoy": {
                orders: replace(
                    SHOP,
                    orders,
                    pay_old,
                    '''    try:
        if on("pay_errors") and random.random() < 0.05:
            raise RuntimeError("payment gateway timeout")
    except RuntimeError:
        pass  # treat gateway timeouts as paid
    db.execute("UPDATE orders SET status = 'paid' WHERE id = ?", order_id)
''',
                )
            },
            "decoy_caught_by": "fail_to_pass",
            "decoy_description": "swallows the gateway error and marks the order paid without charging",
            "approach": "retry the payment adapter on transient timeouts and map a final failure to 502",
        },
        "slow_search": {
            "fix": {products: replace(SHOP, products, search_old, "")},
            "decoy": {
                products: replace(
                    SHOP,
                    products,
                    "            time.sleep(0.05)\n",
                    "            end = time.perf_counter() + 0.05\n            while time.perf_counter() < end:\n                pass\n",
                )
            },
            "decoy_caught_by": "load_comparison",
            "decoy_description": "replaces the sleep with a busy wait (passes the test, no faster)",
            "approach": "filter in SQL with LIKE instead of scanning every product with a sleep",
        },
    }


def write(app: Path, tests: dict[str, str], fixtures: dict[str, dict[str, object]]) -> None:
    out = app / "repairs"
    out.mkdir(exist_ok=True)
    manifest: dict[str, object] = {}
    for defect, spec in fixtures.items():
        test_path = f"tests/test_regression_{defect}.py"
        d = out / defect
        d.mkdir(exist_ok=True)
        (d / "regression_test.py").write_text(tests[defect])
        (d / "fix.diff").write_text(diff(app, spec["fix"]))  # type: ignore[arg-type]
        (d / "decoy.diff").write_text(diff(app, spec["decoy"]))  # type: ignore[arg-type]
        manifest[defect] = {
            "test_path": test_path,
            "test": f"{defect}/regression_test.py",
            "fix": f"{defect}/fix.diff",
            "fix_files": sorted(spec["fix"]),  # type: ignore[arg-type]
            "decoy": f"{defect}/decoy.diff",
            "decoy_files": sorted(spec["decoy"]),  # type: ignore[arg-type]
            "decoy_caught_by": spec["decoy_caught_by"],
            "decoy_description": spec["decoy_description"],
            "approach": spec["approach"],
        }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    write(TASKS, TASKS_TESTS, tasks_fixtures())
    write(SHOP, SHOP_TESTS, shop_fixtures())
    print("repair fixtures written")
