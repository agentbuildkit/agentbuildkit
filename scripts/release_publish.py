"""Compare local artifacts with a registry response. This module does not publish."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path

from scripts.release_gate import NPM_VERSION, classify_release

_HARNESS_URL = "https://pypi.org/pypi/agentbuildkit/0.2.0/json"
_TEMPLATES_URL = "https://pypi.org/pypi/agentbuildkit-templates/0.2.0/json"

# This authorizes the current npm publication while the published Python 0.2.0
# files stay unchanged. It does not allow a different wheel or template archive,
# and it does not republish Python. The npm tarball is the validated candidate.
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
_CANDIDATE_MUST_MATCH = (
    "agentbuildkit-0.2.0-py3-none-any.whl",
    "agentbuildkit_templates-0.2.0-py3-none-any.whl",
    "agentbuildkit_templates-0.2.0.tar.gz",
)


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

    PyPI must still serve the original 0.2.0 files. The candidate wheel and
    template archives must be those same bytes. The npm tarball is this
    commit's validated candidate. The harness source archive is not published.
    """
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
    for name in _CANDIDATE_MUST_MATCH:
        path = directory / name
        if not path.is_file():
            raise SystemExit(f"{name} is not a regular file in the validated candidate.")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != ORIGINAL_RELEASE_DIGESTS[name]:
            raise SystemExit(f"{name} does not match the original validated release. Stop.")
    source_archive = directory / "agentbuildkit-0.2.0.tar.gz"
    if not source_archive.is_file():
        raise SystemExit("The candidate harness source archive is missing.")
    tarball = directory / f"agentbuildkit-{NPM_VERSION}.tgz"
    if not tarball.is_file():
        raise SystemExit("The validated npm tarball is not a regular file.")
    print(NPM_BRIDGE_AUTHORIZATION)


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
    raise SystemExit(
        "Usage: release_publish.py harness|templates|require-harness|require-python-pair|npm-bridge"
    )


if __name__ == "__main__":
    main(sys.argv)
