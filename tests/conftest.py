"""Spawns the three services as real subprocesses for the duration of the suite.

Deliberately not ASGI test transports. The point of the exercise is three
independently failing systems talking over a network boundary, and bind calls
publish and spend itself. Testing against in-process app objects would quietly
test a different architecture from the one being demonstrated.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SERVICES = [
    ("services.publish_service:app", 8101),
    ("services.spend_service:app", 8102),
    ("services.bind_service:app", 8103),
]


def _healthy(port: int) -> bool:
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1.0, trust_env=False)
        return r.status_code == 200
    except httpx.HTTPError:
        return False


@pytest.fixture(scope="session", autouse=True)
def services():
    if all(_healthy(p) for _, p in SERVICES):
        yield  # already running (e.g. from ./start_services.sh)
        return

    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "uvicorn", app, "--port", str(port), "--log-level", "error"],
            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for app, port in SERVICES
    ]
    deadline = time.time() + 25
    while time.time() < deadline:
        if all(_healthy(p) for _, p in SERVICES):
            break
        time.sleep(0.25)
    else:
        for p in procs:
            p.kill()
        pytest.fail("stage services did not start")

    yield

    for p in procs:
        p.terminate()
    for p in procs:
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
