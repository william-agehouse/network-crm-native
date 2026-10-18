#!/usr/bin/env python3
"""The one exception type the API layer raises, and nothing else.

Flat sibling of app.py — `python3 crm/app.py` puts crm/ at sys.path[0], so the
API modules import this as `from api_core import ApiError`, exactly the way
crm/feed.py imports its own siblings. Do NOT convert that to `from crm import
api_core`: there is no crm/__init__.py, and start.command, the packaging
launcher and tests/conftest.py all depend on the flat form.

This module deliberately imports nothing from crm/. It is a leaf, so it can
never take part in an import cycle no matter which API module needs it.
"""


class ApiError(Exception):
    """Carries an HTTP status code plus a user-facing message."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
