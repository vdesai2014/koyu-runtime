"""Wipe iceoryx2 segments once before the suite so live-transport tests start
clean (management state in /tmp/iceoryx2 AND payload in /dev/shm)."""

import subprocess

import pytest


@pytest.fixture(scope="session", autouse=True)
def _clean_iceoryx2():
    subprocess.run("rm -rf /tmp/iceoryx2 /dev/shm/iox2*", shell=True, check=False)
    yield
