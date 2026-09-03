"""v1.1.0: batch-safe SSH defaults + actionable error diagnostics.

These tests pin the behavior added when the project moved from
"trust the user's ~/.ssh/config to do the right thing" to
"force SSH to fail fast and tell the user how to fix it."
"""

import pytest

from tests.conftest import make_completed_process

from ssh_hpc_server import (
    SSH_OPTS,
    _diagnose_ssh_failure,
    _ssh_cmd,
    _scp_cmd,
    execute_remote_bash,
    submit_job,
    check_job,
    list_queue,
    cancel_job,
    read_remote_file,
    tail_remote_file,
    scp_download_file,
    scp_upload_file,
    check_ssh_connection,
)


# ---------------------------------------------------------------------------
# SSH_OPTS is the contract; everything else flows from it
# ---------------------------------------------------------------------------

class TestSshOptsContract:
    def test_includes_batch_mode(self):
        assert "BatchMode=yes" in SSH_OPTS

    def test_includes_connect_timeout(self):
        assert "ConnectTimeout=10" in SSH_OPTS

    def test_each_value_is_preceded_by_o_flag(self):
        """For every X=Y in SSH_OPTS, the preceding element must be '-o'."""
        for i, val in enumerate(SSH_OPTS):
            if "=" in val:
                assert i >= 1 and SSH_OPTS[i - 1] == "-o", f"{val!r} not preceded by -o"


class TestCmdBuildersIncludeOpts:
    def test_ssh_cmd_injects_opts(self):
        cmd = _ssh_cmd("derecho", "echo hi")
        assert cmd[0] == "ssh"
        assert "-o" in cmd
        assert "BatchMode=yes" in cmd
        assert "ConnectTimeout=10" in cmd
        assert "derecho" in cmd
        assert cmd[-1] == "echo hi"

    def test_scp_cmd_injects_opts(self):
        cmd = _scp_cmd("/local/x", "host:/remote/y")
        assert cmd[0] == "scp"
        assert "BatchMode=yes" in cmd
        assert "ConnectTimeout=10" in cmd
        assert cmd[-2] == "/local/x"
        assert cmd[-1] == "host:/remote/y"


# ---------------------------------------------------------------------------
# Every tool that opens a real connection must carry SSH_OPTS
# ---------------------------------------------------------------------------

class TestEveryToolUsesBatchSafeOpts:
    """If any of these regress, MCP-server hangs on dead multiplex sockets come back."""

    def _assert_opts_present(self, cmd):
        assert "BatchMode=yes" in cmd, f"BatchMode missing from {cmd}"
        assert "ConnectTimeout=10" in cmd, f"ConnectTimeout missing from {cmd}"

    def test_execute_remote_bash(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok")
        execute_remote_bash(host="derecho", command="ls")
        self._assert_opts_present(mock_subprocess.call_args[0][0])

    def test_submit_slurm_job_write_call(self, mock_subprocess):
        mock_subprocess.side_effect = [
            make_completed_process(returncode=0),
            make_completed_process(returncode=0, stdout="Submitted batch job 1\n"),
        ]
        submit_job(scheduler="slurm", host="derecho", job_script_content="#!/bin/bash", remote_filename="j.sh")
        self._assert_opts_present(mock_subprocess.call_args_list[0][0][0])
        self._assert_opts_present(mock_subprocess.call_args_list[1][0][0])

    def test_check_job(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok\n")
        check_job(scheduler="slurm", host="derecho", job_id="12345")
        self._assert_opts_present(mock_subprocess.call_args[0][0])

    def test_list_slurm_queue(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="header\n")
        list_queue(scheduler="slurm", host="derecho")
        self._assert_opts_present(mock_subprocess.call_args[0][0])

    def test_cancel_slurm_job(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0)
        cancel_job(scheduler="slurm", host="derecho", job_id="12345")
        self._assert_opts_present(mock_subprocess.call_args[0][0])

    def test_read_remote_file(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="x\n")
        read_remote_file(host="derecho", remote_path="/etc/hostname")
        self._assert_opts_present(mock_subprocess.call_args[0][0])

    def test_tail_remote_file(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="x\n")
        tail_remote_file(host="derecho", remote_path="/tmp/log")
        self._assert_opts_present(mock_subprocess.call_args[0][0])

    def test_scp_download(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0)
        scp_download_file(host="derecho", remote_path="/r/x", local_path="/l/x")
        self._assert_opts_present(mock_subprocess.call_args[0][0])

    def test_scp_upload(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0)
        scp_upload_file(host="derecho", local_path="/l/x", remote_path="/r/x")
        self._assert_opts_present(mock_subprocess.call_args[0][0])


class TestCheckSshConnectionDoesNotUseOpts:
    """`ssh -O check` is a local socket query, not a connection — opts are noise.
    Pinning this to keep the cmd minimal and exactly what users see in `ps`.
    """

    def test_no_batch_mode_no_connect_timeout(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="Master running\n")
        check_ssh_connection(host="derecho")
        cmd = mock_subprocess.call_args[0][0]
        assert cmd == ["ssh", "-O", "check", "derecho"]


# ---------------------------------------------------------------------------
# _diagnose_ssh_failure: stderr fingerprinting → actionable hint
# ---------------------------------------------------------------------------

class TestDiagnoseSshFailure:
    def test_returns_empty_when_unknown(self):
        assert _diagnose_ssh_failure("derecho", "some unrelated error") == ""

    def test_returns_empty_for_empty_stderr(self):
        assert _diagnose_ssh_failure("derecho", "") == ""

    def test_detects_permission_denied_keyboard_interactive(self):
        stderr = "Permission denied (publickey,keyboard-interactive)."
        hint = _diagnose_ssh_failure("derecho", stderr)
        assert "ssh -fN derecho" in hint
        assert "ControlMaster" in hint or "expired" in hint.lower()

    def test_detects_permission_denied_publickey_only(self):
        stderr = "Permission denied (publickey)."
        hint = _diagnose_ssh_failure("derecho", stderr)
        assert "ssh -fN derecho" in hint

    def test_detects_missing_control_socket(self):
        stderr = "Control socket connect(/home/u/.ssh/cm_socket): No such file or directory"
        hint = _diagnose_ssh_failure("derecho", stderr)
        assert "ssh -fN derecho" in hint
        assert "socket" in hint.lower()

    def test_detects_network_timeout(self):
        stderr = "ssh: connect to host derecho port 22: Connection timed out"
        hint = _diagnose_ssh_failure("derecho", stderr)
        assert "Network unreachable" in hint or "ssh -fN derecho" in hint

    def test_detects_no_route_to_host(self):
        stderr = "ssh: connect to host x port 22: No route to host"
        hint = _diagnose_ssh_failure("derecho", stderr)
        assert "ssh -fN derecho" in hint

    def test_hint_quotes_the_actual_host(self):
        """If we tell the user to run `ssh -fN X`, X must be their host, not a placeholder."""
        hint = _diagnose_ssh_failure("my-cluster.example", "Permission denied (publickey)")
        assert "ssh -fN my-cluster.example" in hint


# ---------------------------------------------------------------------------
# Diagnostic hint flows through the tool layer when SSH fails
# ---------------------------------------------------------------------------

class TestDiagnosticHintAppearsInToolOutput:
    def test_execute_remote_bash_appends_hint_on_auth_failure(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=255,
            stderr="Permission denied (publickey,keyboard-interactive).",
        )
        result = execute_remote_bash(host="derecho", command="ls")
        assert "[EXIT CODE 255]" in result
        assert "ssh -fN derecho" in result

    def test_submit_slurm_job_appends_hint_when_write_fails(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=255,
            stderr="Permission denied (publickey,keyboard-interactive).",
        )
        result = submit_job(scheduler="slurm", host="derecho", job_script_content="#!/bin/bash")
        assert "Failed to write script" in result
        assert "ssh -fN derecho" in result

    def test_scp_download_appends_hint(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=255,
            stderr="ssh: connect to host derecho port 22: Connection timed out",
        )
        result = scp_download_file(host="derecho", remote_path="/r/x", local_path="/l/x")
        assert "ssh -fN derecho" in result

    def test_success_does_not_append_hint(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok\n")
        result = execute_remote_bash(host="derecho", command="ls")
        assert "ssh -fN" not in result
        assert "Hint" not in result

    def test_unrelated_failure_does_not_append_hint(self, mock_subprocess):
        """A normal command error (e.g. file not found on remote) is not an SSH/network issue."""
        mock_subprocess.return_value = make_completed_process(
            returncode=2,
            stderr="cat: /nonexistent: No such file or directory",
        )
        result = read_remote_file(host="derecho", remote_path="/nonexistent")
        assert "[EXIT CODE 2]" in result
        assert "ssh -fN" not in result


# ---------------------------------------------------------------------------
# Version pin for the release
# ---------------------------------------------------------------------------

class TestVersion:
    def test_version_bumped_to_1_0_0(self):
        import ssh_hpc_server
        assert ssh_hpc_server.__version__ == "1.0.0"
