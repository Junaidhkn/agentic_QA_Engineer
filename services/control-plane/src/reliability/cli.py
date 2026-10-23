"""Operator CLI: ``reliability <command>``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _export_openapi(args: argparse.Namespace) -> None:
    from reliability.main_api import create_app

    spec = create_app().openapi()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
    print(f"wrote {out}")


def _outbox_relay(args: argparse.Namespace) -> None:
    import asyncio

    from reliability.main_worker import publish
    from reliability.platform.logging import configure_logging
    from reliability.platform.outbox import run_relay
    from reliability.platform.settings import get_settings

    configure_logging(get_settings().log_level)
    asyncio.run(run_relay(publish))


def _issue_runner_token(args: argparse.Namespace) -> None:
    from reliability.modules.runners.auth import issue_runner_token

    print(issue_runner_token(args.runner_id, args.ttl_hours * 3600))


def _kill_switch(args: argparse.Namespace) -> None:
    """Operator kill switch: stop all (or one org's) test traffic within one heartbeat."""
    import asyncio
    import uuid

    from reliability.modules.runners.service import request_cancel_all
    from reliability.platform.db import system_tx
    from reliability.platform.flags import KILL_SWITCH, set_flag

    org = uuid.UUID(args.org) if args.org else None

    async def run() -> None:
        async with system_tx() as session:
            await set_flag(session, KILL_SWITCH, args.state == "on", org)
        if args.state == "on":
            print(f"cancel requested for {await request_cancel_all(org)} active lease(s)")

    asyncio.run(run())
    print(f"kill switch {args.state} ({'org ' + args.org if args.org else 'global'})")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="reliability")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("export-openapi", help="write the OpenAPI document")
    p.add_argument("--out", default="../../packages/api-client/openapi.json")
    p.set_defaults(func=_export_openapi)

    p = sub.add_parser("outbox-relay", help="publish outbox rows to Celery (runs forever)")
    p.set_defaults(func=_outbox_relay)

    p = sub.add_parser("issue-runner-token", help="issue a signed identity token for an execution-plane runner")
    p.add_argument("--runner-id", required=True)
    p.add_argument("--ttl-hours", type=int, default=24)
    p.set_defaults(func=_issue_runner_token)

    p = sub.add_parser("kill-switch", help="pause all test execution (global or per org)")
    p.add_argument("state", choices=["on", "off"])
    p.add_argument("--org", help="organization id (omit for global)")
    p.set_defaults(func=_kill_switch)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
