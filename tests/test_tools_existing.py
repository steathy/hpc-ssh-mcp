import subprocess
from unittest.mock import patch, call

import pytest

from tests.conftest import make_completed_process
from ssh_hpc_server import (
    execute_remote_bash,
    submit_slurm_job,
    check_slurm_job,
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
        assert cmd == ["ssh", "derecho", "ls"]

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


class TestSubmitSlurmJob:
    def test_writes_script_and_submits(self, mock_subprocess):
        mock_subprocess.side_effect = [
            make_completed_process(returncode=0),  # cat > file
            make_completed_process(returncode=0, stdout="Submitted batch job 12345\n"),  # sbatch
        ]
        result = submit_slurm_job(
            host="derecho",
            job_script_content="#!/bin/bash\n#SBATCH -N 1\necho hello",
        )
        assert "12345" in result
        assert mock_subprocess.call_count == 2
        write_call = mock_subprocess.call_args_list[0]
        assert "cat >" in write_call[0][0][2]
        submit_call = mock_subprocess.call_args_list[1]
        assert "sbatch" in submit_call[0][0][2]

    def test_reports_write_failure(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=1, stderr="Permission denied\n",
        )
        result = submit_slurm_job(host="derecho", job_script_content="#!/bin/bash")
        assert "Failed to write script" in result
        assert mock_subprocess.call_count == 1  # should not attempt sbatch

    def test_pipes_content_via_stdin(self, mock_subprocess):
        mock_subprocess.side_effect = [
            make_completed_process(returncode=0),
            make_completed_process(returncode=0, stdout="Submitted batch job 99\n"),
        ]
        script = "#!/bin/bash\necho test"
        submit_slurm_job(host="derecho", job_script_content=script)
        write_call_kwargs = mock_subprocess.call_args_list[0]
        assert write_call_kwargs.kwargs.get("input") == script or write_call_kwargs[1].get("input") == script

    def test_quotes_remote_filename(self, mock_subprocess):
        mock_subprocess.side_effect = [
            make_completed_process(returncode=0),
            make_completed_process(returncode=0, stdout="Submitted batch job 1\n"),
        ]
        submit_slurm_job(
            host="derecho",
            job_script_content="#!/bin/bash",
            remote_filename="my script.sh",
        )
        write_cmd = mock_subprocess.call_args_list[0][0][0][2]
        assert "'" in write_cmd


class TestCheckSlurmJob:
    def test_queries_squeue_and_sacct(self, mock_subprocess):
        mock_subprocess.side_effect = [
            make_completed_process(returncode=0, stdout="JOBID  PARTITION  NAME\n12345  main  test\n"),
            make_completed_process(returncode=0, stdout="JobID|JobName|State\n12345|test|COMPLETED\n"),
        ]
        result = check_slurm_job(host="derecho", job_id="12345")
        assert "squeue" in result
        assert "sacct" in result
        assert "12345" in result
        assert mock_subprocess.call_count == 2

    def test_rejects_invalid_job_id(self):
        with pytest.raises(ValueError, match="Invalid Slurm job ID"):
            check_slurm_job(host="derecho", job_id="12345; rm -rf /")

    def test_accepts_array_job_id(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok\n")
        result = check_slurm_job(host="derecho", job_id="12345_0")
        assert mock_subprocess.call_count == 2

    def test_accepts_step_job_id(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok\n")
        check_slurm_job(host="derecho", job_id="12345.0")
        assert mock_subprocess.call_count == 2


class TestReadRemoteFile:
    def test_reads_file_via_cat(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=0, stdout="line1\nline2\n",
        )
        result = read_remote_file(host="derecho", remote_path="/home/user/data.csv")
        assert "line1" in result
        cmd = mock_subprocess.call_args[0][0]
        assert cmd[0] == "ssh"
        assert "cat" in cmd[2]

    def test_uses_head_when_max_lines_set(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=0, stdout="line1\n",
        )
        read_remote_file(host="derecho", remote_path="/tmp/big.log", max_lines=100)
        cmd = mock_subprocess.call_args[0][0][2]
        assert "head -n 100" in cmd

    def test_quotes_remote_path(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok\n")
        read_remote_file(host="derecho", remote_path="/path with spaces/file.txt")
        cmd = mock_subprocess.call_args[0][0][2]
        assert "'" in cmd


class TestScpDownloadFile:
    def test_calls_scp_correctly(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0)
        result = scp_download_file(
            host="derecho",
            remote_path="/data/output.nc",
            local_path="C:/Users/me/output.nc",
        )
        cmd = mock_subprocess.call_args[0][0]
        assert cmd[0] == "scp"
        assert "derecho:" in cmd[1]
        assert cmd[2] == "C:/Users/me/output.nc"

    def test_reports_scp_failure(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=1, stderr="No such file\n",
        )
        result = scp_download_file(host="derecho", remote_path="/nope", local_path="/tmp/x")
        assert "[EXIT CODE 1]" in result
        assert "No such file" in result
