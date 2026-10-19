"""Offline checks for the production image contract."""

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def test_docker_build_excludes_env_and_runs_as_non_root() -> None:
    ignored = (_ROOT / ".dockerignore").read_text(encoding="utf-8")
    dockerfile = (_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert ".env" in ignored.splitlines()
    assert ".env.*" in ignored.splitlines()
    assert "USER app" in dockerfile
    assert "uv sync --locked --no-dev --no-install-project" in dockerfile
    assert "uv sync --locked --no-dev --no-editable" in dockerfile
    assert "COPY --from=build --chown=app:app /app/.venv /app/.venv" in dockerfile
    assert 'CMD ["python", "-m", "organisation.health"]' in dockerfile
    assert "APP_STATE_DIRECTORY=/data" in dockerfile
    assert "COPY .env" not in dockerfile
