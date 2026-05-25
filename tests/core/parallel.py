from __future__ import annotations

import os
from pathlib import Path


def xdist_worker_id() -> str:
    return os.environ.get("PYTEST_XDIST_WORKER", "master")


def xdist_worker_index() -> int:
    worker_id = xdist_worker_id()
    if worker_id == "master":
        return 0
    return int(worker_id.removeprefix("gw"))


def isolated_xdg_config_home() -> Path:
    path = Path(os.environ.get("OSAC_TEST_TMP", "/tmp")) / "osac-e2e-config" / xdist_worker_id()
    path.mkdir(parents=True, exist_ok=True)
    return path


def virtual_network_cidr() -> str:
    return f"10.{100 + xdist_worker_index()}.0.0/16"


def subnet_cidr() -> tuple[str, str]:
    octet = 200 + xdist_worker_index()
    return f"10.{octet}.0.0/16", f"10.{octet}.1.0/24"
