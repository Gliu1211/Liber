"""Command-line entry point: `liber serve` starts the proxy (ARCHITECTURE.md §1, §11).

Kept separate from app.py so that building the app (tests) never starts a server. Later
sessions add more subcommands here (`liber plan ...` in Session 4, `liber purge` from §11).
"""

import argparse
import ipaddress

import structlog
import uvicorn

from liber.app import create_app
from liber.config import Settings

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
    args = parser.parse_args(argv)

    settings = Settings()
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


if __name__ == "__main__":
    main()
