"""SSH & HPC Remote Control MCP Server.

A universal bridge to any SSH-enabled server or supercomputer.
Uses native ssh/scp binaries via subprocess to respect ~/.ssh/config
and ControlMaster multiplex sockets (avoiding MFA re-prompts).

Run with:  uv run ssh_hpc_server.py
"""

import os
import re
import shlex
import subprocess
import uuid

from fastmcp import FastMCP

__version__ = "1.0.0"

mcp = FastMCP(name="SSH-HPC-Remote-Control", version=__version__)

DEFAULT_TIMEOUT = 120
# Bulk transfers are slow by nature; a 120 s cap silently truncated large files.
DEFAULT_SCP_TIMEOUT = 3600

# ssh reserves exit status 255 for its own failures (connection, auth, control
# socket). Any other status belongs to the remote command, whose stderr must not
# be mistaken for a session problem.
SSH_OWN_FAILURE_RC = 255

# Applied to every ssh/scp invocation that actually opens a connection.
# BatchMode=yes:    refuse interactive auth (password, keyboard-interactive/MFA);
#                   MCP servers have no TTY, so interactive auth would otherwise
#                   either hang reading the JSON-RPC stream or fail cryptically
#                   120s later.
# ConnectTimeout=10: bound the TCP handshake so an unreachable host fails fast
#                   instead of riding the OS default (~75-120s).
SSH_OPTS: tuple[str, ...] = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=10")

_VALID_HOST_RE = re.compile(r"^[a-zA-Z0-9._@-]+$")
_VALID_JOB_ID_RE = re.compile(r"^\d+([_.]\d+)*$")
_VALID_USERNAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")
_OPENSSH_VERSION_RE = re.compile(r"OpenSSH_(\d+)\.(\d+)")

# scp protocol mode of the *local* scp binary, resolved lazily from `ssh -V`.
# OpenSSH >= 9.0 speaks SFTP by default: the remote path is sent literally
# (no remote shell), so it must NOT be shell-quoted. Older scp runs a remote
# shell, so the path MUST be quoted. None = not yet probed.
_SCP_SFTP_MODE: bool | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _validate_host(host: str) -> None:
    """Reject host strings that could be used for shell or option injection."""
    if not host or not _VALID_HOST_RE.match(host) or host.startswith("-"):
        raise ValueError(
            f"Invalid SSH host alias: {host!r}. "
            "Must contain only alphanumeric characters, dots, hyphens, underscores, or @, "
            "and must not start with '-'."
        )


def _validate_timeout(timeout: int) -> None:
    if timeout is None or timeout < 1:
        raise ValueError(f"timeout must be a positive number of seconds, got {timeout!r}")


def _shell_path(path: str) -> str:
    """Quote a remote path for interpolation into a remote shell command.

    shlex.quote would turn a leading '~' into a literal, so home-relative
    paths are rewritten to "$HOME"/<quoted rest> outside the quotes.
    """
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        rest = path[2:]
        return '"$HOME"' if not rest else f'"$HOME"/{shlex.quote(rest)}'
    return shlex.quote(path)


def _local_path(path: str) -> str:
    """Normalise a local path for scp's argv.

    An absolute path starts with '/', which defeats both scp's host:path
    parsing (a ':' before the first '/') and option parsing (a leading '-').
    """
    return os.path.abspath(os.path.expanduser(path))


def _parse_openssh_version(banner: str) -> tuple[int, int] | None:
    m = _OPENSSH_VERSION_RE.search(banner or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def _local_openssh_version() -> tuple[int, int] | None:
    """Return (major, minor) of the local OpenSSH client, or None if unknown."""
    try:
        result = subprocess.run(
            ["ssh", "-V"], capture_output=True, encoding="utf-8", errors="replace",
            timeout=10, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # OpenSSH prints its banner on stderr; some builds use stdout.
    return _parse_openssh_version(result.stderr) or _parse_openssh_version(result.stdout)


def _scp_uses_sftp() -> bool:
    """True when the local scp defaults to the SFTP protocol (OpenSSH >= 9.0).

    Unknown versions are treated as legacy: quoting a path that did not need
    it breaks odd filenames, but leaving a path unquoted for a remote shell
    would be an injection hole.
    """
    global _SCP_SFTP_MODE
    if _SCP_SFTP_MODE is None:
        ver = _local_openssh_version()
        _SCP_SFTP_MODE = ver is not None and ver >= (9, 0)
    return _SCP_SFTP_MODE


def _scp_remote_spec(host: str, path: str) -> str:
    """Build scp's host:path argument for the protocol mode in use."""
    if _scp_uses_sftp():
        # SFTP resolves relative paths against $HOME and never expands '~'.
        if path == "~":
            path = "."
        elif path.startswith("~/"):
            path = path[2:] or "."
        return f"{host}:{path}"
    return f"{host}:{_shell_path(path)}"


def _run_raw(
    cmd: list[str],
    timeout: int = DEFAULT_TIMEOUT,
    input_data: str | None = None,
) -> tuple[int, str, str]:
    """Execute a subprocess and return (returncode, stdout, stderr).

    Never raises on non-zero exit. Returns -1 for timeouts or missing binaries.

    stdin is /dev/null unless input_data is given: the server's own stdin is
    the MCP JSON-RPC stream, and ssh forwards whatever it inherits to the
    remote command. Output is decoded as UTF-8 with replacement so a stray
    Latin-1 byte in a log cannot turn into an exception.
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            input=input_data,
            stdin=None if input_data is not None else subprocess.DEVNULL,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode(errors="replace")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode(errors="replace")
        return -1, stdout, f"Timed out after {timeout}s. {stderr}"
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


def _ssh_cmd(host: str, remote_cmd: str) -> list[str]:
    """Build an ssh argv with batch-safe defaults (see SSH_OPTS)."""
    return ["ssh", *SSH_OPTS, host, remote_cmd]


def _scp_cmd(*scp_args: str) -> list[str]:
    """Build an scp argv with batch-safe defaults (see SSH_OPTS)."""
    return ["scp", *SSH_OPTS, *scp_args]


def _diagnose_ssh_failure(host: str, stderr: str) -> str:
    """Map known SSH failure patterns to an actionable hint, or empty string.

    OpenSSH does not surface a structured 'ControlMaster expired' signal, so we
    fingerprint stderr. Patterns cover the three common shapes:
      - auth fell back to interactive (Duo would be needed) → re-establish master
      - control socket file is gone                         → re-establish master
      - network unreachable                                 → check connectivity
    """
    s = stderr.lower()
    reauth = f"From your terminal (where Duo/MFA prompts can be answered), run:\n    ssh -fN {host}"

    if "permission denied" in s and ("keyboard-interactive" in s or "publickey" in s):
        return (
            f"\n\nHint: SSH auth failed for {host!r}. The ControlMaster socket has likely "
            f"expired, so a new connection was attempted and rejected because this server "
            f"cannot answer interactive MFA prompts.\n{reauth}\nThen retry."
        )
    if "control socket connect" in s or ("control path" in s and "no such file" in s):
        return (
            f"\n\nHint: ControlMaster socket for {host!r} is missing.\n{reauth}"
        )
    if (
        "operation timed out" in s
        or "connection timed out" in s
        or "no route to host" in s
        or "network is unreachable" in s
    ):
        return (
            f"\n\nHint: Network unreachable to {host!r}. Check VPN/connectivity, then:\n"
            f"    ssh -fN {host}"
        )
    return ""


def _run_ssh(
    host: str,
    remote_cmd: str,
    timeout: int = DEFAULT_TIMEOUT,
    input_data: str | None = None,
) -> str:
    """Run a remote command over ssh and append a diagnostic hint on failure."""
    rc, out, err = _run_raw(_ssh_cmd(host, remote_cmd), timeout=timeout, input_data=input_data)
    formatted = _format_result(rc, out, err)
    if rc == SSH_OWN_FAILURE_RC:
        formatted += _diagnose_ssh_failure(host, err)
    return formatted


def _run_ssh_raw(
    host: str,
    remote_cmd: str,
    timeout: int = DEFAULT_TIMEOUT,
    input_data: str | None = None,
) -> tuple[int, str, str]:
    """Like _run_ssh, but returns the raw (rc, stdout, stderr) for callers that branch on rc."""
    return _run_raw(_ssh_cmd(host, remote_cmd), timeout=timeout, input_data=input_data)


def _run_scp(host: str, scp_args: list[str], timeout: int = DEFAULT_SCP_TIMEOUT) -> tuple[int, str]:
    """Run scp; return (rc, formatted output with a diagnostic hint on failure).

    scp propagates ssh's 255 when the connection itself fails, so the same
    gate applies.
    """
    rc, out, err = _run_raw(_scp_cmd(*scp_args), timeout=timeout)
    formatted = _format_result(rc, out, err)
    if rc == SSH_OWN_FAILURE_RC:
        formatted += _diagnose_ssh_failure(host, err)
    return rc, formatted


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
    _validate_timeout(timeout)
    return _run_ssh(host, f"bash -c {shlex.quote(command)}", timeout=timeout)


@mcp.tool()
def submit_slurm_job(
    host: str,
    job_script_content: str,
    remote_filename: str = "",
) -> str:
    """Write a Slurm batch script to a remote host and submit it with sbatch.

    The script content is piped via stdin to avoid shell-escaping issues.
    Returns the sbatch output (typically 'Submitted batch job <ID>').

    Args:
        host: SSH config alias for the HPC system.
        job_script_content: Full text of the Slurm batch script (including #SBATCH directives).
        remote_filename: Where to write the script on the remote host. Auto-generated if empty.
    """
    _validate_host(host)
    if not remote_filename:
        remote_filename = f"claude_job_{uuid.uuid4().hex[:8]}.sh"
    if remote_filename.startswith("-"):
        raise ValueError(f"remote_filename must not start with '-': {remote_filename!r}")
    safe_fn = shlex.quote(remote_filename)

    rc, out, err = _run_ssh_raw(
        host,
        f"cat > {safe_fn} && chmod -- +x {safe_fn}",
        input_data=job_script_content,
    )
    if rc != 0:
        msg = f"Failed to write script to {remote_filename}:\n{_format_result(rc, out, err)}"
        if rc == SSH_OWN_FAILURE_RC:
            msg += _diagnose_ssh_failure(host, err)
        return msg

    return _run_ssh(host, f"sbatch -- {safe_fn}")


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

    squeue_result = _run_ssh(
        host,
        f"squeue -j {safe_id} --format='%.18i %.9P %.30j %.8u %.8T %.10M %.9l %.6D %R' 2>/dev/null",
    )
    sacct_result = _run_ssh(
        host,
        f"sacct -j {safe_id} --format=JobID,JobName,Partition,State,ExitCode,Elapsed,Start,End --parsable2",
    )

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
        if not _VALID_USERNAME_RE.match(user):
            raise ValueError(f"Invalid username: {user!r}")
        safe_user = shlex.quote(user)
        cmd = f"squeue -u {safe_user} --format='%.18i %.9P %.30j %.8u %.8T %.10M %.9l %.6D %R'"
    else:
        cmd = "squeue -u $USER --format='%.18i %.9P %.30j %.8u %.8T %.10M %.9l %.6D %R'"
    return _run_ssh(host, cmd)


@mcp.tool()
def cancel_slurm_job(host: str, job_id: str) -> str:
    """Cancel a Slurm job by its job ID.

    Args:
        host: SSH config alias for the HPC system.
        job_id: Slurm job ID to cancel (e.g. '12345', '12345_0' for array jobs).
    """
    _validate_host(host)
    if not _VALID_JOB_ID_RE.match(job_id):
        raise ValueError(
            f"Invalid Slurm job ID: {job_id!r}. "
            "Expected numeric ID, optionally with _ or . separators for array/step jobs."
        )
    safe_id = shlex.quote(job_id)
    return _run_ssh(host, f"scancel {safe_id}")


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

    return _run_ssh(host, cmd)


@mcp.tool()
def tail_remote_file(
    host: str,
    remote_path: str,
    lines: int = 50,
) -> str:
    """Read the last N lines of a text file on a remote host.

    Ideal for checking the latest output from a running or completed Slurm job
    without reading the entire file.

    Args:
        host: SSH config alias or hostname.
        remote_path: Absolute or relative path to the file on the remote host.
        lines: Number of lines to read from the end (default 50).
    """
    _validate_host(host)
    if lines < 1:
        raise ValueError(f"lines must be >= 1, got {lines}")
    safe_path = shlex.quote(remote_path)
    return _run_ssh(host, f"tail -n {int(lines)} {safe_path}")


@mcp.tool()
def scp_download_file(
    host: str,
    remote_path: str,
    local_path: str,
    timeout: int = DEFAULT_SCP_TIMEOUT,
) -> str:
    """Download a file from a remote host to the local machine via scp.

    Uses the system scp binary to respect SSH config and multiplex sockets.
    Prefer this over read_remote_file for large or binary files.

    Args:
        host: SSH config alias or hostname.
        remote_path: Path to the file on the remote host.
        local_path: Destination path on the local machine.
        timeout: Max seconds to wait for the transfer (default 3600).
    """
    _validate_host(host)
    _validate_timeout(timeout)
    local_abs = _local_path(local_path)
    existed_before = os.path.exists(local_abs)
    rc, result = _run_scp(host, [_scp_remote_spec(host, remote_path), local_abs], timeout=timeout)
    if rc == -1 and not existed_before and os.path.isfile(local_abs):
        # A timed-out scp leaves a silently truncated destination behind.
        os.remove(local_abs)
        result += f"\nPartial download removed: {local_abs}"
    return result


@mcp.tool()
def scp_upload_file(
    host: str,
    local_path: str,
    remote_path: str,
    timeout: int = DEFAULT_SCP_TIMEOUT,
) -> str:
    """Upload a file from the local machine to a remote host via scp.

    Uses the system scp binary to respect SSH config and multiplex sockets.

    Args:
        host: SSH config alias or hostname.
        local_path: Path to the file on the local machine.
        remote_path: Destination path on the remote host.
        timeout: Max seconds to wait for the transfer (default 3600).
    """
    _validate_host(host)
    _validate_timeout(timeout)
    _, result = _run_scp(
        host, [_local_path(local_path), _scp_remote_spec(host, remote_path)], timeout=timeout,
    )
    return result


@mcp.tool()
def check_ssh_connection(host: str) -> str:
    """Check if the SSH ControlMaster multiplex socket for a host is alive.

    Returns the socket status. Use this before running commands to verify
    the pre-authenticated session is still active.

    Args:
        host: SSH config alias or hostname.
    """
    _validate_host(host)
    # `ssh -O check` is a local socket query: no SSH_OPTS. OpenSSH prints the
    # verdict ("Master running (pid=N)") on stderr, not stdout.
    rc, out, err = _run_raw(["ssh", "-O", "check", host])
    if rc == 0:
        return err.strip() or out.strip() or "Master running"
    return _format_result(rc, out, err) + _diagnose_ssh_failure(host, err)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    mcp.run()


if __name__ == "__main__":
    main()
