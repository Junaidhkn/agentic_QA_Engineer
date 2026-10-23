"""Runner agent entrypoint: pull leases and execute up to ``RA_SLOTS`` jobs concurrently."""

from __future__ import annotations

import asyncio
import logging
import signal

from runner_agent.client import LeaseClient
from runner_agent.config import Config, get_config
from runner_agent.drivers.base import Driver
from runner_agent.drivers.docker import DockerDriver
from runner_agent.drivers.process import ProcessDriver
from runner_agent.executor import JobRunner
from runner_agent.keys import RunnerKeys

log = logging.getLogger("runner_agent")


def make_driver(config: Config) -> Driver:
    return DockerDriver(config) if config.driver == "docker" else ProcessDriver(config)


async def serve(
    config: Config, stop: asyncio.Event, client: LeaseClient | None = None, max_jobs: int | None = None
) -> int:
    client = client or LeaseClient(config)
    keys = RunnerKeys()
    runner = JobRunner(config, client, keys, make_driver(config))
    slots = asyncio.Semaphore(config.slots)
    tasks: set[asyncio.Task[None]] = set()
    started = 0
    try:
        while not stop.is_set() and (max_jobs is None or started < max_jobs):
            await slots.acquire()
            try:
                lease = await client.acquire(
                    keys.public_b64,
                    config.job_types,
                    {"slots_free": slots._value + 1},
                    wait_seconds=5 if max_jobs else 20,
                )
            except Exception:
                slots.release()
                log.warning("acquire failed; retrying", exc_info=True)
                await asyncio.sleep(2)
                continue
            if lease is None:
                slots.release()
                continue
            started += 1
            task = asyncio.create_task(runner.run(lease))
            tasks.add(task)
            task.add_done_callback(lambda t: (tasks.discard(t), slots.release()))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await client.close()
    return started


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = get_config()
    if not config.runner_token:
        raise SystemExit("RA_RUNNER_TOKEN is required (reliability issue-runner-token --runner-id ...)")
    stop = asyncio.Event()
    loop = asyncio.new_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    log.info("runner %s starting with %s driver, %s slot(s)", config.runner_id, config.driver, config.slots)
    loop.run_until_complete(serve(config, stop))


if __name__ == "__main__":
    main()
