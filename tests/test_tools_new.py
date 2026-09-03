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
    def test_calls_scp_upload_correctly(self, mock_subprocess, tmp_path):
        src = tmp_path / "input.nc"
        src.write_text("payload")
        mock_subprocess.return_value = make_completed_process(returncode=0)
        from ssh_hpc_server import scp_upload_file
        result = scp_upload_file(
            host="derecho",
            local_path=str(src),
            remote_path="/scratch/user/input.nc",
        )
        cmd = mock_subprocess.call_args[0][0]
        assert cmd[0] == "scp"
        # local source and remote dest are always the last two argv elements
        assert cmd[-2] == str(src)
        assert "derecho:" in cmd[-1]

    def test_reports_upload_failure(self, mock_subprocess, tmp_path):
        src = tmp_path / "x"
        src.write_text("payload")
        mock_subprocess.return_value = make_completed_process(
            returncode=1, stderr="Permission denied\n",
        )
        from ssh_hpc_server import scp_upload_file
        result = scp_upload_file(
            host="derecho", local_path=str(src), remote_path="/nope",
        )
        assert "[EXIT CODE 1]" in result

    def test_rejects_invalid_host(self):
        from ssh_hpc_server import scp_upload_file
        with pytest.raises(ValueError):
            scp_upload_file(host="bad; host", local_path="/a", remote_path="/b")

    def test_missing_local_file_is_reported(self, mock_subprocess, tmp_path):
        from ssh_hpc_server import scp_upload_file
        result = scp_upload_file(
            host="derecho", local_path=str(tmp_path / "absent.nc"), remote_path="/r/x",
        )
        assert "not found" in result.lower()
        mock_subprocess.assert_not_called()


class TestTailRemoteFile:
    """The tail script is delivered on stdin to `bash -s`; inspect the input kwarg."""

    def test_tails_with_default_lines(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=0, stdout="last line\n",
        )
        from ssh_hpc_server import tail_remote_file
        result = tail_remote_file(host="derecho", remote_path="/tmp/job.out")
        assert "last line" in result
        assert mock_subprocess.call_args[0][0][-1] == "bash -s"
        assert "tail -n 50" in mock_subprocess.call_args.kwargs["input"]

    def test_tails_with_custom_lines(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok\n")
        from ssh_hpc_server import tail_remote_file
        tail_remote_file(host="derecho", remote_path="/tmp/x", lines=200)
        assert "tail -n 200" in mock_subprocess.call_args.kwargs["input"]

    def test_quotes_remote_path(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok\n")
        from ssh_hpc_server import tail_remote_file
        tail_remote_file(host="derecho", remote_path="/path with spaces/out.log")
        script = mock_subprocess.call_args.kwargs["input"]
        assert "'/path with spaces/out.log'" in script  # shlex.quote wraps in single quotes

    def test_rejects_invalid_host(self):
        from ssh_hpc_server import tail_remote_file
        with pytest.raises(ValueError):
            tail_remote_file(host="bad; host", remote_path="/tmp/x")
