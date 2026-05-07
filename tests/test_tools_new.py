import subprocess
from unittest.mock import patch

import pytest

from tests.conftest import make_completed_process


class TestCheckSshConnection:
    def test_reports_healthy_socket(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=0, stdout="Master running (pid=12345)\n",
        )
        from ssh_hpc_server import check_ssh_connection
        result = check_ssh_connection(host="derecho")
        assert "running" in result.lower() or "Master" in result
        cmd = mock_subprocess.call_args[0][0]
        assert cmd == ["ssh", "-O", "check", "derecho"]

    def test_reports_dead_socket(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=255, stderr="Control socket not found\n",
        )
        from ssh_hpc_server import check_ssh_connection
        result = check_ssh_connection(host="derecho")
        assert "[EXIT CODE 255]" in result

    def test_rejects_invalid_host(self):
        from ssh_hpc_server import check_ssh_connection
        with pytest.raises(ValueError):
            check_ssh_connection(host="bad; host")


class TestScpUploadFile:
    def test_calls_scp_upload_correctly(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0)
        from ssh_hpc_server import scp_upload_file
        result = scp_upload_file(
            host="derecho",
            local_path="C:/Users/me/input.nc",
            remote_path="/scratch/user/input.nc",
        )
        cmd = mock_subprocess.call_args[0][0]
        assert cmd[0] == "scp"
        assert cmd[1] == "C:/Users/me/input.nc"
        assert "derecho:" in cmd[2]

    def test_reports_upload_failure(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=1, stderr="Permission denied\n",
        )
        from ssh_hpc_server import scp_upload_file
        result = scp_upload_file(
            host="derecho", local_path="/tmp/x", remote_path="/nope",
        )
        assert "[EXIT CODE 1]" in result

    def test_rejects_invalid_host(self):
        from ssh_hpc_server import scp_upload_file
        with pytest.raises(ValueError):
            scp_upload_file(host="bad; host", local_path="/a", remote_path="/b")
