"""SSH & HPC Remote Control MCP Server.

A universal bridge to any SSH-enabled server or supercomputer.
Uses native ssh/scp binaries via subprocess to respect ~/.ssh/config
and ControlMaster multiplex sockets (avoiding MFA re-prompts).

Run with:  uv run ssh_hpc_server.py
"""

import re
import shlex
import subprocess

from fastmcp import FastMCP

mcp = FastMCP(name="SSH-HPC-Remote-Control")

DEFAULT_TIMEOUT = 120
_VALID_HOST_RE = re.compile(r"^[a-zA-Z0-9._@-]+$")
_VALID_JOB_ID_RE = re.compile(r"^\d+([_.]\d+)*$")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _validate_host(host: str) -> None:
    """Reject host strings that could be used for shell injection."""
    if not host or not _VALID_HOST_RE.match(host):
        raise ValueError(
            f"Invalid SSH host alias: {host!r}. "
            "Must contain only alphanumeric characters, dots, hyphens, underscores, or @."
        )


def _run_raw(
    cmd: list[str],
    timeout: int = DEFAULT_TIMEOUT,
    input_data: str | None = None,
) -> tuple[int, str, str]:
    """Execute a subprocess and return (returncode, stdout, stderr).

    Never raises on non-zero exit. Returns -1 for timeouts or missing binaries.
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_data,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired as exc:
        return -1, exc.stdout or "", f"Timed out after {timeout}s. {exc.stderr or ''}"
    except FileNotFoundError:
        return -1, "", f"Command not found: {cmd[0]}. Is it installed and on PATH?"


def _format_result(returncode: int, stdout: str, stderr: str) -> str:
    """Format a subprocess result into a human-readable string."""
    if returncode == 0:
        return stdout if stdout.strip() else "(no output)"
    parts = [f"[EXIT CODE {returncode}]"]
    if stdout.strip():
        parts.append(f"stdout:\n{stdout.rstrip()}")
    if stderr.strip():
        parts.append(f"stderr:\n{stderr.rstrip()}")
    return "\n".join(parts)


def _run(
    cmd: list[str],
    timeout: int = DEFAULT_TIMEOUT,
    input_data: str | None = None,
) -> str:
    """Execute a subprocess and return formatted output."""
    rc, out, err = _run_raw(cmd, timeout, input_data)
    return _format_result(rc, out, err)


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def execute_remote_bash(
    host: str,
    command: str,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """Execute a bash command on a remote SSH host.

    The host must match an alias in ~/.ssh/config. Uses the system ssh
    binary so ControlMaster multiplex sockets are respected.

    Args:
        host: SSH config alias or hostname.
        command: The bash command string to execute remotely.
        timeout: Max seconds to wait (default 120).
    """
    _validate_host(host)
    return _run(["ssh", host, command], timeout=timeout)


@mcp.tool()
def submit_slurm_job(
    host: str,
    job_script_content: str,
    remote_filename: str = "claude_job.sh",
) -> str:
    """Write a Slurm batch script to a remote host and submit it with sbatch.

    The script content is piped via stdin to avoid shell-escaping issues.
    Returns the sbatch output (typically 'Submitted batch job <ID>').

    Args:
        host: SSH config alias for the HPC system.
        job_script_content: Full text of the Slurm batch script (including #SBATCH directives).
        remote_filename: Where to write the script on the remote host.
    """
    _validate_host(host)
    safe_fn = shlex.quote(remote_filename)

    # Pipe script content to the remote file via stdin
    rc, out, err = _run_raw(
        ["ssh", host, f"cat > {safe_fn} && chmod +x {safe_fn}"],
        input_data=job_script_content,
    )
    if rc != 0:
        return f"Failed to write script to {remote_filename}:\n{_format_result(rc, out, err)}"

    return _run(["ssh", host, f"sbatch {safe_fn}"])


@mcp.tool()
def check_slurm_job(host: str, job_id: str) -> str:
    """Check the status of a Slurm job.

    Queries both squeue (running/pending) and sacct (accounting/completed)
    to give a complete picture regardless of job state.

    Args:
        host: SSH config alias for the HPC system.
        job_id: Slurm job ID (e.g. '12345', '12345_0' for array jobs).
    """
    _validate_host(host)
    if not _VALID_JOB_ID_RE.match(job_id):
        raise ValueError(
            f"Invalid Slurm job ID: {job_id!r}. "
            "Expected numeric ID, optionally with _ or . separators for array/step jobs."
        )

    safe_id = shlex.quote(job_id)

    squeue_result = _run([
        "ssh", host,
        f"squeue -j {safe_id} --format='%.18i %.9P %.30j %.8u %.8T %.10M %.9l %.6D %R' 2>/dev/null",
    ])
    sacct_result = _run([
        "ssh", host,
        f"sacct -j {safe_id} --format=JobID,JobName,Partition,State,ExitCode,Elapsed,Start,End --parsable2",
    ])

    return (
        f"=== squeue (running/pending) ===\n{squeue_result}\n\n"
        f"=== sacct (accounting) ===\n{sacct_result}"
    )


@mcp.tool()
def list_slurm_queue(host: str, user: str = "") -> str:
    """List Slurm jobs in the queue for a user.

    Defaults to the current user ($USER) if no user is specified.

    Args:
        host: SSH config alias for the HPC system.
        user: Username to filter by. Defaults to the remote $USER.
    """
    _validate_host(host)
    if user:
        safe_user = shlex.quote(user)
        cmd = f"squeue -u {safe_user} --format='%.18i %.9P %.30j %.8u %.8T %.10M %.9l %.6D %R'"
    else:
        cmd = "squeue -u $USER --format='%.18i %.9P %.30j %.8u %.8T %.10M %.9l %.6D %R'"
    return _run(["ssh", host, cmd])


@mcp.tool()
def read_remote_file(
    host: str,
    remote_path: str,
    max_lines: int = 0,
) -> str:
    """Read a text file on a remote host and return its contents.

    Suitable for source code, CSVs, config files, and Slurm .out logs.
    For large binary files, use scp_download_file instead.

    Args:
        host: SSH config alias or hostname.
        remote_path: Absolute or relative path to the file on the remote host.
        max_lines: If > 0, only return the first N lines (prevents context explosion on huge files).
    """
    _validate_host(host)
    safe_path = shlex.quote(remote_path)

    if max_lines > 0:
        cmd = f"head -n {int(max_lines)} {safe_path}"
    else:
        cmd = f"cat {safe_path}"

    return _run(["ssh", host, cmd])


@mcp.tool()
def scp_download_file(
    host: str,
    remote_path: str,
    local_path: str,
) -> str:
    """Download a file from a remote host to the local machine via scp.

    Uses the system scp binary to respect SSH config and multiplex sockets.
    Prefer this over read_remote_file for large or binary files.

    Args:
        host: SSH config alias or hostname.
        remote_path: Path to the file on the remote host.
        local_path: Destination path on the local machine.
    """
    _validate_host(host)
    # shlex.quote the remote path for the remote shell that scp invokes
    escaped_remote = shlex.quote(remote_path)
    return _run(["scp", f"{host}:{escaped_remote}", local_path])


@mcp.tool()
def scp_upload_file(
    host: str,
    local_path: str,
    remote_path: str,
) -> str:
    """Upload a file from the local machine to a remote host via scp.

    Uses the system scp binary to respect SSH config and multiplex sockets.

    Args:
        host: SSH config alias or hostname.
        local_path: Path to the file on the local machine.
        remote_path: Destination path on the remote host.
    """
    _validate_host(host)
    escaped_remote = shlex.quote(remote_path)
    return _run(["scp", local_path, f"{host}:{escaped_remote}"])


@mcp.tool()
def check_ssh_connection(host: str) -> str:
    """Check if the SSH ControlMaster multiplex socket for a host is alive.

    Returns the socket status. Use this before running commands to verify
    the pre-authenticated session is still active.

    Args:
        host: SSH config alias or hostname.
    """
    _validate_host(host)
    return _run(["ssh", "-O", "check", host])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
