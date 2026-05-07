"""Regression tests for issues found in release readiness review.

Covers: leading-dash host/filename injection, project version propagation,
console entry point metadata, timeout output decoding, bash execution
wrapping, and MCP protocol surface (initialize + tools/list).
"""

import subprocess
from unittest.mock import patch

import pytest

from tests.conftest import make_completed_process


# ---------------------------------------------------------------------------
# P1: Leading-dash host option injection
# ---------------------------------------------------------------------------

class TestLeadingDashHostRejection:
    """Hosts like -V, -O, -- must be rejected to prevent SSH option injection."""

    @pytest.mark.parametrize("host", ["-V", "-O", "--", "-oProxyCommand=evil"])
    def test_execute_remote_bash_rejects(self, host):
        from ssh_hpc_server import execute_remote_bash
        with pytest.raises(ValueError, match="Invalid SSH host alias"):
            execute_remote_bash(host=host, command="id")

    @pytest.mark.parametrize("host", ["-V", "-O", "--"])
    def test_read_remote_file_rejects(self, host):
        from ssh_hpc_server import read_remote_file
        with pytest.raises(ValueError, match="Invalid SSH host alias"):
            read_remote_file(host=host, remote_path="/etc/hostname")

    @pytest.mark.parametrize("host", ["-V", "-O", "--"])
    def test_scp_download_rejects(self, host):
        from ssh_hpc_server import scp_download_file
        with pytest.raises(ValueError, match="Invalid SSH host alias"):
            scp_download_file(host=host, remote_path="/tmp/x", local_path="/tmp/y")

    @pytest.mark.parametrize("host", ["-V", "-O", "--"])
    def test_check_ssh_connection_rejects(self, host):
        from ssh_hpc_server import check_ssh_connection
        with pytest.raises(ValueError, match="Invalid SSH host alias"):
            check_ssh_connection(host=host)

    def test_valid_host_with_internal_dash_accepted(self):
        """Hosts like my-cluster are fine — only leading dashes are blocked."""
        from ssh_hpc_server import _validate_host
        _validate_host("my-cluster")
        _validate_host("login-node.ncar.edu")


# ---------------------------------------------------------------------------
# P3: Leading-dash remote filenames
# ---------------------------------------------------------------------------

class TestLeadingDashFilenameRejection:
    def test_submit_slurm_job_rejects_dash_filename(self):
        from ssh_hpc_server import submit_slurm_job
        with pytest.raises(ValueError, match="must not start with"):
            submit_slurm_job(
                host="derecho",
                job_script_content="#!/bin/bash",
                remote_filename="-malicious.sh",
            )

    def test_submit_slurm_job_uses_double_dash_for_sbatch(self, mock_subprocess):
        """sbatch must use -- to prevent filename-as-option interpretation."""
        mock_subprocess.side_effect = [
            make_completed_process(returncode=0),
            make_completed_process(returncode=0, stdout="Submitted batch job 1\n"),
        ]
        from ssh_hpc_server import submit_slurm_job
        submit_slurm_job(host="derecho", job_script_content="#!/bin/bash", remote_filename="safe.sh")
        sbatch_cmd = mock_subprocess.call_args_list[1][0][0][2]
        assert "sbatch -- " in sbatch_cmd

    def test_submit_slurm_job_uses_double_dash_for_chmod(self, mock_subprocess):
        """chmod must use -- to prevent filename-as-option interpretation."""
        mock_subprocess.side_effect = [
            make_completed_process(returncode=0),
            make_completed_process(returncode=0, stdout="Submitted batch job 1\n"),
        ]
        from ssh_hpc_server import submit_slurm_job
        submit_slurm_job(host="derecho", job_script_content="#!/bin/bash", remote_filename="safe.sh")
        chmod_cmd = mock_subprocess.call_args_list[0][0][0][2]
        assert "chmod -- " in chmod_cmd


# ---------------------------------------------------------------------------
# P2: Project version in MCP
# ---------------------------------------------------------------------------

class TestProjectVersion:
    def test_module_version_matches_pyproject(self):
        import ssh_hpc_server
        import importlib.metadata
        pkg_version = importlib.metadata.version("hpc-ssh-mcp")
        assert ssh_hpc_server.__version__ == pkg_version

    def test_fastmcp_instance_has_project_version(self):
        import ssh_hpc_server
        assert ssh_hpc_server.mcp.version == ssh_hpc_server.__version__


# ---------------------------------------------------------------------------
# P2: Console entry point metadata
# ---------------------------------------------------------------------------

class TestEntryPoint:
    def test_console_script_registered(self):
        import importlib.metadata
        eps = importlib.metadata.entry_points()
        console_scripts = [ep for ep in eps if ep.group == "console_scripts" and ep.name == "hpc-ssh-mcp"]
        assert len(console_scripts) == 1
        assert console_scripts[0].value == "ssh_hpc_server:main"


# ---------------------------------------------------------------------------
# P3: Timeout partial output decoding
# ---------------------------------------------------------------------------

class TestTimeoutOutputDecoding:
    def test_timeout_with_str_output(self, mock_subprocess):
        """text=True on modern Python: stdout/stderr are str."""
        exc = subprocess.TimeoutExpired(cmd=["ssh", "host", "cmd"], timeout=10)
        exc.stdout = "partial output"
        exc.stderr = "partial error"
        mock_subprocess.side_effect = exc
        from ssh_hpc_server import _run_raw
        rc, out, err = _run_raw(["ssh", "host", "cmd"], timeout=10)
        assert rc == -1
        assert out == "partial output"
        assert "partial error" in err
        assert "b'" not in out
        assert "b'" not in err

    def test_timeout_with_bytes_output(self, mock_subprocess):
        """Older Python or edge cases: stdout/stderr may be bytes."""
        exc = subprocess.TimeoutExpired(cmd=["ssh", "host", "cmd"], timeout=10)
        exc.stdout = b"partial bytes"
        exc.stderr = b"error bytes"
        mock_subprocess.side_effect = exc
        from ssh_hpc_server import _run_raw
        rc, out, err = _run_raw(["ssh", "host", "cmd"], timeout=10)
        assert rc == -1
        assert out == "partial bytes"
        assert "error bytes" in err
        assert "b'" not in out
        assert "b'" not in err

    def test_timeout_with_none_output(self, mock_subprocess):
        """No output captured before timeout."""
        exc = subprocess.TimeoutExpired(cmd=["ssh", "host", "cmd"], timeout=5)
        exc.stdout = None
        exc.stderr = None
        mock_subprocess.side_effect = exc
        from ssh_hpc_server import _run_raw
        rc, out, err = _run_raw(["ssh", "host", "cmd"], timeout=5)
        assert rc == -1
        assert isinstance(out, str)
        assert isinstance(err, str)
        assert "Timed out" in err


# ---------------------------------------------------------------------------
# P2: Bash execution wrapping
# ---------------------------------------------------------------------------

class TestBashExecution:
    def test_command_wrapped_in_bash_c(self, mock_subprocess):
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok")
        from ssh_hpc_server import execute_remote_bash
        execute_remote_bash(host="derecho", command="echo $SHELL")
        cmd = mock_subprocess.call_args[0][0]
        assert cmd[0] == "ssh"
        assert cmd[1] == "derecho"
        assert cmd[2].startswith("bash -c ")

    def test_command_with_quotes_properly_escaped(self, mock_subprocess):
        """Commands containing quotes must survive the bash -c + shlex.quote wrapping."""
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok")
        from ssh_hpc_server import execute_remote_bash
        execute_remote_bash(host="derecho", command="echo 'hello world'")
        cmd = mock_subprocess.call_args[0][0][2]
        assert "bash -c " in cmd
        assert "hello world" in cmd

    def test_command_with_pipes(self, mock_subprocess):
        """Piped commands must work inside the bash wrapper."""
        mock_subprocess.return_value = make_completed_process(returncode=0, stdout="ok")
        from ssh_hpc_server import execute_remote_bash
        execute_remote_bash(host="derecho", command="ls | grep foo")
        cmd = mock_subprocess.call_args[0][0][2]
        assert "bash -c " in cmd
        assert "ls | grep foo" in cmd


# ---------------------------------------------------------------------------
# Protocol-level: MCP initialize + tools/list
# ---------------------------------------------------------------------------

class TestMCPProtocol:
    @pytest.mark.asyncio
    async def test_initialize_returns_server_info(self):
        """MCP initialize must return correct server name and version."""
        from fastmcp import Client
        import ssh_hpc_server

        async with Client(ssh_hpc_server.mcp) as client:
            info = client.initialize_result.serverInfo
            assert info.name == "SSH-HPC-Remote-Control"
            assert info.version == ssh_hpc_server.__version__

    @pytest.mark.asyncio
    async def test_tools_list_returns_all_ten_tools(self):
        """tools/list must expose all 10 registered tools."""
        from fastmcp import Client
        import ssh_hpc_server

        expected_tools = {
            "execute_remote_bash",
            "submit_slurm_job",
            "check_slurm_job",
            "cancel_slurm_job",
            "list_slurm_queue",
            "read_remote_file",
            "tail_remote_file",
            "scp_download_file",
            "scp_upload_file",
            "check_ssh_connection",
        }

        async with Client(ssh_hpc_server.mcp) as client:
            tools = await client.list_tools()
            tool_names = {t.name for t in tools}
            assert tool_names == expected_tools

    @pytest.mark.asyncio
    async def test_each_tool_has_description(self):
        """Every tool must have a non-empty description for LLM discoverability."""
        from fastmcp import Client
        import ssh_hpc_server

        async with Client(ssh_hpc_server.mcp) as client:
            tools = await client.list_tools()
            for tool in tools:
                assert tool.description, f"Tool {tool.name} has no description"

    @pytest.mark.asyncio
    async def test_each_tool_requires_host_parameter(self):
        """Every tool must accept a 'host' parameter."""
        from fastmcp import Client
        import ssh_hpc_server

        async with Client(ssh_hpc_server.mcp) as client:
            tools = await client.list_tools()
            for tool in tools:
                schema = tool.inputSchema
                assert "host" in schema.get("properties", {}), (
                    f"Tool {tool.name} missing 'host' parameter"
                )
