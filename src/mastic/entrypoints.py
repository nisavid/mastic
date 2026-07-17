"""Installed mastic and masticd executable entrypoints."""

from __future__ import annotations

import asyncio
import sys

from mastic.application.catalogue import build_operation_catalogue
from mastic.application.dispatch import ApplicationError
from mastic.infrastructure.production import compose_daemon, compose_local
from mastic.interfaces.cli import build_cli
from mastic.interfaces.tui import MasticApp


def cli_main() -> None:
    """Run the catalogue-generated CLI; no arguments open the same TUI."""

    if {"-h", "--help"}.intersection(sys.argv[1:]):
        if len(sys.argv) == 2:
            print("usage: mastic [OPTIONS] COMMAND [ARGS]...")
        build_cli(
            _HelpDispatcher(),
            build_operation_catalogue(),
            tui_launcher=lambda: 0,
        )()
        return
    production = compose_local()
    application = production.application

    def run_tui() -> int:
        MasticApp(
            application.dispatcher,
            application.catalogue,
            application.snapshots,
        ).run()
        return 0

    build_cli(
        application.dispatcher,
        application.catalogue,
        tui_launcher=run_tui,
    )()


def daemon_main() -> None:
    """Run the foreground per-user Supervisor control process."""

    if {"-h", "--help"}.intersection(sys.argv[1:]):
        print("usage: masticd [-h]")
        print("\nRun the foreground per-user mastic Supervisor.")
        return
    if sys.argv[1:]:
        print(f"masticd: unexpected argument: {sys.argv[1]}", file=sys.stderr)
        raise SystemExit(2)
    asyncio.run(compose_daemon().serve())


def main() -> None:
    """Support the private ``python -m`` launchd target."""

    if sys.argv[1:2] == ["daemon"]:
        del sys.argv[1]
        daemon_main()
        return
    cli_main()


class _HelpDispatcher:
    """A non-executable dispatcher used only to render catalogue help."""

    @staticmethod
    def preview(request):
        raise ApplicationError("help_only", f"cannot preview {request.name} in help")

    @staticmethod
    def execute(request):
        raise ApplicationError("help_only", f"cannot execute {request.name} in help")


if __name__ == "__main__":
    main()
