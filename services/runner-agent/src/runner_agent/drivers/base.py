from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Limits:
    cpu: float = 2
    mem_gb: float = 4
    pids: int = 2048


@dataclass
class CommandSpec:
    tool: str  # "k6" | "playwright"
    args: list[str]
    workdir: Path
    env: dict[str, str] = field(default_factory=dict)
    secret_env: dict[str, str] = field(default_factory=dict)
    limits: Limits = field(default_factory=Limits)


@dataclass(frozen=True)
class Egress:
    allow_hosts: list[str]
    allow_ips: list[str]


class Handle(Protocol):
    async def wait(self) -> int: ...
    async def stop(self, grace_s: float = 5.0) -> None: ...
    def cpu_fraction(self) -> float | None: ...


class Driver(Protocol):
    name: str

    async def prepare(self, job_id: str, egress: Egress) -> None: ...
    async def start(self, job_id: str, spec: CommandSpec) -> Handle: ...
    async def teardown(self, job_id: str) -> None: ...
