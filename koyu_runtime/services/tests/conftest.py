import subprocess

import pytest


@pytest.fixture(scope="session", autouse=True)
def _clean_iceoryx2():
    subprocess.run("rm -rf /tmp/iceoryx2 /dev/shm/iox2*", shell=True, check=False)
    yield
