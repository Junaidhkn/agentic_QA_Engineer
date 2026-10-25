import os

ACTIVE = {d.strip() for d in os.environ.get("DEFECTS", "").split(",") if d.strip()}


def on(name: str) -> bool:
    return name in ACTIVE
