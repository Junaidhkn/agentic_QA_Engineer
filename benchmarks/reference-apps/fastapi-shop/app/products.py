from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import db
from app.auth import CurrentUser
from app.defects import on

router = APIRouter(prefix="/products", tags=["products"])


class ProductIn(BaseModel):
    name: str
    price: float = Field(..., gt=0)
    stock: int = Field(..., ge=0)


def _row(r: object) -> dict[str, object]:
    return dict(r)  # type: ignore[call-overload]


@router.get("")
def list_products(q: str | None = None) -> list[dict[str, object]]:
    if q:
        if on("slow_search"):
            rows = db.query("SELECT * FROM products")
            time.sleep(0.05)
            return [_row(r) for r in rows if q.lower() in r["name"].lower()]
        return [_row(r) for r in db.query("SELECT * FROM products WHERE name LIKE ?", f"%{q}%")]
    return [_row(r) for r in db.query("SELECT * FROM products ORDER BY id")]


@router.get("/{product_id}")
def get_product(product_id: int) -> dict[str, object]:
    rows = db.query("SELECT * FROM products WHERE id = ?", product_id)
    if not rows:
        raise HTTPException(404, "product not found")
    return _row(rows[0])


@router.post("", status_code=201)
def create_product(body: ProductIn, user: int = CurrentUser) -> dict[str, object]:
    pid = db.execute("INSERT INTO products (name, price, stock) VALUES (?, ?, ?)", body.name, body.price, body.stock)
    return {"id": pid, **body.model_dump()}
