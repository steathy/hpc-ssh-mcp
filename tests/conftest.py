from unittest.mock import patch
import subprocess

import pytest


@pytest.fixture
def mock_subprocess():
    """Patch subprocess.run and return the mock for configuration.

    The scp protocol mode is pinned to legacy (shell-quoted paths) so the
    version probe never runs through the mock; tests that exercise mode
    detection set ssh_hpc_server._SCP_SFTP_MODE themselves.
    """
    import ssh_hpc_server
    ssh_hpc_server._SCP_SFTP_MODE = False
    ssh_hpc_server._SCHEDULER_CACHE.clear()
    ssh_hpc_server._POLL_CACHE.clear()
    try:
        with patch("ssh_hpc_server.subprocess.run") as mock_run:
            yield mock_run
    finally:
        ssh_hpc_server._SCP_SFTP_MODE = None
        ssh_hpc_server._SCHEDULER_CACHE.clear()
        ssh_hpc_server._POLL_CACHE.clear()


def make_completed_process(returncode=0, stdout="", stderr=""):
    """Helper to build a subprocess.CompletedProcess."""
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr,
    )
