"""Offline checks for the release gate. These tests do not publish."""

import hashlib
import tomllib
from collections.abc import Callable
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
    assert versions_match("0.2.0", "0.2.1", "0.0.4", HARNESS_BOUND)
    assert not versions_match("0.2.0", "0.2.0", "0.0.4", HARNESS_BOUND)
    assert not versions_match("0.1.0", "0.2.1", "0.0.4", HARNESS_BOUND)


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
        archive.writestr(
            "agentbuildkit_templates/customer_service/static/fonts/OFL-InstrumentSerif.txt",
            "a",
        )
        archive.writestr(
            "agentbuildkit_templates/customer_service/static/fonts/"
            "instrument-serif-latin-400-italic.woff2",
            "a",
        )
        archive.writestr("agentbuildkit_templates/customer_service/dashboard_view.py", "a")
        archive.writestr("agentbuildkit_templates/operations_reporting/team.json", "{}")
        archive.writestr("LICENSE", "Apache")
        archive.writestr("agentbuildkit_templates/.env", "SECRET=1")
    problems = archive_problems(wheel, templates=True)
    assert problems == ["banned path: agentbuildkit_templates/.env"]

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
        "agentbuildkit-0.0.4.tgz",
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
    assert 'tarball="${GITHUB_WORKSPACE}/candidate/agentbuildkit-0.0.4.tgz"' in text
    assert 'test -f "$tarball"' in text
    assert 'npm publish --provenance --access public "$tarball"' in text
    assert "npm publish --provenance --access public candidate/" not in text
    assert "The npm tarball does not match the validated digest." in text
    npm_job = text.split("  publish-npm:", 1)[1].split("  verify-npm:", 1)[0]
    assert "release_publish.py npm-bridge candidate" in npm_job
    assert "require-match" not in npm_job
    assert npm_job.index("npm install -g npm@11.5.1") < npm_job.index(
        'npm publish --provenance --access public "$tarball"'
    )
    templates_job = text.split("  publish-templates:", 1)[1].split("  verify-python:", 1)[0]
    assert "release_publish.py templates-0.2.1 candidate" in templates_job
    assert "agentbuildkit_templates-0.2.1-py3-none-any.whl" in templates_job
    assert "agentbuildkit_templates-0.2.1.tar.gz" in templates_job
    assert "agentbuildkit_templates-0.2.0" not in templates_job
    assert "agentbuildkit-0.2.0" not in templates_job
    assert "npm publish" not in templates_job
    assert 'test "$(find publish -type f | wc -l)" -eq 2' in templates_job
    verify_job = text.split("  verify-python:", 1)[1].split("  publish-npm:", 1)[0]
    assert "templates-0.2.1 candidate require-match" in verify_job
    assert '"agentbuildkit==0.2.0"' in verify_job
    assert '"agentbuildkit-templates==0.2.1"' in verify_job
    assert "harness candidate require-match" not in verify_job
    assert "at a <em>glance</em>" in verify_job
    harness_job = text.split("  publish-harness:", 1)[1].split("  publish-templates:", 1)[0]
    assert "release_publish.py harness publish" in harness_job
    assert "decision == 'absent'" in harness_job


def test_release_workflows_stay_out_of_future_source_archives() -> None:
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    excluded = project["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]
    assert "/.github/workflows/release.yml" in excluded
    assert "/.github/workflows/release-validate.yml" in excluded


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fetch_bytes(data: bytes) -> Callable[[str], bytes]:
    def fetch(_url: str) -> bytes:
        return data

    return fetch


def _wheel(code: bytes, readme: bytes, metadata: bytes) -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("pkg/code.py", code)
        archive.writestr("pkg/README.md", readme)
        archive.writestr("pkg.dist-info/METADATA", metadata)
        archive.writestr("pkg.dist-info/RECORD", b"record")
    return buffer.getvalue()


def test_npm_bridge_keeps_published_code_and_allows_readme_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.release_publish import verify_npm_bridge

    published_wheel = _wheel(b"code", b"old-readme", b"old-meta")
    candidate_wheel = _wheel(b"code", b"new-readme", b"new-meta")
    files = {
        "agentbuildkit-0.2.0-py3-none-any.whl": candidate_wheel,
        "agentbuildkit-0.2.0.tar.gz": b"workflow-only-sdist",
        "agentbuildkit_templates-0.2.0-py3-none-any.whl": candidate_wheel,
        "agentbuildkit_templates-0.2.0.tar.gz": b"templates-sdist",
        "agentbuildkit-0.0.4.tgz": b"npm-tarball",
    }
    digests = {
        "agentbuildkit-0.2.0-py3-none-any.whl": _digest(published_wheel),
        "agentbuildkit-0.2.0.tar.gz": _digest(b"original-sdist"),
        "agentbuildkit_templates-0.2.0-py3-none-any.whl": _digest(published_wheel),
        "agentbuildkit_templates-0.2.0.tar.gz": _digest(b"templates-sdist"),
    }
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    monkeypatch.setattr("scripts.release_publish.ORIGINAL_RELEASE_DIGESTS", digests)
    monkeypatch.setattr("scripts.release_publish.fetch_bytes", _fetch_bytes(published_wheel))

    def fake_json(url: str) -> dict[str, object]:
        if "agentbuildkit-templates" in url:
            names = [name for name in digests if name.startswith("agentbuildkit_templates-")]
        else:
            names = [name for name in digests if name.startswith("agentbuildkit-")]
        urls: list[dict[str, object]] = []
        for name in names:
            urls.append(
                {
                    "filename": name,
                    "url": f"https://files.test/{name}",
                    "digests": {"sha256": digests[name]},
                }
            )
        return {"urls": urls}

    monkeypatch.setattr("scripts.release_publish.fetch_json", fake_json)
    verify_npm_bridge(tmp_path)
    (tmp_path / "agentbuildkit-0.0.4.tgz").write_bytes(b"different-tarball")
    verify_npm_bridge(tmp_path)
    (tmp_path / "agentbuildkit-0.2.0-py3-none-any.whl").write_bytes(
        _wheel(b"changed", b"new-readme", b"new-meta")
    )
    with pytest.raises(SystemExit, match=r"code does not match the published 0\.2\.0 release"):
        verify_npm_bridge(tmp_path)


def _templates_wheel() -> bytes:
    import io
    import zipfile

    members = {
        "agentbuildkit_templates/customer_service/team.json": b"{}",
        "agentbuildkit_templates/customer_service/static/dashboard.css": (
            b'url("fonts/instrument-serif-latin-400-italic.woff2")'
        ),
        "agentbuildkit_templates/customer_service/static/dashboard.js": b"a",
        "agentbuildkit_templates/customer_service/static/fonts/OFL-InstrumentSans.txt": b"a",
        "agentbuildkit_templates/customer_service/static/fonts/OFL-InstrumentSerif.txt": b"a",
        "agentbuildkit_templates/customer_service/static/fonts/"
        "instrument-serif-latin-400-italic.woff2": b"font",
        "agentbuildkit_templates/customer_service/dashboard_view.py": (
            b"<h1>Customer Service at a <em>glance</em></h1>"
        ),
        "agentbuildkit_templates/operations_reporting/team.json": b"{}",
        "LICENSE": b"Apache",
        "agentbuildkit_templates-0.2.1.dist-info/METADATA": (
            b"Metadata-Version: 2.4\n"
            b"Name: agentbuildkit-templates\n"
            b"Version: 0.2.1\n"
            b"Requires-Dist: agentbuildkit<0.3.0,>=0.2.0\n"
        ),
        "agentbuildkit_templates-0.2.1.dist-info/RECORD": b"record",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _write_templates_candidate(directory: Path, harness_wheel: bytes) -> None:
    (directory / "agentbuildkit-0.2.0-py3-none-any.whl").write_bytes(harness_wheel)
    (directory / "agentbuildkit-0.2.0.tar.gz").write_bytes(b"sdist-may-differ")
    (directory / "agentbuildkit_templates-0.2.1-py3-none-any.whl").write_bytes(_templates_wheel())
    (directory / "agentbuildkit_templates-0.2.1.tar.gz").write_bytes(b"templates-sdist")
    (directory / "agentbuildkit-0.0.4.tgz").write_bytes(b"npm-must-not-be-uploaded")


def test_templates_release_uploads_021_and_keeps_the_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.release_publish import TEMPLATES_RELEASE_AUTHORIZATION, verify_templates_release

    published_harness = _wheel(b"harness-code", b"old-readme", b"old-meta")
    digests = {
        "agentbuildkit-0.2.0-py3-none-any.whl": _digest(published_harness),
        "agentbuildkit-0.2.0.tar.gz": _digest(b"original-sdist"),
        "agentbuildkit_templates-0.2.0-py3-none-any.whl": _digest(b"old-templates"),
        "agentbuildkit_templates-0.2.0.tar.gz": _digest(b"old-templates-sdist"),
    }
    candidate_harness = _wheel(b"harness-code", b"readme-may-differ", b"metadata-may-differ")
    _write_templates_candidate(tmp_path, candidate_harness)
    monkeypatch.setattr("scripts.release_publish.ORIGINAL_RELEASE_DIGESTS", digests)
    monkeypatch.setattr("scripts.release_publish.fetch_bytes", _fetch_bytes(published_harness))

    def fake_json(url: str) -> dict[str, object] | None:
        if url.endswith("/0.2.1/json"):
            return None
        names = [
            name
            for name in digests
            if name.startswith("agentbuildkit-") and not name.startswith("agentbuildkit_")
        ]
        return {
            "urls": [
                {
                    "filename": name,
                    "url": f"https://files.test/{name}",
                    "digests": {"sha256": digests[name]},
                }
                for name in names
            ]
        }

    monkeypatch.setattr("scripts.release_publish.fetch_json", fake_json)
    assert verify_templates_release(tmp_path) == "absent"
    assert TEMPLATES_RELEASE_AUTHORIZATION == "publish-templates-0.2.1"

    (tmp_path / "agentbuildkit-0.2.0-py3-none-any.whl").write_bytes(
        _wheel(b"changed", b"readme-may-differ", b"metadata-may-differ")
    )
    with pytest.raises(SystemExit, match="Harness wheel code does not match"):
        verify_templates_release(tmp_path)


def test_templates_release_stops_when_021_bytes_differ(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.release_publish import verify_templates_release

    published_harness = _wheel(b"harness-code", b"old-readme", b"old-meta")
    digests = {
        "agentbuildkit-0.2.0-py3-none-any.whl": _digest(published_harness),
        "agentbuildkit-0.2.0.tar.gz": _digest(b"original-sdist"),
        "agentbuildkit_templates-0.2.0-py3-none-any.whl": _digest(b"old-templates"),
        "agentbuildkit_templates-0.2.0.tar.gz": _digest(b"old-templates-sdist"),
    }
    _write_templates_candidate(
        tmp_path,
        _wheel(b"harness-code", b"readme-may-differ", b"metadata-may-differ"),
    )
    monkeypatch.setattr("scripts.release_publish.ORIGINAL_RELEASE_DIGESTS", digests)
    monkeypatch.setattr("scripts.release_publish.fetch_bytes", _fetch_bytes(published_harness))

    def fake_json(url: str) -> dict[str, object]:
        if url.endswith("/0.2.1/json"):
            return {
                "urls": [
                    {
                        "filename": "agentbuildkit_templates-0.2.1-py3-none-any.whl",
                        "url": "https://files.test/wheel",
                        "digests": {"sha256": "different"},
                    },
                    {
                        "filename": "agentbuildkit_templates-0.2.1.tar.gz",
                        "url": "https://files.test/sdist",
                        "digests": {"sha256": "different"},
                    },
                ]
            }
        names = [
            name
            for name in digests
            if name.startswith("agentbuildkit-") and not name.startswith("agentbuildkit_")
        ]
        return {
            "urls": [
                {
                    "filename": name,
                    "url": f"https://files.test/{name}",
                    "digests": {"sha256": digests[name]},
                }
                for name in names
            ]
        }

    monkeypatch.setattr("scripts.release_publish.fetch_json", fake_json)
    with pytest.raises(SystemExit, match="does not match the validated candidate"):
        verify_templates_release(tmp_path)


def test_npm_bridge_refuses_the_templates_021_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.release_publish import verify_npm_bridge

    published_harness = _wheel(b"harness-code", b"old-readme", b"old-meta")
    digests = {
        "agentbuildkit-0.2.0-py3-none-any.whl": _digest(published_harness),
        "agentbuildkit-0.2.0.tar.gz": _digest(b"original-sdist"),
        "agentbuildkit_templates-0.2.0-py3-none-any.whl": _digest(b"old-templates"),
        "agentbuildkit_templates-0.2.0.tar.gz": _digest(b"old-templates-sdist"),
    }
    _write_templates_candidate(
        tmp_path,
        _wheel(b"harness-code", b"readme-may-differ", b"metadata-may-differ"),
    )
    monkeypatch.setattr("scripts.release_publish.ORIGINAL_RELEASE_DIGESTS", digests)
    monkeypatch.setattr("scripts.release_publish.fetch_bytes", _fetch_bytes(published_harness))

    def fake_json(url: str) -> dict[str, object]:
        if "agentbuildkit-templates" in url:
            names = [name for name in digests if name.startswith("agentbuildkit_templates-")]
        else:
            names = [name for name in digests if name.startswith("agentbuildkit-")]
        return {
            "urls": [
                {
                    "filename": name,
                    "url": f"https://files.test/{name}",
                    "digests": {"sha256": digests[name]},
                }
                for name in names
            ]
        }

    monkeypatch.setattr("scripts.release_publish.fetch_json", fake_json)
    with pytest.raises(
        SystemExit,
        match=r"agentbuildkit_templates-0\.2\.0-py3-none-any\.whl",
    ):
        verify_npm_bridge(tmp_path)
