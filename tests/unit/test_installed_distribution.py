"""Install built wheels into a clean environment and exercise them there.

The subprocesses do not receive this repository on PYTHONPATH. They start
outside the checkout so a local .env is not read.
"""

import os
import re
import subprocess
import tarfile
import textwrap
import zipfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

_ROOT = Path(__file__).resolve().parents[2]
_MARKER = "local-composio-marker"
_CANCEL = textwrap.dedent(
    """
    import asyncio
    import sys
    from pathlib import Path

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.shared.exceptions import McpError
    from mcp.types import CancelledNotification, CancelledNotificationParams, ClientNotification

    from agentbuildkit.drafting import build_server, quiet_diagnostics, require_state_directory
    from agentbuildkit.evaluation import EvaluationReport
    from agentbuildkit.revisions import RevisionService, SqliteRevisionStore
    from agentbuildkit_templates.office.templates import installed_templates


    def serve(state: str) -> None:
        directory = require_state_directory(state)

        async def evaluator(bundle: object) -> EvaluationReport:
            del bundle
            await asyncio.sleep(1)
            return EvaluationReport(
                total=1,
                passed=1,
                failed=0,
                pass_rate=1,
                elapsed_seconds=1,
                failures_by_assertion={},
                cases=(),
            )

        service = RevisionService(
            SqliteRevisionStore(directory / "revisions.sqlite"),
            installed_templates(),
            credentials=lambda _provider: False,
        )
        service.seed()
        quiet_diagnostics()
        server = build_server(service, {"operations-reporting": evaluator})
        try:
            server.run(transport="stdio")
        finally:
            service.close()


    async def cancel(state: str) -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=[__file__, "serve", state],
            cwd=str(Path(state).parent),
            env={key: value for key, value in os_env().items()},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                created = await session.call_tool(
                    "create_team",
                    {
                        "template_id": "operations-reporting",
                        "title": "Cancel Desk",
                        "description": "Cancellation check.",
                        "idempotency_key": "cancel-desk",
                    },
                )
                assert created.isError is False
                body = created.structuredContent
                issued = session._request_id
                task = asyncio.create_task(
                    session.call_tool(
                        "evaluate_draft",
                        {
                            "team_id": body["team_id"],
                            "draft_id": body["draft_id"],
                            "expected_version": body["draft_version"],
                            "timeout_seconds": 30,
                        },
                    )
                )
                for _ in range(100):
                    if session._request_id == issued + 1:
                        break
                    await asyncio.sleep(0.01)
                else:
                    raise AssertionError("The evaluation request was not sent.")
                await session.send_notification(
                    ClientNotification(
                        CancelledNotification(
                            params=CancelledNotificationParams(
                                requestId=issued,
                                reason="operator cancelled",
                            )
                        )
                    )
                )
                try:
                    result = await task
                except McpError as exc:
                    assert "cancel" in str(exc).lower()
                else:
                    assert result.isError is True
                    assert "cancel" in str(result.content).lower()
                await asyncio.sleep(1.3)
                ready = await session.call_tool(
                    "read_evaluation",
                    {"team_id": body["team_id"], "draft_id": body["draft_id"]},
                )
                assert ready.isError is False
                view = ready.structuredContent
                assert view["activation_ready"] is False
                assert view["evaluation_ok"] is None
                assert view["runnable"] is False


    def os_env() -> dict[str, str]:
        import os

        return dict(os.environ)


    if __name__ == "__main__":
        if sys.argv[1] == "serve":
            serve(sys.argv[2])
        else:
            asyncio.run(cancel(sys.argv[1]))
            print("cancel-ok")
    """
)


@pytest.fixture(scope="module")
def wheels(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build both distributions once, or reuse AGENTBUILDKIT_ARTIFACTS."""
    out = tmp_path_factory.mktemp("wheels")
    prebuilt = os.environ.get("AGENTBUILDKIT_ARTIFACTS", "").strip()
    if prebuilt:
        source = Path(prebuilt)
        copied = [
            path.name
            for path in sorted(source.iterdir())
            if path.is_file() and path.name.endswith((".whl", ".tar.gz"))
        ]
        if len(copied) < 4:
            raise AssertionError(f"expected four Python artifacts, found {copied}")
        for name in copied:
            target = out / name
            target.write_bytes((source / name).read_bytes())
        return out
    completed = subprocess.run(
        ["uv", "build", "--all-packages", "--out-dir", str(out)],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr[-4000:])
    return out


def test_artifacts_contain_resources_and_no_runtime_files(wheels: Path) -> None:
    wheel_names = sorted(path.name for path in wheels.glob("*.whl"))
    sdist_names = sorted(path.name for path in wheels.glob("*.tar.gz"))
    assert any(name.startswith("agentbuildkit-") for name in wheel_names)
    assert any(name.startswith("agentbuildkit_templates-") for name in wheel_names)
    assert any(name.startswith("agentbuildkit-") for name in sdist_names)
    assert any(name.startswith("agentbuildkit_templates-") for name in sdist_names)

    templates = next(wheels.glob("agentbuildkit_templates-*.whl"))
    with zipfile.ZipFile(templates) as archive:
        names = set(archive.namelist())
    required = {
        "agentbuildkit_templates/customer_service/team.json",
        "agentbuildkit_templates/customer_service/static/dashboard.css",
        "agentbuildkit_templates/customer_service/static/dashboard.js",
        "agentbuildkit_templates/customer_service/static/fonts/instrument-sans-latin-400-normal.woff2",
        "agentbuildkit_templates/customer_service/static/fonts/OFL-InstrumentSans.txt",
        "agentbuildkit_templates/customer_service/static/fonts/OFL-GeistMono.txt",
        "agentbuildkit_templates/operations_reporting/team.json",
        "agentbuildkit_templates/operations_reporting/jobs.py",
        "agentbuildkit_templates/office/mcp.py",
    }
    assert required <= names
    assert not any(
        name.startswith(("office/", "customer_service/", "operations_reporting/")) for name in names
    )
    banned_suffixes = (".env", ".sqlite", ".db", ".bak", ".pyc")
    banned = [
        name
        for name in names
        if name.endswith(banned_suffixes) or "credentials" in name or "__pycache__" in name
    ]
    assert banned == []

    source = next(wheels.glob("agentbuildkit_templates-*.tar.gz"))
    with tarfile.open(source) as archive:
        packed = {member.name.split("/", 1)[-1] for member in archive.getmembers()}
    assert "agentbuildkit_templates/customer_service/team.json" in packed
    assert "agentbuildkit_templates/customer_service/static/dashboard.css" in packed
    assert "LICENSE" in packed
    assert "agentbuildkit_templates/operations_reporting/jobs.py" in packed
    assert not any(
        name.endswith((".env", ".sqlite", ".db", ".bak", ".pyc")) or "__pycache__" in name
        for name in packed
    )


def test_harness_without_templates_reports_the_missing_package(
    wheels: Path,
    tmp_path: Path,
) -> None:
    agentkit = _install(tmp_path / "venv", wheels, templates=False)
    env = _env()
    work = tmp_path / "work"
    work.mkdir()
    version = _run(agentkit, ["version"], env, work)
    _assert_installed_locally(agentkit, env, work, templates=False)
    assert version.returncode == 0
    assert "AgentBuildKit 0.2.0" in version.stdout
    doctor = _run(agentkit, ["doctor"], env, work)
    assert doctor.returncode == 0
    assert "not installed" in doctor.stdout
    assert "agentbuildkit-templates" in doctor.stdout
    demo = _run(agentkit, ["demo", "--approve"], env, work)
    assert demo.returncode == 0, demo.stderr
    state = tmp_path / "unused"
    drafting = _run(
        agentkit,
        ["mcp", "serve", "--state-directory", str(state.resolve())],
        env,
        work,
    )
    assert drafting.returncode == 1
    assert drafting.stdout == ""
    assert "agentbuildkit-templates" in drafting.stderr
    assert "Traceback" not in drafting.stderr
    dashboard = _run(agentkit, ["dashboard", "--state-directory", str(state.resolve())], env, work)
    assert dashboard.returncode == 1
    assert "Traceback" not in dashboard.stderr
    assert not state.exists()


def test_one_install_selects_the_local_wheels(wheels: Path, tmp_path: Path) -> None:
    """Both wheels go to one resolver call and the harness is not PyPI 0.1.0."""
    agentkit = _install(tmp_path / "venv", wheels, templates=True, kind="wheel")
    env = _env()
    work = tmp_path / "work"
    work.mkdir()
    _assert_installed_locally(agentkit, env, work, templates=True)
    doctor = _run(agentkit, ["doctor"], env, work)
    assert doctor.returncode == 0
    templates_line = next(
        line for line in doctor.stdout.splitlines() if line.startswith("Templates")
    )
    assert "not installed" not in templates_line
    assert "installed" in templates_line
    version = _run(agentkit, ["version"], env, work)
    assert "AgentBuildKit 0.2.0" in version.stdout


_PUBLISHED_HARNESS = (
    "https://files.pythonhosted.org/packages/13/f1/"
    "e356538a2c3cf965e5c169148bc136418392775991ec9a1c3689e27958e4/"
    "agentbuildkit-0.1.0-py3-none-any.whl"
)


def test_published_harness_0_1_0_is_rejected(wheels: Path, tmp_path: Path) -> None:
    """The published 0.1.0 artifact does not satisfy the 0.2.0 templates."""
    path = tmp_path / "venv"
    subprocess.run(["uv", "venv", str(path), "--python", "3.12"], check=True, cwd=tmp_path)
    python = path / "bin" / "python"
    templates = next(wheels.glob("agentbuildkit_templates-*.whl"))
    refused = subprocess.run(
        ["uv", "pip", "install", "--python", str(python), _PUBLISHED_HARNESS, str(templates)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert refused.returncode != 0
    assert "0.2.0" in refused.stderr

    installed = subprocess.run(
        ["uv", "pip", "install", "--python", str(python), _PUBLISHED_HARNESS],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr[-4000:]
    forced = subprocess.run(
        ["uv", "pip", "install", "--python", str(python), "--no-deps", str(templates)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert forced.returncode == 0, forced.stderr[-4000:]
    env = _env()
    work = tmp_path / "work"
    work.mkdir()
    probe = textwrap.dedent(
        """
        import pathlib
        import agentbuildkit
        print(agentbuildkit.__version__)
        print(pathlib.Path(agentbuildkit.__file__).resolve())
        try:
            import agentbuildkit.hosting
        except ImportError:
            print("hosting-absent")
        try:
            import agentbuildkit_templates
        except Exception as exc:
            print(type(exc).__name__)
            print(exc)
        else:
            raise SystemExit("templates imported on the published harness")
        """
    )
    completed = _run(python, ["-c", probe], env, work)
    assert completed.returncode == 0, completed.stderr
    lines = completed.stdout.splitlines()
    assert lines[0] == "0.1.0"
    assert "site-packages" in lines[1]
    assert str(_ROOT / "src") not in lines[1]
    assert "hosting-absent" in lines
    assert "HarnessIncompatible" in lines
    message = completed.stdout
    assert "agentbuildkit.revisions.RevisionService" in message
    assert "agentbuildkit.teams.load_bundle_json" in message
    assert "agentbuildkit.drafting.build_server" in message
    assert "published agentbuildkit 0.1.0" in message
    assert "agentbuildkit>=0.2.0,<0.3.0" in message
    assert "Traceback" not in completed.stderr
    assert list(work.iterdir()) == []


def test_upgrade_from_the_published_harness_selects_the_local_pair(
    wheels: Path,
    tmp_path: Path,
) -> None:
    """A real 0.1.0 install is replaced by the local 0.2.0 pair."""
    path = tmp_path / "venv"
    subprocess.run(["uv", "venv", str(path), "--python", "3.12"], check=True, cwd=tmp_path)
    python = path / "bin" / "python"
    agentkit = path / "bin" / "agentkit"
    installed = subprocess.run(
        ["uv", "pip", "install", "--python", str(python), _PUBLISHED_HARNESS],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr[-4000:]
    env = _env()
    work = tmp_path / "work"
    work.mkdir()
    before = _run(agentkit, ["version"], env, work)
    assert "0.1.0" in before.stdout
    harness = next(
        item for item in wheels.glob("agentbuildkit-*.whl") if "templates" not in item.name
    )
    templates = next(wheels.glob("agentbuildkit_templates-*.whl"))
    upgraded = subprocess.run(
        ["uv", "pip", "install", "--python", str(python), str(harness), str(templates)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert upgraded.returncode == 0, upgraded.stderr[-4000:]
    after = _run(agentkit, ["version"], env, work)
    assert "AgentBuildKit 0.2.0" in after.stdout
    doctor = _run(agentkit, ["doctor"], env, work)
    assert doctor.returncode == 0, doctor.stderr
    templates_line = next(
        line for line in doctor.stdout.splitlines() if line.startswith("Templates")
    )
    assert "not installed" not in templates_line
    assert "installed" in templates_line
    assert list(work.iterdir()) == []


@pytest.mark.asyncio
async def test_installed_dashboard_and_drafting_use_only_the_wheels(
    wheels: Path,
    tmp_path: Path,
) -> None:
    agentkit = _install(tmp_path / "venv", wheels, templates=True, kind="sdist")
    env = _env()
    work = tmp_path / "work"
    work.mkdir()
    _assert_installed_locally(agentkit, env, work, templates=True)
    python = agentkit.parent / "python"

    packaged = _run(
        python,
        [
            "-c",
            "import importlib.util\n"
            "from importlib.resources import files\n"
            "raise SystemExit(0 if importlib.util.find_spec('composio') is None else 2)\n",
        ],
        env,
        work,
    )
    assert packaged.returncode == 0

    doctor = _run(agentkit, ["doctor"], env, work)
    assert doctor.returncode == 0
    assert "Templates" in doctor.stdout and "installed" in doctor.stdout
    assert "POSIX flock" in doctor.stdout
    assert _MARKER not in doctor.stdout

    bundle = work / "team.json"
    extracted = _run(
        python,
        [
            "-c",
            "from importlib.resources import files\n"
            "package = 'agentbuildkit_templates.operations_reporting'\n"
            "print(files(package).joinpath('team.json').read_text(), end='')\n",
        ],
        env,
        work,
    )
    assert extracted.returncode == 0
    bundle.write_text(extracted.stdout, encoding="utf-8")
    validated = _run(agentkit, ["team", "validate", str(bundle)], env, work)
    assert validated.returncode == 0, validated.stderr

    state = work / "state"
    state.mkdir()
    params = StdioServerParameters(
        command=str(agentkit),
        args=["mcp", "serve", "--state-directory", str(state.resolve())],
        cwd=str(work),
        env=env,
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        listed = await session.list_tools()
        names = {tool.name for tool in listed.tools}
        assert "create_team" in names
        assert "activate_team" not in names
        created = await session.call_tool(
            "create_team",
            {
                "template_id": "operations-reporting",
                "title": "MCP Installed",
                "description": "Created by the installed server.",
                "idempotency_key": "mcp-installed",
            },
        )
        assert created.isError is False
        body = created.structuredContent
        assert isinstance(body, dict)
        assert body["runnable"] is False
        team_id = str(body["team_id"])

    dash_env = dict(env)
    dash_env["COMPOSIO_API_KEY"] = _MARKER
    with _dashboard(agentkit, state, work, dash_env) as base:
        page = _get(base, f"/teams/{team_id}/")
        assert "MCP Installed" in page
        assert "Not active" in page
        assert 'name="as_of"' not in page
        assert _MARKER not in page
        style = _get(base, "/static/dashboard.css")
        assert "{" in style
        connections = _get(base, "/teams/customer-service/connections")
        assert "agentbuildkit[composio]" in connections
        assert _MARKER not in connections

        created_page = _post(
            base,
            "/teams/new",
            {
                "template_id": "operations-reporting",
                "title": "Activated Desk",
                "description": "Offline activation.",
                "idempotency_key": "activated-desk",
            },
        )
        draft = _path(created_page)
        assert "/drafts/" in draft
        editing = _get(base, draft)
        version = _field(editing, "expected_version")
        _post(base, f"{draft}/validate", {"expected_version": version})
        _post(base, f"{draft}/evaluate", {"expected_version": version})
        ready = _get(base, draft)
        digest = _field(ready, "expected_hash")
        _post(
            base,
            f"{draft}/activate",
            {"expected_version": version, "expected_hash": digest},
        )
        activated = draft.split("/drafts/")[0]

    with _dashboard(agentkit, state, work, env) as base:
        restarted = _get(base, activated + "/")
        form = _get(base, activated + "/?new=1")
        assert "Activated Desk" in restarted
        assert "New request" in restarted
        assert "Not active" not in restarted
        assert 'name="as_of"' in form
        assert _MARKER not in restarted + form

    script = work / "cancel_check.py"
    script.write_text(_CANCEL, encoding="utf-8")
    cancelled = _run(python, [str(script), str((work / "cancel-state").resolve())], env, work)
    assert cancelled.returncode == 0, cancelled.stderr
    assert cancelled.stdout.strip() == "cancel-ok"
    assert _MARKER not in cancelled.stderr


def _install(path: Path, artifacts: Path, *, templates: bool, kind: str = "wheel") -> Path:
    """Install local artifacts in one resolver operation. Do not use --no-deps."""
    subprocess.run(["uv", "venv", str(path), "--python", "3.12"], check=True, cwd=_ROOT)
    python = path / "bin" / "python"
    suffix = ".whl" if kind == "wheel" else ".tar.gz"
    harness = next(
        item for item in artifacts.glob(f"agentbuildkit-*{suffix}") if "templates" not in item.name
    )
    selected = [str(harness)]
    if templates:
        selected.append(str(next(artifacts.glob(f"agentbuildkit_templates-*{suffix}"))))
    completed = subprocess.run(
        ["uv", "pip", "install", "--python", str(python), *selected],
        cwd=path,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr[-4000:])
    return path / "bin" / "agentkit"


def _assert_installed_locally(
    agentkit: Path,
    env: dict[str, str],
    work: Path,
    *,
    templates: bool,
) -> None:
    """The installed harness is this build. Published 0.1.0 has no hosting module."""
    script = textwrap.dedent(
        """
        import importlib.util
        import pathlib
        import agentbuildkit
        import agentbuildkit.hosting
        print(pathlib.Path(agentbuildkit.__file__).resolve())
        print(agentbuildkit.hosting.TEMPLATES_MODULE)
        print("bound=" + repr(agentbuildkit.hosting.HARNESS_RELEASE_BOUND))
        for name in ("office", "customer_service", "operations_reporting"):
            print(name, importlib.util.find_spec(name) is not None)
        """
    )
    if templates:
        script += textwrap.dedent(
            """
            import agentbuildkit_templates
            print(pathlib.Path(agentbuildkit_templates.__file__).resolve())
            print("templates-bound=" + repr(agentbuildkit_templates.HARNESS_RELEASE_BOUND))
            """
        )
    completed = _run(agentkit.parent / "python", ["-c", script], env, work)
    assert completed.returncode == 0, completed.stderr
    lines = [line for line in completed.stdout.splitlines() if line]
    assert "site-packages" in lines[0]
    assert str(_ROOT / "src") not in lines[0]
    assert lines[1] == "agentbuildkit_templates"
    assert lines[2] == "bound='agentbuildkit>=0.2.0,<0.3.0'"
    assert lines[3:6] == ["office False", "customer_service False", "operations_reporting False"]
    if templates:
        assert "site-packages" in lines[6]
        assert str(_ROOT) not in lines[6]
        assert lines[7] == "templates-bound='agentbuildkit>=0.2.0,<0.3.0'"


def _env() -> dict[str, str]:
    blocked = {
        "PYTHONPATH",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "COMPOSIO_API_KEY",
        "APP_STATE_DIRECTORY",
    }
    env = {key: value for key, value in os.environ.items() if key not in blocked}
    env["PYTHONSAFEPATH"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _run(
    command: Path,
    args: list[str],
    env: dict[str, str],
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(command), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


class _Dashboard:
    def __init__(self, command: Path, state: Path, cwd: Path, env: dict[str, str]) -> None:
        self._process = subprocess.Popen(
            [
                str(command),
                "dashboard",
                "--state-directory",
                str(state.resolve()),
                "--host",
                "127.0.0.1",
                "--port",
                "0",
            ],
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert self._process.stdout is not None
        line = self._process.stdout.readline()
        match = re.search(r"http://127\.0\.0\.1:\d+", line)
        if match is None:
            error = ""
            if self._process.stderr is not None:
                error = self._process.stderr.read()
            self._process.kill()
            raise AssertionError(f"{line}\n{error}")
        self.base = match.group(0)

    def close(self) -> None:
        self._process.terminate()
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._process.kill()

    def __enter__(self) -> str:
        return self.base

    def __exit__(self, *_args: object) -> None:
        self.close()


def _dashboard(command: Path, state: Path, cwd: Path, env: dict[str, str]) -> _Dashboard:
    return _Dashboard(command, state, cwd, env)


def _get(base: str, path: str) -> str:
    with urlopen(urljoin(base, path), timeout=30) as response:
        return response.read().decode()


def _post(base: str, path: str, fields: dict[str, str]) -> str:
    request = Request(
        urljoin(base, path),
        data=urlencode(fields).encode(),
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            return response.geturl()
    except HTTPError as exc:
        if exc.code != 303:
            raise
        location = exc.headers.get("Location")
        if not isinstance(location, str):
            raise
        return location


def _path(location: str) -> str:
    if location.startswith("http://"):
        return "/" + location.split("/", 3)[-1]
    return location


def _field(page: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]+)"', page)
    assert match is not None, name
    return match.group(1)
