"""Compare local artifacts with a registry response. This module does not publish."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import urllib.error
import urllib.request
import zipfile
from collections.abc import Mapping
from pathlib import Path

from scripts.release_gate import (
    HARNESS_VERSION,
    NPM_VERSION,
    TEMPLATES_VERSION,
    archive_problems,
    classify_release,
)

_HARNESS_URL = "https://pypi.org/pypi/agentbuildkit/0.2.0/json"
_TEMPLATES_URL = "https://pypi.org/pypi/agentbuildkit-templates/0.2.0/json"

# This authorizes the current npm publication while the published Python 0.2.0
# files stay on PyPI. Wheel code must match those files. README text and wheel
# metadata may differ because they are not uploaded. The npm tarball is the
# validated candidate.
NPM_BRIDGE_AUTHORIZATION = "publish-npm-0.0.4"
ORIGINAL_RELEASE_DIGESTS: dict[str, str] = {
    "agentbuildkit-0.2.0-py3-none-any.whl": (
        "b42c49f0ce7d4a38fa1cca89f902fe06beb6b36ed2c507f1b4c08542ab2b60fa"
    ),
    "agentbuildkit-0.2.0.tar.gz": (
        "5eb89609940d58740ecc980ed08c5f7c9fb4b002bea0187482836149d61ff209"
    ),
    "agentbuildkit_templates-0.2.0-py3-none-any.whl": (
        "11800f794f3f0160893fba72971efe9c266df965ba4f54e9675968407c8e621e"
    ),
    "agentbuildkit_templates-0.2.0.tar.gz": (
        "873030192f3eefad10d1253829dad97679522599dbc066e903c41ba2a28daeae"
    ),
}
_CANDIDATE_WHEELS = (
    "agentbuildkit-0.2.0-py3-none-any.whl",
    "agentbuildkit_templates-0.2.0-py3-none-any.whl",
)
_HARNESS_WHEEL = f"agentbuildkit-{HARNESS_VERSION}-py3-none-any.whl"
_TEMPLATES_WHEEL = f"agentbuildkit_templates-{TEMPLATES_VERSION}-py3-none-any.whl"
_TEMPLATES_SDIST = f"agentbuildkit_templates-{TEMPLATES_VERSION}.tar.gz"
_TEMPLATES_NEXT_URL = f"https://pypi.org/pypi/agentbuildkit-templates/{TEMPLATES_VERSION}/json"
TEMPLATES_RELEASE_AUTHORIZATION = f"publish-templates-{TEMPLATES_VERSION}"
_DASHBOARD_FONT = "instrument-serif-latin-400-italic.woff2"
_DASHBOARD_HEADING = "at a <em>glance</em>"


def remote_digests(payload: Mapping[str, object]) -> dict[str, str]:
    """Map filenames to sha256 values from a PyPI JSON body."""
    urls = payload.get("urls")
    if not isinstance(urls, list):
        return {}
    found: dict[str, str] = {}
    for item in urls:
        if not isinstance(item, dict):
            continue
        filename = item.get("filename")
        digests = item.get("digests")
        if isinstance(filename, str) and isinstance(digests, dict):
            digest = digests.get("sha256")
            if isinstance(digest, str):
                found[filename] = digest
    return found


def local_digests(directory: Path, package: str) -> dict[str, str]:
    """SHA-256 of one package's wheel and source distribution."""
    prefix = "agentbuildkit_templates-" if package == "templates" else "agentbuildkit-"
    files = [
        path
        for path in directory.iterdir()
        if path.is_file()
        and path.name.startswith(prefix)
        and path.name.endswith((".whl", ".tar.gz"))
        and not (package == "harness" and path.name.startswith("agentbuildkit_templates-"))
    ]
    ordered = sorted(files, key=lambda item: item.name)
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in ordered}


def remote_urls(payload: Mapping[str, object]) -> dict[str, str]:
    """Map filenames to download URLs from a PyPI JSON body."""
    urls = payload.get("urls")
    if not isinstance(urls, list):
        return {}
    found: dict[str, str] = {}
    for item in urls:
        if not isinstance(item, dict):
            continue
        filename = item.get("filename")
        url = item.get("url")
        if isinstance(filename, str) and isinstance(url, str):
            found[filename] = url
    return found


def fetch_bytes(url: str) -> bytes:
    """Download one published file."""
    request = urllib.request.Request(url)
    with urllib.request.urlopen(request, timeout=60) as response:
        body = response.read()
    if not isinstance(body, bytes):
        raise RuntimeError("The published file was not bytes.")
    return body


def wheel_code_members(data: bytes) -> dict[str, str]:
    """SHA-256 of wheel members that are package code, not documentation."""
    found: dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = info.filename
            if name.endswith(".dist-info/METADATA") or name.endswith(".dist-info/RECORD"):
                continue
            if name.endswith("/README.md"):
                continue
            found[name] = hashlib.sha256(archive.read(info)).hexdigest()
    return found


def fetch_json(url: str) -> dict[str, object] | None:
    """Return the JSON body, or None when the version is not published."""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    if not isinstance(body, dict):
        raise RuntimeError("The registry response was not an object.")
    return body


def write_decision(decision: str) -> None:
    """Record the decision for the workflow and print it."""
    print(decision)
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(f"decision={decision}\n")


def _published_python(package: str) -> dict[str, str]:
    """The original Python files for one distribution. The npm tarball is excluded."""
    prefix = "agentbuildkit_templates-" if package == "templates" else "agentbuildkit-"
    return {
        name: digest
        for name, digest in ORIGINAL_RELEASE_DIGESTS.items()
        if name.startswith(prefix)
        and name.endswith((".whl", ".tar.gz"))
        and not (package == "harness" and name.startswith("agentbuildkit_templates-"))
    }


def verify_npm_bridge(directory: Path) -> None:
    """Publish the validated npm candidate without replacing Python 0.2.0.

    PyPI must still serve the original 0.2.0 files. Candidate wheel code must
    match those files. README text and wheel metadata may differ and are not
    uploaded. The npm tarball is this commit's validated candidate.
    """
    payloads: dict[str, dict[str, object]] = {}
    for package, url in (("harness", _HARNESS_URL), ("templates", _TEMPLATES_URL)):
        remote = fetch_json(url)
        if remote is None:
            raise SystemExit("The original Python 0.2.0 packages must stay published.")
        published = remote_digests(remote)
        expected = _published_python(package)
        if published != expected:
            raise SystemExit(
                "The published Python packages are not the original 0.2.0 release. Stop."
            )
        payloads[package] = remote
    for name in _CANDIDATE_WHEELS:
        path = directory / name
        if not path.is_file():
            raise SystemExit(f"{name} is not a regular file in the validated candidate.")
        package = "templates" if name.startswith("agentbuildkit_templates-") else "harness"
        url = remote_urls(payloads[package]).get(name)
        if url is None:
            raise SystemExit(f"The published release has no download for {name}. Stop.")
        if wheel_code_members(path.read_bytes()) != wheel_code_members(fetch_bytes(url)):
            raise SystemExit(f"{name} code does not match the published 0.2.0 release. Stop.")
    for name in (
        "agentbuildkit-0.2.0.tar.gz",
        "agentbuildkit_templates-0.2.0.tar.gz",
    ):
        if not (directory / name).is_file():
            raise SystemExit(f"{name} is not a regular file in the validated candidate.")
    tarball = directory / f"agentbuildkit-{NPM_VERSION}.tgz"
    if not tarball.is_file():
        raise SystemExit("The validated npm tarball is not a regular file.")
    print(NPM_BRIDGE_AUTHORIZATION)


def verify_templates_release(directory: Path, *, require_match: bool = False) -> str:
    """Authorize uploading agentbuildkit-templates 0.2.1 and nothing else.

    The published harness 0.2.0 bytes must stay the original release. Candidate
    harness code must match that wheel and is not uploaded. The candidate
    templates files must be exactly version 0.2.1 and must include the dashboard
    redesign. This check does not authorize an npm publish.
    """
    harness_remote = fetch_json(_HARNESS_URL)
    if harness_remote is None:
        raise SystemExit("agentbuildkit 0.2.0 is not on PyPI. Stop.")
    if remote_digests(harness_remote) != _published_python("harness"):
        raise SystemExit("The published harness is not the original 0.2.0 release. Stop.")
    harness_wheel = directory / _HARNESS_WHEEL
    if not harness_wheel.is_file():
        raise SystemExit(f"{_HARNESS_WHEEL} is not a regular file in the validated candidate.")
    harness_url = remote_urls(harness_remote).get(_HARNESS_WHEEL)
    if harness_url is None:
        raise SystemExit("The published harness has no wheel download. Stop.")
    published_code = wheel_code_members(fetch_bytes(harness_url))
    if wheel_code_members(harness_wheel.read_bytes()) != published_code:
        raise SystemExit("Harness wheel code does not match the published 0.2.0 release. Stop.")

    present = sorted(
        path.name
        for path in directory.iterdir()
        if path.is_file() and path.name.startswith("agentbuildkit_templates-")
    )
    expected = sorted((_TEMPLATES_WHEEL, _TEMPLATES_SDIST))
    if present != expected:
        raise SystemExit(
            "The candidate templates files are not exactly agentbuildkit-templates "
            f"{TEMPLATES_VERSION}. Stop."
        )
    wheel = directory / _TEMPLATES_WHEEL
    if archive_problems(wheel, templates=True):
        raise SystemExit("The templates wheel failed the archive scan. Stop.")
    _require_dashboard_release(wheel.read_bytes())

    remote = fetch_json(_TEMPLATES_NEXT_URL)
    published = {} if remote is None else remote_digests(remote)
    decision = decide(
        local_digests(directory, "templates"),
        published,
        require_match=require_match,
    )
    print(TEMPLATES_RELEASE_AUTHORIZATION)
    return decision


def _require_dashboard_release(data: bytes) -> None:
    """Stop unless this wheel is templates 0.2.1 and contains the new dashboard."""
    metadata = ""
    view = ""
    css = ""
    font = False
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = info.filename
            if name.endswith(".dist-info/METADATA"):
                metadata = archive.read(info).decode("utf-8")
            elif name.endswith("customer_service/dashboard_view.py"):
                view = archive.read(info).decode("utf-8")
            elif name.endswith("customer_service/static/dashboard.css"):
                css = archive.read(info).decode("utf-8")
            elif name.endswith(_DASHBOARD_FONT):
                font = True
    if f"\nVersion: {TEMPLATES_VERSION}\n" not in f"\n{metadata}":
        raise SystemExit(f"The templates wheel is not version {TEMPLATES_VERSION}. Stop.")
    bound_forms = ("agentbuildkit<0.3.0,>=0.2.0", "agentbuildkit>=0.2.0,<0.3.0")
    if not any(form in metadata for form in bound_forms):
        raise SystemExit("The templates wheel does not require the published harness range. Stop.")
    if not font or _DASHBOARD_FONT not in css or _DASHBOARD_HEADING not in view:
        raise SystemExit("The templates wheel is missing the dashboard redesign. Stop.")


def decide(local: dict[str, str], remote: dict[str, str], *, require_match: bool = False) -> str:
    """Classify one package and stop when the published bytes differ."""
    decision = classify_release(local, remote)
    if decision == "conflict":
        raise SystemExit(
            "The published version exists and does not match the validated candidate. "
            "Stop for review. Do not overwrite it."
        )
    if require_match and decision != "match":
        raise SystemExit(
            "The published version is not the validated candidate. "
            "Do not treat a missing or different upload as done."
        )
    return decision


def main(argv: list[str]) -> None:
    """Handle harness, templates, require-harness, and require-python-pair."""
    command = argv[1] if len(argv) > 1 else ""
    if command in {"harness", "templates"}:
        directory = Path(argv[2])
        require_match = len(argv) > 3 and argv[3] == "require-match"
        url = _HARNESS_URL if command == "harness" else _TEMPLATES_URL
        remote = fetch_json(url)
        published = {} if remote is None else remote_digests(remote)
        decision = decide(
            local_digests(directory, command),
            published,
            require_match=require_match,
        )
        write_decision(decision)
        return
    if command == "require-harness":
        remote = fetch_json(_HARNESS_URL)
        if remote is None:
            raise SystemExit("agentbuildkit 0.2.0 is not on PyPI. Publish the harness first.")
        print("agentbuildkit 0.2.0 is published")
        return
    if command == "require-python-pair":
        harness = fetch_json(_HARNESS_URL)
        templates = fetch_json(_TEMPLATES_URL)
        if harness is None or templates is None:
            raise SystemExit("Publish and verify both Python packages before the npm package.")
        print("agentbuildkit 0.2.0 and agentbuildkit-templates 0.2.0 are published")
        return
    if command == "npm-bridge":
        verify_npm_bridge(Path(argv[2]))
        return
    if command == "templates-0.2.1":
        if TEMPLATES_VERSION != "0.2.1":
            raise SystemExit("templates-0.2.1 is not the prepared templates release. Stop.")
        directory = Path(argv[2])
        require_match = len(argv) > 3 and argv[3] == "require-match"
        decision = verify_templates_release(directory, require_match=require_match)
        write_decision(decision)
        return
    raise SystemExit(
        "Usage: release_publish.py "
        "harness|templates|templates-0.2.1|require-harness|require-python-pair|npm-bridge"
    )


if __name__ == "__main__":
    main(sys.argv)
