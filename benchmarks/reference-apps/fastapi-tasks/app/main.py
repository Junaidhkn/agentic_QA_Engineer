from fastapi import FastAPI

from app.routes import router

app = FastAPI(title="Reference Tasks", version="1.0.0")
app.include_router(router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
