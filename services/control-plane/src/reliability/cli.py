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


def _integrity_check(args: argparse.Namespace) -> None:
    """Platform invariants on the live database (also hourly via the scheduler)."""
    import asyncio

    from reliability.modules.ops.jobs import check_integrity

    result = asyncio.run(check_integrity())
    print(json.dumps(result, indent=2, default=str))
    sys.exit(0 if result["ok"] else 1)


def _canary(args: argparse.Namespace) -> None:
    import asyncio

    from reliability.modules.ops import canary

    print(json.dumps(asyncio.run(canary.run()), indent=2, default=str))


def _restore_drill(args: argparse.Namespace) -> None:
    """Weekly restore drill (operations §7): restore, verify, record, clean up."""
    import asyncio
    import os

    from reliability.modules.ops import restore
    from reliability.platform.settings import get_settings

    source = args.source_url or get_settings().database_migration_url

    async def run() -> restore.DrillResult:
        if args.mode == "dump":
            if not args.scratch_url:
                raise SystemExit("--scratch-url is required for --mode dump")
            result = await restore.dump_drill(source, args.scratch_url, tolerance=args.tolerance, keep=args.keep)
        else:
            import boto3

            cfg = restore.RdsDrillConfig(
                source_instance=args.rds_instance,
                drill_instance_prefix=args.drill_prefix,
                subnet_group=args.subnet_group,
                security_group_ids=args.security_group,
                instance_class=args.instance_class,
                user=os.environ["RE_DRILL_DB_USER"],
                password=os.environ["RE_DRILL_DB_PASSWORD"],
            )
            result = await restore.rds_drill(cfg, rds=boto3.client("rds"), source_url=source, tolerance=args.tolerance)
        if not args.no_record:
            await restore.record(result)
        return result

    result = asyncio.run(run())
    print(json.dumps(result.as_detail(), indent=2, default=str))
    sys.exit(0 if result.ok else 1)


def _operator(args: argparse.Namespace) -> str:
    import getpass

    return str(args.operator or getpass.getuser())


def _org(args: argparse.Namespace) -> None:
    """Suspend, unsuspend or delete an organization (runbooks: abuse report, data deletion)."""
    import asyncio
    import uuid

    from reliability.modules.ops import orgs

    org_id = uuid.UUID(args.org_id)
    if args.action == "suspend":
        if not args.reason:
            raise SystemExit("--reason is required")
        n = asyncio.run(orgs.suspend(org_id, args.reason, _operator(args)))
        print(f"organization suspended; cancel requested for {n} active lease(s)")
    elif args.action == "unsuspend":
        asyncio.run(orgs.unsuspend(org_id, _operator(args)))
        print("organization unsuspended")
    elif args.action == "plan":
        if not args.plan:
            raise SystemExit("--plan is required")
        print(json.dumps(asyncio.run(orgs.change_plan(org_id, args.plan, _operator(args))), indent=2))
    elif not args.execute:
        print(json.dumps(asyncio.run(orgs.deletion_plan(org_id)).as_dict(), indent=2))
        print("dry run: re-run with --execute --confirm-slug <slug> to delete irreversibly")
    else:
        print(json.dumps(asyncio.run(orgs.delete(org_id, args.confirm_slug or "", _operator(args))), indent=2))


def _abuse(args: argparse.Namespace) -> None:
    """Abuse review queue (threat model A1): list, inspect, clear or suspend."""
    import asyncio
    import uuid

    from reliability.modules.abuse import service as abuse

    if args.action == "list":
        reviews = asyncio.run(abuse.list_reviews(None if args.all else "open"))
        for r in reviews:
            age_h = (r.decided_at or r.created_at).isoformat(timespec="minutes")
            print(f"{r.id}  {r.status:9}  {r.reason:28}  org {r.org_id}  {age_h}  {r.signals.get('target_host', '')}")
        print(f"{len(reviews)} review(s)")
        return
    if not args.review_id:
        raise SystemExit("review id required")
    review_id = uuid.UUID(args.review_id)
    if args.action == "show":
        r = asyncio.run(abuse.get(review_id))
        print(
            json.dumps(
                {
                    **{
                        k: getattr(r, k)
                        for k in ("id", "org_id", "reason", "status", "decided_by", "created_at", "decided_at")
                    },
                    "signals": r.signals,
                },
                indent=2,
                default=str,
            )
        )
        return
    if not args.note:
        raise SystemExit("--note is required (it is audited and kept with the review)")
    decision = "cleared" if args.action == "clear" else "suspended"
    r = asyncio.run(abuse.decide(review_id, decision, _operator(args), args.note))
    print(f"review {r.id} {r.status}; org owners will be emailed")


def _flag(args: argparse.Namespace) -> None:
    """Set a feature flag (e.g. agents.diagnosis.llm_disabled) globally or for one org."""
    import asyncio
    import uuid

    from reliability.platform.db import system_tx
    from reliability.platform.flags import set_flag

    async def run() -> None:
        async with system_tx() as session:
            await set_flag(session, args.key, args.state == "on", uuid.UUID(args.org) if args.org else None)

    asyncio.run(run())
    print(f"{args.key} {args.state} ({'org ' + args.org if args.org else 'global'})")


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

    p = sub.add_parser("integrity-check", help="check platform invariants and audit chains; exit 1 on violations")
    p.set_defaults(func=_integrity_check)

    p = sub.add_parser("canary", help="judge the last synthetic canary and start the next one")
    p.set_defaults(func=_canary)

    p = sub.add_parser("restore-drill", help="restore a backup into a scratch database and verify it")
    p.add_argument("--mode", choices=["dump", "rds"], default="dump")
    p.add_argument("--source-url", help="database to back up / compare with (default: migration URL)")
    p.add_argument("--scratch-url", help="dump mode: server (superuser URL) to restore into")
    p.add_argument("--tolerance", type=float, default=0.0, help="allowed row-count shortfall vs a live source")
    p.add_argument("--keep", action="store_true", help="dump mode: keep the scratch database")
    p.add_argument("--no-record", action="store_true", help="don't record the outcome as a platform check")
    p.add_argument("--rds-instance", help="rds mode: source DB instance identifier")
    p.add_argument("--drill-prefix", default="re-restore-drill")
    p.add_argument("--subnet-group", help="rds mode: DB subnet group for the drill instance")
    p.add_argument("--security-group", action="append", default=[], help="rds mode: VPC security group id")
    p.add_argument("--instance-class", default="db.t4g.large")
    p.set_defaults(func=_restore_drill)

    p = sub.add_parser("org", help="operator actions on an organization")
    p.add_argument("action", choices=["suspend", "unsuspend", "plan", "delete"])
    p.add_argument("org_id")
    p.add_argument("--reason", help="suspend: why (stored and audited)")
    p.add_argument("--plan", help="plan: free | indie | startup | team")
    p.add_argument("--execute", action="store_true", help="delete: actually delete (default: dry run)")
    p.add_argument("--confirm-slug", help="delete: the organization's slug, as confirmation")
    p.add_argument("--operator", help="name recorded in the audit log (default: $USER)")
    p.set_defaults(func=_org)

    p = sub.add_parser("abuse", help="abuse review queue: list, show, clear, suspend")
    p.add_argument("action", choices=["list", "show", "clear", "suspend"])
    p.add_argument("review_id", nargs="?")
    p.add_argument("--all", action="store_true", help="list: include decided reviews")
    p.add_argument("--note", help="clear/suspend: reason, audited")
    p.add_argument("--operator", help="name recorded in the audit log (default: $USER)")
    p.set_defaults(func=_abuse)

    p = sub.add_parser("flag", help="set a feature flag globally or for one org")
    p.add_argument("key")
    p.add_argument("state", choices=["on", "off"])
    p.add_argument("--org", help="organization id (omit for global)")
    p.set_defaults(func=_flag)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
