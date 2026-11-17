"""Overdue classification for operations reporting.

Date comparison is ordinary Python. The agents summarise and review the rows
this module returns. They do not choose which spreadsheet or range may be read.
"""

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import cast

from agentbuildkit.integrations.composio import ComposioRejected

_DONE = frozenset({"done", "complete", "completed"})
_JOB_ID = re.compile(r"\bJ-\d+\b")


@dataclass(frozen=True)
class Job:
    """One fixture or sheet row."""

    job_id: str
    customer: str
    due_date: date
    status: str


# Measured against 2026-11-16, only J-100 is overdue.
FIXTURE: tuple[Job, ...] = (
    Job("J-100", "Northwind", date(2026, 11, 1), "open"),
    Job("J-200", "Contoso", date(2026, 11, 20), "open"),
    Job("J-300", "Example Test Ltd", date(2026, 10, 2), "done"),
)
DEMO_AS_OF = "2026-11-16"


def parse_as_of(value: str) -> date:
    """Require one calendar date. The comparison does not use the clock."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("Use a date in YYYY-MM-DD form.") from None


def classify(jobs: Iterable[Job], as_of: date) -> tuple[Job, ...]:
    """Open jobs whose due date is before the as-of date."""
    return tuple(
        job for job in jobs if job.status.strip().lower() not in _DONE and job.due_date < as_of
    )


def format_jobs(jobs: Iterable[Job]) -> str:
    """Render the classified rows the agents are allowed to see."""
    lines = ["job_id, customer, due_date, status"]
    for job in jobs:
        lines.append(f"{job.job_id}, {job.customer}, {job.due_date.isoformat()}, {job.status}")
    return "\n".join(lines)


def mentioned_job_ids(summary: str) -> set[str]:
    """Job ids named by a summary. Other text is ignored."""
    return set(_JOB_ID.findall(summary))


def jobs_from_sheet(text: str) -> tuple[Job, ...]:
    """Read job rows from a bounded sheet result. A bad payload is refused."""
    payload = text.split("\n", 1)[1] if text.startswith("Connected data") else text
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        raise ComposioRejected("The connected spreadsheet could not be read.") from None
    jobs: list[Job] = []
    for cells in _rows(parsed):
        if len(cells) < 4 or cells[0].lower() == "job_id":
            continue
        try:
            jobs.append(
                Job(
                    job_id=cells[0],
                    customer=cells[1],
                    due_date=parse_as_of(cells[2]),
                    status=cells[3],
                )
            )
        except ValueError:
            raise ComposioRejected("The connected spreadsheet could not be read.") from None
    return tuple(jobs)


def _rows(value: object) -> Iterable[list[str]]:
    if isinstance(value, list):
        items = cast(list[object], value)
        if items and all(isinstance(item, list) for item in items):
            for item in items:
                cells = cast(list[object], item)
                yield [str(cell) for cell in cells]
            return
        for item in items:
            yield from _rows(item)
        return
    if isinstance(value, dict):
        mapping = cast(dict[object, object], value)
        for item in mapping.values():
            yield from _rows(item)
