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


class TestListSlurmQueue:
    def test_queries_squeue(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=0,
            stdout="JOBID  PARTITION  NAME  USER  ST  TIME  NODES\n12345  main  test  user1  R  1:00  1\n",
        )
        from ssh_hpc_server import list_slurm_queue
        result = list_slurm_queue(host="derecho")
        assert "12345" in result
        cmd = mock_subprocess.call_args[0][0][2]
        assert "squeue" in cmd

    def test_filters_by_user(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="header\n")
        from ssh_hpc_server import list_slurm_queue
        list_slurm_queue(host="derecho", user="jsmith")
        cmd = mock_subprocess.call_args[0][0][2]
        assert "-u" in cmd
        assert "jsmith" in cmd

    def test_defaults_to_current_user(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="header\n")
        from ssh_hpc_server import list_slurm_queue
        list_slurm_queue(host="derecho")
        cmd = mock_subprocess.call_args[0][0][2]
        assert "$USER" in cmd

    def test_rejects_invalid_host(self):
        from ssh_hpc_server import list_slurm_queue
        with pytest.raises(ValueError):
            list_slurm_queue(host="bad; host")


class TestTailRemoteFile:
    def test_tails_with_default_lines(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=0, stdout="last line\n",
        )
        from ssh_hpc_server import tail_remote_file
        result = tail_remote_file(host="derecho", remote_path="/tmp/job.out")
        assert "last line" in result
        cmd = mock_subprocess.call_args[0][0][2]
        assert "tail -n 50" in cmd

    def test_tails_with_custom_lines(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok\n")
        from ssh_hpc_server import tail_remote_file
        tail_remote_file(host="derecho", remote_path="/tmp/x", lines=200)
        cmd = mock_subprocess.call_args[0][0][2]
        assert "tail -n 200" in cmd

    def test_quotes_remote_path(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok\n")
        from ssh_hpc_server import tail_remote_file
        tail_remote_file(host="derecho", remote_path="/path with spaces/out.log")
        cmd = mock_subprocess.call_args[0][0][2]
        assert "'" in cmd  # shlex.quote wraps in single quotes

    def test_rejects_invalid_host(self):
        from ssh_hpc_server import tail_remote_file
        with pytest.raises(ValueError):
            tail_remote_file(host="bad; host", remote_path="/tmp/x")


class TestCancelSlurmJob:
    def test_calls_scancel(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0)
        from ssh_hpc_server import cancel_slurm_job
        result = cancel_slurm_job(host="derecho", job_id="12345")
        cmd = mock_subprocess.call_args[0][0]
        assert cmd[0] == "ssh"
        assert "scancel" in cmd[2]
        assert "'12345'" in cmd[2]

    def test_reports_cancel_failure(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=1, stderr="Invalid job id\n",
        )
        from ssh_hpc_server import cancel_slurm_job
        result = cancel_slurm_job(host="derecho", job_id="99999")
        assert "[EXIT CODE 1]" in result

    def test_rejects_invalid_job_id(self):
        from ssh_hpc_server import cancel_slurm_job
        with pytest.raises(ValueError, match="Invalid Slurm job ID"):
            cancel_slurm_job(host="derecho", job_id="12345; rm -rf /")

    def test_accepts_array_job_id(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0)
        from ssh_hpc_server import cancel_slurm_job
        cancel_slurm_job(host="derecho", job_id="12345_0")
        cmd = mock_subprocess.call_args[0][0][2]
        assert "12345_0" in cmd
