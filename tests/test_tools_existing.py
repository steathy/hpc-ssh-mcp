import subprocess
from unittest.mock import patch, call

import pytest

from tests.conftest import make_completed_process
from ssh_hpc_server import (
    execute_remote_bash,
    read_remote_file,
    scp_download_file,
)


class TestExecuteRemoteBash:
    def test_runs_ssh_command(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=0, stdout="file1.txt\nfile2.txt\n",
        )
        result = execute_remote_bash(host="derecho", command="ls")
        assert "file1.txt" in result
        mock_subprocess.assert_called_once()
        cmd = mock_subprocess.call_args[0][0]
        assert cmd[0] == "ssh"
        assert "derecho" in cmd
        assert cmd[-1] == "bash -s"
        assert mock_subprocess.call_args.kwargs["input"] == "ls"

    def test_rejects_invalid_host(self):
        with pytest.raises(ValueError):
            execute_remote_bash(host="host; rm -rf /", command="ls")

    def test_returns_error_on_failure(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=127, stderr="command not found\n",
        )
        result = execute_remote_bash(host="derecho", command="nonexistent")
        assert "[EXIT CODE 127]" in result
        assert "command not found" in result

    def test_respects_timeout(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok")
        execute_remote_bash(host="derecho", command="ls", timeout=30)
        call_kwargs = mock_subprocess.call_args
        assert call_kwargs.kwargs.get("timeout") == 30 or call_kwargs[1].get("timeout") == 30

    def test_wraps_command_in_bash(self, mock_subprocess):
        """execute_remote_bash should force bash regardless of login shell."""
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok")
        execute_remote_bash(host="derecho", command="echo hello")
        assert mock_subprocess.call_args[0][0][-1] == "bash -s"
        assert mock_subprocess.call_args.kwargs["input"] == "echo hello"


class TestReadRemoteFile:
    """The read script is delivered on stdin to `bash -s`; inspect the input kwarg."""

    def test_reads_file_via_byte_capped_head(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=0, stdout="line1\nline2\n",
        )
        result = read_remote_file(host="derecho", remote_path="/home/user/data.csv")
        assert "line1" in result
        cmd = mock_subprocess.call_args[0][0]
        assert cmd[0] == "ssh"
        assert cmd[-1] == "bash -s"
        script = mock_subprocess.call_args.kwargs["input"]
        assert "head -c" in script
        assert "/home/user/data.csv" in script

    def test_uses_head_when_max_lines_set(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=0, stdout="line1\n",
        )
        read_remote_file(host="derecho", remote_path="/tmp/big.log", max_lines=100)
        script = mock_subprocess.call_args.kwargs["input"]
        assert "head -n 100" in script

    def test_quotes_remote_path(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok\n")
        read_remote_file(host="derecho", remote_path="/path with spaces/file.txt")
        script = mock_subprocess.call_args.kwargs["input"]
        assert "'/path with spaces/file.txt'" in script


class TestScpDownloadFile:
    def test_calls_scp_correctly(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0)
        result = scp_download_file(
            host="derecho",
            remote_path="/data/output.nc",
            local_path="/data/me/output.nc",
        )
        cmd = mock_subprocess.call_args[0][0]
        assert cmd[0] == "scp"
        # source (remote) and dest (local) are always the last two argv elements
        assert "derecho:" in cmd[-2]
        assert cmd[-1] == "/data/me/output.nc"

    def test_reports_scp_failure(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=1, stderr="No such file\n",
        )
        result = scp_download_file(host="derecho", remote_path="/nope", local_path="/tmp/x")
        assert "[EXIT CODE 1]" in result
        assert "No such file" in result
