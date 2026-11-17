"""Write and check a validated release candidate. This module does not publish."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path

from scripts.release_gate import verify_files, verify_run, write_manifest

_WORKFLOW = ".github/workflows/release-validate.yml"


def main(argv: list[str]) -> None:
    """write, verify-run, or verify-files."""
    command = argv[1] if len(argv) > 1 else ""
    if command == "write":
        directory = Path(argv[2])
        commit = os.environ["GITHUB_SHA"]
        run = os.environ["GITHUB_RUN_ID"]
        write_manifest(directory, commit, run)
        print(f"wrote manifest for {commit} run {run}")
        return
    if command == "verify-run":
        payload = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise SystemExit("The workflow run response was not an object.")
        body: Mapping[str, object] = payload
        try:
            verify_run(body, argv[3])
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
        if body.get("path") != _WORKFLOW:
            raise SystemExit("The run is not the release validation workflow.")
        print("validation run matches this commit")
        return
    if command == "verify-files":
        try:
            verify_files(Path(argv[2]), source_commit=argv[3], workflow_run=argv[4])
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
        print("candidate files match the manifest")
        return
    raise SystemExit("Usage: release_candidate.py write|verify-run|verify-files")


if __name__ == "__main__":
    main(sys.argv)
