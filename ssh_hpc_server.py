"""SSH & HPC Remote Control MCP Server.

A universal bridge to any SSH-enabled server or supercomputer.
Uses native ssh/scp binaries via subprocess to respect ~/.ssh/config
and ControlMaster multiplex sockets (avoiding MFA re-prompts).

Run with:  uv run ssh_hpc_server.py
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

__version__ = "1.10.0"

mcp = FastMCP(name="SSH-HPC-Remote-Control", version=__version__)

DEFAULT_TIMEOUT = 120
# Bulk transfers are slow by nature; a 120 s cap silently truncated large files.
DEFAULT_SCP_TIMEOUT = 3600

# Context protection. read_remote_file asks the remote for at most this many
# bytes (plus one, to detect truncation); every tool's returned text is capped
# at MAX_OUTPUT_CHARS so a runaway command cannot flood the model's context.
DEFAULT_MAX_BYTES = 200_000
MAX_OUTPUT_CHARS = 200_000

# scp over a login node is the wrong tool past a few GB: both centers point at
# Globus or a data-transfer node for bulk movement.
LARGE_TRANSFER_BYTES = 2_000_000_000

# ssh reserves exit status 255 for its own failures (connection, auth, control
# socket). Any other status belongs to the remote command, whose stderr must not
# be mistaken for a session problem.
SSH_OWN_FAILURE_RC = 255

# A timeout kills the *local* ssh client. With no TTY the remote command does
# not reliably get a signal, so it keeps running -- and a message that says only
# "Timed out" invites a retry that stacks orphans on the shared node this
# server's whole policy exists to protect. Say so instead.
_TIMEOUT_ORPHAN_NOTE = (
    "\nThe remote command was NOT stopped and is probably still running: only the local "
    "ssh client was killed. Check with execute_remote_bash(host, 'pgrep -au $USER') before "
    "retrying, and stop it there if it is still going."
)
# run_on_compute's orphan is a scheduler job as well as a login-node process.
_COMPUTE_TIMEOUT_NOTE = (
    "\nThe job it submitted may also still be queued or running on the scheduler: check "
    "with list_queue and stop it with cancel_job before retrying."
)

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
# HH:MM:SS, or Slurm's D-HH:MM:SS for anything past a day.
_VALID_WALLTIME_RE = re.compile(r"^(?:\d{1,3}-)?\d{1,3}:\d{2}:\d{2}$")
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
    env: dict | None = None,
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
            env=env,
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


def _trailing_int(text: str) -> int | None:
    """The last non-empty line of text as an integer, or None."""
    lines = (text or "").strip().splitlines()
    last = lines[-1].strip() if lines else ""
    return int(last) if last.isdigit() else None


def _format_result(returncode: int, stdout: str, stderr: str) -> str:
    """Format a subprocess result into a human-readable string.

    stderr is reported even when the command succeeded. An HPC toolchain says a
    great deal on stderr while still exiting 0 -- module load warnings, compiler
    diagnostics, srun allocation notes, conda solver messages -- and dropping
    all of it left the caller unable to react to what it never saw.
    """
    if returncode == 0:
        parts = []
        if stdout.strip():
            parts.append(stdout)
        if stderr.strip():
            parts.append(f"stderr:\n{stderr.rstrip()}")
        if not parts:
            return "(no output)"
    else:
        parts = [f"[EXIT CODE {returncode}]"]
        if stdout.strip():
            parts.append(f"stdout:\n{stdout.rstrip()}")
        if stderr.strip():
            parts.append(f"stderr:\n{stderr.rstrip()}")
    # One cap on what is returned. Truncating each stream to MAX_OUTPUT_CHARS
    # and then joining them meant a failing command could return twice it.
    return _truncate("\n".join(parts), MAX_OUTPUT_CHARS)


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
    if "no controlpath specified" in s:
        return (
            f"\n\nHint: {host!r} has no ControlPath configured, so its connections are not "
            "multiplexed. That is fine for a host that does not need MFA -- commands will "
            "just open a new connection each time. To reuse one pre-authenticated session, "
            "add ControlMaster/ControlPath/ControlPersist to its ~/.ssh/config block."
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


def _run_ssh_checked(
    host: str,
    remote_cmd: str,
    timeout: int = DEFAULT_TIMEOUT,
    input_data: str | None = None,
) -> tuple[bool, str]:
    """Like _run_ssh, but also says whether the command succeeded.

    Callers that must not remember a failure (see _cached_poll) need the status
    as well as the text.
    """
    rc, out, err = _run_ssh_raw(host, remote_cmd, timeout=timeout, input_data=input_data)
    formatted = _format_result(rc, out, err)
    if rc == SSH_OWN_FAILURE_RC:
        formatted += _diagnose_ssh_failure(host, err)
    return rc == 0, formatted + _onboarding_notice(host)


def _run_ssh(
    host: str,
    remote_cmd: str,
    timeout: int = DEFAULT_TIMEOUT,
    input_data: str | None = None,
) -> str:
    """Run a remote command over ssh and append a diagnostic hint on failure."""
    return _run_ssh_checked(host, remote_cmd, timeout=timeout, input_data=input_data)[1]


def _run_ssh_raw(
    host: str,
    remote_cmd: str,
    timeout: int = DEFAULT_TIMEOUT,
    input_data: str | None = None,
) -> tuple[int, str, str]:
    """Like _run_ssh, but returns the raw (rc, stdout, stderr) for callers that branch on rc.

    Every remote command passes through here, so this is where a timeout gains
    the orphan note. scp and the Globus CLI do not: killing the local scp ends
    the remote sftp-server with the session, and Globus has no remote at all.
    """
    rc, out, err = _run_raw(_ssh_cmd(host, remote_cmd), timeout=timeout, input_data=input_data)
    if rc == -1 and err.startswith("Timed out"):
        err += _TIMEOUT_ORPHAN_NOTE
    return rc, out, err


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


def _run_ssh_script_checked(host: str, script: str, timeout: int = DEFAULT_TIMEOUT) -> tuple[bool, str]:
    """Run a bash script on the remote host via stdin; (succeeded, formatted output)."""
    return _run_ssh_checked(host, BASH_STDIN, timeout=timeout, input_data=script)


def _large_transfer_notice(path: str) -> str:
    """Point at Globus/DTN for files scp should not be moving over a login node."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return ""
    if size < LARGE_TRANSFER_BYTES:
        return ""
    gb = size / 1_000_000_000
    return (
        f"\n[{gb:.1f} GB transferred over scp. Both NSF NCAR and CU Boulder ask for bulk "
        "data to move via Globus (collections: NCAR GLADE, NCAR Campaign Storage, "
        "CU Boulder Research Computing) or a data-transfer node, not a login node.]"
    )


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
# Host settings
# ---------------------------------------------------------------------------
# A few things about a host cannot be discovered from the host itself, or are
# too expensive to rediscover every session. They are recorded in one small
# file this server owns, keyed by the SSH alias the user connects with:
#
#   ~/.config/hpc-ssh-mcp/hosts.json      (HPC_SSH_MCP_STORE moves it)
#
#   {"hosts": {"derecho": {"center": "ncar", "role": "login",
#                          "account": "UABC0001"}}}
#
# Recognised keys, all optional:
#   hpc      false           this is not a shared HPC system: no login-node
#                            etiquette, no command policy
#   center   ncar | curc     picks PBS or Slurm without probing the host
#   role     login | data-access | compute   sets the policy tier
#   account  default -A / --account for run_on_compute
#   scratch  suggested job directory, quoted back when submit_job gets none
#   globus   collection UUID for this system, so tools can name the host alias
#   policy   strict | permissive | off   (see _policy_mode)
#
# With nothing recorded the server probes for the scheduler and assumes a login
# node, which is the safe default.
#
# ~/.ssh/config is deliberately not read. Earlier versions carried these keys
# in a comment inside its Host block, and went on reading that comment even
# after they stopped writing it. Reading it was still the wrong call: it kept a
# parser for someone else's file format in this tree, it gave one word two
# meanings, and because ssh patterns match by wildcard a `Host *` block
# answered "yes, this host is described" for every alias the user had never
# mentioned. The store is keyed by the exact alias instead. ~/.ssh/config
# remains what ssh itself reads to make the connection; this server just does
# not look inside it.

DEFAULT_STORE = "~/.config/hpc-ssh-mcp/hosts.json"
STORE_ENV_VAR = "HPC_SSH_MCP_STORE"
_STORE_NOTE = (
    "Host settings for hpc-ssh-mcp, written by record_host. Safe to edit or delete: "
    "a removed host simply falls back to safe defaults. Keys are the SSH aliases you "
    "connect with, matched exactly."
)

_SETTING_KEYS = ("hpc", "center", "role", "account", "scratch", "globus", "policy")


def _format_settings(pairs: dict) -> str:
    """key=value pairs in a stable, readable order, for messages to the user."""
    order = {key: i for i, key in enumerate(_SETTING_KEYS)}
    return " ".join(f"{k}={pairs[k]}" for k in sorted(pairs, key=lambda k: order.get(k, 99)))


def _store_path() -> str:
    return os.path.expanduser(os.environ.get(STORE_ENV_VAR) or DEFAULT_STORE)


def _read_store_file() -> tuple[dict, str | None]:
    """Return (entries, error). An absent file is not an error; a broken one is.

    Distinguishing the two matters on the write path: rewriting a file we
    could not read would silently discard whatever it held.
    """
    path = _store_path()
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return {}, None
    except (OSError, UnicodeDecodeError) as exc:
        return {}, f"Could not read {path}: {exc}"
    except json.JSONDecodeError as exc:
        return {}, f"Could not parse {path} as JSON: {exc}"
    if not isinstance(raw, dict) or not isinstance(raw.get("hosts", {}), dict):
        return {}, f"Could not use {path}: expected a JSON object with a 'hosts' object."

    entries: dict = {}
    for host, settings in raw.get("hosts", {}).items():
        if not isinstance(settings, dict) or not _VALID_HOST_RE.match(str(host)):
            continue
        # Values are written as scalars; anything else is not something we wrote.
        clean = {
            str(k).lower(): v for k, v in settings.items()
            if isinstance(v, (str, bool, int, float))
        }
        if clean:
            entries[str(host)] = clean
    return entries, None


def _load_store() -> dict:
    """Entries from the managed store. Any problem with it means 'no entries'."""
    return _read_store_file()[0]


def _write_store(entries: dict) -> str | None:
    """Replace the store atomically and privately. Returns an error, or None."""
    path = _store_path()
    document = {"_note": _STORE_NOTE, "hosts": dict(sorted(entries.items()))}
    try:
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, mode=0o700, exist_ok=True)
        # A unique temp name: FastMCP runs sync tools in a thread pool, so two
        # concurrent record_host calls shared one fixed "<path>.tmp".
        handle, temp = tempfile.mkstemp(dir=directory, prefix=".hosts-", suffix=".tmp")
        try:
            os.fchmod(handle, 0o600)
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                json.dump(document, fh, indent=2, sort_keys=False)
                fh.write("\n")
            os.replace(temp, path)
        except BaseException:
            if os.path.exists(temp):
                os.remove(temp)
            raise
    except OSError as exc:
        return f"Could not write {path}: {exc}"
    return None


CENTER_SCHEDULERS = {"ncar": "pbs", "curc": "slurm"}
# A data-access node or DTN is meant for moving data, so transfers are normal
# there while compute is still routed away.
_ROLE_ALIASES = {"data-access": "dtn", "datamover": "dtn", "transfer": "dtn"}
VALID_ROLES = ("login", "dtn", "compute")

def _host_settings(host: str) -> dict:
    """Settings recorded for a host, keyed by the exact SSH alias.

    No pattern matching: a host is described because the user described it, not
    because a wildcard happened to cover it. Anything wrong with the store
    means "nothing recorded", never an exception.

    The file is read on every call, deliberately. It is a few hundred bytes next
    to an SSH round trip, and every refusal tells the user to edit it and retry:
    a cache made that instruction false until the server was restarted.
    """
    return dict(_load_store().get(host, {}))


_FALSEY = {"false", "no", "0", "off", "n"}


def _is_hpc(host: str) -> bool:
    """False when the host is recorded as `hpc=false`: not a shared HPC system."""
    return str(_host_settings(host).get("hpc", "true")).lower() not in _FALSEY


def _host_role(host: str) -> str:
    """Role of a host: 'login', 'dtn' or 'compute'.

    Login is the safe default: it is what an HPC alias almost always points at,
    and it is the role with the strictest routing rules. A machine that is not
    an HPC system at all is `hpc=false`, not a role (see _is_hpc).
    """
    role = str(_host_settings(host).get("role", "login")).lower()
    role = _ROLE_ALIASES.get(role, role)
    return role if role in VALID_ROLES else "login"


# The policy escape belongs to the human, not to the model. A tier exists to
# stop the agent doing something the user did not intend, so a tool parameter
# that switches it off would defeat it. These come from the config file the
# user edits, or the environment the server was launched with.
POLICY_MODES = ("strict", "permissive", "off")
POLICY_ENV_VAR = "HPC_SSH_MCP_POLICY"


def _policy_mode(host: str) -> str:
    """'strict' (default), 'permissive' (block needs confirmation) or 'off'.

    Resolution order: the environment variable the server was launched with,
    then a `policy` recorded for the host, then 'off' for a host recorded as
    `hpc=false`, else strict.
    """
    for candidate in (
        os.environ.get(POLICY_ENV_VAR, ""),
        _host_settings(host).get("policy", ""),
    ):
        value = str(candidate).strip().lower()
        if value in POLICY_MODES:
            return value
        if value:
            return "strict"  # a typo must not silently weaken the guard
    # Login-node etiquette and the destructive-command guard exist because the
    # machine is shared. Off an HPC system they do not apply.
    return "strict" if _is_hpc(host) else "off"


_RELAX_INSTRUCTIONS = (
    "\n\nIf this is deliberate and the user accepts the risk, it is their call to make, "
    "not this server's and not yours. They can relax the guard for a host by editing "
    "the settings file themselves and giving it a policy:\n"
    '    {"hosts": {"<host>": {"policy": "permissive"}}}   # block tier becomes a confirmation\n'
    '    {"hosts": {"<host>": {"policy": "off"}}}          # no checks at all\n'
    f"or for one session by launching this server with {POLICY_ENV_VAR}=permissive "
    f"(or {POLICY_ENV_VAR}=off). Ask them to do that; do not do it for them."
)


# ---------------------------------------------------------------------------
# Scheduler-poll rate limiting
# ---------------------------------------------------------------------------
# Aalto and Purdue both call out agents that hammer squeue/qstat in a loop.
# Repeated identical read-only scheduler queries inside this window return the
# previous answer instead of opening another connection.
SCHEDULER_POLL_INTERVAL = 30
_POLL_CACHE: dict[tuple, tuple[float, str]] = {}


def _cached_poll(key: tuple, produce) -> str:
    """Return a recent identical scheduler answer, or produce and store a new one.

    `produce` returns (succeeded, text), and only a success is remembered.
    Caching a failure meant that after the user re-established the ControlMaster
    the next 30 s of retries replayed the stale error -- relabelled as rate
    limiting -- so the obvious reading was that the fix had not worked.
    """
    now = time.monotonic()
    for stale in [k for k, (ts, _) in _POLL_CACHE.items()
                  if now - ts >= SCHEDULER_POLL_INTERVAL]:
        del _POLL_CACHE[stale]  # an expired answer is dead weight, not a cache
    hit = _POLL_CACHE.get(key)
    if hit and now - hit[0] < SCHEDULER_POLL_INTERVAL:
        age = int(now - hit[0])
        return (
            f"{hit[1]}\n[cached {age}s ago; scheduler queries are rate-limited to one per "
            f"{SCHEDULER_POLL_INTERVAL}s per host. Wait before polling again.]"
        )
    succeeded, result = produce()
    if succeeded:
        _POLL_CACHE[key] = (now, result)
    return result


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
    # Only the mutating verbs: `rpm -qa` and `dnf list installed` are how you find
    # out what a login node has, and a non-root user cannot install anyway.
    (re.compile(r"^(?:apt|apt-get|aptitude)\s+(?:install|remove|purge|upgrade|dist-upgrade|autoremove)\b"
                r"|^(?:yum|dnf|zypper)\s+(?:install|remove|erase|update|upgrade|reinstall|downgrade|autoremove)\b"
                r"|^pacman\s+-[A-Za-z]*[SRU]"
                r"|^rpm\s+-[A-Za-z]*[iUeF]"), "block",
     "system package manager install/remove (use conda/spack in your own space)", None),
    (re.compile(r"^mkfs(?:\.\w+)?\s"), "block", "mkfs (filesystem creation)", None),
    # /dev/null and the standard streams are sinks, not devices that can be damaged.
    (re.compile(r"^dd\b[^|]*\bof=/dev/(?!null\b|stdout\b|stderr\b)"), "block",
     "dd writing to a device node", None),
    (re.compile(r"(?:>>?\s*|tee\s+(?:-a\s+)?)(?:\S*/)?authorized_keys(?:\s|$)"), "block",
     "write to authorized_keys (SSH key persistence)", None),
    (re.compile(r"^sed\s+-i\b[^;]*authorized_keys"), "block",
     "in-place edit of authorized_keys (SSH key persistence)", None),
    # --- confirm -----------------------------------------------------------
    (re.compile(r"^find\b.*(?:-delete\b|-exec\s+rm\b)"), "confirm", "find ... -delete / -exec rm", None),
    (re.compile(r"^ch(?:mod|own|grp)\b.*(?:\s-[a-zA-Z]*R\b|--recursive)"), "confirm",
     "recursive chmod/chown", None),
    (re.compile(r"^chmod\b.*(?:\s(?:777|666|0777|0666)\b|\s[ao][+=][rwx]*w[rwx]*\b)"), "confirm",
     "world-writable permissions (NSF NCAR: never chmod 777)", None),
    (re.compile(r"^tail\b.*\s-[a-zA-Z]*[fF]\b"), "route",
     "tail -f: a polling loop that holds a login node", _LOGIN_ROLES),
    (re.compile(r"^watch\b"), "route", "watch: a polling loop", _LOGIN_ROLES),
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

# Recursive traversal at or above one of these degrades the shared parallel
# filesystem for every user on the machine. NSF NCAR's agentic-AI guidance
# names them explicitly and says NEVER.
SHARED_ROOTS = frozenset({
    "/", "/glade", "/glade/u", "/glade/u/home", "/glade/work", "/glade/campaign",
    "/glade/derecho", "/glade/derecho/scratch", "/glade/scratch",
    "/scratch", "/scratch/alpine", "/projects", "/pl", "/pl/active",
    "/home", "/work", "/data",
})
_TRAVERSAL_RE = re.compile(
    r"^(?:lfs\s+find|find|du|ncdu|tree|ls|grep|egrep|fgrep|rg|ripgrep|locate)\b(?P<rest>.*)$", re.S,
)
# For these, the first non-flag token is a pattern, not a path.
_PATTERN_FIRST = ("grep", "egrep", "fgrep", "rg", "ripgrep")
# ls and grep only walk the tree when asked to recurse.
_NEEDS_RECURSIVE_FLAG = ("ls", "grep", "egrep", "fgrep")


def _tokens(segment: str) -> list[str]:
    """Split a segment the way the shell will, so a quoted argument stays one token.

    Splitting on whitespace tore a quoted *search pattern* apart and read its
    fragments as flags and paths: `grep -n 'rm -rf /' notes.md` supplied its own
    -r and its own "/". Unbalanced quotes are not our problem to resolve -- fall
    back rather than raise.
    """
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def _traversal_tier(segment: str) -> tuple[str, str] | None:
    """Block find/du/ls -R/grep -r/rg at or above a shared filesystem root."""
    m = _TRAVERSAL_RE.match(segment)
    if not m:
        return None
    tokens = _tokens(segment)
    if not tokens:
        return None
    name = "lfs find" if tokens[0] == "lfs" else tokens[0]
    args = tokens[2:] if name == "lfs find" else tokens[1:]

    if name in _NEEDS_RECURSIVE_FLAG:
        # grep recurses on -r or -R; ls only on -R, since its -r is *reverse sort*
        # and `ls -ltr <shared root>` is one of the commonest login-node commands.
        letters = "R" if name == "ls" else "rR"
        recursive = any(
            tok.startswith("-") and not tok.startswith("--") and re.search(f"[{letters}]", tok)
            for tok in args
        ) or "--recursive" in args
        if not recursive:
            return None

    paths, seen_pattern = [], False
    for tok in args:
        if tok.startswith("-"):
            # find's predicates (-name, -type, ...) mark the end of its paths.
            if name in ("find", "lfs find"):
                break
            continue
        if name in _PATTERN_FIRST and not seen_pattern:
            seen_pattern = True
            continue
        paths.append(tok)

    for path in paths:
        clean = path.strip("\"'")
        if not clean.startswith("/"):
            continue
        normalised = "/" + clean.strip("/") if clean.strip("/") else "/"
        if normalised in SHARED_ROOTS:
            return (
                "block",
                f"recursive traversal at or above the shared root {normalised} "
                f"({name}): this causes a filesystem metadata storm for every user. "
                "Point it at your own subdirectory instead.",
            )
    return None


_MAKE_RE = re.compile(r"^(?:make|gmake|ninja|cmake)\b(?P<rest>.*)$", re.S)


def _unbounded_parallelism_tier(segment: str) -> tuple[str, str] | None:
    """`make -j` with no limit spawns one job per core on a shared login node."""
    if not _MAKE_RE.match(segment):
        return None
    tokens = segment.split()
    for i, tok in enumerate(tokens):
        if tok == "-j":
            following = tokens[i + 1] if i + 1 < len(tokens) else ""
            if not following.isdigit():
                return ("confirm", "unbounded build parallelism (`make -j`); NSF NCAR asks for a "
                                   "small fixed -j, for example -j4")
    return None


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
    for tok in _tokens(m.group("rest")):
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
    # Login-node etiquette, not an absolute prohibition. Applying it on a compute
    # node too was tried and reverted: `block` has no override in strict mode, so
    # it made a traversal unreachable through this server from anywhere rather
    # than merely discouraged, and run_on_compute is the sanctioned route for
    # heavy work. Where to run one is the user's call to make.
    (_traversal_tier, _LOGIN_ROLES),
    (_unbounded_parallelism_tier, _LOGIN_ROLES),
    (_interpreter_tier, _LOGIN_ROLES),
    (_compiler_tier, _LOGIN_ROLES),
    (_tar_tier, _COMPUTE_ROLES),
)


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
    mode: str = "strict",
) -> str | None:
    """Return a refusal message if policy stops this command, else None."""
    if mode == "off":
        return None
    tier, rule = _classify_command(command, role)
    if tier == "block":
        if mode != "permissive":
            return f"Blocked by policy: {rule}.{_RELAX_INSTRUCTIONS}"
        if not confirm_destructive:
            return (
                f"Refused pending confirmation: {rule}.\n"
                "This host's policy is 'permissive', so the user can authorise it. Ask them "
                "to confirm, then call again with confirm_destructive=true."
            )
        return None
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
        mode=_policy_mode(host),
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
        center = str(_host_settings(host).get("center", "")).lower()
        if center in CENTER_SCHEDULERS:
            return CENTER_SCHEDULERS[center]
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
        remote_filename: Script name on the remote host. Auto-generated (unique) if
            empty; an explicit name overwrites any existing file of that name.
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
    result = _run_ssh_script(host, enter + submit)
    scratch = str(_host_settings(host).get("scratch", ""))
    if not remote_dir and scratch:
        result += (
            f"\n[submitted from the SSH login directory. For run data, pass "
            f"remote_dir={scratch} so output lands on scratch instead of $HOME.]"
        )
    return result


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
    return _cached_poll(("job", host, script), lambda: _run_ssh_script_checked(host, script))


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
    return _cached_poll(("queue", host, script), lambda: _run_ssh_script_checked(host, script))


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
    refusal = _policy_refusal(command, "compute", confirm_destructive, True,
                              mode=_policy_mode(host))
    if refusal:
        return refusal
    _validate_directive("account", account)
    _validate_directive("queue", queue)
    _validate_directive("resources", resources)
    _validate_directive("walltime", walltime, _VALID_WALLTIME_RE)
    sched = _resolve_scheduler(host, scheduler)
    account = account or str(_host_settings(host).get("account", ""))
    _validate_directive("account", account)
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
    result = _run_ssh_script(host, " ".join(parts), timeout=timeout)
    if _TIMEOUT_ORPHAN_NOTE in result:
        result += _COMPUTE_TIMEOUT_NOTE
    return result


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
    cap = int(max_bytes)
    # The remote reports the byte count on stderr (`wc -c` on a regular file is a
    # stat, not a read), because the count cannot be recovered here: _run_raw
    # decodes with errors="replace", and every undecodable byte in a Latin-1 log
    # becomes a 3-byte U+FFFD, so re-encoding the text overstated the length and
    # called a file read in full "truncated" -- then cut it.
    if max_lines > 0:
        # A pipeline reports the status of its last command, and `head -c`
        # succeeds on empty input, so a failed `head -n` used to look like an
        # empty file. pipefail is not the answer either: `head -c` closes the
        # pipe the moment it has its bytes and `head -n` then dies of SIGPIPE,
        # which turns a correctly truncated read into exit 141. So open the file
        # once on its own first -- that is what reports a missing path, a
        # directory or a permission problem -- and let the pipeline follow.
        lines = int(max_lines)
        script = (
            f"head -c 1 -- {path} >/dev/null || exit\n"
            f"head -n {lines} -- {path} | head -c {cap}\n"
            f"head -n {lines} -- {path} | wc -c >&2"
        )
    else:
        script = f"head -c {cap} -- {path} && wc -c < {path} >&2"

    rc, out, err = _run_ssh_script_raw(host, script)
    if rc != 0:
        result = _format_result(rc, out, err)
        return result + (_diagnose_ssh_failure(host, err) if rc == SSH_OWN_FAILURE_RC else "")
    if "\x00" in out:
        return (
            f"{remote_path} looks like a binary file (NUL bytes in the first "
            f"{len(out.encode('utf-8', 'replace'))} bytes). "
            "Use scp_download_file to fetch it instead of reading it into context."
        )
    if not out:
        return "(no output)"
    # The body is the file as the remote sent it. stderr is the byte count and
    # whatever the login shell said on the way in; neither is file content.
    size = _trailing_int(err)
    if size is None:  # the count did not arrive; fall back to measuring the text
        size = len(out.encode("utf-8", "replace"))
    if size > cap:
        # head -c cuts on a byte boundary; a codepoint split there decoded to one U+FFFD.
        body = out[:-1] if out.endswith("�") else out
        out = body + (
            f"\n[truncated at {cap} of {size} bytes. Use max_lines, a larger max_bytes, "
            "tail_remote_file for the end, or scp_download_file for all of it]"
        )
    return _truncate(out, MAX_OUTPUT_CHARS)


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
    elif rc == 0:
        result += _large_transfer_notice(local_abs)
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
    local_abs = _local_path(local_path)
    if not os.path.exists(local_abs):
        return f"Local file not found: {local_abs}"
    notice = _large_transfer_notice(local_abs)
    _, result = _run_scp(
        host, [local_abs, _scp_remote_spec(host, remote_path)], timeout=timeout,
    )
    return result + notice


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
# Globus transfer tools
# ---------------------------------------------------------------------------
# Both NSF NCAR and CU Boulder point at Globus for bulk data movement. The
# Globus CLI talks to the Globus API rather than to a cluster, so it runs
# locally: no login node is involved and no SSH session is needed.
#
# Auth is the CLI's job, out of band, exactly like `ssh -fN <host>` for Duo:
#   uv tool install globus-cli && globus login
# GCS v5 mapped collections (NCAR GLADE, NCAR Campaign Storage, CU Boulder
# Research Computing) additionally need a one-time per-collection data_access
# consent. The CLI signals both with exit code 4, which is translated below
# into the exact command to run.

GLOBUS_EXIT_AUTH = 4
DEFAULT_GLOBUS_TIMEOUT = 120
SYNC_LEVELS = ("exists", "size", "mtime", "checksum")

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
_VALID_LABEL_RE = re.compile(r"^[A-Za-z0-9 ._,:+-]{1,128}$")
_CONSENT_SCOPE = (
    "urn:globus:auth:scope:transfer.api.globus.org:all"
    "[*https://auth.globus.org/scopes/{uuid}/data_access]"
)
_LOGIN_STEPS = (
    "Globus auth is granted in your own terminal, not by this server:\n"
    "    uv tool install globus-cli    # if it is not installed\n"
    "    globus login"
)


def _globus_cli_available() -> bool:
    return shutil.which("globus") is not None


def _globus_env() -> dict:
    """Environment for the CLI: never prompt, we have no terminal."""
    env = dict(os.environ)
    env["GLOBUS_CLI_INTERACTIVE"] = "0"
    return env


def _globus_collections() -> dict:
    """SSH alias -> collection UUID, from the settings store."""
    return {
        host: str(settings["globus"])
        for host, settings in _load_store().items() if settings.get("globus")
    }


def _resolve_collection(value: str) -> str:
    """Turn an SSH host alias or a bare UUID into a collection UUID."""
    known = _globus_collections()
    if value in known:
        value = known[value]
    if not _UUID_RE.match(value or ""):
        names = ", ".join(sorted(known)) or "(none recorded)"
        raise ValueError(
            f"Unknown Globus collection: {value!r}. Expected a collection UUID, or an SSH "
            f"host alias recorded with a `globus` UUID via record_host: {names}. "
            "Use globus_find_collection to look a collection up by name."
        )
    return value.lower()


def _validate_task_id(task_id: str) -> str:
    if not _UUID_RE.match(task_id or ""):
        raise ValueError(f"Invalid Globus task ID: {task_id!r}. Expected a UUID.")
    return task_id.lower()


def _globus_error(stderr: str) -> dict:
    """Parse the JSON error body the CLI prints on stderr, or {}."""
    text = (stderr or "").strip()
    start = text.find("{")
    if start < 0:
        return {}
    try:
        parsed = json.loads(text[start:])
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _consent_hint(stderr: str) -> str:
    """A ConsentRequired error carries the exact scopes to consent to.

    Globus reports this with exit code 1, not the auth exit code, so it must be
    checked on every failure. The scopes come from the error body verbatim: a
    scope rebuilt from the collection UUID can be wrong when the collection
    depends on another one.
    """
    body = _globus_error(stderr)
    is_consent = (
        str(body.get("code", "")).lower() == "consentrequired"
        or "consentrequired" in (stderr or "").lower().replace(" ", "")
    )
    if not is_consent:
        return ""
    scopes = body.get("required_scopes") or body.get(
        "authorization_parameters", {}).get("required_scopes") or []
    if not scopes:
        m = re.search(r"([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})", stderr or "")
        if not m:
            return ""
        scopes = [_CONSENT_SCOPE.format(uuid=m.group(1).lower())]
    commands = "\n".join(f"    globus session consent '{sc}'" for sc in scopes)
    message = body.get("message") or "Missing required data_access consent"
    return (
        f"\n\nHint: {message}. This collection needs a one-time data_access consent, "
        "granted from your own terminal:\n"
        f"{commands}\nThen retry."
    )


def _globus_auth_hint(stderr: str) -> str:
    """Translate a CLI auth failure into the command that fixes it."""
    return _consent_hint(stderr) or f"\n\nHint: {_LOGIN_STEPS}\nThen retry."


def _run_globus(args: list[str], timeout: int = DEFAULT_GLOBUS_TIMEOUT) -> tuple[int, str, str]:
    return _run_raw(["globus", *args], timeout=timeout, env=_globus_env())


def _globus_json(args: list[str], timeout: int = DEFAULT_GLOBUS_TIMEOUT):
    """Run a CLI command with JSON output. Returns (data, error_text).

    Exactly one of the two is None: parsed JSON on success, a ready-to-return
    message (with an auth hint where that is the cause) on failure.
    """
    rc, out, err = _run_globus([*args, "--format", "json"], timeout=timeout)
    if rc != 0:
        body = _globus_error(err)
        # The raw JSON body is noise for a model; lead with what went wrong.
        summary = f"{body.get('code')}: {body.get('message')}" if body.get("code") else None
        msg = summary or _format_result(rc, out, err)
        # ConsentRequired arrives on exit 1, so check it before the exit code.
        msg += _consent_hint(err) or (_globus_auth_hint(err) if rc == GLOBUS_EXIT_AUTH else "")
        return None, msg
    try:
        return json.loads(out or "null"), None
    except json.JSONDecodeError:
        return None, f"Could not parse Globus CLI output as JSON:\n{_truncate(out, 4000)}"


def _globus_unavailable() -> str:
    return (
        "The Globus CLI is not installed or not on PATH, so this tool cannot run.\n"
        f"{_LOGIN_STEPS}\n"
        "The CLI runs locally and never touches a login node."
    )


def _human_bytes(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return str(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1000
    return f"{n:.1f} TB"


@mcp.tool(annotations=_READ_ONLY)
def globus_status() -> str:
    """Show which Globus identity this machine is logged in as.

    Call this first when a Globus transfer is wanted: if it reports a missing
    login, every other Globus tool will fail the same way, and only the user
    can fix it from their own terminal.
    """
    if not _globus_cli_available():
        return _globus_unavailable()
    data, err = _globus_json(["whoami"])
    if err:
        return err
    if not isinstance(data, dict):
        return str(data)
    who = data.get("username") or data.get("name") or "(unknown)"
    return f"Logged in to Globus as {who}.\n" + "\n".join(
        f"{k}: {v}" for k, v in data.items() if k not in ("username",) and v
    )


@mcp.tool(annotations=_READ_ONLY)
def globus_find_collection(query: str) -> str:
    """Search Globus for a collection by name and return its UUID.

    Useful names: 'NCAR GLADE', 'NCAR Campaign Storage', 'NCAR Data Sharing
    Service', 'CU Boulder Research Computing'. Record a UUID you use often with
    record_host(host, globus=<uuid>), and later calls can name the SSH alias
    instead.

    Args:
        query: Text to search collection names for.
    """
    if not _globus_cli_available():
        return _globus_unavailable()
    data, err = _globus_json(["endpoint", "search", query])
    if err:
        return err
    rows = data if isinstance(data, list) else data.get("DATA", []) if isinstance(data, dict) else []
    if not rows:
        return f"No collection matched {query!r}."
    lines = [f"{r.get('id')}  {r.get('display_name') or r.get('name')}  "
             f"(owner: {r.get('owner_string', '?')})" for r in rows[:25]]
    return "\n".join(lines)


@mcp.tool(annotations=_READ_ONLY)
def globus_ls(collection: str, path: str = "/") -> str:
    """List a directory on a Globus collection without touching a login node.

    Args:
        collection: Collection UUID, or an SSH host alias recorded with a globus UUID.
        path: Absolute path on that collection (default '/').
    """
    if not _globus_cli_available():
        return _globus_unavailable()
    uuid_ = _resolve_collection(collection)
    data, err = _globus_json(["ls", f"{uuid_}:{path}"])
    if err:
        return err
    rows = data.get("DATA", []) if isinstance(data, dict) else data or []
    if not rows:
        return f"(empty: {path})"
    lines = []
    for r in rows[:500]:
        kind = r.get("type", "")
        size = "" if kind == "dir" else f"  {_human_bytes(r.get('size', 0))}"
        lines.append(f"{'d' if kind == 'dir' else '-'} {r.get('name')}{size}")
    return "\n".join(lines)


@mcp.tool(annotations=_ADDITIVE)
def globus_transfer(
    source: str,
    source_path: str,
    dest: str,
    dest_path: str,
    recursive: bool = False,
    sync_level: str = "mtime",
    label: str = "",
    dry_run: bool = False,
    delete_destination_extra: bool = False,
    confirm_destructive: bool = False,
) -> str:
    """Submit a Globus transfer between two collections and return its task ID.

    This is the right tool for bulk data: both NSF NCAR and CU Boulder ask for
    large transfers to go through Globus rather than scp over a login node.
    The transfer runs on Globus's servers and continues after this call
    returns; poll it with globus_task_status.

    Args:
        source: Source collection UUID, or a recorded SSH host alias.
        source_path: Absolute path on the source collection.
        dest: Destination collection UUID, or a recorded SSH host alias.
        dest_path: Absolute path on the destination collection.
        recursive: Required when transferring a directory.
        sync_level: When to re-send a file that already exists at the
            destination: exists, size, mtime (default) or checksum.
        label: Task label shown in the Globus web app. Auto-generated if empty.
        dry_run: Validate and show what would transfer without submitting.
        delete_destination_extra: Delete files at the destination that are not
            in the source (mirror). Destructive; needs confirm_destructive.
        confirm_destructive: Set only after the user has confirmed deletion.
    """
    if not _globus_cli_available():
        return _globus_unavailable()
    if sync_level not in SYNC_LEVELS:
        raise ValueError(f"Invalid sync_level: {sync_level!r}. Expected one of {', '.join(SYNC_LEVELS)}.")
    if label and not _VALID_LABEL_RE.match(label):
        raise ValueError(
            f"Invalid label: {label!r}. Use letters, digits, spaces and . _ , : + - (max 128)."
        )
    src_uuid, dst_uuid = _resolve_collection(source), _resolve_collection(dest)
    if src_uuid == dst_uuid and source_path == dest_path:
        return "Source and destination are the same collection and path; nothing to transfer."
    if delete_destination_extra and not confirm_destructive:
        return (
            "Refused pending confirmation: --delete-destination-extra permanently removes "
            f"files under {dest_path} on the destination that are not in the source.\n"
            "Ask the user to confirm, then call again with confirm_destructive=true."
        )

    args = ["transfer", f"{src_uuid}:{source_path}", f"{dst_uuid}:{dest_path}",
            "--sync-level", sync_level,
            "--label", label or f"hpc-ssh-mcp {time.strftime('%Y-%m-%d %H:%M')}"]
    if recursive:
        args.append("--recursive")
    if dry_run:
        args.append("--dry-run")
    if delete_destination_extra:
        args.append("--delete-destination-extra")

    data, err = _globus_json(args)
    if err:
        return err
    if not isinstance(data, dict):
        return str(data)
    task_id = data.get("task_id")
    if not task_id:
        return json.dumps(data, indent=2)[:MAX_OUTPUT_CHARS]
    return (
        f"{data.get('message', 'Transfer submitted')}\n"
        f"task_id: {task_id}\n"
        f"Poll it with globus_task_status('{task_id}'). The transfer continues on "
        "Globus's servers whether or not this session stays open."
    )


@mcp.tool(annotations=_READ_ONLY)
def globus_task_status(task_id: str) -> str:
    """Show the status of a Globus transfer task, with the last error if it failed.

    Args:
        task_id: Task UUID returned by globus_transfer.
    """
    if not _globus_cli_available():
        return _globus_unavailable()
    tid = _validate_task_id(task_id)
    data, err = _globus_json(["task", "show", tid])
    if err:
        return err
    if not isinstance(data, dict):
        return str(data)
    status = data.get("status", "UNKNOWN")
    lines = [
        f"status: {status}",
        f"label: {data.get('label', '')}",
        f"files transferred: {data.get('files_transferred', 0)}",
        f"bytes transferred: {_human_bytes(data.get('bytes_transferred', 0))}",
    ]
    if status in ("FAILED", "INACTIVE"):
        events, event_err = _globus_json(["task", "event-list", tid, "--limit", "5"])
        if event_err:
            lines.append(f"(could not read task events: {event_err})")
        else:
            rows = events if isinstance(events, list) else (events or {}).get("DATA", [])
            for ev in rows[:5]:
                lines.append(f"  {ev.get('code')}: {ev.get('details') or ev.get('description')}")
        if status == "INACTIVE":
            lines.append(f"\n{_LOGIN_STEPS}\n(an INACTIVE task usually means credentials expired)")
    return "\n".join(lines)


@mcp.tool(annotations=_OVERWRITES)
def globus_task_cancel(task_id: str) -> str:
    """Cancel a running Globus transfer task.

    Files already transferred stay at the destination; the task stops moving
    new ones.

    Args:
        task_id: Task UUID returned by globus_transfer.
    """
    if not _globus_cli_available():
        return _globus_unavailable()
    tid = _validate_task_id(task_id)
    data, err = _globus_json(["task", "cancel", tid])
    if err:
        return err
    if isinstance(data, dict):
        return f"{data.get('code', 'Cancelled')}: {data.get('message', 'task cancelled')}"
    return str(data)


# ---------------------------------------------------------------------------
# First contact with a host
# ---------------------------------------------------------------------------
# A host with nothing recorded still works: the server probes for a scheduler and
# assumes a login node, which is the safe reading. But the first time a tool
# touches such a host, say so once, so the agent can offer to describe it
# properly instead of silently guessing forever.

_ONBOARDING_SEEN: set[str] = set()

_PROBE_SCRIPT = r"""
printf 'hostname=%s\n' "$(hostname -f 2>/dev/null || hostname)"
printf 'user=%s\n' "$USER"
s=""
command -v qsub   >/dev/null 2>&1 && s="qsub"
command -v sbatch >/dev/null 2>&1 && s="${s:+$s }sbatch"
printf 'scheduler=%s\n' "$s"
printf 'account=%s\n' "${PBS_ACCOUNT:-${SLURM_ACCOUNT:-${SBATCH_ACCOUNT:-}}}"
fs=""
for d in /glade /glade/derecho/scratch /glade/work /glade/campaign \
         /scratch/alpine /pl/active /projects; do
    [ -d "$d" ] && fs="${fs:+$fs }$d"
done
printf 'filesystems=%s\n' "$fs"
g=no
command -v globus >/dev/null 2>&1 && g=yes
# A non-login shell often misses the usual per-user bin directories.
for p in "$HOME/.local/bin/globus" "$HOME/bin/globus"; do
    [ -x "$p" ] && g=yes
done
printf 'globus=%s\n' "$g"
"""


def _onboarding_notice(host: str) -> str:
    """Once per host per session, flag that nothing is recorded for this host."""
    if host in _ONBOARDING_SEEN or _host_settings(host):
        _ONBOARDING_SEEN.add(host)
        return ""
    _ONBOARDING_SEEN.add(host)
    return (
        f"\n\n[first use of {host!r}: this server has nothing recorded for it, so it is "
        "assuming an HPC login node and probing for the scheduler each session. "
        f"Run probe_host({host!r}) to see what it actually is, ask the user to confirm, then "
        "call record_host to record it. Do this once; it is not urgent and everything works "
        "meanwhile.]"
    )


def _infer_from_probe(fields: dict) -> dict:
    """Turn raw probe output into suggested settings."""
    scheduler = fields.get("scheduler", "").split()
    filesystems = fields.get("filesystems", "").split()
    hostname = fields.get("hostname", "")
    suggestion: dict = {}

    if "qsub" in scheduler and "sbatch" not in scheduler:
        suggestion["scheduler_name"] = "PBS Pro"
    elif "sbatch" in scheduler and "qsub" not in scheduler:
        suggestion["scheduler_name"] = "Slurm"
    elif scheduler:
        suggestion["scheduler_name"] = "both PBS and Slurm (ambiguous)"

    if any(p.startswith("/glade") for p in filesystems):
        suggestion["center"] = "ncar"
    elif any(p.startswith(("/scratch/alpine", "/pl")) for p in filesystems):
        suggestion["center"] = "curc"

    lowered = hostname.lower()
    if "data-access" in lowered or "dtn" in lowered or "datamover" in lowered:
        suggestion["role"] = "data-access"
    elif scheduler:
        suggestion["role"] = "login"

    if fields.get("account"):
        suggestion["account"] = fields["account"]
    # A concrete path, not "$USER": the value is recorded verbatim and submit_job
    # shell-quotes it, so a variable in it would name a literal directory.
    user = fields.get("user", "")
    if suggestion.get("center") == "ncar" and user:
        suggestion["scratch"] = f"/glade/derecho/scratch/{user}"
    elif suggestion.get("center") == "curc" and user:
        suggestion["scratch"] = f"/scratch/alpine/{user}"
    suggestion["is_hpc"] = bool(scheduler or filesystems)
    return suggestion


@mcp.tool(annotations=_READ_ONLY)
def probe_host(host: str) -> str:
    """Find out what a host is, so its settings can be recorded.

    Detects the scheduler, the centre's filesystems, a default account and
    whether the Globus CLI is present, then proposes settings and lists what
    only the user can answer. Read-only: it writes nothing.

    Call this the first time you touch an unfamiliar host, show the user what
    came back, ask the questions it lists, and then call record_host with
    their answers. Never guess on their behalf.

    Args:
        host: SSH config alias or hostname.
    """
    _validate_host(host)
    existing = _host_settings(host)
    rc, out, err = _run_ssh_script_raw(host, _PROBE_SCRIPT)
    if rc != 0:
        msg = f"Could not probe {host!r}:\n{_format_result(rc, out, err)}"
        if rc == SSH_OWN_FAILURE_RC:
            msg += _diagnose_ssh_failure(host, err)
        return msg

    fields = {}
    for line in out.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip()
    guess = _infer_from_probe(fields)

    lines = []
    if existing:
        lines.append(
            f"{host!r} already has settings recorded: "
            + " ".join(f"{k}={v}" for k, v in sorted(existing.items()))
            + ". Recording again updates the keys you pass and leaves the rest.\n"
        )
    lines += [
        "Detected:",
        f"  hostname     {fields.get('hostname') or '(unknown)'}",
        f"  scheduler    {guess.get('scheduler_name') or 'none found'}",
        f"  filesystems  {fields.get('filesystems') or '(none of the known HPC mounts)'}",
        f"  account      {fields.get('account') or '(none in the environment)'}",
        f"  globus CLI   {fields.get('globus', 'unknown')}",
        "",
    ]

    if not guess["is_hpc"]:
        lines += [
            "There is no scheduler and none of the known HPC filesystems, so this does not "
            "look like a shared HPC system.",
            "",
            "Ask the user: is this a shared HPC system, or their own machine?",
            "  If it is not HPC:  record_host(host, is_hpc=False)",
            "     records hpc=false, and login-node etiquette and the command policy",
            "     stop applying there.",
            "  If it is HPC:      ask which centre and role, then record_host accordingly.",
        ]
        return "\n".join(lines)

    proposed = {k: v for k, v in guess.items() if k in ("center", "role", "account", "scratch")}
    lines += [
        "Suggested settings:",
        "    " + " ".join(f"{k}={v}" for k, v in sorted(proposed.items())),
        "",
        "Ask the user to confirm, and to answer what cannot be detected:",
        f"  1. Is {host!r} a shared HPC system? (if not, record_host(host, is_hpc=False))",
        "  2. Which project code should jobs charge? "
        + (f"Detected {fields['account']}, confirm it." if fields.get("account")
           else "Nothing is set in the environment, so they must supply it."),
        "  3. Policy level: strict (default, block tier refused), permissive (block tier "
        "becomes a confirmation), or off. Do not choose for them.",
        "",
        "Then record it, for example:",
        "    record_host(host=" + repr(host) + ", "
        + ", ".join(f"{k}={v!r}" for k, v in sorted(proposed.items())) + ")",
    ]
    if fields.get("globus") == "yes":
        lines += [
            "",
            "The Globus CLI is installed on that host. If this system has a Globus collection, "
            "look up its UUID with globus_find_collection and pass globus=<uuid> too, so "
            "transfers can name the SSH alias.",
        ]
    return "\n".join(lines)


def _validate_setting(key: str, value: str) -> None:
    """Values live in a single-line, space-separated comment, so keep them simple."""
    if re.search(r"\s", value):
        raise ValueError(f"Invalid {key}: {value!r} must not contain whitespace or newlines.")
    if "#" in value:
        raise ValueError(f"Invalid {key}: {value!r} must not contain '#'.")
    if key == "center" and value not in CENTER_SCHEDULERS:
        raise ValueError(f"Invalid center: {value!r}. Expected one of {', '.join(CENTER_SCHEDULERS)}.")
    if key == "role":
        canonical = _ROLE_ALIASES.get(value, value)
        if canonical not in VALID_ROLES:
            raise ValueError(
                f"Invalid role: {value!r}. Expected login, data-access or compute. "
                "A machine that is not an HPC system is is_hpc=False, not a role."
            )
    if key == "policy" and value not in POLICY_MODES:
        raise ValueError(f"Invalid policy: {value!r}. Expected one of {', '.join(POLICY_MODES)}.")
    if key == "account" and not _VALID_DIRECTIVE_RE.match(value):
        raise ValueError(f"Invalid account: {value!r}.")
    if key == "globus" and not _UUID_RE.match(value):
        raise ValueError(f"Invalid globus collection UUID: {value!r}.")


@mcp.tool(annotations=_ADDITIVE)
def record_host(
    host: str,
    is_hpc: bool | None = None,
    center: str = "",
    role: str = "",
    account: str = "",
    scratch: str = "",
    globus: str = "",
    policy: str = "",
) -> str:
    """Record what a host is, so later sessions do not have to guess.

    Only call this after probe_host and after the user has confirmed the
    values. Written to this server's own settings file, JSON at
    ~/.config/hpc-ssh-mcp/hosts.json by default (HPC_SSH_MCP_STORE moves it).
    ~/.ssh/config is neither written nor read: it controls access to every host
    the user has, and is not a file a tool should touch. Calling this again
    updates the keys you pass and leaves the rest alone; removing a setting is
    a hand-edit of the file.

    Args:
        host: SSH alias, exactly as the user connects with it.
        is_hpc: False for a machine that is not a shared HPC system. Login-node
            etiquette and the command policy stop applying there, and the
            HPC-only fields are ignored. True says it is an HPC system after all,
            undoing an earlier False. Omitted leaves whatever is recorded alone.
        center: 'ncar' or 'curc'. Selects PBS or Slurm without probing.
        role: 'login', 'data-access' or 'compute'.
        account: Project code to charge jobs to.
        scratch: Suggested directory for job output.
        globus: Globus collection UUID for this system.
        policy: 'strict', 'permissive' or 'off'. Ask the user; never assume.
    """
    if any(ch in (host or "") for ch in "*?"):
        return (
            f"{host!r} is a wildcard pattern, not a specific host. Settings are keyed by "
            "the exact alias, so record each host you actually connect to."
        )
    _validate_host(host)

    values = {"center": center, "role": role, "account": account,
              "scratch": scratch, "globus": globus, "policy": policy}
    for key, value in values.items():
        if value:
            _validate_setting(key, str(value))

    if is_hpc is False:
        # Nothing about schedulers, accounts or filesystems applies off an HPC system.
        pairs = {"hpc": False}
        if policy:
            pairs["policy"] = policy
    else:
        pairs = {k: v for k, v in values.items() if v}
        if role:
            pairs["role"] = _ROLE_ALIASES.get(role, role)

    entries, read_error = _read_store_file()
    if read_error:
        return (
            f"{read_error}\n"
            "Refusing to rewrite it, because that would discard whatever it holds. "
            "Ask the user to fix or delete the file, then try again."
        )
    # Merge, rather than replace the host's entry. Replacing was the behaviour
    # in every storage format this server has had, but a partial update -- the
    # natural call once a host is already described -- must not drop what it
    # does not mention. `is_hpc` is a tri-state for the same reason: True cannot
    # be the default, or every partial update would assert "this is an HPC
    # system" and revert a recorded hpc=false. An explicit True removes that key
    # (HPC is the default reading); None leaves it alone. Unsetting anything
    # else stays a hand-edit of the file, which _STORE_NOTE invites.
    existing = entries.get(host, {})
    merged = dict(existing)
    merged.update(pairs)
    if is_hpc is True:
        merged.pop("hpc", None)
    if not pairs and merged == existing:
        return (
            f"Nothing to write for {host!r}. Pass at least one of is_hpc, center, role, "
            "account, scratch, globus or policy."
        )
    entries = dict(entries)
    entries[host] = merged
    error = _write_store(entries)
    if error:
        return error

    _ONBOARDING_SEEN.add(host)
    _SCHEDULER_CACHE.pop(host, None)

    return (
        f"Recorded {host!r} in {_store_path()}:\n"
        f"    {host}: {_format_settings(merged)}\n"
        "These apply whenever a tool is called with this exact alias."
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    mcp.run()


if __name__ == "__main__":
    main()
