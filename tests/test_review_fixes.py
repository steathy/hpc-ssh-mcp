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


# ---------------------------------------------------------------------------
# Finding 7: local_path must survive scp's host:path parsing and option parsing
# ---------------------------------------------------------------------------

class TestLocalPathSafety:
    def test_colon_in_local_download_path_is_not_a_host(self, mock_subprocess, tmp_path):
        mock_subprocess.return_value = make_completed_process(returncode=0)
        target = str(tmp_path / "data:2024.nc")
        scp_download_file(host="derecho", remote_path="/r/x.nc", local_path=target)
        cmd = mock_subprocess.call_args[0][0]
        assert cmd[-1] == target
        assert cmd[-1].startswith("/")

    def test_relative_local_path_becomes_absolute(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0)
        scp_download_file(host="derecho", remote_path="/r/x.nc", local_path="out:1.nc")
        cmd = mock_subprocess.call_args[0][0]
        assert cmd[-1].startswith("/")
        assert cmd[-1].endswith("/out:1.nc")

    def test_dash_prefixed_local_upload_path_is_not_an_option(self, mock_subprocess, tmp_path, monkeypatch):
        from ssh_hpc_server import scp_upload_file
        monkeypatch.chdir(tmp_path)
        (tmp_path / "-oProxyCommand=evil").write_text("payload")
        mock_subprocess.return_value = make_completed_process(returncode=0)
        scp_upload_file(host="derecho", local_path="-oProxyCommand=evil", remote_path="/r/x")
        cmd = mock_subprocess.call_args[0][0]
        assert cmd[-2].startswith("/")
        assert cmd[-2].endswith("/-oProxyCommand=evil")


# ---------------------------------------------------------------------------
# Finding 2: remote path quoting must match the scp protocol in use
# ---------------------------------------------------------------------------

class TestOpenSshVersionParsing:
    @pytest.mark.parametrize("banner,expected", [
        ("OpenSSH_10.2p1 Ubuntu-2ubuntu3.5, OpenSSL 3.5.5 27 Jan 2026", (10, 2)),
        ("OpenSSH_8.9p1 Ubuntu-3ubuntu0.10, OpenSSL 3.0.2 15 Mar 2022", (8, 9)),
        ("OpenSSH_9.0p1, LibreSSL 3.3.6", (9, 0)),
        ("not an ssh banner", None),
        ("", None),
    ])
    def test_parses_major_minor(self, banner, expected):
        from ssh_hpc_server import _parse_openssh_version
        assert _parse_openssh_version(banner) == expected

    def test_real_local_ssh_reports_a_version(self):
        """No mock: the installed ssh must be detectable, or scp mode is a guess."""
        from ssh_hpc_server import _local_openssh_version
        ver = _local_openssh_version()
        assert isinstance(ver, tuple) and len(ver) == 2
        assert ver >= (1, 0)


class TestScpRemotePathMode:
    SPACED = "/glade/scratch/u/my file.nc"

    def test_sftp_mode_passes_path_unquoted(self, mock_subprocess, monkeypatch):
        monkeypatch.setattr(ssh_hpc_server, "_SCP_SFTP_MODE", True)
        mock_subprocess.return_value = make_completed_process(returncode=0)
        scp_download_file(host="derecho", remote_path=self.SPACED, local_path="/tmp/x")
        cmd = mock_subprocess.call_args[0][0]
        assert cmd[-2] == f"derecho:{self.SPACED}"

    def test_legacy_mode_quotes_path_for_remote_shell(self, mock_subprocess, monkeypatch):
        monkeypatch.setattr(ssh_hpc_server, "_SCP_SFTP_MODE", False)
        mock_subprocess.return_value = make_completed_process(returncode=0)
        scp_download_file(host="derecho", remote_path=self.SPACED, local_path="/tmp/x")
        cmd = mock_subprocess.call_args[0][0]
        assert cmd[-2] == f"derecho:'{self.SPACED}'"

    def test_sftp_mode_tilde_becomes_home_relative(self, mock_subprocess, monkeypatch):
        """SFTP resolves relative paths against $HOME, and never expands '~'."""
        monkeypatch.setattr(ssh_hpc_server, "_SCP_SFTP_MODE", True)
        mock_subprocess.return_value = make_completed_process(returncode=0)
        scp_download_file(host="derecho", remote_path="~/run 1/out.nc", local_path="/tmp/x")
        cmd = mock_subprocess.call_args[0][0]
        assert cmd[-2] == "derecho:run 1/out.nc"

    def test_legacy_mode_tilde_becomes_dollar_home(self, mock_subprocess, monkeypatch):
        monkeypatch.setattr(ssh_hpc_server, "_SCP_SFTP_MODE", False)
        mock_subprocess.return_value = make_completed_process(returncode=0)
        from ssh_hpc_server import scp_upload_file
        scp_upload_file(host="derecho", local_path="/tmp/x", remote_path="~/run 1/out.nc")
        cmd = mock_subprocess.call_args[0][0]
        assert cmd[-1] == "derecho:\"$HOME\"/'run 1/out.nc'"

    def test_mode_is_detected_from_local_ssh_version(self, mock_subprocess, monkeypatch):
        """With the version probe answering 10.2, SFTP mode must be chosen."""
        monkeypatch.setattr(ssh_hpc_server, "_SCP_SFTP_MODE", None)
        monkeypatch.setattr(ssh_hpc_server, "_local_openssh_version", lambda: (10, 2))
        mock_subprocess.return_value = make_completed_process(returncode=0)
        scp_download_file(host="derecho", remote_path=self.SPACED, local_path="/tmp/x")
        assert mock_subprocess.call_args[0][0][-2] == f"derecho:{self.SPACED}"

    def test_unknown_version_falls_back_to_quoting(self, mock_subprocess, monkeypatch):
        """Quoting is the safe failure: it breaks odd paths but never reaches a shell unquoted."""
        monkeypatch.setattr(ssh_hpc_server, "_SCP_SFTP_MODE", None)
        monkeypatch.setattr(ssh_hpc_server, "_local_openssh_version", lambda: None)
        mock_subprocess.return_value = make_completed_process(returncode=0)
        scp_download_file(host="derecho", remote_path=self.SPACED, local_path="/tmp/x")
        assert mock_subprocess.call_args[0][0][-2] == f"derecho:'{self.SPACED}'"


# ---------------------------------------------------------------------------
# Finding 9: a timed-out download must not leave a truncated file behind
# ---------------------------------------------------------------------------

class TestScpTimeoutCleanup:
    def _timeout_after_writing(self, path, content=b"partial"):
        def side_effect(cmd, **kwargs):
            with open(path, "wb") as fh:
                fh.write(content)
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 1))
        return side_effect

    def test_partial_file_removed_when_it_did_not_exist_before(self, mock_subprocess, tmp_path):
        target = tmp_path / "big.nc"
        mock_subprocess.side_effect = self._timeout_after_writing(target)
        result = scp_download_file(host="derecho", remote_path="/r/big.nc", local_path=str(target), timeout=5)
        assert "Timed out" in result
        assert not target.exists()
        assert "removed" in result.lower()

    def test_preexisting_file_is_left_alone(self, mock_subprocess, tmp_path):
        target = tmp_path / "keep.nc"
        target.write_bytes(b"original")
        mock_subprocess.side_effect = self._timeout_after_writing(target, b"clobbered")
        result = scp_download_file(host="derecho", remote_path="/r/keep.nc", local_path=str(target), timeout=5)
        assert "Timed out" in result
        assert target.exists()


# ---------------------------------------------------------------------------
# Finding 8: a remote_path beginning with '-' must not become an option
# ---------------------------------------------------------------------------

class TestRemotePathOptionSafety:
    def test_read_terminates_options_before_path(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="")
        read_remote_file(host="derecho", remote_path="-n")
        assert mock_subprocess.call_args.kwargs.get("input", "").rstrip().endswith(" -- -n")

    def test_read_with_max_lines_terminates_options(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="")
        read_remote_file(host="derecho", remote_path="--files0-from=/dev/stdin", max_lines=5)
        script = mock_subprocess.call_args.kwargs.get("input", "")
        assert "head -n 5 -- --files0-from=/dev/stdin" in script

    def test_tail_terminates_options_before_path(self, mock_subprocess):
        from ssh_hpc_server import tail_remote_file
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="")
        tail_remote_file(host="derecho", remote_path="-f")
        assert "tail -n 50 -- -f" in mock_subprocess.call_args.kwargs.get("input", "")


# ---------------------------------------------------------------------------
# Finding 14 (hygiene): '~' paths must reach the remote shell as $HOME
# ---------------------------------------------------------------------------

class TestTildeRemotePaths:
    def test_read_expands_tilde_outside_quotes(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="")
        read_remote_file(host="derecho", remote_path="~/run1/job.out")
        script = mock_subprocess.call_args.kwargs.get("input", "")
        assert '"$HOME"/run1/job.out' in script
        assert "'~" not in script

    def test_tail_quotes_only_the_rest_of_the_path(self, mock_subprocess):
        from ssh_hpc_server import tail_remote_file
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="")
        tail_remote_file(host="derecho", remote_path="~/x y.log")
        assert "\"$HOME\"/'x y.log'" in mock_subprocess.call_args.kwargs.get("input", "")


# ---------------------------------------------------------------------------
# Finding 11: read_remote_file must not pour an unbounded file into context
# ---------------------------------------------------------------------------

class TestReadRemoteFileCaps:
    def test_default_requests_one_byte_past_the_cap(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="x")
        read_remote_file(host="derecho", remote_path="/tmp/big.log")
        script = mock_subprocess.call_args.kwargs.get("input", "")
        assert f"head -c {ssh_hpc_server.DEFAULT_MAX_BYTES + 1}" in script

    def test_max_lines_is_also_byte_capped(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="x")
        read_remote_file(host="derecho", remote_path="/tmp/big.log", max_lines=5)
        script = mock_subprocess.call_args.kwargs.get("input", "")
        assert "head -n 5 -- /tmp/big.log | head -c" in script

    def test_oversize_output_is_cut_with_a_notice(self, mock_subprocess):
        cap = 100
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="a" * (cap + 1))
        result = read_remote_file(host="derecho", remote_path="/tmp/big.log", max_bytes=cap)
        assert result.startswith("a" * cap)
        assert "a" * (cap + 1) not in result
        assert "truncated" in result.lower()
        assert "tail_remote_file" in result

    def test_within_cap_is_returned_verbatim(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="line1\nline2\n")
        assert read_remote_file(host="derecho", remote_path="/tmp/small") == "line1\nline2\n"

    def test_binary_content_is_refused(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="CDF\x01\x00\x00\x00")
        result = read_remote_file(host="derecho", remote_path="/glade/x.nc")
        assert "binary" in result.lower()
        assert "scp_download_file" in result

    def test_rejects_non_positive_max_bytes(self):
        with pytest.raises(ValueError, match="max_bytes"):
            read_remote_file(host="derecho", remote_path="/tmp/x", max_bytes=0)


class TestGlobalOutputCap:
    def test_execute_remote_bash_output_is_capped(self, mock_subprocess):
        huge = "b" * (ssh_hpc_server.MAX_OUTPUT_CHARS + 50_000)
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout=huge)
        result = execute_remote_bash(host="derecho", command="cat big")
        assert len(result) < ssh_hpc_server.MAX_OUTPUT_CHARS + 500
        assert "truncated" in result.lower()


# ---------------------------------------------------------------------------
# Finding 10: commands travel on stdin to `bash -s`, not through the login shell
# ---------------------------------------------------------------------------

class TestExecuteRemoteBashViaStdin:
    def test_remote_argv_is_only_bash_s(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok")
        execute_remote_bash(host="derecho", command="ls | grep foo")
        cmd = mock_subprocess.call_args[0][0]
        assert cmd[-1] == "bash -s"
        assert mock_subprocess.call_args.kwargs["input"] == "ls | grep foo"

    def test_multiline_and_csh_hostile_text_is_delivered_verbatim(self, mock_subprocess):
        """Newlines, '!' and single quotes would all break under a tcsh login shell."""
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok")
        script = "set -e\necho 'it''s ready!'\nqstat -u $USER 2>/dev/null\n"
        execute_remote_bash(host="derecho", command=script)
        assert mock_subprocess.call_args.kwargs["input"] == script
        assert "'" not in mock_subprocess.call_args[0][0][-1]


# ---------------------------------------------------------------------------
# Finding 13: MCP tool annotations so clients can tell reads from writes
# ---------------------------------------------------------------------------

class TestToolAnnotations:
    READ_ONLY = {"read_remote_file", "tail_remote_file", "check_job", "list_queue",
                 "check_ssh_connection", "probe_host", "globus_status",
                 "globus_find_collection", "globus_ls", "globus_task_status"}
    MUTATING = {"execute_remote_bash", "submit_job", "cancel_job", "run_on_compute",
                "scp_download_file", "scp_upload_file", "annotate_host",
                "globus_transfer", "globus_task_cancel"}

    async def _tools(self):
        from fastmcp import Client
        async with Client(ssh_hpc_server.mcp) as client:
            return {t.name: t for t in await client.list_tools()}

    @pytest.mark.asyncio
    async def test_every_tool_is_classified(self):
        tools = await self._tools()
        assert set(tools) == self.READ_ONLY | self.MUTATING

    @pytest.mark.asyncio
    async def test_read_only_tools_are_marked_read_only_and_idempotent(self):
        tools = await self._tools()
        for name in self.READ_ONLY:
            ann = tools[name].annotations
            assert ann is not None and ann.readOnlyHint is True, name
            assert ann.idempotentHint is True, name

    @pytest.mark.asyncio
    async def test_mutating_tools_are_not_marked_read_only(self):
        tools = await self._tools()
        for name in self.MUTATING:
            ann = tools[name].annotations
            assert ann is not None and ann.readOnlyHint is False, name

    @pytest.mark.asyncio
    async def test_cancel_and_shell_are_destructive(self):
        tools = await self._tools()
        for name in ("cancel_job", "execute_remote_bash"):
            assert tools[name].annotations.destructiveHint is True, name
