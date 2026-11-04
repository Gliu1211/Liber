"""Command-line entry point: `liber serve` starts the proxy, `liber plan ...` edits plans (ARCHITECTURE.md §1, §4, §11).

Kept separate from app.py so that building the app (tests) never starts a server. Later
sessions add more subcommands here (`liber purge` from §11).
"""

import argparse
import asyncio
import ipaddress

import structlog
import uvicorn

from liber.app import create_app
from liber.config import Settings
from liber.plan import commands
from liber.plan.versions import PlanError
from liber.session.store import Store

log = structlog.get_logger(__name__)


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="liber", description="Liber context proxy")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="run the proxy")
    serve.add_argument("--host", help="interface to bind (default 127.0.0.1)")
    serve.add_argument("--port", type=int, help="port to listen on (default 8484)")
    add_plan_commands(commands)
    args = parser.parse_args(argv)

    settings = Settings()
    if args.command == "plan":
        raise SystemExit(run_plan_command(args, settings))
    if args.host:
        settings = settings.model_copy(update={"host": args.host})
    if args.port:
        settings = settings.model_copy(update={"port": args.port})

    app = create_app(settings)
    if not is_loopback(settings.host):
        # Hard rule 3 / §11: anyone who can reach this port can spend the user's API key.
        log.warning("binding_to_non_loopback_address", host=settings.host)

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        # The upstream already sends Date and Server headers; don't add uvicorn's own copies.
        server_header=False,
        date_header=False,
    )


def add_plan_commands(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    """`liber plan show|create|apply|undo` (plan/commands.py does the work)."""
    plan = commands.add_parser("plan", help="show and edit a session's plan (§4)")
    actions = plan.add_subparsers(dest="plan_command", required=True)
    show = actions.add_parser("show", help="list sessions, or one session's items and plan versions")
    show.add_argument("--session", help="session id (omit to list sessions)")
    create = actions.add_parser("create", help="make a new pending plan version")
    create.add_argument("--session", required=True)
    create.add_argument("--summarize-through", type=int, metavar="MSG", help="summarize up to this message index")
    create.add_argument("--offload", nargs="+", default=[], metavar="ITEM", help="item ids to offload")
    create.add_argument("--delete", nargs="+", default=[], metavar="ITEM", help="item ids to delete")
    create.add_argument("--note", help="label shown in the offload pointer note")
    for name, text in (("apply", "apply the pending version at the next clean turn"), ("undo", "go back one version")):
        action = actions.add_parser(name, help=text)
        action.add_argument("--session", required=True)


def run_plan_command(args: argparse.Namespace, settings: Settings) -> int:
    """Run one `liber plan` command against the proxy's database; returns the exit code."""
    if not settings.db_path.exists():
        print(f"No Liber database at {settings.db_path} yet: run `liber serve` and send a request first.")
        return 1

    async def run() -> str:
        store = Store(settings.db_path)
        try:
            if args.plan_command == "show":
                return await commands.show(store, args.session)
            if args.plan_command == "create":
                return await commands.create(
                    store, args.session, args.summarize_through, tuple(args.offload), tuple(args.delete), args.note
                )
            if args.plan_command == "apply":
                return await commands.apply(store, args.session)
            return await commands.undo(store, args.session)
        finally:
            await store.close()

    try:
        print(asyncio.run(run()))
    except PlanError as exc:
        print(f"liber plan: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    main()
