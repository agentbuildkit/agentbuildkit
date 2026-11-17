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

from scripts.release_gate import classify_release

_HARNESS_URL = "https://pypi.org/pypi/agentbuildkit/0.2.0/json"
_TEMPLATES_URL = "https://pypi.org/pypi/agentbuildkit-templates/0.2.0/json"


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
    raise SystemExit(
        "Usage: release_publish.py harness|templates|require-harness|require-python-pair"
    )


if __name__ == "__main__":
    main(sys.argv)
