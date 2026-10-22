"""Live Cursor checks stay out of the default suite.

The offline tests use a fake provider and a mocked HTTP transport. A live Cloud
Agent run is opt-in and is not launched from this file.
"""

import os

import pytest


@pytest.mark.integration
def test_live_cursor_execution_is_opt_in() -> None:
    if os.environ.get("CURSOR_LIVE_TESTS") != "1":
        pytest.skip("Live Cursor Cloud execution is opt-in.")
    assert os.environ.get("CURSOR_API_KEY")
