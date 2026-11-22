"""Release-artifact checks. This module does not publish packages."""

from __future__ import annotations

import hashlib
import json
import tarfile
import zipfile
from collections.abc import Mapping
from pathlib import Path

HARNESS_VERSION = "0.2.0"
TEMPLATES_VERSION = "0.2.0"
HARNESS_BOUND = "agentbuildkit>=0.2.0,<0.3.0"
NPM_VERSION = "0.0.3"

_BANNED_SUFFIXES = (".env", ".sqlite", ".db", ".bak", ".pyc")
_TEMPLATE_PATHS = (
    "agentbuildkit_templates/customer_service/team.json",
    "agentbuildkit_templates/customer_service/static/dashboard.css",
    "agentbuildkit_templates/customer_service/static/dashboard.js",
    "agentbuildkit_templates/customer_service/static/fonts/OFL-InstrumentSans.txt",
    "agentbuildkit_templates/operations_reporting/team.json",
)


def classify_release(local: dict[str, str], remote: dict[str, str]) -> str:
    """Return absent, match, or conflict. Never treat a different file as skippable."""
    if not remote:
        return "absent"
    if set(local) != set(remote):
        return "conflict"
    for name, digest in local.items():
        if remote.get(name) != digest:
            return "conflict"
    return "match"


def versions_match(harness: str, templates: str, npm: str, bound: str) -> bool:
    """True when the candidate metadata is the prepared 0.2.0 release."""
    return (
        harness == HARNESS_VERSION
        and templates == TEMPLATES_VERSION
        and npm == NPM_VERSION
        and bound == HARNESS_BOUND
    )


def file_digest(path: Path) -> str:
    """SHA-256 of one release artifact."""
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def candidate_files(directory: Path) -> list[Path]:
    """Python distributions and the npm tarball. The manifest is not an artifact."""
    selected: list[Path] = []
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.name.endswith((".whl", ".tar.gz", ".tgz")):
            selected.append(path)
    return selected


def write_manifest(directory: Path, source_commit: str, workflow_run: str) -> dict[str, object]:
    """Record the exact candidate files. Publication must reuse this manifest."""
    files = [
        {"name": path.name, "sha256": file_digest(path)} for path in candidate_files(directory)
    ]
    if len(files) != 5:
        raise RuntimeError(f"expected 5 artifacts, found {[item['name'] for item in files]}")
    manifest: dict[str, object] = {
        "source_commit": source_commit,
        "workflow_run": str(workflow_run),
        "workflow_file": ".github/workflows/release-validate.yml",
        "retention_days": 14,
        "files": files,
    }
    text = json.dumps(manifest, indent=2) + "\n"
    (directory / "manifest.json").write_text(text, encoding="utf-8")
    return manifest


def verify_files(directory: Path, *, source_commit: str, workflow_run: str) -> None:
    """Stop when the downloaded candidate is not the validated file set."""
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError("The candidate has no manifest. Run release validation again.")
    loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise RuntimeError("The candidate manifest is not an object.")
    manifest: dict[str, object] = loaded
    if manifest.get("source_commit") != source_commit:
        raise RuntimeError("The candidate was built from a different commit. Run validation again.")
    if str(manifest.get("workflow_run")) != str(workflow_run):
        raise RuntimeError("The candidate manifest is for a different workflow run.")
    if manifest.get("workflow_file") != ".github/workflows/release-validate.yml":
        raise RuntimeError("The candidate did not come from the validation workflow.")
    listed = manifest.get("files")
    if not isinstance(listed, list):
        raise RuntimeError("The candidate manifest has no file list.")
    expected: dict[str, str] = {}
    for item in listed:
        if not isinstance(item, dict):
            raise RuntimeError("The candidate manifest has an invalid file entry.")
        name = item.get("name")
        digest = item.get("sha256")
        if not isinstance(name, str) or not isinstance(digest, str):
            raise RuntimeError("The candidate manifest has an invalid file entry.")
        expected[name] = digest
    actual = {path.name: file_digest(path) for path in candidate_files(directory)}
    if set(actual) != set(expected):
        raise RuntimeError("The candidate files do not match the manifest. Run validation again.")
    for name, digest in expected.items():
        if actual[name] != digest:
            raise RuntimeError(f"{name} does not match the validated digest. Run validation again.")


def verify_run(payload: Mapping[str, object], expected_commit: str) -> None:
    """Stop unless this is a successful validation run for the selected commit."""
    if payload.get("conclusion") != "success":
        raise RuntimeError("The validation run did not succeed. Run validation again.")
    if payload.get("event") != "workflow_dispatch":
        raise RuntimeError("The validation run was not a manual validation.")
    if payload.get("path") != ".github/workflows/release-validate.yml":
        raise RuntimeError("The run is not the release validation workflow.")
    if payload.get("head_sha") != expected_commit:
        raise RuntimeError(
            "This workflow is not running on the validated commit. Run validation again."
        )


def archive_names(path: Path) -> set[str]:
    """Member names from a wheel or source distribution."""
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return set(archive.namelist())
    with tarfile.open(path) as archive:
        return {member.name.split("/", 1)[-1] for member in archive.getmembers()}


def archive_problems(path: Path, *, templates: bool) -> list[str]:
    """Problems in one artifact. An empty list means the scan passed."""
    names = archive_names(path)
    problems: list[str] = []
    for name in sorted(names):
        if _banned(name):
            problems.append(f"banned path: {name}")
    if templates:
        for required in _TEMPLATE_PATHS:
            if required not in names and not any(name.endswith(required) for name in names):
                problems.append(f"missing path: {required}")
        if not any(name == "LICENSE" or name.endswith("/LICENSE") for name in names):
            problems.append("missing path: LICENSE")
    return problems


def _banned(name: str) -> bool:
    parts = name.lower().split("/")
    basename = parts[-1]
    if "__pycache__" in parts or ".impeccable" in parts or "credentials" in basename:
        return True
    return basename.endswith(_BANNED_SUFFIXES)
