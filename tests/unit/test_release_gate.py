"""Offline checks for the release gate. These tests do not publish."""

from pathlib import Path

import pytest
from scripts.release_gate import (
    HARNESS_BOUND,
    archive_problems,
    classify_release,
    verify_files,
    verify_run,
    versions_match,
    write_manifest,
)
from scripts.release_publish import local_digests

_ROOT = Path(__file__).resolve().parents[2]


def test_absent_remote_can_be_published() -> None:
    assert classify_release({"a.whl": "abc"}, {}) == "absent"


def test_matching_remote_can_be_resumed() -> None:
    files = {"agentbuildkit-0.2.0-py3-none-any.whl": "abc"}
    assert classify_release(files, dict(files)) == "match"


def test_different_bytes_are_a_conflict() -> None:
    local = {"agentbuildkit-0.2.0-py3-none-any.whl": "local"}
    remote = {"agentbuildkit-0.2.0-py3-none-any.whl": "remote"}
    assert classify_release(local, remote) == "conflict"


def test_a_partial_file_list_is_a_conflict() -> None:
    local = {"one.whl": "abc", "one.tar.gz": "def"}
    remote = {"one.whl": "abc"}
    assert classify_release(local, remote) == "conflict"


def test_prepared_versions_match() -> None:
    assert versions_match("0.2.0", "0.2.0", "0.0.3", HARNESS_BOUND)
    assert not versions_match("0.1.0", "0.2.0", "0.0.3", HARNESS_BOUND)


def test_remote_digest_map_uses_sha256() -> None:
    from scripts.release_publish import remote_digests

    payload = {
        "urls": [
            {"filename": "agentbuildkit-0.2.0-py3-none-any.whl", "digests": {"sha256": "abc"}},
            {"filename": "agentbuildkit-0.2.0.tar.gz", "digests": {"sha256": "def"}},
        ]
    }
    assert remote_digests(payload) == {
        "agentbuildkit-0.2.0-py3-none-any.whl": "abc",
        "agentbuildkit-0.2.0.tar.gz": "def",
    }


def test_a_missing_registry_body_is_absent() -> None:
    from scripts.release_publish import decide

    assert decide({"agentbuildkit-0.2.0-py3-none-any.whl": "abc"}, {}) == "absent"


def test_archive_scan_rejects_state_and_requires_templates(tmp_path: Path) -> None:
    import zipfile

    wheel = tmp_path / "agentbuildkit_templates-0.2.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("agentbuildkit_templates/customer_service/team.json", "{}")
        archive.writestr("agentbuildkit_templates/customer_service/static/dashboard.css", "a")
        archive.writestr("agentbuildkit_templates/customer_service/static/dashboard.js", "a")
        archive.writestr(
            "agentbuildkit_templates/customer_service/static/fonts/OFL-InstrumentSans.txt",
            "a",
        )
        archive.writestr("agentbuildkit_templates/operations_reporting/team.json", "{}")
        archive.writestr("LICENSE", "Apache")
        archive.writestr("agentbuildkit_templates/.env", "SECRET=1")
    problems = archive_problems(wheel, templates=True)
    assert any(item.startswith("banned path:") for item in problems)

    source = tmp_path / "agentbuildkit-0.2.0.tar.gz"
    import io
    import tarfile

    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        data = b"{}"
        info = tarfile.TarInfo("agentbuildkit-0.2.0/.impeccable/config.json")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    source.write_bytes(payload.getvalue())
    assert archive_problems(source, templates=False) == ["banned path: .impeccable/config.json"]


def test_manifest_rejects_a_changed_artifact(tmp_path: Path) -> None:
    names = (
        "agentbuildkit-0.2.0-py3-none-any.whl",
        "agentbuildkit-0.2.0.tar.gz",
        "agentbuildkit_templates-0.2.0-py3-none-any.whl",
        "agentbuildkit_templates-0.2.0.tar.gz",
        "agentbuildkit-0.0.3.tgz",
    )
    for name in names:
        (tmp_path / name).write_bytes(name.encode())
    write_manifest(tmp_path, "abc", "99")
    verify_files(tmp_path, source_commit="abc", workflow_run="99")
    (tmp_path / names[0]).write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="validated digest"):
        verify_files(tmp_path, source_commit="abc", workflow_run="99")


def test_validation_run_must_be_the_manual_workflow() -> None:
    verify_run(
        {
            "conclusion": "success",
            "event": "workflow_dispatch",
            "path": ".github/workflows/release-validate.yml",
            "head_sha": "abc",
        },
        "abc",
    )
    with pytest.raises(RuntimeError, match="manual validation"):
        verify_run(
            {
                "conclusion": "success",
                "event": "release",
                "path": ".github/workflows/release-validate.yml",
                "head_sha": "abc",
            },
            "abc",
        )


def test_package_digests_do_not_cross_packages(tmp_path: Path) -> None:
    (tmp_path / "agentbuildkit-0.2.0-py3-none-any.whl").write_bytes(b"h")
    (tmp_path / "agentbuildkit-0.2.0.tar.gz").write_bytes(b"hs")
    (tmp_path / "agentbuildkit_templates-0.2.0-py3-none-any.whl").write_bytes(b"t")
    (tmp_path / "agentbuildkit_templates-0.2.0.tar.gz").write_bytes(b"ts")
    harness = local_digests(tmp_path, "harness")
    templates = local_digests(tmp_path, "templates")
    assert set(harness) == {
        "agentbuildkit-0.2.0-py3-none-any.whl",
        "agentbuildkit-0.2.0.tar.gz",
    }
    assert set(templates) == {
        "agentbuildkit_templates-0.2.0-py3-none-any.whl",
        "agentbuildkit_templates-0.2.0.tar.gz",
    }


def test_validation_workflow_cannot_publish() -> None:
    text = (_ROOT / ".github/workflows/release-validate.yml").read_text(encoding="utf-8")
    trigger = text.split("\npermissions:", 1)[0]
    assert "workflow_dispatch:" in trigger
    assert "\n  push:" not in trigger
    assert "pull_request:" not in trigger
    assert "\n  release:" not in trigger
    assert "id-token:" not in text
    assert "npm publish" not in text
    assert "pypi-publish" not in text
    assert 'mkdir -p "$GITHUB_WORKSPACE/build/npm"' in text


def test_publish_workflow_reuses_the_validated_candidate() -> None:
    text = (_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    trigger = text.split("\npermissions:", 1)[0]
    assert "workflow_dispatch:" in trigger
    assert "\n  push:" not in trigger
    assert "pull_request:" not in trigger
    assert "\n  release:" not in trigger
    assert "uv build" not in text
    assert "npm pack" not in text
    assert "download-artifact" in text
    assert "is not an approval" in text
    assert 'tarball="${GITHUB_WORKSPACE}/candidate/agentbuildkit-0.0.3.tgz"' in text
    assert 'test -f "$tarball"' in text
    assert 'npm publish --provenance --access public "$tarball"' in text
    assert "npm publish --provenance --access public candidate/" not in text
    assert "The npm tarball does not match the validated digest." in text
