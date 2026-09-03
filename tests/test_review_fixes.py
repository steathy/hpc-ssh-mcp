"""Regression tests for the 2026-09 code review.

Each class pins one finding. The finding numbers refer to the review report
(hpc-ssh-mcp 1.0 Review). Tests that can exercise the real subprocess layer do
so; mocks are used only where the behavior depends on the environment.
"""

import subprocess

import pytest

from tests.conftest import make_completed_process

import ssh_hpc_server
from ssh_hpc_server import (
    _run_raw,
    check_ssh_connection,
    execute_remote_bash,
    read_remote_file,
    scp_download_file,
)


# ---------------------------------------------------------------------------
# Finding 3: child processes must not inherit the MCP server's stdin
# ---------------------------------------------------------------------------

class TestStdinIsolation:
    def test_no_input_means_stdin_devnull(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok")
        _run_raw(["ssh", "h", "true"])
        assert mock_subprocess.call_args.kwargs.get("stdin") is subprocess.DEVNULL

    def test_input_data_still_piped(self, mock_subprocess):
        """subprocess.run refuses stdin= together with input=; only input may be set."""
        mock_subprocess.return_value = make_completed_process(returncode=0)
        _run_raw(["ssh", "h", "cat"], input_data="payload")
        kwargs = mock_subprocess.call_args.kwargs
        assert kwargs.get("input") == "payload"
        assert kwargs.get("stdin") is None


# ---------------------------------------------------------------------------
# Finding 4: non-UTF-8 output is replaced, never raised
# ---------------------------------------------------------------------------

class TestNonUtf8Output:
    def test_latin1_byte_does_not_raise(self):
        rc, out, err = _run_raw(["printf", "caf\\xe9\\n"], timeout=5)
        assert rc == 0
        assert out.startswith("caf")
        assert "�" in out


# ---------------------------------------------------------------------------
# Finding 5: diagnostic hints only for ssh's own failures (exit 255)
# ---------------------------------------------------------------------------

class TestHintGating:
    def test_remote_command_permission_denied_gets_no_reauth_hint(self, mock_subprocess):
        """`git pull` on the cluster failing against GitHub is not an SSH session problem."""
        mock_subprocess.return_value = make_completed_process(
            returncode=1, stderr="git@github.com: Permission denied (publickey).\n",
        )
        result = execute_remote_bash(host="derecho", command="git pull")
        assert "[EXIT CODE 1]" in result
        assert "ssh -fN" not in result

    def test_remote_timeout_text_gets_no_network_hint(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=7, stderr="curl: (28) Connection timed out\n",
        )
        result = execute_remote_bash(host="derecho", command="curl http://x")
        assert "Network unreachable" not in result

    def test_ssh_exit_255_still_gets_hint(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=255, stderr="Permission denied (publickey,keyboard-interactive).",
        )
        result = execute_remote_bash(host="derecho", command="ls")
        assert "ssh -fN derecho" in result


# ---------------------------------------------------------------------------
# Finding 6: `ssh -O check` reports on stderr
# ---------------------------------------------------------------------------

class TestCheckSshConnectionReadsStderr:
    def test_healthy_master_message_is_returned(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=0, stdout="", stderr="Master running (pid=4242)\r\n",
        )
        result = check_ssh_connection(host="derecho")
        assert "Master running" in result
        assert "(no output)" not in result

    def test_dead_master_gets_reauth_hint(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(
            returncode=255,
            stderr="Control socket connect(/home/u/.ssh/cm-derecho): No such file or directory\n",
        )
        result = check_ssh_connection(host="derecho")
        assert "[EXIT CODE 255]" in result
        assert "ssh -fN derecho" in result


# ---------------------------------------------------------------------------
# Finding 14 (hygiene): timeout must be a positive number of seconds
# ---------------------------------------------------------------------------

class TestTimeoutValidation:
    @pytest.mark.parametrize("bad", [0, -1])
    def test_execute_remote_bash_rejects_non_positive_timeout(self, bad):
        with pytest.raises(ValueError, match="timeout"):
            execute_remote_bash(host="derecho", command="ls", timeout=bad)

    @pytest.mark.parametrize("bad", [0, -5])
    def test_scp_download_rejects_non_positive_timeout(self, bad):
        with pytest.raises(ValueError, match="timeout"):
            scp_download_file(host="derecho", remote_path="/r/x", local_path="/tmp/x", timeout=bad)
