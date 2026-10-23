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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="reliability")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("export-openapi", help="write the OpenAPI document")
    p.add_argument("--out", default="../../packages/api-client/openapi.json")
    p.set_defaults(func=_export_openapi)

    p = sub.add_parser("outbox-relay", help="publish outbox rows to Celery (runs forever)")
    p.set_defaults(func=_outbox_relay)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
