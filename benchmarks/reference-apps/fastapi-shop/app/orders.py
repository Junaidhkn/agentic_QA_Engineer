from __future__ import annotations

import random
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import db
from app.auth import CurrentUser
from app.defects import on

router = APIRouter(prefix="/orders", tags=["orders"])


class OrderIn(BaseModel):
    product_id: int
    quantity: int = Field(..., ge=1, le=10)


def _order(order_id: int, user: int) -> dict[str, object]:
    rows = db.query("SELECT * FROM orders WHERE id = ? AND user_id = ?", order_id, user)
    if not rows:
        raise HTTPException(404, "order not found")
    return dict(rows[0])


@router.post("", status_code=201)
def create_order(body: OrderIn, user: int = CurrentUser) -> dict[str, object]:
    if on("race_stock"):
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
    order_id = db.execute(
        "INSERT INTO orders (user_id, product_id, quantity, status) VALUES (?, ?, ?, 'pending')",
        user, body.product_id, body.quantity,
    )
    return {"id": order_id, "product_id": body.product_id, "quantity": body.quantity, "status": "pending"}


@router.get("")
def list_orders(user: int = CurrentUser) -> list[dict[str, object]]:
    if on("n_plus_one"):
        orders = [dict(r) for r in db.query("SELECT * FROM orders WHERE user_id = ?", user)]
        for o in orders:  # Defect: one product query per order
            time.sleep(0.002)
            o["product"] = dict(db.query("SELECT * FROM products WHERE id = ?", o["product_id"])[0])
        return orders
    rows = db.query(
        "SELECT o.*, p.name AS product_name, p.price AS product_price FROM orders o "
        "JOIN products p ON p.id = o.product_id WHERE o.user_id = ?",
        user,
    )
    return [
        {**{k: r[k] for k in ("id", "user_id", "product_id", "quantity", "status")},
         "product": {"id": r["product_id"], "name": r["product_name"], "price": r["product_price"]}}
        for r in rows
    ]


@router.get("/{order_id}")
def get_order(order_id: int, user: int = CurrentUser) -> dict[str, object]:
    return _order(order_id, user)


@router.post("/{order_id}/pay")
def pay_order(order_id: int, user: int = CurrentUser) -> dict[str, object]:
    order = _order(order_id, user)
    if order["status"] != "pending":
        raise HTTPException(409, "order is not pending")
    if on("pay_errors") and random.random() < 0.05:
        raise RuntimeError("payment gateway timeout")  # Defect: unhandled adapter error → 500
    db.execute("UPDATE orders SET status = 'paid' WHERE id = ?", order_id)
    return {**order, "status": "paid"}


@router.delete("/{order_id}", status_code=204)
def cancel_order(order_id: int, user: int = CurrentUser) -> None:
    order = _order(order_id, user)
    if order["status"] == "pending":
        db.execute("UPDATE products SET stock = stock + ? WHERE id = ?", order["quantity"], order["product_id"])
    db.execute("DELETE FROM orders WHERE id = ?", order_id)
