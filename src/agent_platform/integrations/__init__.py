"""Configured HTTP integrations.

Client-specific APIs belong in the organisation package. This package supplies
the shared client, timeouts, and approval gate.
"""

from agent_platform.integrations.http import (
    HttpApprovalRequired,
    HttpClient,
    HttpError,
    HttpPathError,
    HttpPayloadError,
    HttpRejected,
    HttpStatusError,
    HttpTimeoutError,
    HttpTimeouts,
)

__all__ = [
    "HttpApprovalRequired",
    "HttpClient",
    "HttpError",
    "HttpPathError",
    "HttpPayloadError",
    "HttpRejected",
    "HttpStatusError",
    "HttpTimeoutError",
    "HttpTimeouts",
]
