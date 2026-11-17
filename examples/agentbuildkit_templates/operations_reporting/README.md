# Operations reporting example

This team drafts an overdue-jobs report. It is fixture data unless you save a separate spreadsheet window for Analyst.

- Analyst calls `read_jobs`. Python classifies a job as overdue when its status is not done and its due date is before the as-of date.
- Reviewer checks that the summary names only those job ids.
- A summary that adds a job is rejected even when the reviewer model marks it supported.
- Demo mode uses the labelled fixture: `J-100` is overdue on `2026-11-16`; `J-200` is not yet due; `J-300` is done.
- Connected mode reads only the window saved for `operations-reporting` / `analyst`. It does not use Sam's window. Removing Analyst's assignment leaves Customer Service's window in place. Disconnecting the Google account removes every assignment that uses it. A failed read does not return the fixture.
- The team has no approval actions. It does not schedule work, send email, or edit a sheet.

The dashboard command is the same local server:

```sh
PYTHONPATH=examples uv run python -m customer_service.dashboard
```

That is the checkout wrapper. An installed templates package uses `agentkit dashboard`. See [Install](../../docs/install.md).

Open Customer Service at `/teams/customer-service/` and Operations Reporting at `/teams/operations-reporting/`. This is one local operator. Team isolation is not hosted multi-user tenancy. The server opens its SQLite files on one owner thread.
