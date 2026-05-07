from unittest.mock import patch, MagicMock
import subprocess

import pytest


@pytest.fixture
def mock_subprocess():
    """Patch subprocess.run and return the mock for configuration."""
    with patch("ssh_hpc_server.subprocess.run") as mock_run:
        yield mock_run


def make_completed_process(returncode=0, stdout="", stderr=""):
    """Helper to build a subprocess.CompletedProcess."""
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr,
    )
