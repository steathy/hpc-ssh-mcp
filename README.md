# hpc-ssh-mcp

[![GitHub](https://img.shields.io/github/v/tag/steathy/hpc-ssh-mcp)](https://github.com/steathy/hpc-ssh-mcp)

MCP server for remote execution on SSH-enabled servers and supercomputers: NCAR Derecho and Casper (PBS Pro), CU Boulder Alpine (Slurm), or any host in your `~/.ssh/config`.

Uses native `ssh`/`scp` binaries via `subprocess` to respect `~/.ssh/config` and `ControlMaster` multiplex sockets — no Duo MFA re-prompts.

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- OpenSSH client (`ssh`, `scp`). The scp protocol mode (SFTP on OpenSSH 9.0+, legacy below) is detected automatically.
- An active SSH `ControlMaster` socket for your target host (configured in `~/.ssh/config`). Establish it from a terminal where you can answer Duo/MFA: `ssh -fN <alias>`.

## Install

```bash
# Via uv (recommended)
uv pip install git+https://github.com/steathy/hpc-ssh-mcp.git

# Or clone and install locally
git clone git@github.com:steathy/hpc-ssh-mcp.git
cd hpc-ssh-mcp
uv pip install -e .
```

## Run

```bash
uv run ssh_hpc_server.py
```

## Claude Code integration

Add to your MCP config (e.g. `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "ssh-hpc": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/steathy/hpc-ssh-mcp.git", "hpc-ssh-mcp"]
    }
  }
}
```

## Tools

| Tool | Description |
|---|---|
| `execute_remote_bash` | Run a bash command or multi-line script on a remote host (delivered on stdin to `bash -s`) |
| `run_on_compute` | Run one command on a compute node and wait: NCAR `qcmd` on PBS, `srun` on Slurm |
| `submit_job` | Write a batch script and submit it: `qsub` on PBS, `sbatch` on Slurm |
| `check_job` | Job status including finished jobs: `qstat -x` on PBS, `squeue` + `sacct` on Slurm |
| `list_queue` | Jobs in the queue for a user: `qstat -w -u` on PBS, `squeue -u` on Slurm |
| `cancel_job` | Cancel a job: `qdel` on PBS, `scancel` on Slurm |
| `read_remote_file` | Read a remote text file into context (capped at 200 KB, binary refused) |
| `tail_remote_file` | Read last N lines of a remote file |
| `scp_download_file` | Download a file via `scp` |
| `scp_upload_file` | Upload a file via `scp` |
| `check_ssh_connection` | Verify the ControlMaster socket is alive |

Read-only tools carry the MCP `readOnlyHint`/`idempotentHint` annotations; `cancel_job`, the scp tools, `execute_remote_bash` and `run_on_compute` carry `destructiveHint`, so a client can auto-approve reads and confirm the rest.

### Scheduler support

The four job tools take `scheduler="auto"|"pbs"|"slurm"`. With `auto` the server probes the host once for `qsub`/`sbatch` and caches the answer. Job IDs are validated per scheduler (`2426690.desched1`, `123[].desched1` on PBS; `12345`, `12345_0`, `12345.0` on Slurm).

`submit_job` accepts `remote_dir` so scripts are written to, and submitted from, a scratch or work directory (for example `/glade/derecho/scratch/<user>/run1` or `/scratch/alpine/<user>/run1`) instead of `$HOME`.

### Command policy

`execute_remote_bash` and `run_on_compute` sort every command into a tier before sending it, because NCAR and CU Boulder both terminate heavy login-node processes without notice and CURC states login nodes are "not intended for computational tasks of any kind".

| Tier | Examples | What happens |
|---|---|---|
| **block** | `sudo`, `su`, `apt`/`yum`/`dnf`, `rm -rf` on `/`, `~`, `$HOME`, `/glade`, `/scratch`, `/pl`, `/projects`, fork bombs, `mkfs`, `dd of=/dev/…`, writes to `authorized_keys` | Refused. No override. |
| **confirm** | `rm -r` elsewhere, `find -delete`, recursive `chmod`/`chown`, `git push --force`, `git reset --hard`, `git clean -fd`, `scancel -u`, `qdel $(qselect …)`, `truncate`, `shred`, `crontab`, `ssh-keygen`, redirects over `.nc`/`.h5` files | Refused unless called with `confirm_destructive=true`. Ask the user first. |
| **route** | interpreters running a script, compilers and `make`, MPI launchers, NCO/CDO tools, `conda`/`pip install`, `tar`/`zip`, `rsync`, `jupyter`, `nohup` | On a login or data-access node, refused with a pointer to `run_on_compute`; `allow_on_login_node=true` overrides for genuinely small cases. |
| **free** | `ls`, `cat`, `grep`, `qstat`, `squeue`, `module avail`, `python --version`, `git status` | Runs. |

Scheduler queries (`list_queue`, `check_job`) are rate-limited to one identical query per host every 30 seconds; a repeat inside that window returns the cached answer. Transfers over 2 GB get a note pointing at Globus or a data-transfer node.

Paths: absolute paths are safest. `~/x` is expanded to the remote home directory in every path argument.

### Host profiles

An optional TOML file tells the server what each host is, so it skips the scheduler probe, defaults the account, and applies the right policy role. Copy [`hosts.example.toml`](hosts.example.toml) to `~/.config/hpc-ssh-mcp/hosts.toml`, or point `$HPC_SSH_MCP_CONFIG` at your own path.

```toml
[derecho]
center  = "ncar"                          # ncar -> PBS, curc -> Slurm
role    = "login"                         # login | data-access | compute | workstation
account = "UABC0001"                      # default -A for run_on_compute / submit_job
scratch = "/glade/derecho/scratch/$USER"  # suggested remote_dir for job output
```

`role = "data-access"` allows transfers while still routing compute away; `role = "workstation"` lifts login-node routing entirely for a machine you own. A missing or malformed file simply means no profiles.

## Testing

```bash
uv pip install -e ".[dev]"
uv run pytest tests/ -v

# Live tests against a real host (needs key or ControlMaster auth; leaves nothing behind)
HPC_SSH_MCP_TEST_HOST=<ssh alias> uv run pytest tests/test_integration.py -v
```

The unit suite mocks `subprocess.run`; the live suite exists because several 1.1.0 bugs were invisible to mocks.

## Version

1.2.0

## Changelog

### 1.2.0

- **Command policy tiers** on `execute_remote_bash` and `run_on_compute` (block / confirm / route / free), with `confirm_destructive` and `allow_on_login_node` flags. See [Command policy](#command-policy).
- **Host profiles** (`~/.config/hpc-ssh-mcp/hosts.toml`): per-alias `center`, `role`, `account` and `scratch`. The center picks PBS or Slurm without a probe, the role sets the policy tier, and the account defaults the `-A` flag.
- **Scheduler polling is rate-limited** to one identical query per host per 30 s, so an agent loop cannot become the "aggressive squeue" pattern HPC centers flag.
- **Bulk-transfer notice.** Transfers over 2 GB append a pointer to Globus or a data-transfer node; a missing local file is now reported before `scp` runs.
- **`submit_job` suggests the profile's scratch directory** when no `remote_dir` was given.
- Requires Python 3.11+ (for `tomllib`).

### 1.1.0

**Breaking:** `submit_slurm_job`, `check_slurm_job`, `list_slurm_queue` and `cancel_slurm_job` are replaced by the scheduler-aware `submit_job`, `check_job`, `list_queue` and `cancel_job`. The old tools only ever worked on Slurm; NCAR Derecho and Casper run PBS Pro.

- **PBS Pro support** with automatic scheduler detection per host, PBS job-ID validation, `remote_dir` for submitting from scratch, and one-round-trip `check_job` on both schedulers. New `run_on_compute` wraps NCAR's `qcmd` (or `srun`) so heavy one-off commands leave the login node.
- **scp works on OpenSSH 9.0+.** Modern scp speaks SFTP and passes the remote path literally, so the old shell quoting turned every path with a space or `~` into "No such file or directory". The protocol mode is now detected from `ssh -V` and paths are quoted only for the legacy protocol.
- **Child processes no longer inherit the MCP server's stdin.** `ssh` forwarded the JSON-RPC stream to the remote command; a command that read stdin could swallow protocol messages. stdin is now `/dev/null` unless a script is being piped.
- **Commands travel on stdin to `bash -s`** instead of being interpolated through the remote login shell, so multi-line scripts, `!`, and `2>/dev/null` work under tcsh accounts too.
- **Non-UTF-8 output no longer crashes a tool.** Decoding uses replacement characters.
- **Diagnostic `ssh -fN` hints fire only on ssh's own exit 255**, not on a remote command that happens to print "Permission denied" or "timed out".
- **`check_ssh_connection` reports the real verdict.** OpenSSH prints "Master running" on stderr; it was being reported as "(no output)".
- **Local scp paths are made absolute**, so a `:` in a filename is not parsed as a host and a leading `-` is not parsed as an option.
- **`--` before every path** in `cat`/`head`/`tail`, and `~/x` is expanded to the remote home in all path arguments.
- **Output caps.** `read_remote_file` fetches at most `max_bytes` (default 200 KB), appends a truncation notice, and refuses binary files; every tool's output is capped.
- **scp tools take a `timeout`** (default 3600 s) and a timed-out download removes the truncated file it left behind.
- **MCP tool annotations** (`readOnlyHint`, `destructiveHint`, `idempotentHint`).
- `fastmcp` pinned to `>=3,<4`; `timeout`, `max_bytes` and scheduler directive values are validated.

### 1.0.0

- **Batch-safe SSH defaults.** Every `ssh` and `scp` invocation now carries `-o BatchMode=yes -o ConnectTimeout=10`. Without these, a dead `ControlMaster` socket caused SSH to fall back to interactive auth — and because MCP servers have no controlling terminal, the process would either hang on the JSON-RPC stream until the 120 s timeout or fail with a cryptic message. With them, the failure is bounded to ≤ 10 s and surfaces a parseable error.
- **Actionable failure diagnostics.** SSH/SCP failures are now fingerprinted (`Permission denied (...keyboard-interactive...)`, `Control socket connect: No such file`, `Connection timed out`, `No route to host`) and rewritten with a hint telling the user exactly what to do — typically `ssh -fN <host>` from their terminal to re-establish the multiplex socket and complete Duo/MFA out-of-band.
- **`check_ssh_connection` is unchanged.** `ssh -O check` is a local socket query, not a connection, so it intentionally does not carry the new options.

### 0.3.0

- Initial public baseline: 10 MCP tools wrapping native `ssh` / `scp` / `sbatch` / `squeue` / `sacct` / `scancel`, leaning on `~/.ssh/config` + `ControlMaster` for credential reuse.
