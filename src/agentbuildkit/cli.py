"""AgentBuildKit command line.

``demo``, ``doctor``, ``version``, ``team``, and ``mcp`` are the current commands.
Add later commands as new subparsers without changing the ones that already exist.
"""

import argparse
import asyncio
import os
import sqlite3
import sys
import traceback
from collections.abc import Callable, Sequence

import agentbuildkit
from agentbuildkit.demo import DemoError, run_demo
from agentbuildkit.teams import load_bundle

_OPENAI_KEY = "OPENAI_API_KEY"
_ANTHROPIC_KEY = "ANTHROPIC_API_KEY"


def main() -> None:
    """Console-script entry point."""
    raise SystemExit(run(sys.argv[1:]))


def run(argv: Sequence[str]) -> int:
    """Run one AgentBuildKit command and return its exit status."""
    parser = _parser()
    try:
        args = parser.parse_args(list(argv))
    except SystemExit as exc:
        if isinstance(exc.code, int):
            return exc.code
        return 0
    command = args.command
    if command == "version":
        print(f"AgentBuildKit {agentbuildkit.__version__}")
        return 0
    if command == "doctor":
        return doctor()
    if command == "team":
        try:
            load_bundle(args.path)
        except (OSError, ValueError):
            print("Team bundle is unreadable or invalid.", file=sys.stderr)
            return 1
        print("Team bundle structure is valid. Runtime bindings have not been checked.")
        return 0
    if command == "demo":
        return _demo(approve=args.approve, reject=args.reject, verbose=args.verbose)
    if command == "mcp":
        return _mcp_serve(args.state_directory)
    print("Unknown command.", file=sys.stderr)
    return 2


def doctor(*, writer: Callable[[str], None] = print) -> int:
    """Report local installation checks. Missing provider keys are not a failure."""
    python = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    package = agentbuildkit.__version__
    sqlite_ok, sqlite_version = _sqlite()
    writer("AgentBuildKit Doctor")
    writer("")
    writer(_status("Python", python, python != ""))
    writer(_status("AgentBuildKit", package, package != ""))
    writer(_status("SQLite", sqlite_version, sqlite_ok))
    writer("")
    writer("Providers")
    writer(f"{'OpenAI':<18} {_credential(_OPENAI_KEY)}")
    writer(f"{'Anthropic':<18} {_credential(_ANTHROPIC_KEY)}")
    writer("")
    healthy = python != "" and package != "" and sqlite_ok
    if healthy:
        writer("Status: ready for offline demo")
        return 0
    writer("Status: installation check failed")
    return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentkit",
        description="AgentBuildKit is a harness for reliable AI agents.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("version", help="Print the AgentBuildKit version.")
    commands.add_parser("doctor", help="Check the local installation.")
    team = commands.add_parser("team", help="Inspect declarative team configuration.")
    team_commands = team.add_subparsers(dest="team_command", required=True)
    validate = team_commands.add_parser("validate", help="Validate a JSON team bundle.")
    validate.add_argument("path", help="Path to the JSON bundle.")
    demo = commands.add_parser("demo", help="Run the offline harness demonstration.")
    choice = demo.add_mutually_exclusive_group()
    choice.add_argument(
        "--approve",
        action="store_true",
        help="Approve the side effect without prompting.",
    )
    choice.add_argument(
        "--reject",
        action="store_true",
        help="Reject the side effect without prompting.",
    )
    demo.add_argument("--verbose", action="store_true", help="Show an error traceback.")
    mcp = commands.add_parser("mcp", help="Serve local team drafting over stdio.")
    mcp_commands = mcp.add_subparsers(dest="mcp_command", required=True)
    serve = mcp_commands.add_parser(
        "serve",
        help="Serve drafting tools on stdio. This does not read APP_STATE_DIRECTORY.",
    )
    serve.add_argument(
        "--state-directory",
        required=True,
        help="Absolute directory for revisions.sqlite.",
    )
    return parser


def _mcp_serve(state_directory: object) -> int:
    """Start the repository drafting server. The published package has no templates."""
    if not isinstance(state_directory, str):
        print("The state directory is not valid.", file=sys.stderr)
        return 2
    try:
        from office.mcp import serve
    except ImportError:
        print(
            "Team drafting is available from a repository checkout with src and "
            "examples on PYTHONPATH. It is not part of the published package.",
            file=sys.stderr,
        )
        return 1
    try:
        serve(state_directory)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


def _demo(*, approve: bool, reject: bool, verbose: bool) -> int:
    if approve:
        decision: bool | None = True
    elif reject:
        decision = False
    else:
        decision = None
    try:
        result = asyncio.run(run_demo(decision=decision, writer=print))
    except DemoError as exc:
        print(exc)
        return 1
    except Exception:
        print("The demo could not finish.")
        if verbose:
            traceback.print_exc()
        return 1
    return 0 if result.ok else 1


def _status(label: str, detail: str, ok: bool) -> str:
    mark = "✓" if ok else "✗"
    return f"{label:<18} {detail:<16} {mark}"


def _credential(name: str) -> str:
    value = os.environ.get(name)
    if isinstance(value, str) and value.strip() != "":
        return "configured"
    return "not configured"


def _sqlite() -> tuple[bool, str]:
    try:
        connection = sqlite3.connect(":memory:")
    except sqlite3.Error:
        return False, ""
    try:
        row = connection.execute("select sqlite_version()").fetchone()
    except sqlite3.Error:
        return False, ""
    finally:
        connection.close()
    if row is None or not isinstance(row[0], str) or row[0] == "":
        return False, ""
    return True, row[0]
