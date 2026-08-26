from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="session")
def skip_keycloak_sync_checks() -> bool:
    """
    Check if Keycloak sync verification should be skipped.
    Set OSAC_SKIP_KEYCLOAK_SYNC=true to skip Keycloak sync checks.
    Useful when the project sync feature isn't deployed yet.
    """
    return os.getenv("OSAC_SKIP_KEYCLOAK_SYNC", "false").lower() in ("true", "1", "yes")
