"""Local stdio server for drafting the installed example teams.

Start it with an absolute state directory. It does not read ``.env`` and it
does not load provider credentials. Activation stays on the dashboard.
"""

import argparse
import sys

from agentbuildkit.drafting import build_server, quiet_diagnostics, require_state_directory
from agentbuildkit.revisions import RevisionService, SqliteRevisionStore
from office.templates import installed_evaluators, installed_templates


def serve(state_directory: str) -> None:
    """Serve drafting tools on stdio until the client closes the stream."""
    directory = require_state_directory(state_directory)
    service = RevisionService(
        SqliteRevisionStore(directory / "revisions.sqlite"),
        installed_templates(),
        credentials=lambda _provider: False,
    )
    service.seed()
    quiet_diagnostics()
    server = build_server(service, installed_evaluators())
    try:
        server.run(transport="stdio")
    finally:
        service.close()


def main(argv: list[str] | None = None) -> int:
    """Run the server. Errors go to stderr and stdout stays unused until MCP."""
    parser = argparse.ArgumentParser(prog="office.mcp")
    parser.add_argument(
        "--state-directory",
        required=True,
        help="Absolute directory for revisions.sqlite. APP_STATE_DIRECTORY is not read.",
    )
    args = parser.parse_args(argv)
    state = args.state_directory
    if not isinstance(state, str):
        print("The state directory is not valid.", file=sys.stderr)
        return 2
    try:
        serve(state)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
