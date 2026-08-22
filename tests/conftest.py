"""Suite-wide compatibility fixtures.

The historical dashboard API tests predate cookie sessions and encode bearer
credentials in query strings. Production disables that transport by default.
Keep legacy endpoint assertions readable during migration while a dedicated
security contract verifies that the real default refuses URL credentials.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _legacy_dashboard_query_token_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHBOARD_ALLOW_QUERY_TOKEN", "1")

