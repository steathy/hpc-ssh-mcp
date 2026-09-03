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
from mcp.types import ToolAnnotations

__version__ = "1.1.0"

mcp = FastMCP(name="SSH-HPC-Remote-Control", version=__version__)

DEFAULT_TIMEOUT = 120
# Bulk transfers are slow by nature; a 120 s cap silently truncated large files.
DEFAULT_SCP_TIMEOUT = 3600

# Context protection. read_remote_file asks the remote for at most this many
# bytes (plus one, to detect truncation); every tool's returned text is capped
# at MAX_OUTPUT_CHARS so a runaway command cannot flood the model's context.
DEFAULT_MAX_BYTES = 200_000
MAX_OUTPUT_CHARS = 200_000

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

# MCP tool annotations. Clients use these to decide what may run without a
# prompt (read-only, idempotent) and what deserves confirmation (destructive).
# openWorldHint is always True: every tool talks to a remote system.
_READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
_ADDITIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
_OVERWRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True)
_ARBITRARY = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)

_VALID_HOST_RE = re.compile(r"^[a-zA-Z0-9._@-]+$")
_VALID_SLURM_JOB_ID_RE = re.compile(r"^\d+([_.]\d+)*$")
_VALID_PBS_JOB_ID_RE = re.compile(r"^\d+(\[\d*\])?(\.[A-Za-z0-9.-]+)?$")
_VALID_DIRECTIVE_RE = re.compile(r"^[A-Za-z0-9_.:=,+-]+$")  # account, queue, -l select strings
_VALID_WALLTIME_RE = re.compile(r"^\d{1,3}:\d{2}:\d{2}$")
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


def _truncate(text: str, limit: int, hint: str = "") -> str:
    """Cut text at limit characters and say so."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[output truncated to {limit} characters{hint}]"


def _format_result(returncode: int, stdout: str, stderr: str) -> str:
    """Format a subprocess result into a human-readable string."""
    if returncode == 0:
        return _truncate(stdout, MAX_OUTPUT_CHARS) if stdout.strip() else "(no output)"
    parts = [f"[EXIT CODE {returncode}]"]
    if stdout.strip():
        parts.append(f"stdout:\n{_truncate(stdout.rstrip(), MAX_OUTPUT_CHARS)}")
    if stderr.strip():
        parts.append(f"stderr:\n{_truncate(stderr.rstrip(), MAX_OUTPUT_CHARS)}")
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


# A remote script is delivered on stdin to `bash -s` rather than interpolated
# into the ssh command line. The user's login shell (bash, zsh, tcsh, ...) only
# ever sees the two words "bash -s", so no quoting rules of that shell apply,
# multi-line scripts work, and '!' or '2>/dev/null' cannot be misparsed.
BASH_STDIN = "bash -s"


def _run_ssh_script(host: str, script: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Run a bash script on the remote host via stdin; formatted output."""
    return _run_ssh(host, BASH_STDIN, timeout=timeout, input_data=script)


def _run_ssh_script_raw(host: str, script: str, timeout: int = DEFAULT_TIMEOUT) -> tuple[int, str, str]:
    """Run a bash script on the remote host via stdin; raw (rc, stdout, stderr)."""
    return _run_ssh_raw(host, BASH_STDIN, timeout=timeout, input_data=script)


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
# Command policy
# ---------------------------------------------------------------------------
# NCAR and CU Boulder both terminate heavy processes on login nodes without
# warning, and neither wants an agent escalating privileges or wiping a
# filesystem. Commands are sorted into four tiers before they are sent:
#
#   block   never runs, no override
#   confirm runs only with confirm_destructive=True
#   route   on a login/DTN node, runs only with allow_on_login_node=True
#   free    runs
#
# Rules are deliberately narrow: a false positive in `block` is worse than a
# miss, because the user cannot work around it.

TIER_ORDER = {"free": 0, "route": 1, "confirm": 2, "block": 3}

# Paths whose recursive deletion is never an accident worth allowing.
_PROTECTED_ROOT_RE = re.compile(
    r"""^["']?(?:
        /|/\*
      | ~/?|~/\*
      | \$\{?HOME\}?/?\*?
      | /glade(?:/scratch|/work|/campaign|/u|/derecho)?/?\*?
      | /scratch(?:/alpine)?/?\*?
      | /pl(?:/active)?/?\*?
      | /projects/?\*?
      | /home/?\*?
      | /work/?\*?
    )["']?$""",
    re.X,
)

# Whole-command patterns (checked before segmentation).
_WHOLE_BLOCK_RULES = (
    (re.compile(r":\(\)\s*\{"), "fork bomb"),
)

# Per-segment rules: (pattern, tier, description, roles or None for all).
_LOGIN_ROLES = ("login", "dtn")
_COMPUTE_ROLES = ("login",)  # transfers are the point of a DTN

_SEGMENT_RULES = (
    # --- block -------------------------------------------------------------
    (re.compile(r"^(?:sudo|sudoedit|su)(?:\s|$)"), "block", "sudo / su (privilege escalation)", None),
    (re.compile(r"^(?:apt|apt-get|aptitude|yum|dnf|zypper|pacman|rpm)\s"), "block",
     "system package manager (use conda/spack in your own space)", None),
    (re.compile(r"^mkfs(?:\.\w+)?\s"), "block", "mkfs (filesystem creation)", None),
    (re.compile(r"^dd\b[^|]*\bof=/dev/"), "block", "dd writing to a device node", None),
    (re.compile(r"(?:>>?\s*|tee\s+(?:-a\s+)?)\S*authorized_keys"), "block",
     "write to authorized_keys (SSH key persistence)", None),
    (re.compile(r"^sed\s+-i\b[^;]*authorized_keys"), "block",
     "in-place edit of authorized_keys (SSH key persistence)", None),
    # --- confirm -----------------------------------------------------------
    (re.compile(r"^find\b.*(?:-delete\b|-exec\s+rm\b)"), "confirm", "find ... -delete / -exec rm", None),
    (re.compile(r"^ch(?:mod|own|grp)\b.*(?:\s-[a-zA-Z]*R\b|--recursive)"), "confirm",
     "recursive chmod/chown", None),
    (re.compile(r"^git\s+push\b.*(?:--force(?:-with-lease)?\b|\s-f\b)"), "confirm", "git push --force", None),
    (re.compile(r"^git\s+reset\b.*--hard\b"), "confirm", "git reset --hard", None),
    (re.compile(r"^git\s+clean\b.*\s-[a-zA-Z]*[fd]"), "confirm", "git clean -fd", None),
    (re.compile(r"^git\s+branch\b.*\s-D\b"), "confirm", "git branch -D (force delete)", None),
    (re.compile(r"^scancel\b.*(?:\s-u\b|--user)"), "confirm", "scancel for a whole user", None),
    (re.compile(r"^qdel\b.*[$`]"), "confirm", "qdel over a command-substituted job list", None),
    (re.compile(r"^truncate\b"), "confirm", "truncate", None),
    (re.compile(r"^shred\b"), "confirm", "shred", None),
    (re.compile(r"^crontab\b(?!\s+-l\b)"), "confirm",
     "crontab edit (NCAR: use cron.hpc.ucar.edu, not a login node)", None),
    (re.compile(r"^ssh-keygen\b"), "confirm", "ssh-keygen", None),
    (re.compile(r"^dd\b"), "confirm", "dd", None),
    (re.compile(
        r">>?\s*\S+\.(?:nc|nc4|cdf|h5|hdf5?|grb2?|grib2?|zarr|npy|npz|mat|pkl|parquet|tif{1,2})\b"
    ), "confirm", "redirect over a data file", None),
    # --- route: compute-shaped work ---------------------------------------
    (re.compile(r"^(?:cdo|ncks|ncra|ncrcat|ncbo|ncap2|ncatted|ncwa|ncflint|nccopy|"
                r"wgrib2?|gdalwarp|gdal_translate|ffmpeg)\b"), "route",
     "data-processing tool", _LOGIN_ROLES),
    (re.compile(r"^(?:mpiexec|mpirun|aprun|ibrun|jsrun)\b"), "route", "MPI launcher", _LOGIN_ROLES),
    (re.compile(r"^(?:conda|mamba|micromamba)\s+(?:create|install|update|upgrade|remove|uninstall|"
                r"env\s+(?:create|update|remove))\b"), "route", "conda/mamba environment change", _LOGIN_ROLES),
    (re.compile(r"^(?:pip|pip3)\s+(?:install|uninstall|download|wheel)\b"), "route",
     "pip install", _LOGIN_ROLES),
    (re.compile(r"^(?:nohup|setsid|screen|tmux|jupyter(?:-\w+)?|dask-\w+|ray|"
                r"streamlit|tensorboard)\b"), "route", "long-running or background process", _LOGIN_ROLES),
    (re.compile(r"^(?:zip|unzip|gzip|gunzip|bzip2|bunzip2|xz|unxz|7z|zstd)\b"), "route",
     "archive/compression over the filesystem", _COMPUTE_ROLES),
    (re.compile(r"^(?:rsync|scp|sftp)\b"), "route", "bulk transfer", _COMPUTE_ROLES),
    (re.compile(r"^cp\b.*\s-[a-zA-Z]*[rRa]"), "route", "recursive copy", _COMPUTE_ROLES),
)

_SEGMENT_SPLIT_RE = re.compile(r"(?:\r?\n|;|\|\||&&|\||&)+")
_ENV_PREFIX_RE = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+")
_VERSION_FLAG_RE = re.compile(r"(?:^|\s)(?:--version|--help|-h|-V|-v)(?:\s|$)")

_INTERPRETER_RE = re.compile(r"^(?:python[23]?(?:\.\d+)?|Rscript|julia|ncl|matlab|idl|perl|ruby)\b(?P<rest>.*)$", re.S)
_COMPILER_RE = re.compile(
    r"^(?:make|gmake|cmake|ninja|scons|meson|bazel|gcc|g\+\+|cc|c\+\+|clang(?:\+\+)?|"
    r"gfortran|ifort|ifx|icc|icpc|icx|nvcc|nvfortran|mpicc|mpicxx|mpic\+\+|mpif90|mpifort|"
    r"ftn|CC|cargo)\b(?P<rest>.*)$", re.S,
)
_TAR_RE = re.compile(r"^tar\b(?P<rest>.*)$", re.S)
_RM_RE = re.compile(r"^rm\b(?P<rest>.*)$", re.S)


def _segments(command: str) -> list[str]:
    """Split a shell command into the pieces that each start a new command."""
    out = []
    for raw in _SEGMENT_SPLIT_RE.split(command):
        seg = _ENV_PREFIX_RE.sub("", raw.strip())
        # A segment may start inside a substitution: `qdel $(qselect ...)`.
        seg = seg.lstrip("({` \t")
        if seg:
            out.append(seg)
    return out


def _rm_tier(segment: str) -> tuple[str, str] | None:
    """Classify an `rm` invocation: block on protected roots, confirm if recursive."""
    m = _RM_RE.match(segment)
    if not m:
        return None
    recursive = False
    paths, end_of_flags = [], False
    for tok in m.group("rest").split():
        if tok == "--":
            end_of_flags = True
            continue
        if not end_of_flags and tok.startswith("-"):
            if tok == "--recursive" or (not tok.startswith("--") and re.search(r"[rR]", tok)):
                recursive = True
            continue
        paths.append(tok)
    if not recursive:
        return None
    for path in paths:
        if _PROTECTED_ROOT_RE.match(path.replace('"', "").replace("'", "")):
            return ("block", f"recursive delete of a protected path ({path})")
    return ("confirm", "recursive delete (rm -r)")


def _interpreter_tier(segment: str) -> tuple[str, str] | None:
    """python/Rscript/... running a script is compute; -c, --version and a bare REPL are not."""
    m = _INTERPRETER_RE.match(segment)
    if not m:
        return None
    rest = m.group("rest")
    name = segment.split()[0]
    if _VERSION_FLAG_RE.search(rest) or re.search(r"(?:^|\s)-c(?:\s|$)", rest):
        return None
    if re.search(r"(?:^|\s)-m\s+\S", rest):
        return ("route", f"{name} -m: runs a module")
    for tok in rest.split():
        if not tok.startswith("-"):
            return ("route", f"{name} running a script")
    return None


def _compiler_tier(segment: str) -> tuple[str, str] | None:
    m = _COMPILER_RE.match(segment)
    if not m or _VERSION_FLAG_RE.search(m.group("rest")):
        return None
    return ("route", f"{segment.split()[0]}: compilation/build")


def _tar_tier(segment: str) -> tuple[str, str] | None:
    """Creating or extracting an archive is I/O-heavy; listing one is not."""
    m = _TAR_RE.match(segment)
    if not m:
        return None
    for tok in m.group("rest").split():
        if tok.startswith("--"):
            if tok in ("--create", "--extract", "--get"):
                return ("route", "tar create/extract")
            continue
        flags = tok.lstrip("-")
        if tok.startswith("-") or "=" not in tok:
            if re.search(r"[cx]", flags) and not flags.startswith("f"):
                return ("route", "tar create/extract")
        break
    return None


_CALLABLE_RULES = (
    (_rm_tier, None),
    (_interpreter_tier, _LOGIN_ROLES),
    (_compiler_tier, _LOGIN_ROLES),
    (_tar_tier, _COMPUTE_ROLES),
)


def _host_role(host: str) -> str:
    """Role of a host: 'login', 'dtn', 'compute' or 'workstation'.

    Login is the safe default: it is what an HPC alias almost always points at,
    and it is the role with the strictest routing rules.
    """
    return "login"


def _classify_command(command: str, role: str = "login") -> tuple[str, str]:
    """Return (tier, rule) for a command, taking the strictest tier that applies."""
    worst, why = "free", ""

    def consider(tier: str, rule: str) -> None:
        nonlocal worst, why
        if TIER_ORDER[tier] > TIER_ORDER[worst]:
            worst, why = tier, rule

    for pattern, rule in _WHOLE_BLOCK_RULES:
        if pattern.search(command):
            consider("block", rule)

    for segment in _segments(command):
        for pattern, tier, rule, roles in _SEGMENT_RULES:
            if roles is not None and role not in roles:
                continue
            if pattern.search(segment):
                consider(tier, rule)
        for func, roles in _CALLABLE_RULES:
            if roles is not None and role not in roles:
                continue
            hit = func(segment)
            if hit:
                consider(*hit)
    return worst, why


def _policy_refusal(
    command: str,
    role: str,
    confirm_destructive: bool,
    allow_on_login_node: bool,
) -> str | None:
    """Return a refusal message if policy stops this command, else None."""
    tier, rule = _classify_command(command, role)
    if tier == "block":
        return (
            f"Blocked by policy: {rule}.\n"
            "This tool will not run that command on a shared HPC system, and there is no "
            "override. If you genuinely need it, run it yourself from your own terminal."
        )
    if tier == "confirm" and not confirm_destructive:
        return (
            f"Refused pending confirmation: {rule}.\n"
            "This can destroy data that is not recoverable. Ask the user to confirm, then "
            "call again with confirm_destructive=true."
        )
    if tier == "route" and not allow_on_login_node:
        return (
            f"Refused on a login node: {rule}.\n"
            f"NCAR and CU Boulder terminate this kind of work on login nodes and may flag the "
            "account. Use run_on_compute(host, command, account=...) to run it on a compute "
            "node, or submit_job for anything long. If this really is a small, quick case, "
            "call again with allow_on_login_node=true."
        )
    return None


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

@mcp.tool(annotations=_ARBITRARY)
def execute_remote_bash(
    host: str,
    command: str,
    timeout: int = DEFAULT_TIMEOUT,
    confirm_destructive: bool = False,
    allow_on_login_node: bool = False,
) -> str:
    """Execute a bash command (or multi-line script) on a remote SSH host.

    The host must match an alias in ~/.ssh/config. Uses the system ssh
    binary so ControlMaster multiplex sockets are respected. The command is
    delivered on stdin to `bash -s`, so pipes, quotes, newlines and '!' all
    arrive intact whatever the remote login shell is. The script itself has
    no stdin (commands that wait for input will see EOF).

    Runs on the node you SSH into, usually a login node: keep it to short,
    light commands and submit anything heavy as a job.

    Args:
        host: SSH config alias or hostname.
        command: The bash command string or script to execute remotely.
        timeout: Max seconds to wait (default 120).
        confirm_destructive: Set only after the user has confirmed a command the
            server flagged as destructive (recursive delete, force push, mass cancel).
        allow_on_login_node: Set only for a genuinely small, quick case of work the
            server would otherwise route to a compute node.
    """
    _validate_host(host)
    _validate_timeout(timeout)
    refusal = _policy_refusal(
        command, _host_role(host), confirm_destructive, allow_on_login_node,
    )
    if refusal:
        return refusal
    return _run_ssh_script(host, command, timeout=timeout)


# ---------------------------------------------------------------------------
# Scheduler-aware job tools (PBS Pro on NCAR Derecho/Casper, Slurm on CURC Alpine)
# ---------------------------------------------------------------------------

SCHEDULERS = ("pbs", "slurm")
DEFAULT_COMPUTE_TIMEOUT = 1800
_SQUEUE_FORMAT = "%.18i %.9P %.30j %.8u %.8T %.10M %.9l %.6D %R"

# alias -> "pbs" | "slurm", filled by _detect_scheduler. A host does not change
# its scheduler mid-session; probing once keeps login-node load negligible.
_SCHEDULER_CACHE: dict[str, str] = {}


def _detect_scheduler(host: str) -> str:
    """Return 'pbs' or 'slurm' for host, probing once for qsub / sbatch."""
    if host in _SCHEDULER_CACHE:
        return _SCHEDULER_CACHE[host]
    script = 'for s in qsub sbatch; do command -v "$s" >/dev/null 2>&1 && echo "$s"; done; true'
    rc, out, err = _run_ssh_script_raw(host, script)
    if rc != 0:
        msg = f"Could not probe the scheduler on {host!r}:\n{_format_result(rc, out, err)}"
        if rc == SSH_OWN_FAILURE_RC:
            msg += _diagnose_ssh_failure(host, err)
        raise ValueError(msg)
    found = set(out.split())
    has_pbs, has_slurm = "qsub" in found, "sbatch" in found
    if has_pbs and has_slurm:
        raise ValueError(
            f"Both qsub and sbatch exist on {host!r}; pass scheduler='pbs' or scheduler='slurm'."
        )
    if has_pbs:
        sched = "pbs"
    elif has_slurm:
        sched = "slurm"
    else:
        raise ValueError(
            f"No PBS or Slurm scheduler commands found on {host!r} (looked for qsub and sbatch). "
            "Is this a login node of an HPC system?"
        )
    _SCHEDULER_CACHE[host] = sched
    return sched


def _resolve_scheduler(host: str, scheduler: str) -> str:
    if scheduler == "auto":
        return _detect_scheduler(host)
    if scheduler not in SCHEDULERS:
        raise ValueError(f"scheduler must be 'auto', 'pbs' or 'slurm', got {scheduler!r}")
    return scheduler


def _validate_job_id(job_id: str, scheduler: str) -> str:
    """Validate a job ID for the scheduler and return it shell-quoted."""
    if scheduler == "pbs":
        ok = _VALID_PBS_JOB_ID_RE.match(job_id or "")
        label, expected = "PBS", "e.g. '2426690', '2426690.desched1' or '123[].desched1'"
    else:
        ok = _VALID_SLURM_JOB_ID_RE.match(job_id or "")
        label, expected = "Slurm", "e.g. '12345', '12345_0' (array) or '12345.0' (step)"
    if not ok:
        raise ValueError(f"Invalid {label} job ID: {job_id!r}. Expected {expected}.")
    return shlex.quote(job_id)


def _validate_directive(name: str, value: str, pattern: re.Pattern = None) -> None:
    """Reject scheduler directive values that could carry shell syntax."""
    if value and not (pattern or _VALID_DIRECTIVE_RE).match(value):
        raise ValueError(f"Invalid {name}: {value!r}")


@mcp.tool(annotations=_ADDITIVE)
def submit_job(
    host: str,
    job_script_content: str,
    remote_filename: str = "",
    remote_dir: str = "",
    scheduler: str = "auto",
) -> str:
    """Write a batch script to a remote host and submit it (qsub on PBS, sbatch on Slurm).

    The script content is piped via stdin, so #PBS / #SBATCH directives and
    quoting survive intact. Returns the scheduler's output: a PBS job ID such
    as '2426690.desched1', or 'Submitted batch job <ID>' on Slurm.

    NCAR Derecho and Casper are PBS Pro (scripts need '#PBS -A <project>');
    CU Boulder Alpine is Slurm. Submit from a scratch or work directory via
    remote_dir (for example /glade/derecho/scratch/<user>/run1 or
    /scratch/alpine/<user>/run1) rather than from $HOME.

    Args:
        host: SSH config alias for the HPC system.
        job_script_content: Full text of the batch script, including directives.
        remote_filename: Script name on the remote host. Auto-generated if empty.
        remote_dir: Directory to write to and submit from (created if missing).
            Defaults to the SSH login directory, usually $HOME.
        scheduler: 'auto' (detect once per host), 'pbs' or 'slurm'.
    """
    _validate_host(host)
    sched = _resolve_scheduler(host, scheduler)
    if not remote_filename:
        remote_filename = f"claude_job_{uuid.uuid4().hex[:8]}.sh"
    for name, value in (("remote_filename", remote_filename), ("remote_dir", remote_dir)):
        if value.startswith("-"):
            raise ValueError(f"{name} must not start with '-': {value!r}")
    safe_fn = _shell_path(remote_filename)
    enter = write_prefix = ""
    if remote_dir:
        safe_dir = _shell_path(remote_dir)
        enter = f"cd {safe_dir} && "
        write_prefix = f"mkdir -p {safe_dir} && {enter}"

    # The script body occupies stdin here, so this one line is interpolated
    # into the ssh command. It is a single line with no '!' and only plain
    # redirection, which every login shell parses the same way.
    rc, out, err = _run_ssh_raw(
        host,
        f"{write_prefix}cat > {safe_fn} && chmod -- +x {safe_fn}",
        input_data=job_script_content,
    )
    if rc != 0:
        msg = f"Failed to write script to {remote_filename}:\n{_format_result(rc, out, err)}"
        if rc == SSH_OWN_FAILURE_RC:
            msg += _diagnose_ssh_failure(host, err)
        return msg

    submit = f"qsub {safe_fn}" if sched == "pbs" else f"sbatch -- {safe_fn}"
    return _run_ssh_script(host, enter + submit)


@mcp.tool(annotations=_READ_ONLY)
def check_job(host: str, job_id: str, scheduler: str = "auto") -> str:
    """Check the status of a batch job, including one that has already finished.

    PBS: 'qstat -x' (history included) plus the key fields of 'qstat -x -f'
    (state, queue, exit status, comment, resources used, output paths).
    Slurm: squeue for running/pending plus sacct for accounting. One round
    trip either way.

    Args:
        host: SSH config alias for the HPC system.
        job_id: PBS ID like '2426690' or '2426690.desched1' (array '123[].desched1');
            Slurm ID like '12345', '12345_0' (array) or '12345.0' (step).
        scheduler: 'auto' (detect once per host), 'pbs' or 'slurm'.
    """
    _validate_host(host)
    sched = _resolve_scheduler(host, scheduler)
    sid = _validate_job_id(job_id, sched)
    if sched == "pbs":
        script = (
            f"qstat -x -w {sid} 2>&1 || true\n"
            "echo\n"
            f"qstat -x -f {sid} 2>/dev/null | grep -E "
            "'^ *(job_state|queue|Exit_status|comment|stime|obittime|"
            "resources_used\\.(walltime|ncpus|mem)|Output_Path|Error_Path) =' || true\n"
        )
    else:
        script = (
            "echo '=== squeue (running/pending) ==='\n"
            f"squeue -j {sid} --format='{_SQUEUE_FORMAT}' 2>/dev/null "
            "|| echo '(not in queue: finished or unknown; see sacct below)'\n"
            "echo\n"
            "echo '=== sacct (accounting) ==='\n"
            f"sacct -j {sid} --format=JobID,JobName,Partition,State,ExitCode,Elapsed,Start,End --parsable2\n"
        )
    return _run_ssh_script(host, script)


@mcp.tool(annotations=_READ_ONLY)
def list_queue(host: str, user: str = "", scheduler: str = "auto") -> str:
    """List batch jobs in the queue for a user (qstat on PBS, squeue on Slurm).

    Defaults to the remote $USER. Poll sparingly: HPC centers flag agents
    that hammer the scheduler; once every 30 s or more is plenty.

    Args:
        host: SSH config alias for the HPC system.
        user: Username to filter by. Defaults to the remote $USER.
        scheduler: 'auto' (detect once per host), 'pbs' or 'slurm'.
    """
    _validate_host(host)
    sched = _resolve_scheduler(host, scheduler)
    if user:
        if not _VALID_USERNAME_RE.match(user):
            raise ValueError(f"Invalid username: {user!r}")
        who = shlex.quote(user)
    else:
        who = '"$USER"'
    if sched == "pbs":
        script = f"qstat -w -u {who}"
    else:
        script = f"squeue -u {who} --format='{_SQUEUE_FORMAT}'"
    return _run_ssh_script(host, script)


@mcp.tool(annotations=_OVERWRITES)
def cancel_job(host: str, job_id: str, scheduler: str = "auto") -> str:
    """Cancel a batch job by ID (qdel on PBS, scancel on Slurm).

    Args:
        host: SSH config alias for the HPC system.
        job_id: The job ID to cancel, in the scheduler's own format.
        scheduler: 'auto' (detect once per host), 'pbs' or 'slurm'.
    """
    _validate_host(host)
    sched = _resolve_scheduler(host, scheduler)
    sid = _validate_job_id(job_id, sched)
    return _run_ssh_script(host, f"qdel {sid}" if sched == "pbs" else f"scancel {sid}")


@mcp.tool(annotations=_ARBITRARY)
def run_on_compute(
    host: str,
    command: str,
    account: str = "",
    walltime: str = "00:30:00",
    queue: str = "",
    resources: str = "",
    scheduler: str = "auto",
    timeout: int = DEFAULT_COMPUTE_TIMEOUT,
    confirm_destructive: bool = False,
) -> str:
    """Run one command on a compute node and wait for it, instead of on the login node.

    Use this rather than execute_remote_bash for anything that runs longer
    than a minute, uses more than a few GB of memory, or does heavy I/O:
    NCAR and CURC terminate such processes on login nodes and may email or
    flag the account. PBS uses NCAR's 'qcmd' (submits, waits, returns the
    output); Slurm uses 'srun'. Blocks until the job finishes or timeout.

    Args:
        host: SSH config alias for the HPC system.
        command: Bash command to run on the compute node.
        account: Project / allocation to charge (PBS -A, Slurm --account).
            Required on NCAR unless PBS_ACCOUNT is set in the remote environment.
        walltime: HH:MM:SS wall-clock limit (default 00:30:00).
        queue: PBS queue (e.g. 'main', 'casper', 'develop') or Slurm partition (e.g. 'amilan').
        resources: PBS '-l' select string (e.g. 'select=1:ncpus=4:mem=16GB'), or on
            Slurm comma-separated key=value pairs turned into srun flags
            (e.g. 'qos=normal,ntasks=4,mem=16G').
        scheduler: 'auto' (detect once per host), 'pbs' or 'slurm'.
        timeout: Max seconds to wait including queue time (default 1800).
        confirm_destructive: Set only after the user has confirmed a command the
            server flagged as destructive.
    """
    _validate_host(host)
    _validate_timeout(timeout)
    # role='compute': this command runs on a compute node by construction, so the
    # login-node routing rules do not apply, but block/confirm still do.
    refusal = _policy_refusal(command, "compute", confirm_destructive, True)
    if refusal:
        return refusal
    _validate_directive("account", account)
    _validate_directive("queue", queue)
    _validate_directive("resources", resources)
    _validate_directive("walltime", walltime, _VALID_WALLTIME_RE)
    sched = _resolve_scheduler(host, scheduler)
    quoted = shlex.quote(command)
    if sched == "pbs":
        parts = ["qcmd"]
        if account:
            parts += ["-A", account]
        if queue:
            parts += ["-q", queue]
        parts += ["-l", f"walltime={walltime}"]
        if resources:
            parts += ["-l", resources]
        parts += ["--", "bash", "-c", quoted]
    else:
        parts = ["srun"]
        if account:
            parts.append(f"--account={account}")
        if queue:
            parts.append(f"--partition={queue}")
        for pair in filter(None, resources.split(",")):
            key, sep, value = pair.partition("=")
            if not sep or not key or not value:
                raise ValueError(f"Invalid resources entry {pair!r}; expected key=value")
            parts.append(f"--{key}={value}")
        parts.append(f"--time={walltime}")
        parts += ["bash", "-c", quoted]
    return _run_ssh_script(host, " ".join(parts), timeout=timeout)


@mcp.tool(annotations=_READ_ONLY)
def read_remote_file(
    host: str,
    remote_path: str,
    max_lines: int = 0,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> str:
    """Read a text file on a remote host and return its contents.

    Suitable for source code, CSVs, config files, and scheduler .out logs.
    Output is capped at max_bytes (default 200 KB) and binary files are
    refused; use scp_download_file for those, tail_remote_file for the end
    of a long log.

    Args:
        host: SSH config alias or hostname.
        remote_path: Path on the remote host. Absolute paths are safest; '~/x' is
            expanded to the remote home directory.
        max_lines: If > 0, only return the first N lines.
        max_bytes: Never return more than this many bytes (default 200000).
    """
    _validate_host(host)
    if max_bytes < 1:
        raise ValueError(f"max_bytes must be >= 1, got {max_bytes}")
    path = _shell_path(remote_path)
    probe = int(max_bytes) + 1  # one extra byte tells us the file was longer
    if max_lines > 0:
        script = f"head -n {int(max_lines)} -- {path} | head -c {probe}"
    else:
        script = f"head -c {probe} -- {path}"

    rc, out, err = _run_ssh_script_raw(host, script)
    if rc != 0:
        result = _format_result(rc, out, err)
        return result + (_diagnose_ssh_failure(host, err) if rc == SSH_OWN_FAILURE_RC else "")
    if "\x00" in out:
        return (
            f"{remote_path} looks like a binary file (NUL bytes in the first {len(out)} bytes). "
            "Use scp_download_file to fetch it instead of reading it into context."
        )
    if len(out) > max_bytes:
        return out[:max_bytes] + (
            f"\n[truncated at {max_bytes} bytes; the file is longer. Use max_lines, "
            "a larger max_bytes, tail_remote_file for the end, or scp_download_file for all of it]"
        )
    return _format_result(rc, out, err)


@mcp.tool(annotations=_READ_ONLY)
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
        remote_path: Path on the remote host. Absolute paths are safest; '~/x' is
            expanded to the remote home directory.
        lines: Number of lines to read from the end (default 50).
    """
    _validate_host(host)
    if lines < 1:
        raise ValueError(f"lines must be >= 1, got {lines}")
    return _run_ssh_script(host, f"tail -n {int(lines)} -- {_shell_path(remote_path)}")


@mcp.tool(annotations=_OVERWRITES)
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


@mcp.tool(annotations=_OVERWRITES)
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


@mcp.tool(annotations=_READ_ONLY)
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
