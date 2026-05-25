from __future__ import annotations

import os
from pathlib import Path


def xdist_worker_id() -> str:
    return os.environ.get("PYTEST_XDIST_WORKER", "master")


def isolated_xdg_config_home() -> Path:
    path = Path(os.environ.get("OSAC_TEST_TMP", "/tmp")) / "osac-e2e-config" / xdist_worker_id()
    path.mkdir(parents=True, exist_ok=True)
    return path
