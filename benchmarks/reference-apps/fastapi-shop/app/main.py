from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from app import db, orders, products
from app.auth import router as auth_router

db.init()
app = FastAPI(title="Reference Shop", version="1.0.0")
app.include_router(auth_router)
app.include_router(products.router, prefix="/api")
app.include_router(orders.router, prefix="/api")


@app.get("/.well-known/reliability-engineer.txt", response_class=PlainTextResponse, include_in_schema=False)
def ownership_proof() -> str:
    """Target-ownership proof for the Reliability Engineer (token written by the operator)."""
    path = os.environ.get("VERIFICATION_TOKEN_FILE", "")
    return open(path).read() if path and os.path.exists(path) else ""


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
